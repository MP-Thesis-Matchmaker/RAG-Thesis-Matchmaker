"""Contracts for the raw data sources: ZORA (publications, researchers, org units)
and scraped thesis postings.

These are the shapes the data-retrieval and scraping components must produce.
Both sides code against these so the pieces plug together without reading each
other's internals.

**Every data model lives here, including the harvester's own output shapes.** That
is not a style preference: `zora/` used to keep a parallel set (`ZoraPublication`,
`ZoraPerson`, `ZoraOrgUnit`, a second `AuthorAuthority`) whose docstring claimed
field-alignment with this file, and the two drifted in three ways at once -- a
`title` that was optional on one side and required on the other, an
`owning_collection_uuid` that existed on only one, and a duplicated authority type
that nothing pinned together. One model per shape is what makes that class of bug
impossible rather than merely documented.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

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


class AuthorAuthority(BaseModel):
    """One author's typed identifier, as DSpace's authority marker classifies it.

    cris:  id is a CRIS Person item UUID — a registered UZH researcher; it
           resolves in the `person` table.
    orcid: id is a bare ORCID, and DSpace did not link THIS item to a local
           Person. That is a statement about the record, not about the human:
           a Person entity with the same ORCID may still exist, and 2 in the
           2026-08-25 corpus do. The honest reading is *unknown affiliation*,
           not external -- CRIS coverage is sparse (~2,000 Person entities), so
           "no UUID" does not mean "not UZH". Such authors are ranked below UZH
           researchers rather than excluded, and `store.reconcile_uzh_authors`
           promotes the ones whose ORCID resolves in `person`.

    The marker decides the type wherever DSpace supplies one, however malformed
    the payload. Shape decides only unmarked values, which is how a bare ORCID
    sent without the marker avoids being filed as a Person id; see
    `zora.normalize._typed_authority`.
    """

    type: Literal["cris", "orcid"]
    id: str


class ZoraPublication(BaseModel):
    """A single publication retrieved from ZORA.

    One object per publication -- persons and org units are ZORA records too, which
    is why this name says which kind. Working out who publishes how much on a topic
    happens later in ranking, not here.

    This is also exactly what the harvester writes: one `publication` row per
    instance, validated once on the way in.
    """

    id: str = Field(description="ZORA handle, unique and stable across harvests.")
    # Optional because the column is. ZORA does have title-less items, and the
    # alternative -- a required field the Postgres reader satisfied with `or ""` --
    # meant the index quietly held publications whose title was the empty string.
    title: str | None = None
    abstract: str | None = Field(default=None, description="Abstract text if ZORA has one.")
    authors: list[str] = Field(
        default_factory=list, description="Every author name as listed on the publication."
    )
    uzh_authors: list[str] = Field(
        default_factory=list,
        description=(
            "Authors carrying a CRIS Person UUID — registered UZH researchers, "
            "resolvable in the `person` table. A subset of `authors`, in the "
            "same order. ORCID-only authors are deliberately absent: DSpace "
            "records those as unlinked to any local Person, so their "
            "affiliation is unknown. They stay in `author_authority_map` and "
            "stay retrievable; retrieval ranks them below UZH researchers "
            "rather than dropping them."
        ),
    )
    author_authority_map: dict[str, AuthorAuthority | None] = Field(
        default_factory=dict,
        description=(
            "author name -> typed authority ({type: cris|orcid, id}), or None "
            "for authors with no identifier at all. The id is stable across a "
            "person's publications, so it's also the right join key for any "
            "future researcher-level rollup — cris ids resolve in the person "
            "table. Position in the author list alone isn't a reliable "
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
        description=(
            "UZH department or center, if known. The display name of the owning "
            "collection; `owning_collection_uuid` is the same unit as a join key."
        ),
    )
    owning_collection_uuid: str | None = Field(
        default=None,
        description=(
            "UUID of the 'Publications of X' collection this item belongs to. "
            "Joins to org_unit.collection_uuid — publications belong to "
            "collections, never directly to the communities that model org units."
        ),
    )
    language: str | None = Field(
        default=None, description="ISO 639 code from dc.language.iso, e.g. 'eng', 'deu'."
    )
    publication_type: str | None = Field(
        default=None, description="e.g. article, working_paper, dissertation."
    )
    doi: str | None = None
    url: str | None = Field(default=None, description="Link to the ZORA landing page.")
    accessioned: str | None = Field(
        default=None,
        description=(
            "dc.date.accessioned verbatim, as ZORA reports it. Text rather than a "
            "datetime because it is fed back into a Solr range query unchanged, and "
            "part of the contract rather than harvester-internal because it is "
            "written to the row: the incremental watermark is recomputed from the "
            "data instead of being trusted blindly."
        ),
    )


class ZoraPerson(BaseModel):
    """A researcher as DSpace-CRIS records them: one `dspace.entity.type:Person` item.

    Distinct from `ResearcherProfile`, which is the same kind of human described by
    their own department page. This one is what a cris-typed `AuthorAuthority.id`
    resolves to, and it carries no affiliation at all: probed 2026-08-24, Person
    items have names, an ORCID and a handle, and nothing else. Department attribution
    has to come from the publications, not from here.

    Coverage is sparse (~2,000 items against ~58,000 distinct author names), so
    absent-from-here does **not** mean not-UZH.
    """

    uuid: str = Field(description="CRIS item UUID, stable across harvests.")
    display_name: str | None = Field(default=None, description='dc.title, "Family, Given".')
    family_name: str | None = None
    given_name: str | None = None
    orcid: str | None = Field(
        default=None, description="Bare ORCID, any URL prefix stripped by the normaliser."
    )
    handle: str | None = None
    url: str | None = Field(default=None, description="Link to the ZORA landing page.")
    accessioned: str | None = None


class ZoraOrgUnit(BaseModel):
    """One node of the UZH community tree: a faculty, institute or center.

    ZORA's own OrgUnit entity type is empty upstream (0 items, probed 2026-08-24) --
    the org structure is modelled as communities, so that is what this mirrors. The
    tree is walked breadth-first from the UZH root, which is where `parent_uuid`,
    `depth` and `faculty_uuid` come from; none of the three is a metadata field.
    """

    uuid: str = Field(description="Community UUID, stable across harvests.")
    name: str = Field(
        description='Verbatim, including the ordering prefix ("03 Faculty of Economics").'
    )
    parent_uuid: str | None = Field(default=None, description="None only for the UZH root.")
    faculty_uuid: str | None = Field(
        default=None,
        description=(
            "The depth-1 ancestor, or itself for a faculty; None for the root. Rolls "
            "an institute up to its faculty without a recursive query."
        ),
    )
    depth: int = Field(description="0 = UZH root, 1 = faculty, 2+ = institute or clinic.")
    handle: str | None = None
    subject_id: str | None = Field(
        default=None,
        description="dc.zora.subjectid — UZH's own numeric org id, independent of DSpace.",
    )
    collection_uuid: str | None = Field(
        default=None,
        description=(
            "The attached 'Publications of X' collection, i.e. what "
            "ZoraPublication.owning_collection_uuid joins against. None for units "
            "that only group other units."
        ),
    )
    collection_name: str | None = None


class ThesisPosting(BaseModel):
    """An open thesis position scraped from a departmental website.

    One object per posting. This model was written before any scraper existed and its
    posting-side guesses were wrong in four ways -- no `status`, a single-valued
    `degree_level`, one `supervisor` string, and no listing date. The shapes here now
    come from what the scraper actually extracts.
    """

    id: str = Field(description="Stable id for the posting, e.g. a hash of the source url.")
    # Optional for the same reason as ZoraPublication.title: the column is nullable,
    # and a required field only meant the reader substituted "" and moved on.
    title: str | None = None
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
    language: str | None = Field(
        default=None,
        description=(
            "Two-letter code, e.g. 'de' or 'en'. Currently always None: the column, "
            "this field and the reader all exist, but scraper/normalize.py::to_posting "
            "never sets it — see the Known gaps section of scraper/README.md."
        ),
    )
    url: str | None = Field(
        default=None,
        description="Source page the posting was scraped from, when the page gave one.",
    )
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
