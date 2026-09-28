"""Tests for the real retriever over an indexed temp store."""

from __future__ import annotations

from pathlib import Path

import pytest

from themis_matcher.indexing.embedder import HashEmbedder
from themis_matcher.indexing.indexer import Indexer
from themis_matcher.indexing.sources import JsonlSourceReader
from themis_matcher.indexing.store import InMemoryVectorStore
from themis_matcher.retrieval.vector import VectorRetriever
from themis_shared.contracts import ParsedQuery, SupervisorMatch, ThesisPosting, ZoraPublication


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


def test_source_scores_name_only_the_sources_that_found_the_person(
    retriever: VectorRetriever,
) -> None:
    """Each source gets its own threshold, so each needs its own score.

    Prof. G. Roth appears only on a posting, Prof. C. Schmid only on a
    publication, so each has exactly one possible answer and the assertion cannot
    pass by accident.
    """
    graph = retriever.retrieve(ParsedQuery(topics=["Representation learning on graphs"]), top_k=10)
    roth = next(m for m in graph if m.supervisor == "Prof. G. Roth")
    assert set(roth.source_scores) == {"thesis_posting"}

    history = retriever.retrieve(ParsedQuery(topics=["Medieval trade routes"]), top_k=10)
    schmid = next(m for m in history if m.supervisor == "Prof. C. Schmid")
    assert set(schmid.source_scores) == {"publication"}


def test_a_person_found_in_both_sources_carries_both_scores(
    retriever: VectorRetriever,
) -> None:
    """The losing source's score must survive grouping, or synthesis cannot use it.

    Prof. A. Müller is credited by a publication and a posting at once. Every
    match must name exactly the sources of its evidence, and `score` must be the
    best of them.
    """
    matches = retriever.retrieve(ParsedQuery(topics=["Dense retrieval for German text"]), top_k=10)
    mueller = next(m for m in matches if m.supervisor == "Prof. A. Müller")
    assert set(mueller.source_scores) == {"publication", "thesis_posting"}
    for match in matches:
        assert set(match.source_scores) == {e.source_type for e in match.evidence}
        assert match.score == max(match.source_scores.values())


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


# --- cross-source person identity -------------------------------------------
#
# A separate fixture on purpose. The shared one above is asserted against by
# exact similarity scores, so adding records to it would move numbers unrelated
# tests depend on. This corpus exists only to put one person under two spellings
# and two people under one family name.


@pytest.fixture()
def identity_retriever(tmp_path: Path) -> VectorRetriever:
    sources = tmp_path / "identity"
    sources.mkdir()
    publications = [
        # ZORA's shape: "Family, Given". The posting below writes the same person
        # the other way round.
        ZoraPublication(
            id="zora:id1",
            title="Event cameras for autonomous drone racing",
            abstract="Vision-based agile flight.",
            authors=["Scaramuzza, Davide"],
            uzh_authors=["Scaramuzza, Davide"],
            year=2024,
            department="Department of Informatics",
        ),
        # A different Müller from the posting's. Same family name, different
        # given name -- the merge that must not happen.
        ZoraPublication(
            id="zora:id2",
            title="Statistical machine translation for Swiss German",
            abstract="Low-resource translation.",
            authors=["Müller, Mathias"],
            uzh_authors=["Müller, Mathias"],
            year=2023,
            department="Department of Computational Linguistics",
        ),
    ]
    postings = [
        ThesisPosting(
            id="posting:id1",
            title="MSc thesis: event cameras for autonomous drone racing",
            description="Vision-based agile flight.",
            supervisors=[{"name": "Davide Scaramuzza"}],
            url="https://uzh.ch/id1",
        ),
        ThesisPosting(
            id="posting:id2",
            title="MSc thesis: statistical machine translation for Swiss German",
            description="Low-resource translation.",
            supervisors=[{"name": "Daniel Müller"}],
            url="https://uzh.ch/id2",
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


def test_one_person_spelled_two_ways_is_one_match(identity_retriever: VectorRetriever) -> None:
    """The defect this change fixes, end to end.

    Before the person key was canonicalised, "Scaramuzza, Davide" on the paper
    and "Davide Scaramuzza" on the posting were two unrelated matches, so
    publication_count and posting_count were never both non-zero and any
    multi-signal score would have been scoring a join that never happened.
    """
    matches = identity_retriever.retrieve(
        ParsedQuery(topics=["event cameras for autonomous drone racing"]), top_k=10
    )
    scaramuzza = [m for m in matches if "Scaramuzza" in m.supervisor]

    assert len(scaramuzza) == 1
    assert scaramuzza[0].publication_count == 1
    assert scaramuzza[0].posting_count == 1
    # The posting's natural-order spelling is what a student reads.
    assert scaramuzza[0].supervisor == "Davide Scaramuzza"
    assert {"zora:id1", "posting:id1"} == {e.source_id for e in scaramuzza[0].evidence}


def test_two_people_sharing_a_family_name_stay_apart(identity_retriever: VectorRetriever) -> None:
    """Daniel Müller must not inherit Mathias Müller's publications.

    Measured on the live corpus: 46 of 403 supervisor names share a family name
    with a different ZORA author. A rule that merged on the family name alone
    would show a student someone else's papers as evidence, and nothing
    downstream could detect it.
    """
    matches = identity_retriever.retrieve(
        ParsedQuery(topics=["statistical machine translation for Swiss German"]), top_k=10
    )
    by_name = {m.supervisor: m for m in matches}

    assert "Daniel Müller" in by_name
    assert "Mathias Müller" in by_name
    assert by_name["Daniel Müller"].publication_count == 0
    assert by_name["Mathias Müller"].posting_count == 0


def test_an_ambiguous_posting_name_joins_neither_reading(tmp_path: Path) -> None:
    """A refused merge must actually be refused.

    "Alessandro De Luca" reads as Luca, Alessandro or as De Luca, Alessandro, and
    both are real ZORA authors here. The earlier fallback, `resolve(...) or
    key_of(...)`, keyed the refused name on (alessandro, luca) -- one of the two
    anchors it had just refused -- and so merged it anyway.
    """
    sources = tmp_path / "ambiguous"
    sources.mkdir()
    publications = [
        ZoraPublication(
            id="zora:luca",
            title="Glacier retreat in the Engadin",
            abstract="Alpine glaciology.",
            authors=["Luca, Alessandro"],
            uzh_authors=["Luca, Alessandro"],
        ),
        ZoraPublication(
            id="zora:deluca",
            title="Glacier retreat in the Valais",
            abstract="Alpine glaciology.",
            authors=["De Luca, Alessandro"],
            uzh_authors=["De Luca, Alessandro"],
        ),
    ]
    postings = [
        ThesisPosting(
            id="posting:ambiguous",
            title="MSc thesis: glacier retreat in the Alps",
            description="Alpine glaciology.",
            supervisors=[{"name": "Alessandro De Luca"}],
            url="https://uzh.ch/ambiguous",
        )
    ]
    (sources / "publications.jsonl").write_text(
        "".join(p.model_dump_json() + "\n" for p in publications)
    )
    (sources / "theses.jsonl").write_text("".join(t.model_dump_json() + "\n" for t in postings))
    embedder = HashEmbedder()
    store = InMemoryVectorStore()
    Indexer(embedder=embedder, store=store).run(JsonlSourceReader(sources))

    matches = VectorRetriever(embedder=embedder, store=store).retrieve(
        ParsedQuery(topics=["glacier retreat"]), top_k=10
    )

    assert len(matches) == 3
    posting_people = [m for m in matches if m.posting_count]
    assert len(posting_people) == 1
    assert posting_people[0].publication_count == 0
    assert all(m.posting_count == 0 for m in matches if m.publication_count)


def test_an_unaffiliated_namesake_does_not_vouch_for_a_posting_name(tmp_path: Path) -> None:
    """Only `uzh_authors` are anchors; a plain author never is.

    The paper below has no UZH author, so `_persons` credits its plain authors
    and "Müller, Daniel" becomes a publication person. Were that person an
    anchor -- as it was while anchors reused `_persons` -- the posting's "Daniel
    Müller" would merge with a stranger's paper: against 331,301 distinct author
    keys, a namesake is the likely reading, not the unlikely one.
    """
    sources = tmp_path / "namesake"
    sources.mkdir()
    publications = [
        ZoraPublication(
            id="zora:namesake",
            title="Soil microbiology of alpine meadows",
            abstract="Microbial communities.",
            authors=["Müller, Daniel"],
            uzh_authors=[],
        )
    ]
    postings = [
        ThesisPosting(
            id="posting:namesake",
            title="MSc thesis: soil microbiology of alpine meadows",
            description="Microbial communities.",
            supervisors=[{"name": "Daniel Müller"}],
            url="https://uzh.ch/namesake",
        )
    ]
    (sources / "publications.jsonl").write_text(
        "".join(p.model_dump_json() + "\n" for p in publications)
    )
    (sources / "theses.jsonl").write_text("".join(t.model_dump_json() + "\n" for t in postings))
    embedder = HashEmbedder()
    store = InMemoryVectorStore()
    Indexer(embedder=embedder, store=store).run(JsonlSourceReader(sources))

    matches = VectorRetriever(embedder=embedder, store=store).retrieve(
        ParsedQuery(topics=["soil microbiology"]), top_k=10
    )

    assert len(matches) == 2
    assert all(not (m.publication_count and m.posting_count) for m in matches)


def test_a_named_department_cannot_empty_the_result(identity_retriever: VectorRetriever) -> None:
    """A department is a nudge, not a filter.

    The LLM parser writes free text ("informatics"); the index stores official
    unit names, which differ between postings and ZORA. As an exact metadata
    filter this matched nothing on either side and returned no one at all.
    """
    query = ParsedQuery(topics=["event cameras for autonomous drone racing"])
    plain = identity_retriever.retrieve(query, top_k=10)
    nudged = identity_retriever.retrieve(
        query.model_copy(update={"department": "informatics"}), top_k=10
    )

    assert nudged
    assert {m.supervisor for m in nudged} == {m.supervisor for m in plain}


def test_an_unaffiliated_namesake_does_not_join_a_uzh_author(tmp_path: Path) -> None:
    """A stranger's paper must not become a UZH researcher's evidence.

    zora:uzh credits the UZH "Müller, Daniel", and the posting resolves to that
    anchor. zora:stranger has no UZH author, so `_persons` credits its plain
    "Müller, Daniel" -- a namesake, as far as anything here can tell. Keyed with
    plain `key_of` it equalled the anchor, and all three hits became one match.
    """
    sources = tmp_path / "uzh-namesake"
    sources.mkdir()
    publications = [
        ZoraPublication(
            id="zora:uzh",
            title="Soil microbiology of alpine meadows",
            abstract="Microbial communities.",
            authors=["Müller, Daniel"],
            uzh_authors=["Müller, Daniel"],
        ),
        ZoraPublication(
            id="zora:stranger",
            title="Soil microbiology of lowland meadows",
            abstract="Microbial communities.",
            authors=["Müller, Daniel"],
            uzh_authors=[],
        ),
    ]
    postings = [
        ThesisPosting(
            id="posting:uzh",
            title="MSc thesis: soil microbiology of alpine meadows",
            description="Microbial communities.",
            supervisors=[{"name": "Daniel Müller"}],
            url="https://uzh.ch/uzh-namesake",
        )
    ]
    (sources / "publications.jsonl").write_text(
        "".join(p.model_dump_json() + "\n" for p in publications)
    )
    (sources / "theses.jsonl").write_text("".join(t.model_dump_json() + "\n" for t in postings))
    embedder = HashEmbedder()
    store = InMemoryVectorStore()
    Indexer(embedder=embedder, store=store).run(JsonlSourceReader(sources))

    matches = VectorRetriever(embedder=embedder, store=store).retrieve(
        ParsedQuery(topics=["soil microbiology"]), top_k=10
    )

    assert len(matches) == 2
    uzh, stranger = matches
    assert uzh.has_uzh_affiliation
    assert {e.source_id for e in uzh.evidence} == {"zora:uzh", "posting:uzh"}
    # Kept, not dropped: the permissive default still reaches them, demoted.
    assert not stranger.has_uzh_affiliation
    assert {e.source_id for e in stranger.evidence} == {"zora:stranger"}


def test_two_uzh_authors_whose_middle_names_contradict_stay_apart(tmp_path: Path) -> None:
    """ "Pascal Felix" and "Pascal Flurin" share a first-token key, not a person.

    Both are UZH authors, so both are anchors and both keyed (pascal, beispiel);
    grouping on that key alone made them one match, each credited with the
    other's paper.
    """
    sources = tmp_path / "middle-names"
    sources.mkdir()
    publications = [
        ZoraPublication(
            id=f"zora:{given}",
            title="Glacier retreat in the eastern Alps",
            abstract="Mass balance.",
            authors=[f"Beispiel, Pascal {given}"],
            uzh_authors=[f"Beispiel, Pascal {given}"],
        )
        for given in ("Felix", "Flurin")
    ]
    (sources / "publications.jsonl").write_text(
        "".join(p.model_dump_json() + "\n" for p in publications)
    )
    (sources / "theses.jsonl").write_text("")
    embedder = HashEmbedder()
    store = InMemoryVectorStore()
    Indexer(embedder=embedder, store=store).run(JsonlSourceReader(sources))

    matches = VectorRetriever(embedder=embedder, store=store).retrieve(
        ParsedQuery(topics=["glacier retreat"]), top_k=10
    )

    assert sorted([e.source_id for e in m.evidence] for m in matches) == [
        ["zora:Felix"],
        ["zora:Flurin"],
    ]


# --- ranking by margin over each source's own bar ----------------------------
#
# Built from SupervisorMatch directly: HashEmbedder scores are arbitrary, so a
# fixture cannot be steered to the specific cross-scale gap this is about.


def _scored(name: str, **source_scores: float) -> SupervisorMatch:
    return SupervisorMatch(
        supervisor=name, score=max(source_scores.values()), source_scores=source_scores
    )


def _bars(retriever: VectorRetriever, **kwargs: object) -> VectorRetriever:
    return VectorRetriever(
        embedder=retriever.embedder,
        store=retriever.store,
        min_score_publication=0.57,
        min_score_posting=0.48,
        **kwargs,
    )


def test_ranking_compares_margins_not_raw_scores(retriever: VectorRetriever) -> None:
    """A posting 0.07 over its bar beats a publication 0.01 over its.

    On raw score the publication person wins (0.58 > 0.55), and `retrieve` cuts
    at top_k after ranking, so at top_k=1 the posting person used to be dropped
    before synthesis could apply the per-source threshold.
    """
    posting_person = _scored("Posting Person", thesis_posting=0.55)
    publication_person = _scored("Publication Person", publication=0.58)

    for strategy in ("uzh_first", "score"):
        ranked = _bars(retriever, ranking_strategy=strategy)._rank(
            [publication_person, posting_person]
        )
        assert [m.supervisor for m in ranked] == ["Posting Person", "Publication Person"]


def test_a_failing_source_does_not_lift_a_merged_person(retriever: VectorRetriever) -> None:
    """Margin is the best source's, so a 0.56 paper under its bar adds nothing.

    The merged person's best margin is their 0.49 posting (+0.01); the posting-only
    person at 0.55 (+0.07) ranks above them, though 0.56 > 0.55 on raw score.
    """
    merged = _scored("Merged Person", publication=0.56, thesis_posting=0.49)
    posting_only = _scored("Posting Only", thesis_posting=0.55)

    ranked = _bars(retriever)._rank([merged, posting_only])
    assert [m.supervisor for m in ranked] == ["Posting Only", "Merged Person"]
