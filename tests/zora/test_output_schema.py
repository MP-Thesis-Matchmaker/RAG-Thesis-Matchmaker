"""Tests for output_schema.py — the flat publication output contract."""
from thesis_matchmaker.zora.output_schema import ZoraPublication, to_output


def _record(**overrides):
    """Build a normalized record dict (as produced by normalize.normalize_item)."""
    base = {
        "handle": "20.500.14742/1001",
        "uuid": "uuid-1",
        "title": "Trade Policy and Growth",
        "authors": ["Doe, Jane"],
        "uzh_authors": ["Doe, Jane"],
        "author_authority_map": {"Doe, Jane": "some-uuid"},
        "author_orcid": "0000-0002-1111-2222",
        "abstract": "This paper examines...",
        "year": 2025,
        "type": "Journal Article",
        "department": "Department of Economics",
        "language": "eng",
        "doi": "10.1234/example",
        "uri": "https://www.zora.uzh.ch/id/eprint/1001",
        "keywords": ["International Trade", "Growth"],
        "accessioned": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_to_output_maps_all_fields():
    record = _record()
    out = to_output(record)

    assert out["id"] == "20.500.14742/1001"
    assert out["title"] == "Trade Policy and Growth"
    assert out["abstract"] == "This paper examines..."
    assert out["authors"] == ["Doe, Jane"]
    assert out["uzh_authors"] == ["Doe, Jane"]
    assert out["author_authority_map"] == {"Doe, Jane": "some-uuid"}
    assert out["year"] == 2025
    assert out["publication_type"] == "Journal Article"
    assert out["department"] == "Department of Economics"
    assert out["language"] == "eng"
    assert out["keywords"] == ["International Trade", "Growth"]
    assert out["doi"] == "10.1234/example"
    assert out["url"] == "https://www.zora.uzh.ch/id/eprint/1001"
    # Dropped fields should not appear
    assert "handle" not in out
    assert "zora_url" not in out
    assert "source_scope" not in out
    assert "faculty" not in out
    assert "harvested_at" not in out
    assert "author_orcid" not in out


def test_to_output_handles_missing_optional_fields():
    record = _record(
        title=None,
        abstract=None,
        authors=[],
        uzh_authors=[],
        author_authority_map={},
        author_orcid=None,
        year=None,
        type=None,
        department=None,
        language=None,
        doi=None,
        uri=None,
        keywords=[],
    )
    out = to_output(record)

    assert out["id"] == "20.500.14742/1001"
    assert out["title"] is None
    assert out["abstract"] is None
    assert out["authors"] == []
    assert out["uzh_authors"] == []
    assert out["author_authority_map"] == {}
    assert out["department"] is None
    assert out["language"] is None
    assert out["keywords"] == []


def test_to_output_does_not_include_internal_fields():
    """Fields like uuid and accessioned are internal — they should not leak into the output."""
    record = _record()
    out = to_output(record)

    assert "uuid" not in out
    assert "accessioned" not in out
    assert "uri" not in out  # mapped to url instead
    assert "type" not in out  # mapped to publication_type instead
    assert "handle" not in out  # mapped to id instead


def test_zora_publication_validates_valid_record():
    """Confirm a well-formed dict passes Pydantic validation."""
    record = _record()
    out = to_output(record)

    validated = ZoraPublication.model_validate(out)
    assert validated.id == "20.500.14742/1001"
    assert validated.department == "Department of Economics"
    assert validated.language == "eng"
    assert validated.uzh_authors == ["Doe, Jane"]
