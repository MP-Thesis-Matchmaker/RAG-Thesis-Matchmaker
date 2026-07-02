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
    source_id: str = Field(description="Id of the ZoraRecord or ThesisPosting.")
    title: str
    url: str | None = None
    year: int | None = None


class SupervisorMatch(BaseModel):
    """One ranked recommendation from the retrieval and ranking layer.

    This is what the orchestration and LLM steps consume. A returned list is
    already sorted by score, highest first.
    """

    supervisor: str = Field(description="Name of the recommended supervisor.")
    department: str | None = None
    score: float = Field(description="Final ranking score, higher is a better match.")
    matched_topics: list[str] = Field(
        default_factory=list, description="Query topics this person matched on."
    )
    publication_count: int = Field(
        default=0, description="Supporting publications, one of the ranking signals."
    )
    has_open_position: bool = False
    evidence: list[Evidence] = Field(
        default_factory=list, description="Publications and postings behind the match."
    )
