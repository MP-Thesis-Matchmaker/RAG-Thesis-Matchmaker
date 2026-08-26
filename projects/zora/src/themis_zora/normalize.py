"""
Convert a raw SimpleDSpaceObject (as returned by search_objects_iter) into a
clean, flat publication dict.

Important: DSpaceObject.get_metadata_values(field) returns the RAW metadata
list straight from the DSpace JSON response — a list of dicts shaped like
{"value": "...", "language": ..., "authority": ..., "confidence": ...,
"place": ...} — despite what the library's own docstring claims ("simple
list of strings"). Every extraction below unwraps ["value"] explicitly.
Trusting the docstring here silently stores dicts where strings are
expected — this was caught by reading models.py directly, not assumed.
"""

import logging
import re
from typing import Any

from . import config, fields

logger = logging.getLogger(__name__)


def _values(dso: Any, field: str) -> list[str]:
    """Extract plain string values for a metadata field, dropping empties."""
    raw = dso.get_metadata_values(field)
    return [entry["value"] for entry in raw if entry.get("value")]


_ORCID_URL_PREFIXES = ("https://orcid.org/", "http://orcid.org/")


def _is_orcid_url(raw: str) -> bool:
    """Whether a value declares itself an ORCID by being an orcid.org URL.

    Not the same as inferring from an id's shape, which `_typed_authority`
    refuses to do: a CRIS Person UUID can never take this form, so there is
    nothing to infer. Whether the payload after the prefix is well-formed is
    `_normalize_orcid`'s problem, not this function's.
    """
    return raw.startswith(_ORCID_URL_PREFIXES)


def _strip_orcid_url(raw: str) -> str:
    """UZH stores ORCIDs as full URLs (https://orcid.org/0000-...); strip to a bare ID."""
    for prefix in _ORCID_URL_PREFIXES:
        if raw.startswith(prefix):
            return raw[len(prefix) :]
    return raw


# The leading ORCID-shaped run of a string: three digit groups, then 3 digits and
# an optional 16th character. Anchored at the start and deliberately not at the
# end, so trailing junk (a stray full stop) falls off instead of failing the match.
_ORCID_RE = re.compile(r"^(\d{4}-\d{4}-\d{4}-\d{3})([\dX]?)")

# The same shape, but whole and with the check digit required. `_ORCID_RE` is
# deliberately loose -- start-anchored, check digit optional -- because it parses a
# value already known to be an ORCID. Deciding whether an *unmarked* value is one
# needs the strict form: it is the only test standing between a bare ORCID and
# being filed as a CRIS Person id.
_ORCID_CANONICAL_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")


def _orcid_checksum(fifteen: str) -> str:
    """The 16th ORCID character, derived from the first 15 digits (ISO 7064 MOD 11-2).

    An ORCID's last character is a check digit over the other fifteen, so it is
    computable rather than merely conventional. That is what lets
    `_normalize_orcid` repair a truncated id without guessing -- and what lets it
    decline to, when the arithmetic does not support the repair.
    """
    total = 0
    for char in fifteen:
        total = (total + int(char)) * 2
    remainder = (12 - total % 11) % 11
    return "X" if remainder == 10 else str(remainder)


def _normalize_orcid(raw: str) -> str:
    """Reduce an upstream ORCID to its canonical 0000-0000-0000-000X form.

    ZORA emits four corruptions of this field, all rare and all seen in the
    2026-08-25 corpus (20 entries of 157,800):

      https://orcid.org/0009-0005-4380-7204   full URL
      0000-0001-5644-045x                     lowercase check digit
      0000-0002-3148-0954.                    trailing punctuation
      0000-0002-8070-773                      check digit missing entirely

    The first three are unambiguous cleanups. The fourth is a repair, and it is
    guarded, because a 3-character final group has two possible causes: a stripped
    `X` (systems coercing the field to numeric drop the one non-numeric character)
    or a dropped leading zero. Both readings of `0000-0002-8070-773` --
    `...-773X` and `...-0773` -- satisfy the checksum, so the arithmetic alone
    cannot separate them.

    So the missing character is appended ONLY when the computed checksum is `X`,
    which is the sole case where the stripped-X explanation actually holds. When it
    computes to a digit, the corruption is something else and the value is left
    exactly as it came in: an id that stays visibly broken is recoverable, whereas
    a fabricated one that passes validation is a wrong ORCID silently attributed to
    a named researcher. (Verified against the three real cases -- 773, 166, 993 --
    which all compute to X and match the values a manual ORCID lookup returns.)

    Anything that is not ORCID-shaped at all comes back untouched, for the same
    reason: bad data should stay visible rather than be blanked.
    """
    candidate = _strip_orcid_url(raw.strip()).upper()
    match = _ORCID_RE.match(candidate)
    if not match:
        return raw
    body, check = match.groups()
    if check:
        return f"{body}{check}"
    computed = _orcid_checksum(body.replace("-", ""))
    return f"{body}X" if computed == "X" else raw


def _first_orcid(dso: Any) -> str | None:
    """Try each candidate ORCID field in order, return the first hit."""
    for field in fields.FIELD_ORCID_CANDIDATES:
        values = _values(dso, field)
        if values:
            return _normalize_orcid(values[0])
    return None


def _department_name(name: str) -> str | None:
    """A collection's display name minus the "Publications of " prefix."""
    if name.startswith(config.ZoraSettings.ZORA_PUBLICATIONS_COLLECTION_PREFIX):
        return name[len(config.ZoraSettings.ZORA_PUBLICATIONS_COLLECTION_PREFIX) :]
    return name if name else None


def _get_owning_collection(dso: Any) -> tuple[str | None, str | None]:
    """Resolve (department name, collection uuid) from the item's embedded collections.

    The owningCollection wins; the first mappedCollections entry is the
    fallback. Name and uuid always come from the *same* collection, so the
    parsed department string and the persisted uuid can never describe two
    different org units.
    """
    embedded = getattr(dso, "embedded", None) or {}

    owning_collection = embedded.get("owningCollection")
    if owning_collection:
        return (
            _department_name(owning_collection.get("name", "")),
            owning_collection.get("uuid"),
        )

    mapped_collections_data = embedded.get("mappedCollections")
    if isinstance(mapped_collections_data, dict):
        colls = mapped_collections_data.get("_embedded", {}).get("mappedCollections", [])
        if colls:
            return _department_name(colls[0].get("name", "")), colls[0].get("uuid")

    return None, None


def _get_uzh_authors(dso: Any) -> list[str]:
    """Return only those authors who have a CRIS authority key (= UZH researchers).

    An ORCID-typed authority does NOT qualify. The marker says this *item* is not
    linked to a local Person, so the author's affiliation is unknown rather than
    confirmed -- and an ORCID is a global identifier that every researcher on
    earth can hold. Accepting any authority made 58,218 names supervisor-eligible
    against 2,943 CRIS-backed ones (measured over the whole corpus 2026-08-25),
    and put Oxford and Belfast co-authors of single UZH papers in front of
    students as candidate supervisors.

    Excluded is not dropped: those authors stay in `author_authority_map`, their
    publications stay indexed, and `retrieval` still credits them through its
    `authors` fallback. What changes is rank, not reachability -- `has_uzh_author`
    becomes truthful, so `uzh_first` sorts them below actual UZH researchers.

    Classification goes through `_typed_authority` rather than re-testing the
    marker here, so the two can never disagree about what an authority means.
    """
    raw = dso.get_metadata_values(fields.FIELD_AUTHOR)
    return [
        entry["value"]
        for entry in raw
        if entry.get("value")
        and (_typed_authority(entry.get("authority")) or {}).get("type") == "cris"
    ]


def _typed_authority(authority: str | None) -> dict | None:
    """Classify a raw authority value into {"type": "cris"|"orcid", "id": ...}.

    DSpace-CRIS stores two different things in `authority`:
      - a bare value is a CRIS Person item UUID — an actual researcher record
        that resolves in the `person` table;
      - 'will be referenced::ORCID::<orcid>' means DSpace did not link THIS
        item to a local Person: the ORCID is known but the affiliation is not.
        Record-level, not person-level -- a Person entity carrying the same
        ORCID can still exist, so this must not be read as "no CRIS record
        exists for this human".

    **The marker wins wherever it exists; shape decides only where there is none.**
    A marked value stays `orcid` however broken its payload: upstream ORCIDs are
    frequently malformed, and letting a bad payload demote one to `cris` would file
    it as a researcher who resolves to nobody.

    Where no marker exists, `cris` used to be the unconditional default, and that
    was the same bug pointing the other way. One real record
    (`20.500.14742/59205`) carries a bare `0000-0002-7695-501X` with the marker
    omitted upstream, so a plain ORCID was filed as a Person id — a supervisor
    candidate that joins to nothing in `person` and still counted toward
    eligibility. An explicit orcid.org URL was already trusted for exactly this
    reason; a canonical ORCID declares itself just as plainly.

    Deciding that by shape is safe in one direction only, which is why the test is
    written the way it is. `_normalize_orcid` uppercases, while a CRIS id is a
    lowercase-hex UUID that `person.uuid` is joined on exactly — so its output is
    used as a *test* and then discarded, never stored unless the value really is an
    ORCID. A UUID cannot pass that test in any case: `_ORCID_RE` needs a hyphen at
    index 4 and a UUID's first group is eight hex characters, so the two shapes are
    disjoint rather than merely unlikely to collide.
    """
    if not authority:
        return None
    prefix = "will be referenced::ORCID::"
    if authority.startswith(prefix):
        return {"type": "orcid", "id": _normalize_orcid(authority[len(prefix) :])}
    # Ahead of the shape test: a URL carrying a malformed payload normalizes to
    # itself and would fail it. The URL is a declaration, not an inference.
    if _is_orcid_url(authority):
        return {"type": "orcid", "id": _normalize_orcid(authority)}
    canonical = _normalize_orcid(authority)
    if _ORCID_CANONICAL_RE.fullmatch(canonical):
        return {"type": "orcid", "id": canonical}
    # `authority`, not `canonical` -- this is the branch where the throwaway is
    # thrown away, and storing the uppercased form would break the person.uuid join.
    return {"type": "cris", "id": authority}


def _get_author_authority_map(dso: Any) -> dict[str, dict | None]:
    """Build a dict mapping each author name → typed authority (or None).

    Full provenance: a cris-typed entry is a registered UZH researcher, an
    orcid-typed entry is an author of unknown affiliation, None is an author
    with no identifier at all.
    """
    raw = dso.get_metadata_values(fields.FIELD_AUTHOR)
    return {
        entry["value"]: _typed_authority(entry.get("authority"))
        for entry in raw
        if entry.get("value")
    }


def normalize_item(dso: Any) -> dict:
    """
    Turn one raw DSpace item into a flat publication record.

    Authors are kept as a list here — aggregation (grouping into researcher
    profiles) happens as a separate step, since one publication has many
    authors and one author has many publications.
    """
    titles = _values(dso, fields.FIELD_TITLE)
    years = _values(dso, fields.FIELD_DATE_ISSUED)
    department, owning_collection_uuid = _get_owning_collection(dso)

    return {
        "handle": dso.handle,
        "uuid": dso.uuid,
        "title": titles[0] if titles else None,
        "authors": _values(dso, fields.FIELD_AUTHOR),
        "uzh_authors": _get_uzh_authors(dso),
        "author_authority_map": _get_author_authority_map(dso),
        "author_orcid": _first_orcid(dso),
        "abstract": next(iter(_values(dso, fields.FIELD_ABSTRACT)), None),
        "year": _extract_year(years[0]) if years else None,
        "type": next(iter(_values(dso, fields.FIELD_TYPE)), None),
        "department": department,
        "owning_collection_uuid": owning_collection_uuid,
        "language": next(iter(_values(dso, fields.FIELD_LANGUAGE)), None),
        "doi": next(iter(_values(dso, fields.FIELD_DOI)), None),
        "uri": next(iter(_values(dso, fields.FIELD_URI)), None),
        "keywords": _collect_keywords(dso),
        "accessioned": next(iter(_values(dso, fields.FIELD_DATE_ACCESSIONED)), None),
    }


def _extract_year(date_str: str) -> int | None:
    """dc.date.issued can be a full date or just a year — pull the year out."""
    if not date_str:
        return None
    digits = date_str[:4]
    return int(digits) if digits.isdigit() else None


def _collect_keywords(dso: Any) -> list[str]:
    """Merge subject/keyword values from all available fields.

    UZH doesn't populate dc.subject (free-text keywords) on most items.
    Instead it has:
      - dc.subject.ddc: Dewey Decimal e.g. "330 Economics"
      - uzh.scopus.subjects: Scopus areas e.g. "Economics and Econometrics"
    We merge all three (deduped, order preserved) so the output has whatever
    subject metadata is available.
    """
    seen: set[str] = set()
    result: list[str] = []
    for field in (fields.FIELD_SUBJECT_DDC, fields.FIELD_SCOPUS_SUBJECTS, fields.FIELD_SUBJECT):
        for val in _values(dso, field):
            if val not in seen:
                seen.add(val)
                result.append(val)
    return result


# ---------------------------------------------------------------------------
# Entity mirrors: Person items and the community (org unit) tree
# ---------------------------------------------------------------------------


def normalize_person(dso: Any) -> dict:
    """Turn one DSpace-CRIS Person item into a flat person record.

    Everything a Person item carries is here — upstream has no affiliation,
    department, or email on these items (probed 2026-08-24), so person-to-org
    attribution has to come from publications, not from this record.
    """
    titles = _values(dso, fields.FIELD_TITLE)
    orcids = _values(dso, fields.FIELD_PERSON_ORCID)

    return {
        "uuid": dso.uuid,
        "display_name": titles[0] if titles else None,
        "family_name": next(iter(_values(dso, fields.FIELD_PERSON_FAMILY)), None),
        "given_name": next(iter(_values(dso, fields.FIELD_PERSON_GIVEN)), None),
        "orcid": _normalize_orcid(orcids[0]) if orcids else None,
        "handle": dso.handle,
        "url": next(iter(_values(dso, fields.FIELD_URI)), None),
        "accessioned": next(iter(_values(dso, fields.FIELD_DATE_ACCESSIONED)), None),
    }


def _community_metadata_value(community: dict, field: str) -> str | None:
    """First non-empty metadata value of a raw community JSON object."""
    entries = (community.get("metadata") or {}).get(field) or []
    for entry in entries:
        if entry.get("value"):
            return entry["value"]
    return None


def normalize_org_unit(
    community: dict,
    parent_uuid: str | None,
    depth: int,
    faculty_uuid: str | None,
    collections: list[dict],
) -> dict:
    """Turn one community (plus its collections) into a flat org_unit record.

    The publications collection is the one whose name carries the
    "Publications of " prefix. More than one would mean the one-collection-
    per-unit assumption broke upstream: warn and take the first rather than
    guessing, so the run still commits and the anomaly is visible in the log.
    """
    publication_collections = [
        c
        for c in collections
        if (c.get("name") or "").startswith(config.ZoraSettings.ZORA_PUBLICATIONS_COLLECTION_PREFIX)
    ]
    if len(publication_collections) > 1:
        logger.warning(
            "Community %s (%s) has %d 'Publications of' collections; keeping the first (%s)",
            community.get("uuid"),
            community.get("name"),
            len(publication_collections),
            publication_collections[0].get("uuid"),
        )
    collection = publication_collections[0] if publication_collections else None

    return {
        "uuid": community["uuid"],
        "name": community.get("name") or "",
        "parent_uuid": parent_uuid,
        "faculty_uuid": faculty_uuid,
        "depth": depth,
        "handle": community.get("handle"),
        "subject_id": _community_metadata_value(community, fields.FIELD_ORG_SUBJECT_ID),
        "collection_uuid": collection.get("uuid") if collection else None,
        "collection_name": collection.get("name") if collection else None,
    }
