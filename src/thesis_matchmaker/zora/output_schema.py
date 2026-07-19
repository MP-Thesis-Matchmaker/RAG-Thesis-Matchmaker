"""
The flat publication output contract — the primary deliverable of this
pipeline. Each line of data/publications.jsonl matches this schema.

This is the SINGLE FILE you edit when the output shape needs to change.
The Pydantic model defines what goes out; the to_output() function maps
the internal normalized dict to that shape.

Field names are aligned with the main repo's ZoraRecord contract
(thesis_matchmaker.contracts.sources) so the indexer can validate
our JSONL directly without an adapter layer.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from . import config


class ZoraPublication(BaseModel):
    """One ZORA publication. This is the atomic unit the RAG system works with."""

    # --- Identity (stable across harvests) ---
    id: str = Field(description="ZORA handle, stable across harvests")
    doi: str | None = None

    # --- Text content (what gets embedded for RAG) ---
    title: str | None = None
    abstract: str | None = None

    # --- Metadata (for filtering + ranking) ---
    authors: list[str] = Field(default_factory=list)
    uzh_authors: list[str] = Field(
        default_factory=list,
        description="Authors with a CRIS authority key (= registered UZH researchers)",
    )
    author_authority_map: dict[str, str | None] = Field(
        default_factory=dict,
        description="Mapping of author name → CRIS Person UUID (null for external co-authors)",
    )
    year: int | None = None
    publication_type: str | None = None
    department: str | None = Field(
        default=None,
        description="UZH department/center derived from the owning collection",
    )
    language: str | None = Field(
        default=None,
        description="Language code from dc.language.iso, e.g. 'eng', 'deu'",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Subject keywords from DDC and Scopus subject areas",
    )

    # --- Links (for display / citation) ---
    url: str | None = Field(default=None, description="Link to the ZORA landing page")


def to_output(record: dict) -> dict:
    """Map internal normalized record → output shape.

    Edit this function to change what fields appear in the output.
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
        language=record.get("language"),
        keywords=record.get("keywords", []),
        doi=record.get("doi"),
        url=record.get("uri"),
    ).model_dump()


def validate_publications_jsonl(path: str) -> tuple[int, list[str]]:
    """
    Validate every line of publications.jsonl against the schema.
    @return: (number of valid records, list of error strings for invalid ones)
    """
    import json

    valid_count = 0
    errors: list[str] = []

    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                ZoraPublication.model_validate(record)
                valid_count += 1
            except Exception as exc:  # noqa: BLE001 — collect every kind of failure
                errors.append(f"line {line_no}: {exc}")

    return valid_count, errors


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else config.PUBLICATIONS_PATH
    count, errs = validate_publications_jsonl(target)
    print(f"{count} valid records in {target}")
    if errs:
        print(f"{len(errs)} invalid records:")
        for e in errs[:20]:
            print(f"  {e}")
        sys.exit(1)
