"""A deterministic, offline synthesiser.

No API calls. Formats the ranked matches into a readable recommendation. It is
grounded by construction: it only prints supervisors and evidence that are in
the matches, so it cannot invent anyone. Used by default and as the fallback
when no LLM is configured.
"""

from __future__ import annotations

from themis_shared.contracts import Evidence, SupervisorMatch


def split_evidence(
    match: SupervisorMatch, min_scores: dict[str, float] | None
) -> tuple[list[Evidence], list[Evidence]]:
    """(cleared, weaker): evidence from sources over their bar, and the rest.

    A person who passed on one source is not vouched for by the other. Those
    titles stay -- they are real -- but are kept apart, so a posting-backed
    supervisor's below-bar papers are not presented as the reason they fit. With
    no bars everything clears. Shared by both synthesisers so the LLM prompt and
    the template fallback cannot disagree about which work is weaker.
    """
    cleared = {
        source
        for source, score in match.source_scores.items()
        if min_scores is None or score >= min_scores[source]
    }
    return (
        [e for e in match.evidence if e.source_type in cleared],
        [e for e in match.evidence if e.source_type not in cleared],
    )


class TemplateSynthesizer:
    """Renders matches into a recommendation without an LLM.

    `min_scores`, keyed like `SupervisorMatch.source_scores`, labels work from a
    source under its bar as weaker-matching. `LLMSynthesizer` passes its own bars
    to the fallback it builds, so an LLM outage does not quietly drop that
    distinction. Without bars -- the offline default, which thresholds nothing --
    every item is listed alike.
    """

    def __init__(self, min_scores: dict[str, float] | None = None) -> None:
        self._min_scores = min_scores

    def synthesize(self, query: str, matches: list[SupervisorMatch]) -> str:
        if not matches:
            return f'No suitable supervisors found for "{query}".'
        lines = [f'Based on your interest in "{query}", here are the top matches:', ""]
        for rank, match in enumerate(matches, start=1):
            where = f" ({match.department})" if match.department else ""
            topics = ", ".join(match.matched_topics) or "your topics"
            # Postings appear only when there are some, and there is deliberately no
            # else branch. A zero count means no posting of this person's reached the
            # top-k -- the posting query is unthresholded, so it says nothing about
            # whether they have one. Printing "no open position" asserted a fact
            # about a named academic that the data cannot support.
            details = [f"{match.publication_count} related publications"]
            if match.posting_count:
                details.append(f"{match.posting_count} open thesis posting(s)")
            lines.append(f"{rank}. {match.supervisor}{where}")
            lines.append(f"   Works on {topics}; {'; '.join(details)}.")
            cleared, weaker = split_evidence(match, self._min_scores)
            for item in cleared:
                reference = f" ({item.url})" if item.url else ""
                lines.append(f"   - {item.title}{reference}")
            if weaker:
                lines.append("   Weaker-matching work:")
                for item in weaker:
                    reference = f" ({item.url})" if item.url else ""
                    lines.append(f"   - {item.title}{reference}")
            lines.append("")
        return "\n".join(lines).rstrip()
