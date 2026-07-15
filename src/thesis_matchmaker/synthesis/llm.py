"""LLM-backed answer synthesiser over an OpenAI-compatible chat endpoint.

Writes a short, natural recommendation, but strictly from the retrieved
matches: the prompt lists the candidates and asks the model to cite them and
invent nothing. Falls back to the template synthesiser on any error.
"""

from __future__ import annotations

from thesis_matchmaker.contracts import SupervisorMatch
from thesis_matchmaker.llm import LLMClient, LLMError
from thesis_matchmaker.synthesis.base import Synthesizer
from thesis_matchmaker.synthesis.template import TemplateSynthesizer

_SYSTEM = (
    "You help a student pick a thesis supervisor. Using only the candidates "
    "provided, write a short, friendly recommendation of a few sentences. Name "
    "the most relevant supervisors, say briefly why each fits, and refer to "
    "their listed work by title. Do not invent supervisors, publications, or "
    "facts that are not in the candidates."
)


def _format_candidates(matches: list[SupervisorMatch]) -> str:
    blocks = []
    for match in matches:
        where = f" ({match.department})" if match.department else ""
        titles = "; ".join(item.title for item in match.evidence) or "no listed work"
        position = "open position" if match.has_open_position else "no open position"
        topics = ", ".join(match.matched_topics) or "n/a"
        blocks.append(
            f"- {match.supervisor}{where}: topics {topics}; "
            f"{match.publication_count} publications; {position}; work: {titles}"
        )
    return "\n".join(blocks)


class LLMSynthesizer:
    """Writes the recommendation with an LLM, grounded in the matches."""

    def __init__(self, client: LLMClient, fallback: Synthesizer | None = None) -> None:
        self._client = client
        self._fallback = fallback or TemplateSynthesizer()

    def synthesize(self, query: str, matches: list[SupervisorMatch]) -> str:
        if not matches:
            return self._fallback.synthesize(query, matches)
        user = f'Student query: "{query}"\n\nCandidates:\n{_format_candidates(matches)}'
        try:
            return self._client.chat(_SYSTEM, user).strip()
        except LLMError:
            return self._fallback.synthesize(query, matches)
