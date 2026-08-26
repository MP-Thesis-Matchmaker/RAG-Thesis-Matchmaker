"""LLM-backed answer synthesiser over an OpenAI-compatible chat endpoint.

Writes a short, natural recommendation, but strictly from the retrieved
matches: the prompt lists the candidates and asks the model to cite them and
invent nothing, and to say plainly when a candidate is only a partial fit.
Candidates below a configurable score threshold are not presented as matches at
all; instead the answer states there is no strong match and names the closest
candidate as a long shot. Falls back to the template synthesiser on any error.
"""

from __future__ import annotations

import logging

from themis_matcher.llm import LLMClient, LLMError
from themis_matcher.synthesis.base import Synthesizer
from themis_matcher.synthesis.template import TemplateSynthesizer
from themis_shared.contracts import SupervisorMatch

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You help a student pick a thesis supervisor. Using only the candidates "
    "provided, write a short, friendly recommendation of a few sentences. Name "
    "the most relevant supervisors, say briefly why each fits, and refer to "
    "their listed work by title. Do not invent supervisors, publications, or "
    "facts that are not in the candidates. If a candidate only partially fits "
    "the student's interests, say so plainly instead of overstating the fit. "
    "If no candidate fits well, open by saying there is no strong match and "
    "present the closest option as a long shot. Never state or imply whether a "
    "supervisor is accepting students, has supervision capacity, or is available: "
    "that information is not in the data."
)


def _format_candidates(matches: list[SupervisorMatch]) -> str:
    blocks = []
    for match in matches:
        where = f" ({match.department})" if match.department else ""
        titles = "; ".join(item.title for item in match.evidence) or "no listed work"
        topics = ", ".join(match.matched_topics) or "n/a"
        # Absent data has to reach the prompt as absent. Given "no open position"
        # the model wrote "not currently accepting new students" about a named
        # academic; a line it never sees is a line it cannot paraphrase.
        details = [f"topics {topics}", f"{match.publication_count} publications"]
        if match.posting_count:
            details.append(f"{match.posting_count} open thesis posting(s)")
        details.append(f"work: {titles}")
        blocks.append(f"- {match.supervisor}{where}: {'; '.join(details)}")
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
        except LLMError as exc:
            # Same reasoning as the parser: the template answer is a fine
            # degradation but an invisible one, so say that the LLM was tried
            # and lost rather than letting it pass for the offline path.
            logger.warning(
                "LLM synthesis failed (%s: %s) - falling back to the template synthesiser",
                type(exc).__name__,
                exc,
            )
            return self._fallback.synthesize(query, strong)
