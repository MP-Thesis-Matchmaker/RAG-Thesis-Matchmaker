"""Tests for the data contracts."""

from thesis_matchmaker.contracts import (
    DegreeLevel,
    Evidence,
    PostingStatus,
    SupervisorMatch,
    ThesisPosting,
    ZoraRecord,
)


def test_zora_record_defaults():
    r = ZoraRecord(id="zora:1", title="A paper")
    assert r.authors == []
    assert r.uzh_authors == []
    assert r.author_authority_map == {}
    assert r.abstract is None


def test_thesis_posting_coerces_degree_levels():
    p = ThesisPosting(
        id="p:1", title="MSc thesis", url="https://x", degree_levels=["bachelor", "master"]
    )
    assert p.degree_levels == [DegreeLevel.bachelor, DegreeLevel.master]


def test_thesis_posting_holds_several_levels_and_supervisors():
    """The two shapes the pre-scraper contract could not express.

    A scalar `degree_level` and a single `supervisor` string fit the 20 invented
    fixtures and neither fits the real corpus: 121 of 247 scraped topics are open to
    two levels, and 36 name more than one person.
    """
    p = ThesisPosting(
        id="p:2",
        title="Co-supervised topic",
        url="https://x",
        degree_levels=["bachelor", "master"],
        supervisors=[{"name": "A. Example"}, {"name": "B. Example", "email": "b@example.org"}],
        status="assigned",
    )
    assert [s.name for s in p.supervisors] == ["A. Example", "B. Example"]
    assert p.supervisors[0].email is None
    assert p.status is PostingStatus.assigned


def test_thesis_posting_defaults_are_empty_not_absent():
    """An unlabelled posting must not read as bachelor-and-nobody."""
    p = ThesisPosting(id="p:3", title="Bare", url="https://x")
    assert p.degree_levels == []
    assert p.supervisors == []
    assert p.status is None
    assert p.listed_on is None


def test_supervisor_match_json_roundtrip():
    m = SupervisorMatch(
        supervisor="Prof. X",
        score=0.9,
        evidence=[Evidence(source_type="publication", source_id="zora:1", title="T")],
    )
    again = SupervisorMatch.model_validate(m.model_dump())
    assert again.evidence[0].source_id == "zora:1"
