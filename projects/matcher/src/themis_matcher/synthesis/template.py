"""A deterministic, offline synthesiser.

No API calls. Formats the ranked matches into a readable recommendation. It is
grounded by construction: it only prints supervisors and evidence that are in
the matches, so it cannot invent anyone. Used by default and as the fallback
when no LLM is configured.

It also holds the threshold logic both synthesisers share -- `split_evidence`,
`margin` and `no_strong_match` -- so the template answer and the LLM answer
cannot disagree about who counts as a match.
"""

from __future__ import annotations

from themis_shared.contracts import Evidence, SupervisorMatch


def margin(match: SupervisorMatch, min_scores: dict[str, float]) -> float:
    """How far this person's best source sits above its own threshold.

    Taken over sources, not from the winner: thresholding only the
    higher-scoring source let a 0.56 publication (bar 0.57) drop someone whose
    0.50 posting (bar 0.48) would have passed alone -- being found twice made a
    person look worse. Negative means no source clears.
    """
    return max(score - min_scores[source] for source, score in match.source_scores.items())


def no_strong_match(
    query: str, matches: list[SupervisorMatch], min_scores: dict[str, float]
) -> str:
    """Deterministic answer for when nothing clears the score threshold.

    "Closest" is by `margin`, the distance to the candidate's own threshold. Raw
    `score` would compare across scales: a publication 0.02 under 0.57 would beat a
    posting 0.01 under 0.48 only because publications score higher everywhere.
    """
    closest = max(matches, key=lambda m: margin(m, min_scores))
    where = f" ({closest.department})" if closest.department else ""
    titles = "; ".join(item.title for item in closest.evidence) or "no listed work"
    return (
        f'No supervisor in our data looks like a strong match for "{query}". '
        f"The closest is {closest.supervisor}{where}. Their listed work: {titles}. "
        "It may still be worth contacting them, but treat it as a long shot."
    )


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

    `min_scores`, keyed like `SupervisorMatch.source_scores`, does what it does in
    `LLMSynthesizer`: a candidate with no source over its bar is not presented as
    a match -- if none clears, the answer says so and names the closest as a long
    shot -- and work from a source under its bar is labelled weaker-matching.
    `build_synthesizer` passes the configured bars, and `LLMSynthesizer` passes
    its own to the fallback it builds. Constructed without bars, nothing is
    filtered and every item is listed alike.
    """

    def __init__(self, min_scores: dict[str, float] | None = None) -> None:
        self._min_scores = min_scores

    def synthesize(self, query: str, matches: list[SupervisorMatch]) -> str:
        if not matches:
            return f'No suitable supervisors found for "{query}".'
        if self._min_scores is not None:
            bars = self._min_scores
            strong = [m for m in matches if margin(m, bars) >= 0]
            if not strong:
                return no_strong_match(query, matches, bars)
            matches = strong
        lines = [f'Based on your interest in "{query}", here are the top matches:', ""]
        for rank, match in enumerate(matches, start=1):
            where = f" ({match.department})" if match.department else ""
            # No topics, no claim: "Works on your topics" said the same circular
            # thing as copying the query into `matched_topics` did.
            works_on = (
                f"Works on {', '.join(match.matched_topics)}; " if match.matched_topics else ""
            )
            # Postings appear only when there are some, and there is deliberately no
            # else branch. A zero count means no posting of this person's reached the
            # top-k -- the posting query is unthresholded, so it says nothing about
            # whether they have one. Printing "no open position" asserted a fact
            # about a named academic that the data cannot support.
            # Not "open": available means only "not marked assigned or private",
            # which includes postings that are pending or carry no status at all.
            details = [f"{match.publication_count} related publications"]
            if match.posting_count:
                details.append(f"{match.posting_count} thesis posting(s)")
            lines.append(f"{rank}. {match.supervisor}{where}")
            lines.append(f"   {works_on}{'; '.join(details)}.")
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
