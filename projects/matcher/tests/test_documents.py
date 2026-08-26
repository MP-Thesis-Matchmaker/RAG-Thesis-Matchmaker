"""Tests for turning source records into indexable documents."""

from __future__ import annotations

from themis_matcher.indexing.documents import (
    posting_to_document,
    prepare_text,
    zora_to_document,
)
from themis_shared.contracts import PostingStatus, ThesisPosting, ZoraPublication


def _zora(**overrides) -> ZoraPublication:
    base = dict(
        id="zora:1",
        title="Dense Retrieval for German Text",
        abstract="We study dense retrieval.",
        authors=["A. Müller", "X. External"],
        uzh_authors=["A. Müller"],
        author_authority_map={
            "A. Müller": {"type": "cris", "id": "uuid-1"},
            "X. External": None,
        },
        year=2024,
        keywords=["retrieval", "german"],
        department="Department of Informatics",
        language="eng",
        url="https://www.zora.uzh.ch/id/eprint/1",
    )
    base.update(overrides)
    return ZoraPublication(**base)


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
        "A. Müller": {"type": "cris", "id": "uuid-1"},
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
        supervisors=[{"name": "Prof. A. Müller"}],
        degree_levels=["master"],
        status="open",
        url="https://www.cl.uzh.ch/theses/rag",
    )
    doc = posting_to_document(posting)
    assert doc.id == "posting:7"
    assert doc.metadata["source_type"] == "thesis_posting"
    assert doc.metadata["degree_levels"] == ["master"]
    assert doc.metadata["supervisors"] == ["Prof. A. Müller"]
    assert doc.metadata["status"] == "open"
    assert "Ground LLM answers" in doc.text


def test_posting_document_emits_one_boolean_per_degree_level() -> None:
    """The filterable companions, and the whole reason they exist.

    Neither store can filter a list-valued metadata field, so a posting open to two
    levels has to be findable through scalars or it is findable by nobody.
    """
    posting = ThesisPosting(
        id="posting:8",
        title="Either level",
        url="https://x",
        degree_levels=["bachelor", "master"],
    )
    doc = posting_to_document(posting)
    assert doc.metadata["degree_levels"] == ["bachelor", "master"]
    assert doc.metadata["degree_bachelor"] is True
    assert doc.metadata["degree_master"] is True
    assert doc.metadata["degree_phd"] is False


def test_posting_without_a_supervisor_is_flagged_as_such() -> None:
    """63 of 247 scraped topics name nobody; retrieval has to be able to see that."""
    doc = posting_to_document(ThesisPosting(id="posting:9", title="Anon", url="https://x"))
    assert doc.metadata["has_supervisor"] is False
    assert doc.metadata["supervisors"] == []


def test_unavailable_postings_are_flagged_but_still_become_documents() -> None:
    """Assigned and private topics are embedded; the boolean is what excludes them.

    Indexing used to drop these rows outright, which made
    `retrieval_require_available_posting` impossible to turn off without a re-index.
    """
    for status in (PostingStatus.assigned, PostingStatus.private):
        doc = posting_to_document(
            ThesisPosting(
                id=f"posting:{status.value}", title="Taken", url="https://x", status=status
            )
        )
        assert doc.metadata["is_available"] is False
        assert doc.metadata["status"] == status.value


def test_open_pending_and_silent_postings_all_count_as_available() -> None:
    """`pending` and a missing status are not the same claim as "taken"."""
    for status in (PostingStatus.open, PostingStatus.pending, None):
        doc = posting_to_document(
            ThesisPosting(id="posting:11", title="Free", url="https://x", status=status)
        )
        assert doc.metadata["is_available"] is True
    # A status-less posting carries no `status` key at all -- which is why the filter
    # is a boolean rather than an equality test on `status`.
    silent = posting_to_document(ThesisPosting(id="posting:12", title="Free", url="https://x"))
    assert "status" not in silent.metadata


def test_posting_title_stays_the_first_line_of_the_embedded_text() -> None:
    """retrieval recovers Evidence.title as text.splitlines()[0], not from metadata."""
    posting = ThesisPosting(
        id="posting:10",
        title="The title",
        description="The description.",
        keywords=["kw"],
        url="https://x",
    )
    assert posting_to_document(posting).text.splitlines()[0] == "The title"


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
