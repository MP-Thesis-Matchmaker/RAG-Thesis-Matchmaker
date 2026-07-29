"""LLM-backed answer synthesiser over an OpenAI-compatible chat endpoint.

Writes a short, natural recommendation, but strictly from the retrieved
matches: the prompt lists the candidates and asks the model to cite them and
invent nothing, and to say plainly when a candidate is only a partial fit.
Candidates below a configurable score threshold are not presented as matches at
all; instead the answer states there is no strong match and names the closest
candidate as a long shot. Falls back to the template synthesiser on any error.
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
    "facts that are not in the candidates. If a candidate only partially fits "
    "the student's interests, say so plainly instead of overstating the fit. "
    "If no candidate fits well, open by saying there is no strong match and "
    "present the closest option as a long shot."
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


def _no_strong_match(query: str, matches: list[SupervisorMatch]) -> str:
    """Deterministic answer for when nothing clears the score threshold."""
    closest = max(matches, key=lambda m: m.score)
    where = f" ({closest.department})" if closest.department else ""
    titles = "; ".join(item.title for item in closest.evidence) or "no listed work"
    return (
        f'No supervisor in our data looks like a strong match for "{query}". '
        f"The closest is {closest.supervisor}{where}. Their listed work: {titles}. "
        "It may still be worth contacting them, but treat it as a long shot."
    )


class LLMSynthesizer:
    """Writes the recommendation with an LLM, grounded in the matches."""

    def __init__(
        self,
        client: LLMClient,
        fallback: Synthesizer | None = None,
        min_score: float = 0.0,
    ) -> None:
        self._client = client
        self._fallback = fallback or TemplateSynthesizer()
        self._min_score = min_score

    def synthesize(self, query: str, matches: list[SupervisorMatch]) -> str:
        if not matches:
            return self._fallback.synthesize(query, matches)
        strong = [m for m in matches if m.score >= self._min_score]
        if not strong:
            return _no_strong_match(query, matches)
        user = f'Student query: "{query}"\n\nCandidates:\n{_format_candidates(strong)}'
        try:
            return self._client.chat(_SYSTEM, user).strip()
        except LLMError:
            return self._fallback.synthesize(query, strong)
