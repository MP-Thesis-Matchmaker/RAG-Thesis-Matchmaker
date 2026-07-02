"""The retrieval boundary that the retrieval and ranking component implements."""

from __future__ import annotations

from typing import Protocol

from thesis_matchmaker.contracts import ParsedQuery, SupervisorMatch


class Retriever(Protocol):
    """What the orchestration layer depends on for retrieval.

    Anything that implements this method can be dropped in. The real retriever
    and the fake one below are interchangeable from the pipeline's point of view.
    """

    def retrieve(self, query: ParsedQuery, top_k: int = 5) -> list[SupervisorMatch]:
        """Return up to top_k matches, sorted by score, highest first."""
        ...
