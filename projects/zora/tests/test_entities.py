"""Offline tests for the two entity-mirror harvest steps.

The client and the store are faked, so these cover the wiring: fetch → normalize →
dump → validate → snapshot write, and the `--limit` cap on each step.
"""

from __future__ import annotations

import json

import pytest

from fake_dso import FakeDSO
from thesis_matchmaker.zora import config, entities, store


@pytest.fixture()
def raw_dir(tmp_path, monkeypatch):
    """Point the raw-dump cache at a temp directory for the duration of a test."""
    monkeypatch.setattr(config, "RAW_DIR", str(tmp_path / "raw"))
    return tmp_path / "raw"


def _person_dso(uuid: str) -> FakeDSO:
    return FakeDSO(
        handle=f"20.500.14742/{uuid}",
        uuid=uuid,
        fields={
            config.FIELD_TITLE: [f"Person, {uuid}"],
            config.FIELD_PERSON_FAMILY: ["Person"],
            config.FIELD_PERSON_GIVEN: [uuid],
            config.FIELD_PERSON_ORCID: ["0000-0002-0450-9897"],
        },
    )


def _community(uuid: str, name: str) -> dict:
    return {"uuid": uuid, "name": name, "handle": f"20.500.14742/{uuid}", "metadata": {}}


def _captured_writer(captured: list[list[dict]], **result_kwargs):
    def write(rows, dsn=None):
        captured.append(rows)
        return store.EntityWriteResult(
            total=result_kwargs.get("total", len(rows)),
            upserted=result_kwargs.get("upserted", len(rows)),
            deleted=result_kwargs.get("deleted", 0),
            aborted=result_kwargs.get("aborted", False),
        )

    return write


def _dump_lines(raw_dir, kind: str) -> list[dict]:
    files = sorted(raw_dir.glob(f"*_{kind}.jsonl"))
    assert len(files) == 1, f"expected exactly one {kind} dump, found {files}"
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def test_harvest_persons_normalizes_dumps_and_writes(raw_dir, monkeypatch):
    monkeypatch.setattr(
        entities.zora_client,
        "iter_persons",
        lambda client: iter([_person_dso("p1"), _person_dso("p2")]),
    )
    written: list[list[dict]] = []
    monkeypatch.setattr(entities.store, "write_persons", _captured_writer(written))

    result = entities.harvest_persons(client=object())

    assert (result.total, result.upserted, result.aborted) == (2, 2, False)
    assert [row["uuid"] for row in written[0]] == ["p1", "p2"]
    # Validated through the contract, so the full shape is present.
    assert written[0][0]["display_name"] == "Person, p1"
    assert written[0][0]["orcid"] == "0000-0002-0450-9897"
    # The dump holds the *normalized* records, written before the DB write.
    assert [record["uuid"] for record in _dump_lines(raw_dir, "persons")] == ["p1", "p2"]


def test_harvest_persons_honours_limit(raw_dir, monkeypatch):
    monkeypatch.setattr(
        entities.zora_client,
        "iter_persons",
        lambda client: iter([_person_dso(f"p{i}") for i in range(10)]),
    )
    written: list[list[dict]] = []
    monkeypatch.setattr(entities.store, "write_persons", _captured_writer(written))

    entities.harvest_persons(client=object(), limit=3)

    assert [row["uuid"] for row in written[0]] == ["p0", "p1", "p2"]


def test_harvest_persons_propagates_an_aborted_snapshot(raw_dir, monkeypatch):
    """The store's empty-snapshot rail has to reach the caller, not be swallowed."""
    monkeypatch.setattr(entities.zora_client, "iter_persons", lambda client: iter([]))
    monkeypatch.setattr(
        entities.store, "write_persons", _captured_writer([], total=7, upserted=0, aborted=True)
    )

    assert entities.harvest_persons(client=object()).aborted is True


def test_harvest_org_units_normalizes_the_walk(raw_dir, monkeypatch):
    walk = [
        (_community("root", "University of Zurich"), None, 0, None, []),
        (
            _community("fac-1", "03 Faculty of Economics"),
            "root",
            1,
            "fac-1",
            [{"uuid": "coll-1", "name": "Publications of Faculty of Economics"}],
        ),
    ]
    monkeypatch.setattr(entities.zora_client, "iter_org_tree", lambda client: iter(walk))
    written: list[list[dict]] = []
    monkeypatch.setattr(entities.store, "write_org_units", _captured_writer(written))

    result = entities.harvest_org_units(client=object())

    assert (result.total, result.aborted) == (2, False)
    rows = {row["uuid"]: row for row in written[0]}
    assert rows["root"]["parent_uuid"] is None
    assert rows["root"]["depth"] == 0
    assert rows["root"]["collection_uuid"] is None
    assert rows["fac-1"]["faculty_uuid"] == "fac-1"
    assert rows["fac-1"]["collection_uuid"] == "coll-1"
    assert [record["uuid"] for record in _dump_lines(raw_dir, "orgunits")] == ["root", "fac-1"]


def test_harvest_org_units_honours_limit(raw_dir, monkeypatch):
    walk = [(_community(f"c{i}", f"Unit {i}"), "root", 2, "fac-1", []) for i in range(5)]
    monkeypatch.setattr(entities.zora_client, "iter_org_tree", lambda client: iter(walk))
    written: list[list[dict]] = []
    monkeypatch.setattr(entities.store, "write_org_units", _captured_writer(written))

    entities.harvest_org_units(client=object(), limit=2)

    assert [row["uuid"] for row in written[0]] == ["c0", "c1"]


def test_a_malformed_record_fails_before_the_write(raw_dir, monkeypatch):
    """Validation sits before the store, so a bad record cannot half-commit."""
    walk = [({"name": "no uuid"}, None, 0, None, [])]
    monkeypatch.setattr(entities.zora_client, "iter_org_tree", lambda client: iter(walk))
    written: list[list[dict]] = []
    monkeypatch.setattr(entities.store, "write_org_units", _captured_writer(written))

    with pytest.raises(KeyError):
        entities.harvest_org_units(client=object())

    assert written == []


# ---------------------------------------------------------------------------
# Replaying a dump instead of fetching
# ---------------------------------------------------------------------------


def _write(raw_dir, name: str, records: list[dict]) -> str:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / name
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return str(path)


def test_replay_reads_the_dump_and_never_calls_zora(raw_dir, monkeypatch):
    """The stranded-dump case: 2,018 records already on disk, no API request left to make."""

    def unreachable(client):
        raise AssertionError("a replay must not call iter_persons")

    monkeypatch.setattr(entities.zora_client, "iter_persons", unreachable)
    written: list[list[dict]] = []
    monkeypatch.setattr(entities.store, "write_persons", _captured_writer(written))
    path = _write(
        raw_dir,
        "20260825T091305Z_persons.jsonl",
        [
            {"uuid": "p1", "display_name": "Doe, Jane", "orcid": "0000-0002-0450-9897"},
            {"uuid": "p2", "display_name": "Roe, Ada"},
        ],
    )

    result = entities.harvest_persons(client=None, from_dump=path)

    assert (result.total, result.aborted) == (2, False)
    assert [row["uuid"] for row in written[0]] == ["p1", "p2"]
    # Still validated through the contract, so the replay is not a shortcut past it.
    assert written[0][0]["orcid"] == "0000-0002-0450-9897"


def test_replay_does_not_write_a_second_dump(raw_dir, monkeypatch):
    """The source file already *is* the cache; a copy would make replays ambiguous."""
    monkeypatch.setattr(entities.store, "write_persons", _captured_writer([]))
    path = _write(raw_dir, "20260825T091305Z_persons.jsonl", [{"uuid": "p1", "display_name": "X"}])

    entities.harvest_persons(client=None, from_dump=path)

    assert sorted(f.name for f in raw_dir.glob("*.jsonl")) == ["20260825T091305Z_persons.jsonl"]


def test_replay_honours_limit(raw_dir, monkeypatch):
    """`--limit` works on a replay too, which is what makes one usable as a smoke test."""
    written: list[list[dict]] = []
    monkeypatch.setattr(entities.store, "write_persons", _captured_writer(written))
    path = _write(
        raw_dir,
        "20260825T091305Z_persons.jsonl",
        [{"uuid": f"p{i}", "display_name": f"P{i}"} for i in range(10)],
    )

    entities.harvest_persons(client=None, from_dump=path, limit=3)

    assert [row["uuid"] for row in written[0]] == ["p0", "p1", "p2"]


def test_org_unit_replay_reads_the_dump(raw_dir, monkeypatch):
    def unreachable(client):
        raise AssertionError("a replay must not walk the community tree")

    monkeypatch.setattr(entities.zora_client, "iter_org_tree", unreachable)
    written: list[list[dict]] = []
    monkeypatch.setattr(entities.store, "write_org_units", _captured_writer(written))
    path = _write(
        raw_dir,
        "20260825T091305Z_orgunits.jsonl",
        [{"uuid": "fac-1", "name": "03 Faculty of Economics", "depth": 1}],
    )

    entities.harvest_org_units(client=None, from_dump=path)

    assert [row["uuid"] for row in written[0]] == ["fac-1"]
