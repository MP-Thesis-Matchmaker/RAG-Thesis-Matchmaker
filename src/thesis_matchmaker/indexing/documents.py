"""Turn source records into the documents the index stores.

One record becomes one document: the text that gets embedded, the metadata
used for filtering at query time, and a content hash so unchanged records can
be skipped on re-index. Pure functions, no I/O.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field

from thesis_matchmaker.contracts import ThesisPosting, ZoraRecord

# The store keeps metadata in a jsonb column, so lists and maps are stored as
# themselves. Filters are still flat equality over scalars -- that is all
# retrieval asks for, and it maps onto one jsonb containment predicate.
MetadataScalar = str | int | float | bool
MetadataValue = MetadataScalar | list[str] | dict[str, str | None]


class Document(BaseModel):
    """What the vector store holds for one source record."""

    id: str = Field(description="Same id as the source record, so Evidence can point back.")
    text: str = Field(description="The string that gets embedded.")
    metadata: dict[str, MetadataValue] = Field(
        default_factory=dict, description="Filterable fields; missing values are omitted."
    )
    content_hash: str = Field(description="sha256 over text and metadata, for change detection.")


def _build(
    doc_id: str, parts: list[str | None], metadata: dict[str, MetadataValue | None]
) -> Document:
    text = "\n".join(p for p in parts if p)
    clean_meta = {k: v for k, v in metadata.items() if v is not None}
    payload = json.dumps({"text": text, "metadata": clean_meta}, sort_keys=True)
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return Document(id=doc_id, text=text, metadata=clean_meta, content_hash=content_hash)


def zora_to_document(record: ZoraRecord) -> Document:
    """Compose a publication into one embeddable document."""
    return _build(
        record.id,
        [record.title, record.abstract, ", ".join(record.keywords) or None],
        {
            "source_type": "publication",
            "department": record.department,
            "year": record.year,
            "language": record.language,
            "url": record.url,
            "authors": record.authors,
            "uzh_authors": record.uzh_authors,
            "author_authority_map": record.author_authority_map,
            "keywords": record.keywords,
            # Query-time eligibility filter: only publications with at least one
            # registered UZH researcher can lead to a supervisor match. Kept as
            # its own scalar even though jsonb could express the array test,
            # because the filter API is flat equality -- this is the only shape
            # the eligibility rule fits into.
            "has_uzh_author": bool(record.uzh_authors),
        },
    )


def posting_to_document(posting: ThesisPosting) -> Document:
    """Compose a thesis posting into one embeddable document."""
    return _build(
        posting.id,
        [posting.title, posting.description, ", ".join(posting.keywords) or None],
        {
            "source_type": "thesis_posting",
            "department": posting.department,
            "degree_level": posting.degree_level.value if posting.degree_level else None,
            "supervisor": posting.supervisor,
            "url": posting.url,
        },
    )
