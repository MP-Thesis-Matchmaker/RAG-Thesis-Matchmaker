"""LLM-backed answer synthesiser over an OpenAI-compatible chat endpoint.

Writes a short, natural recommendation, but strictly from the retrieved
matches: the prompt lists the candidates and asks the model to cite them and
invent nothing, and to say plainly when a candidate is only a partial fit.
Candidates below a configurable score threshold are not presented as matches at
all; instead the answer states there is no strong match and names the closest
candidate as a long shot -- closest by margin to its own source's threshold, not
by raw score. The threshold is per source type -- publications and
postings are not on a common scale, so one value cannot serve both without
deleting posting-backed supervisors; see docs/score-calibration.md. A candidate
retrieved through both sources passes if **either** source's best score clears
that source's threshold; the titles from a source that did not clear still reach
the prompt, but labelled as weaker-matching work rather than as the reason for the
fit. Falls back to the template synthesiser on any error.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from themis_matcher.llm import LLMClient, LLMError
from themis_matcher.synthesis.base import Synthesizer
from themis_matcher.synthesis.template import TemplateSynthesizer, split_evidence
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
    "present the closest option as a long shot. Work listed as weaker-matching "
    "matched the query less well: mention it only as secondary, never as the "
    "main reason a candidate fits. Never state or imply whether a "
    "supervisor is accepting students, has supervision capacity, or is available: "
    "that information is not in the data."
)


def _format_candidates(
    matches: list[SupervisorMatch], min_scores: dict[str, float] | None = None
) -> str:
    blocks = []
    for match in matches:
        where = f" ({match.department})" if match.department else ""
        cleared, below = split_evidence(match, min_scores)
        titles = "; ".join(e.title for e in cleared)
        weaker = "; ".join(e.title for e in below)
        # Absent data has to reach the prompt as absent. Given "no open position"
        # the model wrote "not currently accepting new students" about a named
        # academic; a line it never sees is a line it cannot paraphrase. Topics
        # likewise: the retriever does not compute them, and "topics n/a" invites
        # the model to fill the gap from the query.
        details = [f"{match.publication_count} publications"]
        if match.matched_topics:
            details.insert(0, f"topics {', '.join(match.matched_topics)}")
        if match.posting_count:
            details.append(f"{match.posting_count} open thesis posting(s)")
        details.append(f"work: {titles or 'no listed work'}")
        if weaker:
            details.append(f"weaker-matching work: {weaker}")
        blocks.append(f"- {match.supervisor}{where}: {'; '.join(details)}")
    return "\n".join(blocks)


def _no_strong_match(
    query: str, matches: list[SupervisorMatch], margin: Callable[[SupervisorMatch], float]
) -> str:
    """Deterministic answer for when nothing clears the score threshold.

    "Closest" is by `margin`, the distance to the candidate's own threshold. Raw
    `score` would compare across scales: a publication 0.02 under 0.57 would beat a
    posting 0.01 under 0.48 only because publications score higher everywhere.
    """
    closest = max(matches, key=margin)
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
        min_score_publication: float = 0.0,
        min_score_posting: float = 0.0,
    ) -> None:
        self._client = client
        # Keyed like SupervisorMatch.source_scores, so a third kind of source is a
        # data change rather than another branch. Both default to 0.0: an
        # explicitly constructed synthesiser filters nothing unless told to, and
        # the measured values arrive from settings via build_synthesizer.
        self._min_scores = {
            "publication": min_score_publication,
            "thesis_posting": min_score_posting,
        }
        # The fallback gets the same bars, so an LLM outage still labels below-bar
        # work as weaker instead of listing it as the reason a person fits.
        self._fallback = fallback or TemplateSynthesizer(min_scores=self._min_scores)

    def _margin(self, match: SupervisorMatch) -> float:
        """How far this person's best source sits above its own threshold.

        Taken over sources, not from the winner: thresholding only the
        higher-scoring source let a 0.56 publication (bar 0.57) drop someone whose
        0.50 posting (bar 0.48) would have passed alone -- being found twice made a
        person look worse. Negative means no source clears.
        """
        return max(
            score - self._min_scores[source] for source, score in match.source_scores.items()
        )

    def _clears_threshold(self, match: SupervisorMatch) -> bool:
        """Whether any one source vouches for this person on its own scale."""
        return self._margin(match) >= 0

    def synthesize(self, query: str, matches: list[SupervisorMatch]) -> str:
        if not matches:
            return self._fallback.synthesize(query, matches)
        strong = [m for m in matches if self._clears_threshold(m)]
        if not strong:
            return _no_strong_match(query, matches, self._margin)
        candidates = _format_candidates(strong, self._min_scores)
        user = f'Student query: "{query}"\n\nCandidates:\n{candidates}'
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
