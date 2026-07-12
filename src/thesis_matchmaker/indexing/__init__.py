"""Indexing: turns ingested records into a searchable vector index.

Read side of the ingestion boundary: consumes the JSONL files the ingestion
component writes, embeds them, and maintains the vector store that retrieval
queries. Never writes back to the source data.
"""

from __future__ import annotations

from pathlib import Path

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
from thesis_matchmaker.indexing.store import ChromaVectorStore, VectorStore


def build_embedder(settings: Settings) -> Embedder:
    """Pick the embedder from config; "hash-fake" selects the offline fake."""
    if settings.embedding_model == "hash-fake":
        return HashEmbedder()
    return SentenceTransformerEmbedder(settings.embedding_model)


def build_store(settings: Settings) -> VectorStore:
    """Open the configured vector store."""
    return ChromaVectorStore(
        path=settings.vector_store_path,
        collection_name=settings.collection_name,
    )


def build_indexer(settings: Settings) -> Indexer:
    """Wire an indexer over the configured embedder and store."""
    return Indexer(
        embedder=build_embedder(settings),
        store=build_store(settings),
        index_path=Path(settings.vector_store_path),
    )


__all__ = [
    "Document",
    "Embedder",
    "HashEmbedder",
    "Indexer",
    "SentenceTransformerEmbedder",
    "VectorStore",
    "build_embedder",
    "build_indexer",
    "build_store",
    "posting_to_document",
    "zora_to_document",
]
