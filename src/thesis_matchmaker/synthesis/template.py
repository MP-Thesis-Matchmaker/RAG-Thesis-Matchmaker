"""A deterministic, offline synthesiser.

No API calls. Formats the ranked matches into a readable recommendation. It is
grounded by construction: it only prints supervisors and evidence that are in
the matches, so it cannot invent anyone. Used by default and as the fallback
when no LLM is configured.
"""

from __future__ import annotations

from thesis_matchmaker.contracts import SupervisorMatch


class TemplateSynthesizer:
    """Renders matches into a recommendation without an LLM."""

    def synthesize(self, query: str, matches: list[SupervisorMatch]) -> str:
        if not matches:
            return f'No suitable supervisors found for "{query}".'
        lines = [f'Based on your interest in "{query}", here are the top matches:', ""]
        for rank, match in enumerate(matches, start=1):
            where = f" ({match.department})" if match.department else ""
            position = (
                "has an open thesis position"
                if match.has_open_position
                else "no open position listed"
            )
            topics = ", ".join(match.matched_topics) or "your topics"
            lines.append(f"{rank}. {match.supervisor}{where}")
            lines.append(
                f"   Works on {topics}; {match.publication_count} related publications; {position}."
            )
            for item in match.evidence:
                reference = f" ({item.url})" if item.url else ""
                lines.append(f"   - {item.title}{reference}")
            lines.append("")
        return "\n".join(lines).rstrip()
