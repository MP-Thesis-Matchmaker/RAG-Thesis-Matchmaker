"""The answer-synthesis boundary used by the orchestration layer."""

from __future__ import annotations

from typing import Protocol

from thesis_matchmaker.contracts import SupervisorMatch


class Synthesizer(Protocol):
    """Turns ranked matches into a recommendation for the student.

    The pipeline depends only on this. The template and LLM-backed
    implementations are interchangeable, same idea as the other boundaries.
    """

    def synthesize(self, query: str, matches: list[SupervisorMatch]) -> str:
        """Return a written recommendation grounded in the given matches."""
        ...
