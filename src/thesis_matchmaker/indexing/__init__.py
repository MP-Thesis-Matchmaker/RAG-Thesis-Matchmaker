"""Indexing: turns ingested records into a searchable vector index.

Read side of the ingestion boundary: consumes whatever the configured
`SourceReader` yields -- the harvested `publication` table, or a directory of
JSONL files -- embeds it, and maintains the vector store that retrieval queries.
Never writes back to the source data.
"""

from __future__ import annotations

from thesis_matchmaker.config import Settings
from thesis_matchmaker.indexing.documents import (
    Document,
    posting_to_document,
    zora_to_document,
)
from thesis_matchmaker.indexing.embedder import (
    Embedder,
    HashEmbedder,
    SentenceTransformerEmbedder,
)
from thesis_matchmaker.indexing.indexer import Indexer
from thesis_matchmaker.indexing.sources import (
    JsonlSourceReader,
    PostgresSourceReader,
    SourceReader,
)
from thesis_matchmaker.indexing.store import (
    IndexManifest,
    InMemoryVectorStore,
    PgVectorStore,
    VectorStore,
)


def build_embedder(settings: Settings) -> Embedder:
    """Pick the embedder from config; "hash-fake" selects the offline fake."""
    if settings.embedding_model == "hash-fake":
        return HashEmbedder()
    return SentenceTransformerEmbedder(
        settings.embedding_model,
        max_seq_length=settings.embedding_max_seq_length,
        batch_size=settings.embedding_batch_size,
    )


def build_store(settings: Settings) -> VectorStore:
    """Open the configured vector store."""
    return PgVectorStore(dsn=settings.database_url)


def build_indexer(settings: Settings) -> Indexer:
    """Wire an indexer over the configured embedder and store."""
    return Indexer(
        embedder=build_embedder(settings),
        store=build_store(settings),
        chunk_size=settings.index_chunk_size,
    )


# What `--source db` means, versus a filesystem path.
DATABASE_SOURCE = "db"


def build_source_reader(settings: Settings, source: str | None = None) -> SourceReader:
    """Pick where the indexer reads records from.

    `db` reads the harvested `publication` table -- what a deployed indexer does.
    Anything else is a directory of JSONL files, which is how the checked-in
    samples and the not-yet-built scraper's output are indexed.
    """
    chosen = source or settings.sources_path
    if chosen == DATABASE_SOURCE:
        return PostgresSourceReader(dsn=settings.database_url)
    return JsonlSourceReader(directory=chosen)


def read_manifest(settings: Settings) -> IndexManifest | None:
    """The manifest of the built index, or None if nothing has been indexed.

    None means "no index yet", which the CLI and the MCP adapter answer by
    falling back to the fake retriever. A database that cannot be reached is a
    different thing entirely and is left to raise: quietly serving canned
    recommendations because Postgres is down would be worse than an error.
    """
    return build_store(settings).read_manifest()


__all__ = [
    "DATABASE_SOURCE",
    "Document",
    "Embedder",
    "HashEmbedder",
    "IndexManifest",
    "InMemoryVectorStore",
    "Indexer",
    "JsonlSourceReader",
    "PgVectorStore",
    "PostgresSourceReader",
    "SentenceTransformerEmbedder",
    "SourceReader",
    "VectorStore",
    "build_embedder",
    "build_indexer",
    "build_source_reader",
    "build_store",
    "posting_to_document",
    "read_manifest",
    "zora_to_document",
]
