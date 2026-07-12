"""Tests for the vector store seam, against a real ChromaDB in a temp dir."""

from __future__ import annotations

from pathlib import Path

import pytest

from thesis_matchmaker.indexing.documents import Document
from thesis_matchmaker.indexing.embedder import HashEmbedder
from thesis_matchmaker.indexing.store import ChromaVectorStore


def _doc(doc_id: str, text: str, **metadata) -> Document:
    return Document(id=doc_id, text=text, metadata=metadata, content_hash=f"hash-{text}")


@pytest.fixture()
def store(tmp_path: Path) -> ChromaVectorStore:
    return ChromaVectorStore(path=str(tmp_path / "index"), collection_name="test")


def _seed(store: ChromaVectorStore) -> HashEmbedder:
    embedder = HashEmbedder()
    docs = [
        _doc("zora:1", "dense retrieval for german text", source_type="publication", year=2024),
        _doc(
            "posting:1",
            "msc thesis on rag grounding",
            source_type="thesis_posting",
            degree_level="master",
        ),
        _doc(
            "posting:2",
            "phd position in medieval history",
            source_type="thesis_posting",
            degree_level="phd",
        ),
    ]
    store.upsert(docs, embedder.embed_documents([d.text for d in docs]))
    return embedder


def test_query_returns_exact_match_first(store: ChromaVectorStore) -> None:
    embedder = _seed(store)
    hits = store.query(embedder.embed_query("dense retrieval for german text"), top_k=3)
    assert hits[0].id == "zora:1"
    assert hits[0].metadata["source_type"] == "publication"


def test_query_respects_metadata_filters(store: ChromaVectorStore) -> None:
    embedder = _seed(store)
    hits = store.query(
        embedder.embed_query("anything"),
        top_k=3,
        filters={"source_type": "thesis_posting", "degree_level": "master"},
    )
    assert [h.id for h in hits] == ["posting:1"]


def test_existing_hashes_roundtrip(store: ChromaVectorStore) -> None:
    _seed(store)
    hashes = store.existing_hashes()
    assert hashes["zora:1"] == "hash-dense retrieval for german text"
    assert len(hashes) == 3


def test_delete_removes_points(store: ChromaVectorStore) -> None:
    _seed(store)
    store.delete(["posting:2"])
    assert "posting:2" not in store.existing_hashes()


def test_upsert_overwrites_same_id(store: ChromaVectorStore) -> None:
    embedder = _seed(store)
    updated = _doc("zora:1", "totally new abstract", source_type="publication")
    store.upsert([updated], embedder.embed_documents([updated.text]))
    hashes = store.existing_hashes()
    assert hashes["zora:1"] == "hash-totally new abstract"
    assert len(hashes) == 3
