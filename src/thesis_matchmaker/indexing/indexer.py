"""The index build: read source JSONL, embed what changed, keep the store in sync.

Embedding is the slow step, so the indexer diffs content hashes against the
store and only re-embeds new or changed records. Records that vanished from
the sources are deleted so the index never serves stale positions.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ValidationError

from thesis_matchmaker.contracts import ThesisPosting, ZoraRecord
from thesis_matchmaker.indexing.documents import Document, posting_to_document, zora_to_document
from thesis_matchmaker.indexing.embedder import Embedder
from thesis_matchmaker.indexing.store import IndexManifest, VectorStore

logger = logging.getLogger(__name__)

PUBLICATIONS_FILE = "publications.jsonl"
THESES_FILE = "theses.jsonl"


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

    def __init__(self, embedder: Embedder, store: VectorStore) -> None:
        self.embedder = embedder
        self.store = store

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

    def _check_manifest(self) -> None:
        manifest = self.store.read_manifest()
        if manifest is None:
            return
        if manifest.embedding_model != self.embedder.model_name:
            raise ModelMismatchError(
                f"the index was built with '{manifest.embedding_model}' but the configured "
                f"model is '{self.embedder.model_name}'; rebuild with "
                "`thesis-matchmaker index --rebuild`"
            )
        if manifest.embedding_dim != self.embedder.dimensions:
            # A different vector width is a schema change, not a config change:
            # the `document.embedding` column is `vector(n)`.
            raise ModelMismatchError(
                f"the index holds {manifest.embedding_dim}-dimensional vectors but "
                f"'{self.embedder.model_name}' produces {self.embedder.dimensions}; this "
                "needs a migration that alters document.embedding, then a rebuild"
            )

    def _write_manifest(self, document_count: int, sources_dir: Path) -> None:
        self.store.write_manifest(
            IndexManifest(
                embedding_model=self.embedder.model_name,
                embedding_dim=self.embedder.dimensions,
                document_count=document_count,
                sources=str(sources_dir),
            )
        )
