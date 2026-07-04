"""The index build: read source JSONL, embed what changed, keep the store in sync.

Embedding is the slow step, so the indexer diffs content hashes against the
store and only re-embeds new or changed records. Records that vanished from
the sources are deleted so the index never serves stale positions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, ValidationError

from thesis_matchmaker.contracts import ThesisPosting, ZoraRecord
from thesis_matchmaker.indexing.documents import Document, posting_to_document, zora_to_document
from thesis_matchmaker.indexing.embedder import Embedder
from thesis_matchmaker.indexing.store import VectorStore

logger = logging.getLogger(__name__)

PUBLICATIONS_FILE = "publications.jsonl"
THESES_FILE = "theses.jsonl"
MANIFEST_FILE = "manifest.json"


class ModelMismatchError(RuntimeError):
    """The index was built with a different embedding model.

    Vectors from different models live in incompatible spaces; mixing them
    silently would corrupt search results. Rebuild the index instead.
    """


class IndexResult(BaseModel):
    """Counts from one index run, for logs and tests."""

    embedded: int = 0
    skipped: int = 0
    deleted: int = 0
    invalid_lines: int = 0


class Indexer:
    """Runs one load -> diff -> embed -> upsert pass over the source files."""

    def __init__(self, embedder: Embedder, store: VectorStore, index_path: Path) -> None:
        self.embedder = embedder
        self.store = store
        self.index_path = Path(index_path)

    def run(self, sources_dir: Path) -> IndexResult:
        sources_dir = Path(sources_dir)
        self._check_manifest()

        documents: list[Document] = []
        invalid = 0
        for filename, model, to_document in (
            (PUBLICATIONS_FILE, ZoraRecord, zora_to_document),
            (THESES_FILE, ThesisPosting, posting_to_document),
        ):
            path = sources_dir / filename
            if not path.exists():
                logger.warning("source file missing, skipping: %s", path)
                continue
            records, bad = self._load_jsonl(path, model)
            invalid += bad
            documents.extend(to_document(r) for r in records)

        known = self.store.existing_hashes()
        current_ids = {d.id for d in documents}
        changed = [d for d in documents if known.get(d.id) != d.content_hash]
        removed = [doc_id for doc_id in known if doc_id not in current_ids]

        if changed:
            vectors = self.embedder.embed_documents([d.text for d in changed])
            self.store.upsert(changed, vectors)
        self.store.delete(removed)

        result = IndexResult(
            embedded=len(changed),
            skipped=len(documents) - len(changed),
            deleted=len(removed),
            invalid_lines=invalid,
        )
        self._write_manifest(document_count=len(documents), sources_dir=sources_dir)
        logger.info(
            "index run: embedded=%d skipped=%d deleted=%d invalid_lines=%d",
            result.embedded,
            result.skipped,
            result.deleted,
            result.invalid_lines,
        )
        return result

    @staticmethod
    def _load_jsonl(path: Path, model: type[BaseModel]) -> tuple[list, int]:
        """Parse one record per line; count bad lines instead of failing the run."""
        records, invalid = [], 0
        with path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(model.model_validate_json(line))
                except ValidationError as exc:
                    invalid += 1
                    logger.warning("skipping invalid line %s:%d: %s", path, line_no, exc)
        return records, invalid

    @property
    def _manifest_path(self) -> Path:
        return self.index_path / MANIFEST_FILE

    def _check_manifest(self) -> None:
        if not self._manifest_path.exists():
            return
        manifest = json.loads(self._manifest_path.read_text())
        built_with = manifest.get("embedding_model")
        if built_with != self.embedder.model_name:
            raise ModelMismatchError(
                f"index at {self.index_path} was built with '{built_with}' but the "
                f"configured model is '{self.embedder.model_name}'; delete the index "
                "directory and rebuild"
            )

    def _write_manifest(self, document_count: int, sources_dir: Path) -> None:
        self.index_path.mkdir(parents=True, exist_ok=True)
        self._manifest_path.write_text(
            json.dumps(
                {
                    "embedding_model": self.embedder.model_name,
                    "document_count": document_count,
                    "sources_dir": str(sources_dir),
                },
                indent=2,
            )
        )
