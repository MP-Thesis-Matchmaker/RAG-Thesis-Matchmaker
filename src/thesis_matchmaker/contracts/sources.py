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

    Checked against ZoraPipeline's live output directly (13,352 records,
    2026-07-08) — every field below is populated in production, not aspirational:

    - department is real now. ZoraPipeline resolves it from the owning
      DSpace collection UUID (each WWF sub-department has its own
      "Publications of ..." collection), not from item-level metadata.
    - uzh_authors / author_authority_map replace the author-identity approach
      from earlier drafts of this contract. UZH's DSpace-CRIS attaches a
      Person-entity UUID (an "authority" key) to each author metadata value
      when that author is a registered UZH researcher; external co-authors
      get no UUID. This is a materially better signal than picking
      authors[0] as a stand-in for "the supervisor" — position in an
      author list is not a reliable seniority signal (economics in
      particular defaults to alphabetical-by-surname ordering), and this
      lets retrieval identify actual candidate researchers directly instead
      of guessing from list position.
    """

    id: str = Field(description="ZORA record id (handle), unique and stable.")
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
            "author name -> CRIS Person UUID, or None for external "
            "co-authors. The UUID is stable across a person's publications, "
            "so it's also the right join key if researcher-level rollups "
            "are ever rebuilt."
        ),
    )
    year: int | None = None
    keywords: list[str] = Field(
        default_factory=list,
        description=(
            "DDC + Scopus subject labels. Coarse-grained (discipline level, "
            "not sub-topic) — useful for broad filtering, not for "
            "distinguishing e.g. NLP from other CS subfields. That "
            "distinction has to come from the title/abstract embedding."
        ),
    )
    department: str | None = Field(
        default=None,
        description="UZH department or center in the Faculty of Business and Informatics.",
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
