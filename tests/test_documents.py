"""Tests for turning source records into indexable documents."""

from __future__ import annotations

from thesis_matchmaker.contracts import ThesisPosting, ZoraRecord
from thesis_matchmaker.indexing.documents import (
    posting_to_document,
    prepare_text,
    zora_to_document,
)


def _zora(**overrides) -> ZoraRecord:
    base = dict(
        id="zora:1",
        title="Dense Retrieval for German Text",
        abstract="We study dense retrieval.",
        authors=["A. Müller", "X. External"],
        uzh_authors=["A. Müller"],
        author_authority_map={"A. Müller": "uuid-1", "X. External": None},
        year=2024,
        keywords=["retrieval", "german"],
        department="Department of Informatics",
        language="eng",
        url="https://www.zora.uzh.ch/id/eprint/1",
    )
    base.update(overrides)
    return ZoraRecord(**base)


def test_zora_document_text_contains_title_abstract_keywords() -> None:
    doc = zora_to_document(_zora())
    assert "Dense Retrieval for German Text" in doc.text
    assert "We study dense retrieval." in doc.text
    assert "retrieval" in doc.text


def test_zora_document_carries_id_and_metadata() -> None:
    doc = zora_to_document(_zora())
    assert doc.id == "zora:1"
    assert doc.metadata["source_type"] == "publication"
    assert doc.metadata["department"] == "Department of Informatics"
    assert doc.metadata["year"] == 2024
    assert doc.metadata["language"] == "eng"


def test_zora_document_keeps_author_fields_as_native_collections() -> None:
    """The store column is jsonb, so lists and maps are stored as themselves."""
    doc = zora_to_document(_zora())
    assert doc.metadata["authors"] == ["A. Müller", "X. External"]
    assert doc.metadata["uzh_authors"] == ["A. Müller"]
    assert doc.metadata["author_authority_map"] == {
        "A. Müller": "uuid-1",
        "X. External": None,
    }
    assert doc.metadata["keywords"] == ["retrieval", "german"]
    assert doc.metadata["has_uzh_author"] is True


def test_zora_document_without_uzh_authors_is_flagged_ineligible() -> None:
    doc = zora_to_document(_zora(uzh_authors=[], author_authority_map={"A. Müller": None}))
    assert doc.metadata["has_uzh_author"] is False
    assert doc.metadata["uzh_authors"] == []


def test_zora_document_handles_missing_optionals() -> None:
    doc = zora_to_document(_zora(abstract=None, department=None, year=None, keywords=[]))
    assert doc.text.startswith("Dense Retrieval for German Text")
    assert "department" not in doc.metadata
    assert "year" not in doc.metadata


def test_posting_document_metadata() -> None:
    posting = ThesisPosting(
        id="posting:7",
        title="MSc thesis on RAG",
        description="Ground LLM answers with retrieval.",
        supervisor="Prof. A. Müller",
        degree_level="master",
        url="https://www.cl.uzh.ch/theses/rag",
    )
    doc = posting_to_document(posting)
    assert doc.id == "posting:7"
    assert doc.metadata["source_type"] == "thesis_posting"
    assert doc.metadata["degree_level"] == "master"
    assert doc.metadata["supervisor"] == "Prof. A. Müller"
    assert "Ground LLM answers" in doc.text


def test_content_hash_stable_and_sensitive() -> None:
    a = zora_to_document(_zora())
    b = zora_to_document(_zora())
    changed = zora_to_document(_zora(abstract="Different abstract."))
    assert a.content_hash == b.content_hash
    assert a.content_hash != changed.content_hash


def test_prepare_text_strips_tags_and_collapses_whitespace() -> None:
    assert prepare_text("<p>Dense   retrieval</p>\n\n<br/>for German") == (
        "Dense retrieval for German"
    )


def test_prepare_text_unescapes_entities() -> None:
    assert prepare_text("Fish &amp; Chips &lt;3") == "Fish & Chips <3"


def test_prepare_text_does_not_strip_tags_it_created_by_unescaping() -> None:
    """`&lt;p&gt;` is text *about* a tag; unescaping must not turn it into one."""
    assert prepare_text("the &lt;p&gt; element") == "the <p> element"


def test_prepare_text_of_markup_only_is_empty() -> None:
    assert prepare_text("<div>\n  <br/>\t</div>") == ""


def test_markup_only_part_does_not_leave_a_blank_line() -> None:
    """Preparation happens before the emptiness filter, so the part drops out."""
    doc = zora_to_document(_zora(abstract="<br/>", keywords=[]))
    assert doc.text == "Dense Retrieval for German Text"


def test_prepared_text_is_what_gets_hashed() -> None:
    """Two records differing only in markup are the same document to the index."""
    plain = zora_to_document(_zora(abstract="We study dense retrieval."))
    marked = zora_to_document(_zora(abstract="<p>We  study   dense retrieval.</p>"))
    assert plain.text == marked.text
    assert plain.content_hash == marked.content_hash
