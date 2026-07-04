"""The orchestration skeleton: parse -> retrieve -> (rank) -> (synthesise).

Parsing now runs through a real extractor (Claude when configured, rule-based
otherwise). Ranking currently lives inside the retriever, and answer synthesis
is not wired yet. Both slot in here without changing the callers.
"""

from __future__ import annotations

from thesis_matchmaker.contracts import ParsedQuery, SupervisorMatch
from thesis_matchmaker.parsing import QueryExtractor, RuleBasedExtractor, build_extractor
from thesis_matchmaker.retrieval.base import Retriever
from thesis_matchmaker.retrieval.fake import FakeRetriever


def parse_query(raw_query: str) -> ParsedQuery:
    """Structure a free-text query with the offline rule-based extractor.

    Convenience wrapper that never touches the network. The Pipeline uses the
    configured extractor (real LLM when a key is set) instead.
    """
    return RuleBasedExtractor().extract(raw_query)


class Pipeline:
    """Ties the steps together behind one call."""

    def __init__(
        self,
        retriever: Retriever | None = None,
        extractor: QueryExtractor | None = None,
    ) -> None:
        self.retriever = retriever or FakeRetriever()
        self.extractor = extractor or build_extractor()

    def run(self, raw_query: str, top_k: int = 5) -> list[SupervisorMatch]:
        parsed = self.extractor.extract(raw_query)
        return self.retriever.retrieve(parsed, top_k=top_k)
