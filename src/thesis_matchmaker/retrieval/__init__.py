"""Retrieval layer: the boundary and its implementations."""

from __future__ import annotations

from thesis_matchmaker.config import Settings
from thesis_matchmaker.retrieval.base import Retriever
from thesis_matchmaker.retrieval.fake import FakeRetriever


def build_retriever(settings: Settings) -> Retriever:
    """Wire the real retriever over the configured embedder and store.

    Imported lazily so importing the retrieval package (e.g. for the fake)
    does not pull in chromadb.
    """
    from thesis_matchmaker.indexing import build_embedder, build_store
    from thesis_matchmaker.retrieval.chroma import ChromaRetriever

    return ChromaRetriever(embedder=build_embedder(settings), store=build_store(settings))


__all__ = ["FakeRetriever", "Retriever", "build_retriever"]
