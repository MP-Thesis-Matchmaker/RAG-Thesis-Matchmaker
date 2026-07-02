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
        default_factory=list, description="Author names as listed on the publication."
    )
    year: int | None = None
    keywords: list[str] = Field(default_factory=list)
    department: str | None = Field(
        default=None, description="UZH department or institute, if known."
    )
    publication_type: str | None = Field(
        default=None, description="e.g. journal article, conference paper."
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
