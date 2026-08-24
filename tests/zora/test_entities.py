"""Offline tests for the two entity-mirror harvest steps.

The client and the store are faked, so these cover the wiring: fetch → normalize →
dump → validate → snapshot write, and the `--limit` cap on each step.
"""

from __future__ import annotations

import json

import pytest

from thesis_matchmaker.zora import config, entities, store

from .fake_dso import FakeDSO


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
