"""A deterministic, offline synthesiser.

No API calls. Formats the ranked matches into a readable recommendation. It is
grounded by construction: it only prints supervisors and evidence that are in
the matches, so it cannot invent anyone. Used by default and as the fallback
when no LLM is configured.
"""

from __future__ import annotations

from themis_shared.contracts import SupervisorMatch


class TemplateSynthesizer:
    """Renders matches into a recommendation without an LLM."""

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
            for item in match.evidence:
                reference = f" ({item.url})" if item.url else ""
                lines.append(f"   - {item.title}{reference}")
            lines.append("")
        return "\n".join(lines).rstrip()
