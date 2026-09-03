"""The vector store seam and its two implementations.

Indexing (writes) and retrieval (reads) depend only on the `VectorStore`
protocol. Postgres with pgvector is the deployed store: the UZH cluster provides
a managed Postgres with the extension, which puts the vectors in the same
backed-up database as the source rows instead of in a PersistentVolumeClaim
beside it.

`InMemoryVectorStore` is a real implementation, not a mock -- the same choice as
`HashEmbedder`, `FakeRetriever` and `TemplateSynthesizer`. It is what lets the
indexer, pipeline, CLI and adapter tests run with no database and no network,
and it is the second half of the parametrised store contract suite.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Collection
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from themis_matcher.indexing.documents import Document, MetadataValue
from themis_shared import db

logger = logging.getLogger(__name__)

# How wide a net HNSW casts before the LIMIT is applied. Raising it trades query
# time for recall. 100 is pgvector's usual starting recommendation; it matters
# here because every retrieval query is filtered.
_EF_SEARCH = 100

# Keep scanning the index until top_k rows survive the WHERE clause, instead of
# filtering one fixed candidate set and returning short. strict_order preserves
# exact distance ordering, which matters because the score is surfaced to the
# user and fed to MATCHER_SYNTHESIS_MIN_SCORE.
_ITERATIVE_SCAN = "strict_order"


class ScoredHit(BaseModel):
    """One nearest-neighbour result from the store."""

    id: str
    score: float = Field(
        description=(
            "Cosine similarity in [-1, 1], higher is closer. Not a probability and "
            "not a percentage: it can be negative. Any threshold set against it -- "
            "MATCHER_SYNTHESIS_MIN_SCORE -- is in cosine units."
        )
    )
    text: str
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class IndexManifest(BaseModel):
    """What built the current index.

    Vectors from different embedding models are not comparable, so the model
    that built an index has to be recorded with it. This used to be
    data/index/manifest.json; it is now a single row, which also means "has an
    index been built?" is a query rather than a filesystem check.
    """

    embedding_model: str
    embedding_dim: int
    document_count: int
    sources: str | None = None
    # None for an embedder with no token window (the offline hash-fake).
    max_seq_length: int | None = None
    truncated_docs: int = 0


class VectorStore(Protocol):
    """What indexing (writes) and retrieval (reads) depend on."""

    def upsert(self, documents: list[Document], vectors: list[list[float]]) -> None:
        """Insert or overwrite documents by id."""
        ...

    def delete(self, ids: list[str]) -> None:
        """Remove documents by id; unknown ids are ignored."""
        ...

    def existing_hashes(self, source_types: Collection[str] | None = None) -> dict[str, str]:
        """Map of stored document id -> content hash, for change detection.

        `source_types` narrows the map to those kinds. This is not an
        optimisation: `Indexer.run` treats every id in this map that the run did
        not see as an orphan and deletes it, so an unscoped map during a
        single-kind run would condemn every document of the other kind.
        """
        ...

    def count(self) -> int:
        """How many documents the store holds, across every kind."""
        ...

    def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filters: dict[str, MetadataValue] | None = None,
    ) -> list[ScoredHit]:
        """Return up to top_k nearest documents, optionally metadata-filtered."""
        ...

    def read_manifest(self) -> IndexManifest | None:
        """The manifest of the current index, or None if nothing is built."""
        ...

    def write_manifest(self, manifest: IndexManifest) -> None:
        """Record what built the index that is now in place."""
        ...

    def clear(self) -> None:
        """Drop every document and the manifest, leaving the schema in place."""
        ...


# Metadata key that lives in its own column as well as in the jsonb blob. It
# stays in the blob so a ScoredHit still carries it without a special case; the
# column exists so the partial HNSW indexes are reachable by the planner.
_SOURCE_TYPE = "source_type"

_UPSERT = """
INSERT INTO document (id, source_type, text, metadata, content_hash, embedding, updated_at)
VALUES (%s, %s, %s, %s, %s, %s::vector, now())
ON CONFLICT (id) DO UPDATE SET
    source_type  = EXCLUDED.source_type,
    text         = EXCLUDED.text,
    metadata     = EXCLUDED.metadata,
    content_hash = EXCLUDED.content_hash,
    embedding    = EXCLUDED.embedding,
    updated_at   = now()
"""

# `metadata @> %s` is jsonb containment, which is exactly the flat-equality filter
# the retriever asks for, and is what document_metadata_gin indexes. An empty
# filter becomes `@> '{}'`, which matches every row -- no branching needed there.
# source_type is the exception: it is matched against the column so the query
# predicate is identical to the partial indexes' predicate.
#
# The two appearances of `<=>` are not the same quantity and must not be merged.
# The SELECT complements the cosine *distance* (pgvector's `<=>`, over [0, 2]) into
# a cosine *similarity* over [-1, 1] -- the score, deliberately signed. The ORDER BY
# sorts on the distance itself, ascending. Rescaling the score into [0, 1] would
# leave every ordering and every other store test intact while silently changing
# what MATCHER_SYNTHESIS_MIN_SCORE means; test_store_contract.py pins both endpoints
# against exactly that.
_QUERY_TEMPLATE = """
SELECT id, text, metadata, 1 - (embedding <=> %(vector)s::vector) AS score
FROM document
WHERE metadata @> %(filters)s{source_type_clause}
ORDER BY embedding <=> %(vector)s::vector
LIMIT %(top_k)s
"""

_WRITE_MANIFEST = """
INSERT INTO index_manifest (id, embedding_model, embedding_dim, document_count, sources,
                            max_seq_length, truncated_docs, built_at)
VALUES (1, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (id) DO UPDATE SET
    embedding_model = EXCLUDED.embedding_model,
    embedding_dim   = EXCLUDED.embedding_dim,
    document_count  = EXCLUDED.document_count,
    sources         = EXCLUDED.sources,
    max_seq_length  = EXCLUDED.max_seq_length,
    truncated_docs  = EXCLUDED.truncated_docs,
    built_at        = now()
"""


class PgVectorStore:
    """VectorStore over a Postgres `document` table with a pgvector column."""

    def __init__(self, dsn: str, batch_size: int = 500) -> None:
        self.dsn = dsn
        self.batch_size = batch_size

    def upsert(self, documents: list[Document], vectors: list[list[float]]) -> None:
        if not documents:
            return
        rows = [
            (
                d.id,
                str(d.metadata.get(_SOURCE_TYPE, "")),
                d.text,
                Jsonb(d.metadata),
                d.content_hash,
                db.to_vector_literal(v),
            )
            for d, v in zip(documents, vectors, strict=True)
        ]
        # Committed per batch rather than in one transaction: a full re-embed of
        # the corpus is long enough that it needs to be resumable, and the
        # content-hash diff makes a partially finished run pick up where it
        # stopped instead of starting over.
        for start in range(0, len(rows), self.batch_size):
            with db.connection(self.dsn) as conn:
                conn.cursor().executemany(_UPSERT, rows[start : start + self.batch_size])

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        with db.connection(self.dsn) as conn:
            conn.execute("DELETE FROM document WHERE id = ANY(%s)", (list(ids),))

    def existing_hashes(self, source_types: Collection[str] | None = None) -> dict[str, str]:
        # Matched against the column rather than the jsonb blob, for the same
        # reason `query` does: `source_type` was promoted out of metadata so the
        # planner can reach the partial indexes.
        sql = "SELECT id, content_hash FROM document"
        params: tuple[object, ...] = ()
        if source_types is not None:
            sql += " WHERE source_type = ANY(%s)"
            params = (list(source_types),)
        with db.connection(self.dsn) as conn:
            rows = conn.execute(sql, params).fetchall()
        return {row[0]: row[1] for row in rows}

    def count(self) -> int:
        with db.connection(self.dsn) as conn:
            row = conn.execute("SELECT count(*) FROM document").fetchone()
        return int(row[0]) if row else 0

    def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filters: dict[str, MetadataValue] | None = None,
    ) -> list[ScoredHit]:
        wanted = dict(filters or {})
        source_type = wanted.pop(_SOURCE_TYPE, None)
        sql = _QUERY_TEMPLATE.format(
            source_type_clause="\n  AND source_type = %(source_type)s" if source_type else ""
        )
        params: dict[str, object] = {
            "vector": db.to_vector_literal(vector),
            "filters": Jsonb(wanted),
            "top_k": top_k,
        }
        if source_type:
            params["source_type"] = str(source_type)

        with db.connection(self.dsn) as conn:
            self._tune(conn)
            rows = conn.execute(sql, params).fetchall()
        return [
            ScoredHit(id=row[0], text=row[1] or "", metadata=row[2] or {}, score=row[3])
            for row in rows
        ]

    @staticmethod
    def _tune(conn) -> None:
        """Per-transaction HNSW settings. SET LOCAL takes no parameters, hence
        the interpolation -- the values are module constants, never caller input.

        `iterative_scan` is what actually fixes filtered search: without it
        pgvector applies the WHERE clause to a fixed candidate set and can return
        fewer rows than asked for; with it, the index scan continues until enough
        rows survive the filter. It arrived in pgvector 0.8, and the version on
        the UZH server is still an open question (docs/deployment.md), so a
        missing GUC has to be survivable rather than fatal. The savepoint keeps
        the failed SET from poisoning the transaction.
        """
        conn.execute(f"SET LOCAL hnsw.ef_search = {int(_EF_SEARCH)}")
        try:
            with conn.transaction():
                conn.execute(f"SET LOCAL hnsw.iterative_scan = {_ITERATIVE_SCAN}")
        except psycopg.errors.UndefinedObject:
            logger.debug("hnsw.iterative_scan unavailable; pgvector is older than 0.8")

    def read_manifest(self) -> IndexManifest | None:
        with db.connection(self.dsn) as conn:
            row = conn.execute(
                "SELECT embedding_model, embedding_dim, document_count, sources, "
                "max_seq_length, truncated_docs FROM index_manifest WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return IndexManifest(
            embedding_model=row[0],
            embedding_dim=row[1],
            document_count=row[2],
            sources=row[3],
            max_seq_length=row[4],
            truncated_docs=row[5] or 0,
        )

    def clear(self) -> None:
        with db.connection(self.dsn) as conn:
            conn.execute("TRUNCATE document")
            conn.execute("DELETE FROM index_manifest")

    def write_manifest(self, manifest: IndexManifest) -> None:
        with db.connection(self.dsn) as conn:
            conn.execute(
                _WRITE_MANIFEST,
                (
                    manifest.embedding_model,
                    manifest.embedding_dim,
                    manifest.document_count,
                    manifest.sources,
                    manifest.max_seq_length,
                    manifest.truncated_docs,
                ),
            )


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, not assuming either vector is normalised."""
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if norm == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm


class InMemoryVectorStore:
    """VectorStore with brute-force search, for offline runs and tests.

    Exhaustive scan rather than an approximate index, so its results are the
    ground truth the pgvector implementation is checked against by the shared
    contract suite.
    """

    def __init__(self) -> None:
        self._documents: dict[str, tuple[Document, list[float]]] = {}
        self._manifest: IndexManifest | None = None

    def upsert(self, documents: list[Document], vectors: list[list[float]]) -> None:
        for document, vector in zip(documents, vectors, strict=True):
            self._documents[document.id] = (document, list(vector))

    def delete(self, ids: list[str]) -> None:
        for doc_id in ids:
            self._documents.pop(doc_id, None)

    def existing_hashes(self, source_types: Collection[str] | None = None) -> dict[str, str]:
        wanted = set(source_types) if source_types is not None else None
        return {
            doc_id: doc.content_hash
            for doc_id, (doc, _) in self._documents.items()
            if wanted is None or doc.metadata.get(_SOURCE_TYPE) in wanted
        }

    def count(self) -> int:
        return len(self._documents)

    def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filters: dict[str, MetadataValue] | None = None,
    ) -> list[ScoredHit]:
        wanted = filters or {}
        hits = [
            ScoredHit(
                id=doc.id,
                score=_cosine(vector, stored),
                text=doc.text,
                metadata=dict(doc.metadata),
            )
            for doc, stored in self._documents.values()
            if all(doc.metadata.get(key) == value for key, value in wanted.items())
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]

    def read_manifest(self) -> IndexManifest | None:
        return self._manifest

    def write_manifest(self, manifest: IndexManifest) -> None:
        self._manifest = manifest

    def clear(self) -> None:
        self._documents.clear()
        self._manifest = None
