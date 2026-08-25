"""Tests for the real retriever over an indexed temp store."""

from __future__ import annotations

from pathlib import Path

import pytest

from thesis_matchmaker.contracts import ParsedQuery, ThesisPosting, ZoraRecord
from thesis_matchmaker.indexing.embedder import HashEmbedder
from thesis_matchmaker.indexing.indexer import Indexer
from thesis_matchmaker.indexing.sources import JsonlSourceReader
from thesis_matchmaker.indexing.store import InMemoryVectorStore
from thesis_matchmaker.retrieval.vector import VectorRetriever


@pytest.fixture()
def retriever(tmp_path: Path) -> VectorRetriever:
    sources = tmp_path / "src"
    sources.mkdir()
    publications = [
        ZoraRecord(
            id="zora:1",
            title="Dense retrieval for German text",
            abstract="Neural search over German corpora.",
            authors=["Prof. A. Müller", "B. Student"],
            uzh_authors=["Prof. A. Müller"],
            year=2024,
            department="Department of Computational Linguistics",
        ),
        ZoraRecord(
            id="zora:2",
            title="Medieval trade routes of the Alps",
            abstract="Archival study of alpine commerce.",
            authors=["Prof. C. Schmid"],
            uzh_authors=["Prof. C. Schmid"],
            year=2023,
            department="Department of History",
        ),
        # External-only author list: indexed, but never supervisor-eligible.
        ZoraRecord(
            id="zora:3",
            title="Dense retrieval for German text, external edition",
            abstract="Neural search over German corpora.",
            authors=["Dr. E. External"],
            uzh_authors=[],
            year=2022,
            department="Department of Computational Linguistics",
        ),
        # Two UZH co-authors on one publication: both get credited.
        ZoraRecord(
            id="zora:4",
            title="Sleep and risk-seeking behaviour",
            abstract="Sleep intensity and risk decisions.",
            authors=["Prof. D. Werth", "Prof. E. Huber", "F. External"],
            uzh_authors=["Prof. D. Werth", "Prof. E. Huber"],
            year=2021,
            department="Department of Economics",
        ),
    ]
    postings = [
        ThesisPosting(
            id="posting:1",
            title="MSc thesis: dense retrieval for German text",
            description="Neural search over German corpora.",
            supervisors=[{"name": "Prof. A. Müller"}],
            degree_levels=["bachelor", "master"],
            url="https://uzh.ch/p1",
        ),
        # Two supervisors and no level, so the fan-out and the unlabelled case are
        # both covered by the shared fixture rather than by a special one.
        ThesisPosting(
            id="posting:2",
            title="Co-supervised topic on graph learning",
            description="Representation learning on graphs.",
            supervisors=[{"name": "Prof. G. Roth"}, {"name": "Prof. H. Stein"}],
            url="https://uzh.ch/p2",
        ),
        ThesisPosting(
            id="posting:3",
            title="Unattributed topic on graph learning",
            description="Representation learning on graphs.",
            url="https://uzh.ch/p3",
        ),
    ]
    (sources / "publications.jsonl").write_text(
        "".join(p.model_dump_json() + "\n" for p in publications)
    )
    (sources / "theses.jsonl").write_text("".join(t.model_dump_json() + "\n" for t in postings))
    embedder = HashEmbedder()
    store = InMemoryVectorStore()
    Indexer(embedder=embedder, store=store).run(JsonlSourceReader(sources))
    return VectorRetriever(embedder=embedder, store=store)


def test_exact_topic_match_ranks_person_first(retriever: VectorRetriever) -> None:
    query = ParsedQuery(topics=["Dense retrieval for German text"])
    matches = retriever.retrieve(query, top_k=3)
    assert matches
    assert matches[0].supervisor == "Prof. A. Müller"
    assert matches[0].posting_count == 1
    assert matches[0].publication_count >= 1


def test_matches_sorted_by_score(retriever: VectorRetriever) -> None:
    query = ParsedQuery(topics=["Dense retrieval for German text"])
    matches = retriever.retrieve(query, top_k=3)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_degree_level_filter_narrows_postings(retriever: VectorRetriever) -> None:
    query = ParsedQuery(topics=["anything at all"], degree_level="phd")
    matches = retriever.retrieve(query, top_k=5)
    for match in matches:
        assert match.posting_count == 0


def test_a_two_level_posting_is_found_by_both_levels(retriever: VectorRetriever) -> None:
    """The assertion the whole degree_levels change exists for.

    posting:1 is open to bachelor and master. Under the old scalar field it could be
    stored as only one of them and would have been invisible to the other -- which is
    121 of 247 real topics, not a corner case.
    """
    topic = ["dense retrieval for German text"]
    for level in ("bachelor", "master"):
        matches = retriever.retrieve(ParsedQuery(topics=topic, degree_level=level), top_k=5)
        found = {e.source_id for m in matches for e in m.evidence}
        assert "posting:1" in found, f"posting:1 unreachable for a {level} query"


def test_a_posting_credits_every_named_supervisor(retriever: VectorRetriever) -> None:
    """Postings fan out like publications; co-supervision is normal."""
    matches = retriever.retrieve(ParsedQuery(topics=["graph learning"]), top_k=5)
    credited = {
        m.supervisor for m in matches if any(e.source_id == "posting:2" for e in m.evidence)
    }
    assert credited == {"Prof. G. Roth", "Prof. H. Stein"}


def test_a_posting_naming_nobody_credits_nobody(retriever: VectorRetriever) -> None:
    """Documented rather than desirable: posting:3 cannot become a recommendation.

    There is nobody to recommend, so it is correctly absent from the grouped results.
    It stays visible in the index -- `has_supervisor` is false on it -- which is what
    a future ranking pass would need to surface it some other way.
    """
    matches = retriever.retrieve(ParsedQuery(topics=["graph learning"]), top_k=5)
    assert not any(e.source_id == "posting:3" for m in matches for e in m.evidence)


def test_evidence_points_back_to_source_ids(retriever: VectorRetriever) -> None:
    query = ParsedQuery(topics=["Dense retrieval for German text"])
    matches = retriever.retrieve(query, top_k=3)
    ids = {e.source_id for m in matches for e in m.evidence}
    assert "zora:1" in ids or "posting:1" in ids


def test_publications_without_uzh_authors_are_filtered_out(retriever: VectorRetriever) -> None:
    query = ParsedQuery(topics=["Dense retrieval for German text"])
    matches = retriever.retrieve(query, top_k=5)
    supervisors = {m.supervisor for m in matches}
    evidence_ids = {e.source_id for m in matches for e in m.evidence}
    assert "Dr. E. External" not in supervisors
    assert "zora:3" not in evidence_ids


def test_multi_uzh_author_publication_credits_every_author(retriever: VectorRetriever) -> None:
    query = ParsedQuery(topics=["Sleep and risk-seeking behaviour"])
    matches = retriever.retrieve(query, top_k=5)
    supervisors = {m.supervisor for m in matches}
    assert {"Prof. D. Werth", "Prof. E. Huber"} <= supervisors
    for match in matches:
        if match.supervisor in {"Prof. D. Werth", "Prof. E. Huber"}:
            assert "zora:4" in {e.source_id for e in match.evidence}
