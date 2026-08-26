"""Convert the scraper's nested dataset into the contracts the rest of the system speaks.

`dataset.py` builds the shape the scraping plan describes:

    faculties[code].units[unit_id].{people, process, concrete_topics, groups{...}}

Nothing downstream wants that nesting. `indexing/` wants flat records, and the faculty
and unit a record belongs to are carried by its *position* in the tree rather than by
any field on it -- so flattening is the whole job here, and it has to inject what the
position implied.

The three record kinds map onto three contracts: a concrete topic is a
`ThesisPosting`, a person is a `ResearcherProfile`, a process entry is an
`ApplicationProcess`.

Three normalisations are decisions rather than translations. Every count below is
measured over the 103 frozen page snapshots in `data/scraper/specs/`.

**Degree level is a list.** Pages write it as prose: `"Bachelor, Master"` (121 of 247
topics -- the plurality), `"Master"` (102), `"Master Thesis (30 ECTS)"` (3),
`"Bachelor Thesis (18 ECTS)"` (1), `"Bachelor"` (1), nothing at all (19). Half the
corpus offers one topic at two levels, which is why `ThesisPosting` carries
`degree_levels`: collapsing `"Bachelor, Master"` to a single enum value would hide
that topic from half the students it was written for.

**Status vocabulary is folded.** Sources say `open` (221), `taken` (12), `assigned`
(3), `private` (2), `pending` (1). `taken` and `assigned` are the same claim, so they
converge; `private` does not, because it means the topic exists but is not on offer.

**Supervisors come in two shapes.** 205 topics carry a `supervisors` list, 34 carry
flat `supervisor_name` / `supervisor_email` keys instead. They never both carry data --
measured, zero records -- so the second is a fallback, not a conflict to resolve.
Inside the list only `name` is dependable: of 264 entries, 96 are a bare name, 48 have
an email, and 120 have a profile link under one of three different keys.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any

from themis_shared.contracts import (
    ApplicationProcess,
    DegreeLevel,
    PostingStatus,
    ResearcherProfile,
    Supervisor,
    ThesisPosting,
)

# Matched against the lowered raw string, so "Master Thesis (30 ECTS)" and
# "Bachelor, Master" both resolve without an exhaustive value list. Every word is
# tested independently, which is what makes the multi-valued case fall out for free
# instead of needing a split on a separator the pages do not agree on.
_DEGREE_WORDS: dict[str, DegreeLevel] = {
    "bachelor": DegreeLevel.bachelor,
    "bsc": DegreeLevel.bachelor,
    "master": DegreeLevel.master,
    "msc": DegreeLevel.master,
    "phd": DegreeLevel.phd,
    "doctoral": DegreeLevel.phd,
    "doctorate": DegreeLevel.phd,
    "dissertation": DegreeLevel.phd,
}

_STATUS_WORDS: dict[str, PostingStatus] = {
    "open": PostingStatus.open,
    "available": PostingStatus.open,
    # Same claim as `assigned`; see the module docstring.
    "taken": PostingStatus.assigned,
    "assigned": PostingStatus.assigned,
    "pending": PostingStatus.pending,
    "private": PostingStatus.private,
}

# The three keys a supervisor's profile link hides behind, in preference order.
_PROFILE_KEYS = ("profile_url", "contact_url", "_url")

_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def degree_levels(raw: Any) -> list[DegreeLevel]:
    """Every level named in a free-text degree string, deduplicated, in enum order.

    Returns `[]` rather than guessing when the page said nothing. An empty list is
    honest, and it means a level-filtered query will not return the topic -- correct,
    because we do not know an unlabelled topic is open to a bachelor student.
    """
    if not raw:
        return []
    lowered = str(raw).lower()
    found = {level for word, level in _DEGREE_WORDS.items() if word in lowered}
    return [level for level in DegreeLevel if level in found]


def posting_status(raw: Any) -> PostingStatus | None:
    """Fold the source status vocabulary onto the enum, or None when unrecognised.

    Unrecognised means None rather than a guess: this field decides whether a topic is
    presented as available at all, and inventing `open` for a word we do not
    understand is exactly the failure worth avoiding.
    """
    if not raw:
        return None
    return _STATUS_WORDS.get(str(raw).strip().lower())


def listed_on(raw: Any) -> date | None:
    """Parse a listing date only when it is unambiguous.

    Pages write dates every way a human might, and `title_check.py` exists partly
    because one page's "November 3, 2021" was being read as a topic title. Guessing a
    format per locale would put wrong dates in the record, so this takes ISO and
    otherwise declines.
    """
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    match = _ISO_DATE_RE.search(str(raw))
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def supervisors(record: dict[str, Any]) -> list[Supervisor]:
    """The people named on a topic, from whichever of the two shapes the page used."""
    out: list[Supervisor] = []
    for entry in record.get("supervisors") or []:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        out.append(
            Supervisor(
                name=name,
                email=entry.get("email"),
                profile_url=next((entry[k] for k in _PROFILE_KEYS if entry.get(k)), None),
                chair=entry.get("chair"),
            )
        )
    if out:
        return out
    flat = (record.get("supervisor_name") or "").strip()
    if flat:
        out.append(
            Supervisor(
                name=flat,
                email=record.get("supervisor_email"),
                profile_url=record.get("_supervisor_url"),
            )
        )
    return out


def _digest(*parts: Any) -> str:
    """Stable id for record kinds that carry none of their own."""
    joined = " ".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def _profile_url(record: dict[str, Any]) -> str | None:
    """Live records use `_profile_url`; records reloaded from the cleaned JSON use
    `profile_url`. Same thing under two names."""
    return record.get("_profile_url") or record.get("profile_url")


def to_posting(
    record: dict[str, Any], *, faculty: str | None = None, department: str | None = None
) -> ThesisPosting:
    """One concrete topic as a `ThesisPosting`."""
    area = (record.get("research_area") or "").strip()
    return ThesisPosting(
        id=record.get("topic_id") or _digest(record.get("source_link"), record.get("title")),
        title=(record.get("title") or "").strip(),
        description=record.get("topic_description"),
        supervisors=supervisors(record),
        faculty=faculty,
        department=department,
        degree_levels=degree_levels(record.get("degree_level")),
        status=posting_status(record.get("status")),
        # research_area is the only topical label these pages carry.
        keywords=[area] if area else [],
        url=record.get("source_link") or "",
        listed_on=listed_on(record.get("date_of_listing")),
        source_id=record.get("source_id"),
        scraped_at=record.get("scraped_at"),
    )


def to_profile(
    record: dict[str, Any], *, faculty: str | None = None, department: str | None = None
) -> ResearcherProfile:
    """One person as a `ResearcherProfile`.

    `office` and `phone`, which 19 scraped profiles carry, are dropped on purpose --
    see the note on the contract.
    """
    url = _profile_url(record)
    return ResearcherProfile(
        id=_digest(record.get("source_id"), record.get("name"), url),
        name=(record.get("name") or "").strip(),
        email=record.get("email"),
        role=record.get("role"),
        research_interest=record.get("research_interest"),
        research_field=record.get("research_field"),
        research_group=record.get("research_group"),
        bio=record.get("bio"),
        personal_website=record.get("personal_website"),
        profile_url=url,
        faculty=faculty,
        department=department,
        source_id=record.get("source_id"),
        scraped_at=record.get("scraped_at"),
    )


def _split_source_ids(raw: Any) -> list[str]:
    """`dataset.consolidate_process` joins contributing ids with commas when merging."""
    if not raw:
        return []
    return [part for part in (chunk.strip() for chunk in str(raw).split(",")) if part]


def to_process(
    record: dict[str, Any], *, faculty: str | None = None, department: str | None = None
) -> ApplicationProcess:
    """One application procedure as an `ApplicationProcess`.

    `dataset.consolidate_process` has already collapsed several source pages into one
    entry per degree level, which is why the id keys on the unit and the level rather
    than on a source, and why `source_ids` is a list.
    """
    levels = degree_levels(record.get("degree_level"))
    return ApplicationProcess(
        id=_digest(faculty, department, record.get("degree_level")),
        # One level per entry by construction, so the first is the only one. An
        # unrecognised label ("Unspecified") leaves this None rather than inventing one.
        degree_level=levels[0] if levels else None,
        description=record.get("process_description"),
        relevant_links=[
            {"url": str(link.get("url") or ""), "description": str(link.get("description") or "")}
            for link in record.get("relevant_links") or []
            if link.get("url")
        ],
        url=record.get("source_url"),
        faculty=faculty,
        department=department,
        source_ids=record.get("source_ids") or _split_source_ids(record.get("source_id")),
        scraped_at=record.get("scraped_at"),
    )


def _unit_pools(unit: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """A unit's own records plus every one of its chair groups'.

    Groups are a second level of nesting under a unit, and downstream a group's
    records are the unit's -- the same pooling `dataset`'s SQLite mirror did before it
    was removed.
    """
    pools = {kind: list(unit.get(kind) or []) for kind in ("people", "process", "concrete_topics")}
    for group in (unit.get("groups") or {}).values():
        for kind in pools:
            pools[kind] += group.get(kind) or []
    return pools


def iter_records(
    data: dict[str, Any],
) -> tuple[list[ThesisPosting], list[ResearcherProfile], list[ApplicationProcess]]:
    """Flatten the whole dataset into the three contract lists.

    Faculty-scope process entries -- a shared "how to apply" page covering a whole
    faculty rather than one institute -- come out with `department` unset, which is
    what the removed SQLite mirror expressed as a NULL `unit_id`.
    """
    postings: list[ThesisPosting] = []
    profiles: list[ResearcherProfile] = []
    processes: list[ApplicationProcess] = []

    for fac in (data.get("faculties") or {}).values():
        faculty = fac.get("faculty")
        for record in fac.get("process") or []:
            processes.append(to_process(record, faculty=faculty, department=None))

        for unit in (fac.get("units") or {}).values():
            department = unit.get("unit")
            pools = _unit_pools(unit)
            for record in pools["concrete_topics"]:
                postings.append(to_posting(record, faculty=faculty, department=department))
            for record in pools["people"]:
                profiles.append(to_profile(record, faculty=faculty, department=department))
            for record in pools["process"]:
                processes.append(to_process(record, faculty=faculty, department=department))

    return postings, profiles, processes
