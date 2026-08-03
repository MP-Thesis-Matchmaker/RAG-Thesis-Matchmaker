"""The ground-truth query set: one shared file the whole team contributes to.

Each line of the JSONL file is one query with the supervisors we agree it
should surface, or a flag saying no supervisor at UZH fits. Keeping it as JSONL
means several people can add lines without constantly conflicting in git.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from thesis_matchmaker.contracts import DegreeLevel

DEFAULT_DATASET = Path("eval/ground_truth.jsonl")


class GroundTruthQuery(BaseModel):
    """One annotated student query.

    A query is either answerable, in which case `relevant_supervisors` lists the
    people we expect to see, or a no-match case, where the right behaviour is to
    say nobody suitable was found. The list is not exhaustive: other genuinely
    relevant people may exist, which is why we report recall-style metrics
    rather than treating unlisted names as wrong.
    """

    id: str = Field(description="Stable id, e.g. gt-001.")
    query: str = Field(description="What the student would actually type.")
    relevant_supervisors: list[str] = Field(
        default_factory=list,
        description=(
            "Names as they appear in the source data (ZORA uses 'Surname, "
            "Firstname'). Empty for a no-match query."
        ),
    )
    no_match: bool = Field(
        default=False,
        description="True when no UZH supervisor should be recommended for this query.",
    )
    difficulty: str = Field(
        default="easy",
        description=(
            "easy = clearly in or out of scope; hard = near-domain, thin "
            "evidence, or only ineligible people match. Hard cases are the "
            "interesting ones."
        ),
    )
    degree_level: DegreeLevel | None = None
    department: str | None = Field(default=None, description="Only if the query implies one.")
    notes: str | None = Field(default=None, description="Why this answer, for the review round.")
    contributor: str | None = Field(default=None, description="Who added it, so we can ask.")

    @model_validator(mode="after")
    def _check_consistency(self) -> GroundTruthQuery:
        if self.no_match and self.relevant_supervisors:
            raise ValueError(f"{self.id}: a no-match query cannot list relevant supervisors")
        if not self.no_match and not self.relevant_supervisors:
            raise ValueError(f"{self.id}: set no_match=true, or list at least one supervisor")
        return self


def load_dataset(path: Path | str = DEFAULT_DATASET) -> list[GroundTruthQuery]:
    """Read the JSONL file, skipping blank lines and `#` comments."""
    path = Path(path)
    queries: list[GroundTruthQuery] = []
    seen: set[str] = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            query = GroundTruthQuery.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
        if query.id in seen:
            raise ValueError(f"{path}:{number}: duplicate id {query.id}")
        seen.add(query.id)
        queries.append(query)
    return queries
