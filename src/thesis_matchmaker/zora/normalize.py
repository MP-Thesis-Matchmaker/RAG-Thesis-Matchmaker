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
from typing import Any

from . import config

logger = logging.getLogger(__name__)


def _values(dso: Any, field: str) -> list[str]:
    """Extract plain string values for a metadata field, dropping empties."""
    raw = dso.get_metadata_values(field)
    return [entry["value"] for entry in raw if entry.get("value")]


def _first_orcid(dso: Any) -> str | None:
    """Try each candidate ORCID field in order, return the first hit.

    UZH stores ORCIDs as full URLs (https://orcid.org/0000-...); we strip
    the prefix to store a bare ID.
    """
    for field in config.FIELD_ORCID_CANDIDATES:
        values = _values(dso, field)
        if values:
            raw = values[0]
            # Strip URL prefix if present
            if raw.startswith("https://orcid.org/"):
                return raw[len("https://orcid.org/"):]
            if raw.startswith("http://orcid.org/"):
                return raw[len("http://orcid.org/"):]
            return raw
    return None


def _get_department(dso: Any) -> str | None:
    """Resolve the department name from the item's embedded owningCollection or mappedCollections.

    Parses the collection name, stripping the "Publications of " prefix if present.
    Falls back to the first mapped collection if the owning collection has no name.
    """
    embedded = getattr(dso, "embedded", None) or {}

    # 1. Parse the owningCollection name
    owning_collection = embedded.get("owningCollection")
    if owning_collection:
        name = owning_collection.get("name", "")
        if name.startswith("Publications of "):
            return name[len("Publications of "):]
        return name if name else None

    # 2. Fall back to the first mapped collection name
    mapped_collections_data = embedded.get("mappedCollections")
    if isinstance(mapped_collections_data, dict):
        colls = mapped_collections_data.get("_embedded", {}).get("mappedCollections", [])
        if colls:
            name = colls[0].get("name", "")
            if name.startswith("Publications of "):
                return name[len("Publications of "):]
            return name if name else None

    return None


def _get_uzh_authors(dso: Any) -> list[str]:
    """Return only those authors who have a CRIS authority key (= UZH researchers).

    External co-authors have authority=None in the metadata entry and are excluded.
    """
    raw = dso.get_metadata_values(config.FIELD_AUTHOR)
    return [
        entry["value"]
        for entry in raw
        if entry.get("value") and entry.get("authority")
    ]


def _clean_authority(authority: str | None) -> str | None:
    """Strip DSpace placeholder prefix 'will be referenced::ORCID::' if present."""
    if not authority:
        return None
    prefix = "will be referenced::ORCID::"
    if authority.startswith(prefix):
        return authority[len(prefix):]
    return authority


def _get_author_authority_map(dso: Any) -> dict[str, str | None]:
    """Build a dict mapping each author name → their CRIS Person UUID or bare ORCID (or None).

    This provides full provenance: UZH-affiliated authors have a UUID or ORCID,
    external co-authors have None.
    """
    raw = dso.get_metadata_values(config.FIELD_AUTHOR)
    return {
        entry["value"]: _clean_authority(entry.get("authority"))
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
    titles = _values(dso, config.FIELD_TITLE)
    years = _values(dso, config.FIELD_DATE_ISSUED)

    return {
        "handle": dso.handle,
        "uuid": dso.uuid,
        "title": titles[0] if titles else None,
        "authors": _values(dso, config.FIELD_AUTHOR),
        "uzh_authors": _get_uzh_authors(dso),
        "author_authority_map": _get_author_authority_map(dso),
        "author_orcid": _first_orcid(dso),
        "abstract": next(iter(_values(dso, config.FIELD_ABSTRACT)), None),
        "year": _extract_year(years[0]) if years else None,
        "type": next(iter(_values(dso, config.FIELD_TYPE)), None),
        "department": _get_department(dso),
        "language": next(iter(_values(dso, config.FIELD_LANGUAGE)), None),
        "doi": next(iter(_values(dso, config.FIELD_DOI)), None),
        "uri": next(iter(_values(dso, config.FIELD_URI)), None),
        "keywords": _collect_keywords(dso),
        "accessioned": next(
            iter(_values(dso, config.FIELD_DATE_ACCESSIONED)), None
        ),
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
    for field in (config.FIELD_SUBJECT_DDC, config.FIELD_SCOPUS_SUBJECTS, config.FIELD_SUBJECT):
        for val in _values(dso, field):
            if val not in seen:
                seen.add(val)
                result.append(val)
    return result
