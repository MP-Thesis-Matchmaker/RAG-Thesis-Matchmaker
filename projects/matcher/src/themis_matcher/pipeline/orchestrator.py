"""The orchestration skeleton: parse -> retrieve -> rank -> synthesise.

Parsing runs through a real extractor (LLM when configured, rule-based
otherwise), retrieval and ranking sit behind the retriever, and synthesis turns
the ranked matches into a written recommendation. Each step is swappable.
"""

from __future__ import annotations

from thesis_matchmaker.contracts import ParsedQuery, SupervisorMatch
from thesis_matchmaker.parsing import QueryExtractor, RuleBasedExtractor, build_extractor
from thesis_matchmaker.retrieval.base import Retriever
from thesis_matchmaker.retrieval.fake import FakeRetriever
from thesis_matchmaker.synthesis import Synthesizer, build_synthesizer


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
        synthesizer: Synthesizer | None = None,
    ) -> None:
        self.retriever = retriever or FakeRetriever()
        self.extractor = extractor or build_extractor()
        self.synthesizer = synthesizer or build_synthesizer()

    def run(self, raw_query: str, top_k: int = 5) -> list[SupervisorMatch]:
        """Parse and retrieve: return the ranked matches, no prose."""
        parsed = self.extractor.extract(raw_query)
        return self.retriever.retrieve(parsed, top_k=top_k)

    def recommend(self, raw_query: str, top_k: int = 5) -> str:
        """Full flow: parse, retrieve, then write a grounded recommendation."""
        matches = self.run(raw_query, top_k=top_k)
        return self.synthesizer.synthesize(raw_query, matches)
