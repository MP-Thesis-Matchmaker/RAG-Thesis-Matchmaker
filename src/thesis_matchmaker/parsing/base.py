"""The query-extraction boundary used by the orchestration layer."""

from __future__ import annotations

from typing import Protocol

from thesis_matchmaker.contracts import ParsedQuery


class QueryExtractor(Protocol):
    """Turns a student's free text into a structured ParsedQuery.

    The pipeline depends only on this. The rule-based and LLM-backed
    implementations are interchangeable, same idea as the retriever boundary.
    """

    def extract(self, raw_query: str) -> ParsedQuery:
        """Return a ParsedQuery for the given free-text query."""
        ...
