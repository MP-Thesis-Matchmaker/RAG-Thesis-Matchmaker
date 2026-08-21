"""Contracts for the two raw data sources: ZORA publications and thesis postings.

These are the shapes the data-retrieval and scraping components must produce.
Both sides code against these so the pieces plug together without reading each
other's internals.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DegreeLevel(StrEnum):
    """Study level, normalised to one of three values."""

    bachelor = "bachelor"
    master = "master"
    phd = "phd"


class PostingStatus(StrEnum):
    """Whether a scraped topic is still available.

    Departmental pages mark topics as taken rather than removing them, so this is
    load-bearing: without it an assigned topic is indistinguishable from an open one
    and the system would recommend work nobody can do. Measured over the frozen spec
    corpus, 26 of 247 topics are not open.

    `taken` in the source vocabulary normalises to `assigned` -- they are the same
    claim, and three synonyms in one enum is a filter bug waiting to happen. `private`
    stays distinct: it means the topic exists but is not offered openly.
    """

    open = "open"
    assigned = "assigned"
    pending = "pending"
    private = "private"


class Supervisor(BaseModel):
    """A person named on a posting as supervising it.

    A list rather than a single string on `ThesisPosting`, because co-supervision is
    normal and the scraper extracts it.

    Only `name` is dependable. Measured over the 264 supervisor entries in the frozen
    spec corpus: 96 carry a bare name, 48 carry an email, and the remaining 120 carry
    some form of profile link instead (`profile_url`, `contact_url` or the internal
    `_url`), 64 of those alongside a chair. So a model demanding an email would
    discard four fifths of the contact information that exists.

    `email` is personal data the department chose to publish; it travels no further
    than the record carrying it and is never embedded.
    """

    name: str
    email: str | None = None
    profile_url: str | None = Field(
        default=None, description="Directory or homepage entry, however the page linked it."
    )
    chair: str | None = Field(default=None, description="Research group or chair, when given.")


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

    One object per posting. This model was written before any scraper existed and its
    posting-side guesses were wrong in four ways -- no `status`, a single-valued
    `degree_level`, one `supervisor` string, and no listing date. The shapes here now
    come from what the scraper actually extracts.
    """

    id: str = Field(description="Stable id for the posting, e.g. a hash of the source url.")
    title: str
    description: str | None = None
    supervisors: list[Supervisor] = Field(
        default_factory=list,
        description=(
            "Everyone the page names as supervising this topic. May be empty: plenty "
            "of pages list topics without naming anyone, and retrieval cannot turn "
            "such a posting into a supervisor recommendation."
        ),
    )
    faculty: str | None = Field(default=None, description="UZH faculty the source belongs to.")
    department: str | None = Field(
        default=None, description="Institute or organisational unit the source belongs to."
    )
    degree_levels: list[DegreeLevel] = Field(
        default_factory=list,
        description=(
            "Every level this topic is open to. A list, not a scalar: pages routinely "
            "offer one topic as either a Bachelor or a Master thesis, and collapsing "
            "that to one value hides the topic from half the students it is meant for. "
            "Plural name on purpose -- a same-named type change would have compiled at "
            "every call site and misbehaved at runtime."
        ),
    )
    status: PostingStatus | None = Field(
        default=None, description="open / assigned / pending, when the page says."
    )
    keywords: list[str] = Field(default_factory=list)
    language: str | None = Field(default=None, description="Two-letter code, e.g. 'de' or 'en'.")
    url: str = Field(description="Source page the posting was scraped from.")
    listed_on: date | None = Field(
        default=None, description="Date the page gives for the listing, if any."
    )
    source_id: str | None = Field(
        default=None, description="Scraper registry id of the page this came from."
    )
    scraped_at: datetime | None = None


class ResearcherProfile(BaseModel):
    """A researcher as their own department page describes them.

    Not derived from publications: this is a person stating their interests in their
    own words, which is a different and independent signal from what ZORA infers from
    authorship. Persisted now, not yet used by retrieval.
    """

    id: str = Field(description="Stable id, e.g. a hash of the profile url.")
    name: str
    email: str | None = None
    role: str | None = Field(default=None, description="e.g. Professor, Postdoc, PhD student.")
    research_interest: str | None = None
    research_field: str | None = None
    research_group: str | None = None
    bio: str | None = None
    personal_website: str | None = None
    profile_url: str | None = None
    faculty: str | None = None
    department: str | None = None
    source_id: str | None = None
    scraped_at: datetime | None = None

    # Deliberately absent: `office` and `phone`, which 19 scraped profiles carry.
    # They are personal data that contributes nothing to matching a student to a
    # supervisor, so the normaliser drops them rather than storing them because they
    # happened to be on the page.


class ApplicationProcess(BaseModel):
    """How to apply for a thesis at one unit, for one degree level.

    One object per (unit, degree level). The prototype consolidates several source
    pages -- a Bachelor page, a Bachelor PDF -- into a single entry per level, so
    `source_ids` is a list.
    """

    id: str = Field(description="Stable id, e.g. a hash of unit and degree level.")
    degree_level: DegreeLevel | None = None
    description: str | None = Field(
        default=None, description="Prose summary of the procedure. LLM-written."
    )
    relevant_links: list[dict[str, str]] = Field(
        default_factory=list, description="[{url, description}] pulled off the page."
    )
    url: str | None = Field(default=None, description="Primary source page.")
    faculty: str | None = None
    department: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    scraped_at: datetime | None = None
