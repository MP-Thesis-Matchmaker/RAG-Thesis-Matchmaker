"""Tests for building the indexing and retrieval stack from settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from themis_shared.config import Settings
from themis_matcher.indexing import build_embedder, build_indexer
from themis_matcher.indexing.embedder import HashEmbedder, SentenceTransformerEmbedder
from themis_matcher.indexing.store import PgVectorStore
from themis_matcher.retrieval import build_retriever
from themis_matcher.retrieval.vector import VectorRetriever

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


def test_embedding_device_setting_reaches_the_embedder(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.embedding_model = "BAAI/bge-m3"
    settings.embedding_device = "cpu"
    assert build_embedder(settings)._device == "cpu"


# --- The two UZH-affiliation settings ---


def test_affiliation_settings_default_to_permissive_but_demoted(tmp_path: Path) -> None:
    """The defaults are the product decision, so they are asserted, not assumed.

    Off + uzh_first is the only combination where both knobs do something: with the
    filter on, nothing non-UZH survives for the strategy to order.
    """
    settings = _settings(tmp_path)
    assert settings.retrieval_require_uzh_author is False
    assert settings.retrieval_ranking_strategy == "uzh_first"
    # Availability is the one eligibility rule that stays on: no strategy demotes a
    # topic that is already taken, so leaving it off would put taken topics in results.
    assert settings.retrieval_require_available_posting is True


def test_affiliation_settings_reach_the_retriever(tmp_path: Path) -> None:
    """A setting nothing reads is worse than no setting -- pin the wiring."""
    settings = _settings(tmp_path)
    settings.retrieval_require_uzh_author = True
    settings.retrieval_ranking_strategy = "score"
    settings.retrieval_require_available_posting = False

    retriever = build_retriever(settings)

    assert retriever.require_uzh_author is True
    assert retriever.ranking_strategy == "score"
    assert retriever.require_available_posting is False


def test_an_unknown_ranking_strategy_is_refused_at_load(tmp_path: Path) -> None:
    """A typo must fail loudly, not silently select a strategy nobody asked for."""
    with pytest.raises(ValidationError, match="uzh_first"):
        Settings(
            embedding_model="hash-fake",
            database_url=_DSN,
            sources_path=str(tmp_path / "src"),
            retrieval_ranking_strategy="uzh-first",
        )
