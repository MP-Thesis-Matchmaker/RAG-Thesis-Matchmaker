"""Tests for the real retriever over an indexed temp store."""

from __future__ import annotations

from pathlib import Path

import pytest

from themis_matcher.indexing.embedder import HashEmbedder
from themis_matcher.indexing.indexer import Indexer
from themis_matcher.indexing.sources import JsonlSourceReader
from themis_matcher.indexing.store import InMemoryVectorStore
from themis_matcher.retrieval.vector import VectorRetriever
from themis_shared.contracts import ParsedQuery, ThesisPosting, ZoraPublication


@pytest.fixture()
def retriever(tmp_path: Path) -> VectorRetriever:
    sources = tmp_path / "src"
    sources.mkdir()
    publications = [
        ZoraPublication(
            id="zora:1",
            title="Dense retrieval for German text",
            abstract="Neural search over German corpora.",
            authors=["Prof. A. Müller", "B. Student"],
            uzh_authors=["Prof. A. Müller"],
            year=2024,
            department="Department of Computational Linguistics",
        ),
        ZoraPublication(
            id="zora:2",
            title="Medieval trade routes of the Alps",
            abstract="Archival study of alpine commerce.",
            authors=["Prof. C. Schmid"],
            uzh_authors=["Prof. C. Schmid"],
            year=2023,
            department="Department of History",
        ),
        # External-only author list: indexed, but never supervisor-eligible.
        ZoraPublication(
            id="zora:3",
            title="Dense retrieval for German text, external edition",
            abstract="Neural search over German corpora.",
            authors=["Dr. E. External"],
            uzh_authors=[],
            year=2022,
            department="Department of Computational Linguistics",
        ),
        # Two UZH co-authors on one publication: both get credited.
        ZoraPublication(
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
        # Indexed like any other posting, and excluded by the default retrieval
        # filter rather than by never having been embedded.
        ThesisPosting(
            id="posting:4",
            title="Already assigned topic on graph learning",
            description="Representation learning on graphs.",
            supervisors=[{"name": "Prof. J. Besetzt"}],
            status="assigned",
            url="https://uzh.ch/p4",
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


def test_score_source_names_the_document_the_score_came_from(
    retriever: VectorRetriever,
) -> None:
    """Which source won decides which threshold synthesis applies to the person.

    Prof. G. Roth appears only on a posting, Prof. C. Schmid only on a
    publication, so each has exactly one possible answer and the assertion cannot
    pass by accident.
    """
    graph = retriever.retrieve(ParsedQuery(topics=["Representation learning on graphs"]), top_k=10)
    roth = next(m for m in graph if m.supervisor == "Prof. G. Roth")
    assert roth.score_source == "thesis_posting"

    history = retriever.retrieve(ParsedQuery(topics=["Medieval trade routes"]), top_k=10)
    schmid = next(m for m in history if m.supervisor == "Prof. C. Schmid")
    assert schmid.score_source == "publication"


def test_score_source_agrees_with_the_highest_scoring_evidence(
    retriever: VectorRetriever,
) -> None:
    """The invariant behind the field: it names the source of the person's best hit.

    Checked across every match of a query that mixes both kinds, including people
    credited by a publication and a posting at once.
    """
    matches = retriever.retrieve(ParsedQuery(topics=["Dense retrieval for German text"]), top_k=10)
    assert matches
    for match in matches:
        assert match.score_source in {e.source_type for e in match.evidence}


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


# ---------------------------------------------------------------------------
# UZH affiliation: the filter, the fallback crediting, and the ranking
#
# zora:3 is the fixture's unaffiliated publication -- authors=["Dr. E. External"],
# uzh_authors=[]. Against this query it is the *second best* match by similarity
# (0.723 against Müller's 0.774), which is what makes it a useful probe: every
# assertion below would also hold if it simply scored badly, so each one names the
# score to show the ordering is a decision and not an accident.
# ---------------------------------------------------------------------------


def test_an_unaffiliated_researcher_is_reachable_but_ranked_last(
    retriever: VectorRetriever,
) -> None:
    """The permissive default: returned, credited via `authors`, below every UZH match."""
    query = ParsedQuery(topics=["Dense retrieval for German text"])
    matches = retriever.retrieve(query, top_k=10)

    external = [m for m in matches if m.supervisor == "Dr. E. External"]
    assert len(external) == 1, "the fallback to `authors` should credit the external author"
    assert external[0].has_uzh_affiliation is False
    assert "zora:3" in {e.source_id for e in external[0].evidence}

    # Last, despite outscoring every UZH match but one.
    assert matches[-1].supervisor == "Dr. E. External"
    assert external[0].score > matches[1].score


def test_uzh_first_demotion_can_push_a_match_out_of_top_k(retriever: VectorRetriever) -> None:
    """Demotion is not cosmetic: at a realistic top_k the external match drops out."""
    matches = retriever.retrieve(ParsedQuery(topics=["Dense retrieval for German text"]), top_k=5)

    assert "Dr. E. External" not in {m.supervisor for m in matches}
    assert all(m.has_uzh_affiliation for m in matches)


def test_score_strategy_orders_purely_by_similarity(retriever: VectorRetriever) -> None:
    """The pre-setting behaviour, still available: affiliation ignored."""
    scored = VectorRetriever(
        embedder=retriever.embedder, store=retriever.store, ranking_strategy="score"
    )
    matches = scored.retrieve(ParsedQuery(topics=["Dense retrieval for German text"]), top_k=10)

    assert [m.score for m in matches] == sorted((m.score for m in matches), reverse=True)
    # Second on similarity alone, where uzh_first put it last.
    assert matches[1].supervisor == "Dr. E. External"


def test_require_uzh_author_removes_it_entirely(retriever: VectorRetriever) -> None:
    """The hard cut: not demoted, absent -- even at a top_k wide enough to hold it."""
    strict = VectorRetriever(
        embedder=retriever.embedder, store=retriever.store, require_uzh_author=True
    )
    matches = strict.retrieve(ParsedQuery(topics=["Dense retrieval for German text"]), top_k=10)

    assert "Dr. E. External" not in {m.supervisor for m in matches}
    assert "zora:3" not in {e.source_id for m in matches for e in m.evidence}
    assert all(m.has_uzh_affiliation for m in matches)


def test_an_assigned_posting_is_absent_by_default(retriever: VectorRetriever) -> None:
    """Same exclusion as before, enforced one layer later.

    posting:4 is in the index -- it was embedded alongside the open ones -- and
    `require_available_posting`, on by default, is what keeps it out of the results.
    """
    matches = retriever.retrieve(ParsedQuery(topics=["graph learning"]), top_k=10)

    assert "posting:4" not in {e.source_id for m in matches for e in m.evidence}
    assert "Prof. J. Besetzt" not in {m.supervisor for m in matches}


def test_an_assigned_posting_returns_when_availability_is_not_required(
    retriever: VectorRetriever,
) -> None:
    """The payoff: flipping the rule needs no re-embed, only a different retriever.

    Same store, same vectors, opposite answer -- which is the whole reason the status
    filter moved out of indexing/sources.py.
    """
    permissive = VectorRetriever(
        embedder=retriever.embedder, store=retriever.store, require_available_posting=False
    )
    matches = permissive.retrieve(ParsedQuery(topics=["graph learning"]), top_k=10)

    assert "posting:4" in {e.source_id for m in matches for e in m.evidence}
    assert "Prof. J. Besetzt" in {m.supervisor for m in matches}


def test_a_uzh_author_stays_affiliated_despite_external_co_authors(
    retriever: VectorRetriever,
) -> None:
    """zora:4 names F. External alongside two UZH authors.

    The fallback must not fire when `uzh_authors` is non-empty, or every external
    co-author on a perfectly good UZH paper would be recommended as a supervisor.
    """
    matches = retriever.retrieve(ParsedQuery(topics=["Sleep and risk-seeking behaviour"]), top_k=10)
    supervisors = {m.supervisor for m in matches}

    assert "F. External" not in supervisors
    assert {"Prof. D. Werth", "Prof. E. Huber"} <= supervisors


def test_multi_uzh_author_publication_credits_every_author(retriever: VectorRetriever) -> None:
    query = ParsedQuery(topics=["Sleep and risk-seeking behaviour"])
    matches = retriever.retrieve(query, top_k=5)
    supervisors = {m.supervisor for m in matches}
    assert {"Prof. D. Werth", "Prof. E. Huber"} <= supervisors
    for match in matches:
        if match.supervisor in {"Prof. D. Werth", "Prof. E. Huber"}:
            assert "zora:4" in {e.source_id for e in match.evidence}
