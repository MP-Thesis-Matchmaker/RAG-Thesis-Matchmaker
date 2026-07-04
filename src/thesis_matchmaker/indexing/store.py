"""The vector store seam and its first implementation (ChromaDB).

The store is a swappable decision (CLAUDE.md invariant 3): indexing and
retrieval only depend on the VectorStore protocol. ChromaDB was picked first
because it is embedded (no server), persists to a directory, and supports
metadata filters — enough for the current corpus size.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from thesis_matchmaker.indexing.documents import Document, MetadataValue

# Metadata key under which the store keeps each document's content hash. It is
# internal bookkeeping for incremental re-indexing and never returned to callers.
_HASH_KEY = "content_hash"


class ScoredHit(BaseModel):
    """One nearest-neighbour result from the store."""

    id: str
    score: float = Field(description="Similarity in [0, 1], higher is closer.")
    text: str
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class VectorStore(Protocol):
    """What indexing (writes) and retrieval (reads) depend on."""

    def upsert(self, documents: list[Document], vectors: list[list[float]]) -> None:
        """Insert or overwrite documents by id."""
        ...

    def delete(self, ids: list[str]) -> None:
        """Remove documents by id; unknown ids are ignored."""
        ...

    def existing_hashes(self) -> dict[str, str]:
        """Map of stored document id -> content hash, for change detection."""
        ...

    def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filters: dict[str, MetadataValue] | None = None,
    ) -> list[ScoredHit]:
        """Return up to top_k nearest documents, optionally metadata-filtered."""
        ...


def _to_where(filters: dict[str, MetadataValue] | None) -> dict | None:
    """Translate a flat equality-filter dict into Chroma's where syntax."""
    if not filters:
        return None
    if len(filters) == 1:
        return dict(filters)
    return {"$and": [{key: value} for key, value in filters.items()]}


class ChromaVectorStore:
    """VectorStore over an embedded, on-disk ChromaDB collection."""

    def __init__(self, path: str, collection_name: str) -> None:
        import chromadb

        self._client = chromadb.PersistentClient(path=path)
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(self, documents: list[Document], vectors: list[list[float]]) -> None:
        if not documents:
            return
        self._collection.upsert(
            ids=[d.id for d in documents],
            embeddings=vectors,
            documents=[d.text for d in documents],
            metadatas=[{**d.metadata, _HASH_KEY: d.content_hash} for d in documents],
        )

    def delete(self, ids: list[str]) -> None:
        if ids:
            self._collection.delete(ids=ids)

    def existing_hashes(self) -> dict[str, str]:
        result = self._collection.get(include=["metadatas"])
        return {
            doc_id: meta[_HASH_KEY]
            for doc_id, meta in zip(result["ids"], result["metadatas"], strict=True)
            if meta and _HASH_KEY in meta
        }

    def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filters: dict[str, MetadataValue] | None = None,
    ) -> list[ScoredHit]:
        result = self._collection.query(
            query_embeddings=[vector],
            n_results=top_k,
            where=_to_where(filters),
            include=["documents", "metadatas", "distances"],
        )
        hits = []
        for doc_id, text, meta, distance in zip(
            result["ids"][0],
            result["documents"][0],
            result["metadatas"][0],
            result["distances"][0],
            strict=True,
        ):
            metadata = {k: v for k, v in (meta or {}).items() if k != _HASH_KEY}
            hits.append(
                ScoredHit(id=doc_id, score=1.0 - distance, text=text or "", metadata=metadata)
            )
        return hits
