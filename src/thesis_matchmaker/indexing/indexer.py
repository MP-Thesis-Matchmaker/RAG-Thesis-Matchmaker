"""The index build: read the sources, embed what changed, keep the store in sync.

Embedding is the slow step, so the indexer diffs content hashes against the
store and only re-embeds new or changed records. Records that vanished from
the sources are deleted so the index never serves stale positions.

Records are streamed and committed in chunks rather than collected, embedded and
written in one go. Over a 215k-record corpus the eager form held every Document
plus every vector in memory before its first write -- roughly 8 GB, against a
4 GiB namespace quota -- and a crash at any point during the hours of embedding
threw away the whole run. Chunking bounds the memory and makes the store's
per-batch commits reachable, which is what turns an interrupted run into a
resumable one.

Where the records come from is a `SourceReader` (the database, or JSONL files),
which is why nothing here knows about either.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from pydantic import BaseModel

from thesis_matchmaker.indexing.documents import Document, posting_to_document, zora_to_document
from thesis_matchmaker.indexing.embedder import Embedder
from thesis_matchmaker.indexing.sources import SourceReader
from thesis_matchmaker.indexing.store import IndexManifest, VectorStore

logger = logging.getLogger(__name__)


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
    truncated: int = 0


class Indexer:
    """Runs one read -> diff -> embed -> upsert pass over the source records."""

    def __init__(self, embedder: Embedder, store: VectorStore, chunk_size: int = 1000) -> None:
        self.embedder = embedder
        self.store = store
        self.chunk_size = chunk_size

    def run(self, reader: SourceReader) -> IndexResult:
        self._check_manifest()

        known = self.store.existing_hashes()
        # Ids, not Documents. This set is only needed to work out what disappeared
        # from the sources, and at corpus scale keeping whole Documents around to
        # answer that question was most of the memory the eager version used.
        current_ids: set[str] = set()
        buffer: list[Document] = []
        total = embedded = truncated = 0

        for document in self._documents(reader):
            total += 1
            current_ids.add(document.id)
            if known.get(document.id) == document.content_hash:
                continue
            buffer.append(document)
            if len(buffer) >= self.chunk_size:
                embedded, truncated = self._flush(buffer, embedded, truncated, total)
        if buffer:
            embedded, truncated = self._flush(buffer, embedded, truncated, total)

        # Read after the loop, never inside it: a reader counts the lines it could
        # not parse as it yields, so the number is only final once it is drained.
        invalid = reader.invalid_records

        # Also after the loop, for the same shape of reason: nothing can be called
        # missing from current_ids until current_ids is complete.
        removed = [doc_id for doc_id in known if doc_id not in current_ids]
        self.store.delete(removed)

        result = IndexResult(
            embedded=embedded,
            skipped=total - embedded,
            deleted=len(removed),
            invalid_lines=invalid,
            truncated=truncated,
        )
        self._write_manifest(document_count=total, sources=reader.label, truncated=truncated)
        logger.info(
            "index run: embedded=%d skipped=%d deleted=%d invalid_lines=%d truncated=%d",
            result.embedded,
            result.skipped,
            result.deleted,
            result.invalid_lines,
            result.truncated,
        )
        return result

    @staticmethod
    def _documents(reader: SourceReader) -> Iterator[Document]:
        """Every source record as a Document, yielded rather than collected."""
        for record in reader.publications():
            yield zora_to_document(record)
        for posting in reader.postings():
            yield posting_to_document(posting)

    def _flush(
        self, buffer: list[Document], embedded: int, truncated: int, seen: int
    ) -> tuple[int, int]:
        """Embed and commit one chunk, then empty the buffer.

        Committing per chunk instead of once at the end is what makes a killed run
        resumable: the rows already written still match on content hash next time
        and are skipped, so the next run continues rather than starting over.
        """
        vectors = self.embedder.embed_documents([d.text for d in buffer])
        self.store.upsert(buffer, vectors)
        embedded += len(buffer)
        truncated += self.embedder.last_truncated
        # A full run is hours long. Without this it is indistinguishable from a hang.
        logger.info("committed %d embedded document(s); %d source records read", embedded, seen)
        buffer.clear()
        return embedded, truncated

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
        if manifest.max_seq_length != self.embedder.max_seq_length:
            # Same class of hazard as a changed model, and less obvious: the token
            # window changes every vector while leaving every content hash alone,
            # so a plain re-index would skip all of it and leave the index holding
            # two incompatible generations of vector at once.
            raise ModelMismatchError(
                f"the index was built with a {manifest.max_seq_length}-token window but "
                f"the configured cap is {self.embedder.max_seq_length}; that changes every "
                "vector without changing any content hash, so nothing would be re-embedded. "
                "Rebuild with `thesis-matchmaker index --rebuild`"
            )

    def _write_manifest(self, document_count: int, sources: str, truncated: int) -> None:
        self.store.write_manifest(
            IndexManifest(
                embedding_model=self.embedder.model_name,
                embedding_dim=self.embedder.dimensions,
                document_count=document_count,
                sources=sources,
                max_seq_length=self.embedder.max_seq_length,
                truncated_docs=truncated,
            )
        )
