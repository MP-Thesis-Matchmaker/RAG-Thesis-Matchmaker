"""Turn source records into the documents the index stores.

One record becomes one document: the text that gets embedded, the metadata
used for filtering at query time, and a content hash so unchanged records can
be skipped on re-index. Pure functions, no I/O.
"""

from __future__ import annotations

import hashlib
import html
import json
import re

from pydantic import BaseModel, Field

from thesis_matchmaker.contracts import DegreeLevel, ThesisPosting, ZoraRecord

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


# Markup, entities and runaway whitespace, and nothing else. Measured over the
# harvested corpus: 329 abstracts carry HTML tags, 579 carry entities, 2,300 carry
# runs of three or more whitespace characters.
#
# Deliberately NOT done here: stop-word removal and chunking. Stop-word removal is
# a sparse-retrieval idea (BM25, TF-IDF) where function words are pure noise; a
# transformer's self-attention uses them for syntax, negation and relation, so
# dropping "not" inverts a meaning rather than trimming filler. Chunking is the
# textbook alternative to truncation, but our retrieval unit is a person scored
# max(hit.score) over their publications, so splitting one long dissertation into
# sixty windows would hand that author sixty chances at the top spot. See
# indexing/README.md for the numbers behind both.
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def prepare_text(value: str) -> str:
    """Strip markup and collapse whitespace ahead of embedding.

    Tags go before entities are unescaped, so an escaped `&lt;p&gt;` -- which is
    text *about* a tag -- cannot be turned into a real tag by the unescape and then
    stripped as one.
    """
    return _WHITESPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()


def _build(
    doc_id: str, parts: list[str | None], metadata: dict[str, MetadataValue | None]
) -> Document:
    # Prepared before the emptiness filter, not after: a part that is nothing but
    # markup collapses to "" and has to drop out, rather than contributing a blank
    # line that the embedder would then have to see.
    prepared = (prepare_text(p) for p in parts if p)
    text = "\n".join(p for p in prepared if p)
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
    """Compose a thesis posting into one embeddable document.

    The part order is load-bearing and must not change: `retrieval` recovers a
    posting's displayed title as `text.splitlines()[0]` rather than from metadata, so
    moving `title` off the front silently retitles every posting in every result.

    Two metadata shapes here exist because the filter API is flat equality over
    scalars. `degree_levels` is stored as the honest list and is *not* filterable --
    Postgres jsonb containment does not match a scalar against a nested array, and
    `InMemoryVectorStore` compares with `==`, so both stores would miss it in the same
    way and the parametrised store contract could not tell. The three
    `degree_*` booleans are what a level query actually filters on, following the
    `has_uzh_author` precedent that `indexing/README.md` sets for exactly this case.
    `has_supervisor` is the same trick for a different question.
    """
    levels = {level.value for level in posting.degree_levels}
    return _build(
        posting.id,
        [posting.title, posting.description, ", ".join(posting.keywords) or None],
        {
            "source_type": "thesis_posting",
            "faculty": posting.faculty,
            "department": posting.department,
            # Stored for display and debugging; see the docstring on why the
            # booleans below are what gets filtered.
            "degree_levels": sorted(levels),
            "degree_bachelor": DegreeLevel.bachelor.value in levels,
            "degree_master": DegreeLevel.master.value in levels,
            "degree_phd": DegreeLevel.phd.value in levels,
            "status": posting.status.value if posting.status else None,
            "supervisors": [s.name for s in posting.supervisors],
            # A posting nobody is named on cannot become a supervisor
            # recommendation. 63 of 247 scraped topics are in that position, so this
            # is a routine case rather than an edge one.
            "has_supervisor": bool(posting.supervisors),
            "url": posting.url,
        },
    )
