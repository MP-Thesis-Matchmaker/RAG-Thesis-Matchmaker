"""Tests for building the indexing and retrieval stack from settings."""

from __future__ import annotations

from pathlib import Path

from thesis_matchmaker.config import Settings
from thesis_matchmaker.indexing import build_embedder, build_indexer
from thesis_matchmaker.indexing.embedder import HashEmbedder, SentenceTransformerEmbedder
from thesis_matchmaker.indexing.store import PgVectorStore
from thesis_matchmaker.retrieval import build_retriever
from thesis_matchmaker.retrieval.vector import VectorRetriever

_DSN = "postgresql://nobody@localhost:1/unused"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        embedding_model="hash-fake",
        database_url=_DSN,
        sources_path=str(tmp_path / "src"),
    )


def test_hash_fake_model_name_selects_fake_embedder(tmp_path: Path) -> None:
    assert isinstance(build_embedder(_settings(tmp_path)), HashEmbedder)


def test_real_model_name_selects_sentence_transformers(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.embedding_model = "BAAI/bge-m3"
    embedder = build_embedder(settings)
    assert isinstance(embedder, SentenceTransformerEmbedder)
    assert embedder.model_name == "BAAI/bge-m3"


def test_build_indexer_and_retriever_share_config(tmp_path: Path) -> None:
    """Wiring the stack must not touch the database: no connection until a query."""
    settings = _settings(tmp_path)
    indexer = build_indexer(settings)
    retriever = build_retriever(settings)
    assert isinstance(retriever, VectorRetriever)
    assert isinstance(indexer.store, PgVectorStore)
    assert indexer.store.dsn == _DSN
