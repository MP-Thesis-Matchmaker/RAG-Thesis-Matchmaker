"""Contracts for the two raw data sources: ZORA publications and thesis postings.

These are the shapes the data-retrieval and scraping components must produce.
Both sides code against these so the pieces plug together without reading each
other's internals.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DegreeLevel(StrEnum):
    """Study level, normalised to one of three values."""

    bachelor = "bachelor"
    master = "master"
    phd = "phd"


class ZoraRecord(BaseModel):
    """A single publication retrieved from ZORA.

    One object per publication. Working out who publishes how much on a topic
    happens later in ranking, not here.
    """

    id: str = Field(description="ZORA record id, unique and stable.")
    title: str
    abstract: str | None = Field(default=None, description="Abstract text if ZORA has one.")
    authors: list[str] = Field(
        default_factory=list, description="Every author name as listed on the publication."
    )
    uzh_authors: list[str] = Field(
        default_factory=list,
        description=(
            "Subset of authors with a CRIS authority key — i.e. registered "
            "UZH researchers. These are the candidate people for supervisor "
            "matching; the rest of authors[] are external collaborators."
        ),
    )
    author_authority_map: dict[str, str | None] = Field(
        default_factory=dict,
        description=(
            "author name -> stable UZH researcher id, or None for external "
            "co-authors. Stable across a person's publications, so it's "
            "also the right join key for any future researcher-level "
            "rollup. Position in the author list alone isn't a reliable "
            "stand-in for this — it's not a seniority signal in every "
            "field (economics, for instance, defaults to alphabetical "
            "ordering by surname)."
        ),
    )
    year: int | None = None
    keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Subject/classification labels. Discipline-level rather than "
            "sub-topic — useful for broad filtering, not for "
            "distinguishing specific research areas within a field."
        ),
    )
    department: str | None = Field(
        default=None,
        description="UZH department or center, if known.",
    )
    language: str | None = Field(
        default=None, description="ISO 639 code from dc.language.iso, e.g. 'eng', 'deu'."
    )
    publication_type: str | None = Field(
        default=None, description="e.g. article, working_paper, dissertation."
    )
    doi: str | None = None
    url: str | None = Field(default=None, description="Link to the ZORA landing page.")


class ThesisPosting(BaseModel):
    """An open thesis position scraped from a departmental website.

    One object per posting.
    """

    id: str = Field(description="Stable id for the posting, e.g. a hash of the source url.")
    title: str
    description: str | None = None
    supervisor: str | None = Field(
        default=None, description="Named supervisor if the page lists one."
    )
    department: str | None = None
    degree_level: DegreeLevel | None = Field(
        default=None, description="Normalised to bachelor / master / phd."
    )
    keywords: list[str] = Field(default_factory=list)
    language: str | None = Field(default=None, description="Two-letter code, e.g. 'de' or 'en'.")
    url: str = Field(description="Source page the posting was scraped from.")
    scraped_at: datetime | None = None
