"""Tests for the data contracts."""

from thesis_matchmaker.contracts import (
    DegreeLevel,
    Evidence,
    SupervisorMatch,
    ThesisPosting,
    ZoraRecord,
)


def test_zora_record_defaults():
    r = ZoraRecord(id="zora:1", title="A paper")
    assert r.authors == []
    assert r.abstract is None


def test_thesis_posting_coerces_degree_level():
    p = ThesisPosting(id="p:1", title="MSc thesis", url="https://x", degree_level="master")
    assert p.degree_level is DegreeLevel.master


def test_supervisor_match_json_roundtrip():
    m = SupervisorMatch(
        supervisor="Prof. X",
        score=0.9,
        evidence=[Evidence(source_type="publication", source_id="zora:1", title="T")],
    )
    again = SupervisorMatch.model_validate(m.model_dump())
    assert again.evidence[0].source_id == "zora:1"
