"""Retrieval layer: the boundary and its implementations."""

from __future__ import annotations

from themis_shared.config import Settings
from themis_matcher.retrieval.base import Retriever
from themis_matcher.retrieval.fake import FakeRetriever


def build_retriever(settings: Settings) -> Retriever:
    """Wire the real retriever over the configured embedder and store.

    Imported lazily so importing the retrieval package (e.g. for the fake) does
    not open a database connection or reach for the embedding model.
    """
    from themis_matcher.indexing import build_embedder, build_store
    from themis_matcher.retrieval.vector import VectorRetriever

    return VectorRetriever(
        embedder=build_embedder(settings),
        store=build_store(settings),
        require_uzh_author=settings.retrieval_require_uzh_author,
        require_available_posting=settings.retrieval_require_available_posting,
        ranking_strategy=settings.retrieval_ranking_strategy,
    )


__all__ = ["FakeRetriever", "Retriever", "build_retriever"]
