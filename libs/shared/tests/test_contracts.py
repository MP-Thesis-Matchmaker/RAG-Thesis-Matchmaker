"""Tests for the data contracts."""

import pytest
from pydantic import ValidationError

from themis_shared.contracts import (
    AuthorAuthority,
    DegreeLevel,
    Evidence,
    PostingStatus,
    SupervisorMatch,
    ThesisPosting,
    ZoraOrgUnit,
    ZoraPerson,
    ZoraPublication,
)


def test_zora_publication_defaults():
    r = ZoraPublication(id="zora:1", title="A paper")
    assert r.authors == []
    assert r.uzh_authors == []
    assert r.author_authority_map == {}
    assert r.abstract is None
    assert r.owning_collection_uuid is None
    assert r.accessioned is None


def test_zora_publication_title_may_be_absent():
    """The column is nullable, so the contract is too -- ZORA has title-less items.

    A required field here meant the Postgres reader satisfied it with `or ""`,
    which put publications with a blank title into the index instead of surfacing
    them as what they are.
    """
    assert ZoraPublication(id="zora:1").title is None


def test_zora_publication_carries_accessioned_and_owning_collection():
    r = ZoraPublication(
        id="zora:1",
        title="A paper",
        owning_collection_uuid="coll-1",
        accessioned="2026-01-01T00:00:00Z",
    )
    assert r.owning_collection_uuid == "coll-1"
    assert r.accessioned == "2026-01-01T00:00:00Z"


# --- AuthorAuthority: the cris/orcid distinction the whole uzh_authors gap turns on ---


def test_author_authority_map_accepts_both_kinds_and_none():
    r = ZoraPublication(
        id="zora:1",
        title="A paper",
        author_authority_map={
            "Registered, Rita": {"type": "cris", "id": "3991287f-eb76-4f2c-9b98-cde42e6f4a65"},
            "Unknown, Ursula": {"type": "orcid", "id": "0000-0002-9454-3617"},
            "Anonymous, Alex": None,
        },
    )
    assert r.author_authority_map["Registered, Rita"].type == "cris"
    assert r.author_authority_map["Unknown, Ursula"].id == "0000-0002-9454-3617"
    assert r.author_authority_map["Anonymous, Alex"] is None


def test_author_authority_rejects_any_other_kind():
    """The Literal is the point: a third kind would silently break every rule built on it."""
    with pytest.raises(ValidationError):
        AuthorAuthority(type="scopus", id="whatever")


def test_author_authority_does_not_infer_the_kind_from_the_id():
    """Classification comes from DSpace's marker, never from the id's shape.

    20 upstream authority values are malformed ORCIDs, so a shape-sniffing model
    would file them under the wrong kind.
    """
    truncated_orcid_as_cris = AuthorAuthority(type="cris", id="0000-0002-8070-773")
    assert truncated_orcid_as_cris.type == "cris"


# --- the two entity mirrors ---


def test_zora_person_requires_only_a_uuid():
    """Person items carry no affiliation upstream, so almost everything is optional."""
    p = ZoraPerson(uuid="00d53153-03a6-4fd3-a581-de9a75a0015a")
    assert p.display_name is None
    assert p.orcid is None


def test_zora_org_unit_root_shape():
    """Depth 0 with no parent is the UZH root; a faculty is its own faculty_uuid."""
    root = ZoraOrgUnit(uuid="root", name="University of Zurich", depth=0)
    assert root.parent_uuid is None
    assert root.faculty_uuid is None
    assert root.collection_uuid is None

    faculty = ZoraOrgUnit(
        uuid="fac-1",
        name="03 Faculty of Economics",
        parent_uuid="root",
        faculty_uuid="fac-1",
        depth=1,
        collection_uuid="coll-1",
    )
    assert faculty.faculty_uuid == faculty.uuid


def test_zora_org_unit_requires_a_name_and_depth():
    with pytest.raises(ValidationError):
        ZoraOrgUnit(uuid="only-a-uuid")


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
        score_source="publication",
        evidence=[Evidence(source_type="publication", source_id="zora:1", title="T")],
    )
    again = SupervisorMatch.model_validate(m.model_dump())
    assert again.evidence[0].source_id == "zora:1"
    assert again.score_source == "publication"
