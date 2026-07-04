"""Tests for the embedding seam, exercised through the deterministic fake."""

from __future__ import annotations

from thesis_matchmaker.indexing.embedder import HashEmbedder


def test_hash_embedder_is_deterministic() -> None:
    e = HashEmbedder()
    assert e.embed_documents(["same text"]) == e.embed_documents(["same text"])


def test_hash_embedder_distinguishes_texts() -> None:
    e = HashEmbedder()
    a, b = e.embed_documents(["dense retrieval", "medieval history"])
    assert a != b


def test_hash_embedder_dimensions_consistent() -> None:
    e = HashEmbedder(dim=32)
    vectors = e.embed_documents(["one", "two"])
    assert all(len(v) == 32 for v in vectors)
    assert len(e.embed_query("query")) == 32


def test_query_and_document_share_vector_space() -> None:
    e = HashEmbedder()
    assert e.embed_query("dense retrieval") == e.embed_documents(["dense retrieval"])[0]


def test_model_name_reported() -> None:
    assert HashEmbedder().model_name == "hash-fake"
