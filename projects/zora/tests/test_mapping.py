"""Tests for mapping.py — normalized harvester dicts → validated contract models."""

from thesis_matchmaker.contracts import ZoraOrgUnit, ZoraPerson, ZoraPublication
from thesis_matchmaker.zora.mapping import to_org_unit, to_person, to_publication


def _record(**overrides):
    """Build a normalized record dict (as produced by normalize.normalize_item)."""
    base = {
        "handle": "20.500.14742/1001",
        "uuid": "uuid-1",
        "title": "Trade Policy and Growth",
        "authors": ["Doe, Jane"],
        "uzh_authors": ["Doe, Jane"],
        "author_authority_map": {"Doe, Jane": {"type": "cris", "id": "some-uuid"}},
        "author_orcid": "0000-0002-1111-2222",
        "abstract": "This paper examines...",
        "year": 2025,
        "type": "Journal Article",
        "department": "Department of Economics",
        "owning_collection_uuid": "coll-uuid-1",
        "language": "eng",
        "doi": "10.1234/example",
        "uri": "https://www.zora.uzh.ch/id/eprint/1001",
        "keywords": ["International Trade", "Growth"],
        "accessioned": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_to_publication_maps_all_fields():
    out = to_publication(_record())

    assert out["id"] == "20.500.14742/1001"
    assert out["title"] == "Trade Policy and Growth"
    assert out["abstract"] == "This paper examines..."
    assert out["authors"] == ["Doe, Jane"]
    assert out["uzh_authors"] == ["Doe, Jane"]
    assert out["author_authority_map"] == {"Doe, Jane": {"type": "cris", "id": "some-uuid"}}
    assert out["year"] == 2025
    assert out["publication_type"] == "Journal Article"
    assert out["department"] == "Department of Economics"
    assert out["owning_collection_uuid"] == "coll-uuid-1"
    assert out["language"] == "eng"
    assert out["keywords"] == ["International Trade", "Growth"]
    assert out["doi"] == "10.1234/example"
    assert out["url"] == "https://www.zora.uzh.ch/id/eprint/1001"
    # Internal keys stay internal; the renames are the whole job of this module.
    assert "handle" not in out
    assert "uuid" not in out
    assert "uri" not in out
    assert "type" not in out
    assert "author_orcid" not in out


def test_to_publication_carries_accessioned():
    """It is a contract field now, not spliced on after validation."""
    out = to_publication(_record())
    assert out["accessioned"] == "2026-01-01T00:00:00Z"
    assert ZoraPublication.model_validate(out).accessioned == "2026-01-01T00:00:00Z"


def test_to_publication_handles_missing_optional_fields():
    out = to_publication(
        _record(
            title=None,
            abstract=None,
            authors=[],
            uzh_authors=[],
            author_authority_map={},
            author_orcid=None,
            year=None,
            type=None,
            department=None,
            owning_collection_uuid=None,
            language=None,
            doi=None,
            uri=None,
            keywords=[],
            accessioned=None,
        )
    )

    assert out["id"] == "20.500.14742/1001"
    assert out["title"] is None
    assert out["author_authority_map"] == {}
    assert out["owning_collection_uuid"] is None
    assert out["accessioned"] is None


def test_to_publication_validates_against_the_contract():
    validated = ZoraPublication.model_validate(to_publication(_record()))
    assert validated.id == "20.500.14742/1001"
    assert validated.department == "Department of Economics"
    assert validated.uzh_authors == ["Doe, Jane"]


# --- persons ---


def _person_record(**overrides):
    base = {
        "uuid": "00d53153-03a6-4fd3-a581-de9a75a0015a",
        "display_name": "Runge, Jan-Niklas",
        "family_name": "Runge",
        "given_name": "Jan-Niklas",
        "orcid": "0000-0002-0450-9897",
        "handle": "20.500.14742/239047",
        "url": "https://www.zora.uzh.ch/handle/20.500.14742/239047",
        "accessioned": "2025-12-08T16:28:41Z",
    }
    base.update(overrides)
    return base


def test_to_person_maps_all_fields():
    out = to_person(_person_record())

    assert out == _person_record()
    ZoraPerson.model_validate(out)


def test_to_person_handles_missing_optionals():
    out = to_person(
        _person_record(
            display_name=None,
            family_name=None,
            given_name=None,
            orcid=None,
            handle=None,
            url=None,
            accessioned=None,
        )
    )

    assert out["uuid"] == "00d53153-03a6-4fd3-a581-de9a75a0015a"
    assert out["orcid"] is None


# --- org units ---


def _org_unit_record(**overrides):
    base = {
        "uuid": "9e8a319a-6d8f-4882-bf2a-684e358e6fff",
        "name": "03 Faculty of Economics",
        "parent_uuid": "323725a5-950d-4b89-8765-1b955e305664",
        "faculty_uuid": "9e8a319a-6d8f-4882-bf2a-684e358e6fff",
        "depth": 1,
        "handle": "20.500.14742/36",
        "subject_id": "10232",
        "collection_uuid": "coll-uuid-1",
        "collection_name": "Publications of Faculty of Economics",
    }
    base.update(overrides)
    return base


def test_to_org_unit_maps_all_fields():
    out = to_org_unit(_org_unit_record())

    assert out == _org_unit_record()
    ZoraOrgUnit.model_validate(out)


def test_to_org_unit_root_has_no_parent():
    out = to_org_unit(
        _org_unit_record(parent_uuid=None, faculty_uuid=None, depth=0, collection_uuid=None)
    )

    assert out["parent_uuid"] is None
    assert out["depth"] == 0
