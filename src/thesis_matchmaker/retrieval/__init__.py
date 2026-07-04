"""Retrieval layer: the boundary and its implementations."""

from thesis_matchmaker.retrieval.base import Retriever
from thesis_matchmaker.retrieval.fake import FakeRetriever

__all__ = ["FakeRetriever", "Retriever"]
