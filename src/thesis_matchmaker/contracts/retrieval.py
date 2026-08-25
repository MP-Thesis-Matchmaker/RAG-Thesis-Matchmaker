"""Contracts for the retrieval boundary: the query going in and matches coming out.

These sit between the orchestration layer and the retrieval and ranking
component. The orchestration and LLM steps only ever touch these shapes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from thesis_matchmaker.contracts.sources import DegreeLevel


class ParsedQuery(BaseModel):
    """A student's free-text query after it has been structured.

    This is what the retriever receives. In the LibreChat setup the agent fills
    these fields from the conversation; locally the parse step produces it.
    """

    topics: list[str] = Field(description="Research topics or interests pulled from the query.")
    keywords: list[str] = Field(default_factory=list)
    degree_level: DegreeLevel | None = None
    department: str | None = Field(
        default=None, description="Department, only if the student named one."
    )
    raw_query: str | None = Field(default=None, description="Original text, kept for reference.")


class Evidence(BaseModel):
    """One piece of support behind a recommendation, used for citations.

    Points back to a real publication or posting so the answer stays grounded
    instead of inventing a supervisor.
    """

    source_type: Literal["publication", "thesis_posting"]
    source_id: str = Field(description="Id of the ZoraPublication or ThesisPosting.")
    title: str
    url: str | None = None
    year: int | None = None


class SupervisorMatch(BaseModel):
    """One ranked recommendation from the retrieval and ranking layer.

    This is what the orchestration and LLM steps consume. A returned list is
    already ranked, best first -- but **not necessarily by score**: under the
    default `uzh_first` strategy, `has_uzh_affiliation` outranks similarity, so a
    lower-scored UZH supervisor precedes a higher-scored external researcher. Do
    not re-sort on `score` and assume the order is preserved.
    """

    supervisor: str = Field(description="Name of the recommended supervisor.")
    department: str | None = None
    score: float = Field(description="Final ranking score, higher is a better match.")
    has_uzh_affiliation: bool = Field(
        default=True,
        description=(
            "Whether this person is a registered UZH researcher -- a UZH author on "
            "some retrieved publication, or the named supervisor of a UZH thesis "
            "posting. False means an external co-author surfaced only because "
            "RETRIEVAL_REQUIRE_UZH_AUTHOR is off: relevant work, but nobody a "
            "student here can actually be supervised by, so callers should say so "
            "rather than presenting them as a supervisor. Defaults True because "
            "every producer predating the setting emitted UZH-only matches."
        ),
    )
    matched_topics: list[str] = Field(
        default_factory=list, description="Query topics this person matched on."
    )
    publication_count: int = Field(
        default=0, description="Supporting publications, one of the ranking signals."
    )
    posting_count: int = Field(
        default=0,
        description=(
            "Thesis postings retrieved for this person by this query. Says nothing "
            "about whether they accept students: the posting query is unthresholded, "
            "so 0 means none of theirs reached the top-k, not that none exist."
        ),
    )
    evidence: list[Evidence] = Field(
        default_factory=list, description="Publications and postings behind the match."
    )
