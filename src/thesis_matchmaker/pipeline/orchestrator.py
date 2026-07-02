"""The orchestration skeleton: parse -> retrieve -> (rank) -> (synthesise).

Ranking currently lives inside the retriever, and answer synthesis is not wired
yet. Both slot in here without changing the callers.
"""

from __future__ import annotations

from thesis_matchmaker.contracts import ParsedQuery, SupervisorMatch
from thesis_matchmaker.retrieval.base import Retriever
from thesis_matchmaker.retrieval.fake import FakeRetriever


def parse_query(raw_query: str) -> ParsedQuery:
    """Turn free text into a structured query.

    Placeholder: the real version calls the LLM to pull out topics, keywords,
    and degree level. For now it keeps the whole query as a single topic so the
    rest of the pipeline can run end to end.
    """
    return ParsedQuery(topics=[raw_query], raw_query=raw_query)


class Pipeline:
    """Ties the steps together behind one call."""

    def __init__(self, retriever: Retriever | None = None) -> None:
        self.retriever = retriever or FakeRetriever()

    def run(self, raw_query: str, top_k: int = 5) -> list[SupervisorMatch]:
        parsed = parse_query(raw_query)
        return self.retriever.retrieve(parsed, top_k=top_k)
