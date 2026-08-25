"""Normalized harvester dicts → validated contract models.

`normalize.py` produces flat dicts in the harvester's own vocabulary; this module
maps them onto the shapes in `thesis_matchmaker.contracts` and validates them. It
is the last thing that runs before a row reaches `store.py`, so a malformed record
fails here rather than in Postgres.

What the mapping actually does is rename: `handle` → `id`, `type` →
`publication_type`, `uri` → `url`. Everything else passes through under its own
name, which is why there is one function per record kind and no adapter layer.

This file used to be called `output_schema.py` and defined its own models next to
these mappers -- a second `ZoraPublication`, a second `AuthorAuthority`, and the
only copies of the person and org-unit shapes. The duplicates drifted from
`contracts/` in three ways at once, so the models moved there and this module kept
the half that is genuinely harvester-specific.
"""

from __future__ import annotations

from thesis_matchmaker.contracts import ZoraOrgUnit, ZoraPerson, ZoraPublication


def to_publication(record: dict) -> dict:
    """Map one normalized publication record → a validated `publication` row.

    Edit this function when the row shape changes; the shape itself is
    `contracts.ZoraPublication`.
    """
    return ZoraPublication(
        id=record["handle"],
        title=record.get("title"),
        abstract=record.get("abstract"),
        authors=record.get("authors", []),
        uzh_authors=record.get("uzh_authors", []),
        author_authority_map=record.get("author_authority_map", {}),
        year=record.get("year"),
        publication_type=record.get("type"),
        department=record.get("department"),
        owning_collection_uuid=record.get("owning_collection_uuid"),
        language=record.get("language"),
        keywords=record.get("keywords", []),
        doi=record.get("doi"),
        url=record.get("uri"),
        accessioned=record.get("accessioned"),
    ).model_dump()


def to_person(record: dict) -> dict:
    """Map one normalized person record → a validated `person` row.

    No renames needed -- `normalize_person` already emits contract field names --
    so this exists for the validation and to keep all three kinds symmetric.
    """
    return ZoraPerson.model_validate(record).model_dump()


def to_org_unit(record: dict) -> dict:
    """Map one normalized org-unit record → a validated `org_unit` row."""
    return ZoraOrgUnit.model_validate(record).model_dump()
