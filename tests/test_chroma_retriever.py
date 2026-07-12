"""Tests for the real retriever over an indexed temp store."""

from __future__ import annotations

from pathlib import Path

import pytest

from thesis_matchmaker.contracts import ParsedQuery, ThesisPosting, ZoraRecord
from thesis_matchmaker.indexing.embedder import HashEmbedder
from thesis_matchmaker.indexing.indexer import Indexer
from thesis_matchmaker.indexing.store import ChromaVectorStore
from thesis_matchmaker.retrieval.chroma import ChromaRetriever


@pytest.fixture()
def retriever(tmp_path: Path) -> ChromaRetriever:
    sources = tmp_path / "src"
    sources.mkdir()
    publications = [
        ZoraRecord(
            id="zora:1",
            title="Dense retrieval for German text",
            abstract="Neural search over German corpora.",
            authors=["Prof. A. Müller", "B. Student"],
            year=2024,
            department="Department of Computational Linguistics",
        ),
        ZoraRecord(
            id="zora:2",
            title="Medieval trade routes of the Alps",
            abstract="Archival study of alpine commerce.",
            authors=["Prof. C. Schmid"],
            year=2023,
            department="Department of History",
        ),
    ]
    postings = [
        ThesisPosting(
            id="posting:1",
            title="MSc thesis: dense retrieval for German text",
            description="Neural search over German corpora.",
            supervisor="Prof. A. Müller",
            degree_level="master",
            url="https://uzh.ch/p1",
        ),
    ]
    (sources / "publications.jsonl").write_text(
        "".join(p.model_dump_json() + "\n" for p in publications)
    )
    (sources / "theses.jsonl").write_text("".join(t.model_dump_json() + "\n" for t in postings))
    embedder = HashEmbedder()
    store = ChromaVectorStore(path=str(tmp_path / "index"), collection_name="test")
    Indexer(embedder=embedder, store=store, index_path=tmp_path / "index").run(sources)
    return ChromaRetriever(embedder=embedder, store=store)


def test_exact_topic_match_ranks_person_first(retriever: ChromaRetriever) -> None:
    query = ParsedQuery(topics=["Dense retrieval for German text"])
    matches = retriever.retrieve(query, top_k=3)
    assert matches
    assert matches[0].supervisor == "Prof. A. Müller"
    assert matches[0].has_open_position is True
    assert matches[0].publication_count >= 1


def test_matches_sorted_by_score(retriever: ChromaRetriever) -> None:
    query = ParsedQuery(topics=["Dense retrieval for German text"])
    matches = retriever.retrieve(query, top_k=3)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_degree_level_filter_narrows_postings(retriever: ChromaRetriever) -> None:
    query = ParsedQuery(topics=["anything at all"], degree_level="phd")
    matches = retriever.retrieve(query, top_k=5)
    for match in matches:
        assert match.has_open_position is False


def test_evidence_points_back_to_source_ids(retriever: ChromaRetriever) -> None:
    query = ParsedQuery(topics=["Dense retrieval for German text"])
    matches = retriever.retrieve(query, top_k=3)
    ids = {e.source_id for m in matches for e in m.evidence}
    assert "zora:1" in ids or "posting:1" in ids
