"""Tests for turning source records into indexable documents."""

from __future__ import annotations

from thesis_matchmaker.contracts import ThesisPosting, ZoraRecord
from thesis_matchmaker.indexing.documents import posting_to_document, zora_to_document


def _zora(**overrides) -> ZoraRecord:
    base = dict(
        id="zora:1",
        title="Dense Retrieval for German Text",
        abstract="We study dense retrieval.",
        authors=["A. Müller"],
        year=2024,
        keywords=["retrieval", "german"],
        department="Department of Informatics",
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
    assert doc.metadata["main_author"] == "A. Müller"


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
