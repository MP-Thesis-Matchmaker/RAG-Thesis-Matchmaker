"""End-to-end tests for the harvester entrypoint, with ZORA and Postgres faked.

`run()` was the one module in `zora/` with no test, which is how a leftover
dict-style access to `HarvestState` survived the Postgres migration and only
surfaced after a 2.5-hour full harvest had already been fetched. These tests
exercise the orchestration itself -- what `run()` passes where -- so the seam
between `load_state`, `write_harvest` and `save_state` cannot drift again.
"""

from __future__ import annotations

import json
import os

import pytest

from thesis_matchmaker import db
from thesis_matchmaker.zora import config, harvest, store, zora_client

from .fake_dso import FakeDSO


def _dso(handle: str, *, accessioned: str = "2026-01-01T00:00:00Z") -> FakeDSO:
    return FakeDSO(
        handle=handle,
        uuid=f"uuid-{handle}",
        fields={
            config.FIELD_TITLE: [f"Title {handle}"],
            config.FIELD_AUTHOR: ["Doe, Jane"],
            config.FIELD_DATE_ACCESSIONED: [accessioned],
        },
        authorities={config.FIELD_AUTHOR: ["cris-person-1"]},
    )


class _Spy:
    """Records the calls `run()` makes into the store, and what it passed."""

    def __init__(self, state: store.HarvestState) -> None:
        self.state = state
        self.write_calls: list[dict] = []
        self.save_calls: list[tuple] = []

    def load_state(self, dsn: str | None = None) -> store.HarvestState:
        return self.state

    def write_harvest(self, rows, **kwargs) -> store.HarvestWriteResult:
        self.write_calls.append({"rows": rows, **kwargs})
        return store.HarvestWriteResult(
            total=len(rows), upserted=len(rows), deleted=0, aborted=False
        )

    def save_state(self, last_accessioned, total, mode, dsn=None) -> None:
        self.save_calls.append((last_accessioned, total, mode))


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch, tmp_path) -> _Spy:
    """Wire `run()` up to fake ZORA items and an in-memory store."""
    s = _Spy(store.HarvestState(last_accessioned="2025-06-01", last_total_publications=22541))
    monkeypatch.setattr(store, "load_state", s.load_state)
    monkeypatch.setattr(store, "write_harvest", s.write_harvest)
    monkeypatch.setattr(store, "save_state", s.save_state)
    monkeypatch.setattr(zora_client, "get_client", lambda: object())
    monkeypatch.setattr(
        zora_client,
        "iter_items",
        lambda client, since=None: iter([_dso("123/1"), _dso("123/2", accessioned="2026-02-02")]),
    )
    monkeypatch.setattr(config, "RAW_DIR", str(tmp_path / "raw"))
    return s


def test_full_harvest_passes_previous_total_from_state(spy: _Spy) -> None:
    """The regression: previous_total is read off HarvestState, not dict-indexed.

    `HarvestState` is a Pydantic model, so a `.get()` here raises AttributeError
    *while building the argument list* -- before write_harvest is entered. The
    raw dump is already on disk at that point, which is why the failure looked
    like a successful harvest that silently wrote nothing.
    """
    assert harvest.run("full") == 0

    assert len(spy.write_calls) == 1
    assert spy.write_calls[0]["previous_total"] == 22541
    assert spy.write_calls[0]["mode"] == "full"


def test_full_harvest_writes_raw_dump_and_saves_watermark(spy: _Spy) -> None:
    assert harvest.run("full") == 0

    dumps = os.listdir(config.RAW_DIR)
    assert len(dumps) == 1 and dumps[0].endswith("_full.jsonl")
    with open(os.path.join(config.RAW_DIR, dumps[0]), encoding="utf-8") as f:
        assert [json.loads(line)["handle"] for line in f] == ["123/1", "123/2"]

    # The watermark advances to the newest accessioned date seen, not the oldest.
    assert spy.save_calls == [("2026-02-02", 2, "full")]


def test_aborted_write_returns_failure_and_leaves_watermark_alone(
    spy: _Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        store,
        "write_harvest",
        lambda rows, **kw: store.HarvestWriteResult(total=0, upserted=0, deleted=0, aborted=True),
    )

    assert harvest.run("full") == 1
    # A rolled-back harvest must not move the resume point.
    assert spy.save_calls == []


def test_incremental_with_no_new_items_stamps_run_and_keeps_total(
    spy: _Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(zora_client, "iter_items", lambda client, since=None: iter([]))

    assert harvest.run("incremental") == 0
    assert spy.write_calls == []
    assert spy.save_calls == [("2025-06-01", 22541, "incremental")]


# ---------------------------------------------------------------------------
# Replaying a raw dump (--from-dump)
# ---------------------------------------------------------------------------


def _record(handle: str, *, accessioned: str = "2026-01-01T00:00:00Z", **overrides) -> dict:
    """One line of a raw dump: the *normalized* record shape, not the output shape."""
    record = {
        "handle": handle,
        "uuid": f"uuid-{handle}",
        "title": f"Title {handle}",
        "abstract": "We study dense retrieval.",
        "authors": ["Doe, Jane"],
        "uzh_authors": ["Doe, Jane"],
        "author_authority_map": {"Doe, Jane": "cris-person-1"},
        "author_orcid": None,
        "year": 2024,
        "type": "article",
        "department": "Department of Informatics",
        "language": "eng",
        "keywords": ["retrieval"],
        "doi": f"10.1000/{handle}",
        "uri": f"https://www.zora.uzh.ch/id/eprint/{handle}",
        "accessioned": accessioned,
    }
    record.update(overrides)
    return record


def _write_dump(path, records: list[dict]) -> str:
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(path)


@pytest.fixture
def dump(tmp_path) -> str:
    return _write_dump(
        tmp_path / "20260821T151956Z_full.jsonl",
        [_record("123/1"), _record("123/2", accessioned="2026-02-02T00:00:00Z")],
    )


def test_from_dump_replays_without_contacting_zora(
    spy: _Spy, dump: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the raw cache: a re-run must not re-hit ZORA.

    get_client() is where the API token is resolved, so a replay that reached it
    would also fail on a machine that has no token -- which is most of them.
    """

    def unreachable():
        raise AssertionError("--from-dump must not build a ZORA client")

    monkeypatch.setattr(zora_client, "get_client", unreachable)

    assert harvest.run("full", from_dump=dump) == 0

    assert len(spy.write_calls) == 1
    rows = spy.write_calls[0]["rows"]
    assert [row["id"] for row in rows] == ["123/1", "123/2"]
    # to_output ran, so the normalized keys were mapped to the output shape.
    assert rows[0]["publication_type"] == "article"
    assert rows[0]["url"] == "https://www.zora.uzh.ch/id/eprint/123/1"
    assert spy.write_calls[0]["previous_total"] == 22541


def test_from_dump_does_not_write_another_raw_dump(spy: _Spy, dump: str) -> None:
    """The source file already *is* the cache; re-dumping it would just duplicate it."""
    assert harvest.run("full", from_dump=dump) == 0
    assert not os.path.exists(config.RAW_DIR) or os.listdir(config.RAW_DIR) == []


def test_from_dump_advances_watermark_to_last_record_seen(spy: _Spy, dump: str) -> None:
    assert harvest.run("full", from_dump=dump) == 0
    assert spy.save_calls == [("2026-02-02T00:00:00Z", 2, "full")]


def test_watermark_is_the_last_record_seen_not_the_maximum(spy: _Spy, tmp_path) -> None:
    """Documents a real dependency on ZORA returning items accessioned-ascending.

    `run()` overwrites `last_accessioned_seen` on every record, so the watermark
    is whichever record came last -- not `max(accessioned)`. Both harvested dumps
    so far are sorted ascending, which makes the two identical, and the direction
    of the error if that ever changes is the safe one: a watermark that is too old
    only makes the next incremental re-fetch items it already has, whereas one
    that is too new would silently skip them. Pinned here so a future change to
    the sort order shows up as a failing test rather than as a quiet gap.
    """
    path = _write_dump(
        tmp_path / "unsorted.jsonl",
        [
            _record("123/1", accessioned="2026-05-05T00:00:00Z"),
            _record("123/2", accessioned="2026-01-01T00:00:00Z"),
        ],
    )
    assert harvest.run("full", from_dump=path) == 0
    assert spy.save_calls == [("2026-01-01T00:00:00Z", 2, "full")]


def test_from_dump_respects_limit(spy: _Spy, dump: str) -> None:
    assert harvest.run("full", from_dump=dump, limit=1) == 0
    assert [row["id"] for row in spy.write_calls[0]["rows"]] == ["123/1"]


def test_from_dump_skips_records_without_a_handle(spy: _Spy, tmp_path) -> None:
    """A handle is the primary key, so a record without one cannot be upserted."""
    path = _write_dump(
        tmp_path / "dump.jsonl",
        [_record("123/1"), {**_record("ignored"), "handle": None}, _record("123/3")],
    )
    assert harvest.run("full", from_dump=path) == 0
    assert [row["id"] for row in spy.write_calls[0]["rows"]] == ["123/1", "123/3"]


def test_from_dump_tolerates_blank_lines(spy: _Spy, tmp_path) -> None:
    path = tmp_path / "dump.jsonl"
    path.write_text(
        json.dumps(_record("123/1")) + "\n\n" + json.dumps(_record("123/2")) + "\n",
        encoding="utf-8",
    )
    assert harvest.run("full", from_dump=str(path)) == 0
    assert [row["id"] for row in spy.write_calls[0]["rows"]] == ["123/1", "123/2"]


def test_from_dump_aborted_write_still_reports_failure(
    spy: _Spy, dump: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        store,
        "write_harvest",
        lambda rows, **kw: store.HarvestWriteResult(total=0, upserted=0, deleted=0, aborted=True),
    )
    assert harvest.run("full", from_dump=dump) == 1
    assert spy.save_calls == []


def test_main_wires_from_dump_through(
    spy: _Spy, dump: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "close_pools", lambda: None)
    monkeypatch.setattr("sys.argv", ["harvest", "--mode", "full", "--from-dump", dump])

    with pytest.raises(SystemExit) as exc:
        harvest.main()

    assert exc.value.code == 0
    assert [row["id"] for row in spy.write_calls[0]["rows"]] == ["123/1", "123/2"]


def test_missing_dump_fails_cleanly_without_a_traceback(
    spy: _Spy, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd path is a config mistake, so it gets the RuntimeError treatment."""
    monkeypatch.setattr(db, "close_pools", lambda: None)
    monkeypatch.setattr(
        "sys.argv", ["harvest", "--mode", "full", "--from-dump", str(tmp_path / "nope.jsonl")]
    )

    with pytest.raises(SystemExit) as exc:
        harvest.main()

    assert exc.value.code == 1
    assert spy.write_calls == []


def test_main_closes_connection_pools(spy: _Spy, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the process hangs ~20 s on exit complaining about pool threads.

    The pool is opened lazily by the first store call, so any entrypoint that
    touches the database owns closing it -- `cli.main` already does.
    """
    closed: list[bool] = []
    monkeypatch.setattr(db, "close_pools", lambda: closed.append(True))
    monkeypatch.setattr("sys.argv", ["harvest", "--mode", "full"])

    with pytest.raises(SystemExit) as exc:
        harvest.main()

    assert exc.value.code == 0
    assert closed == [True]


def test_main_closes_pools_even_when_run_crashes(
    spy: _Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-harvest is exactly when the pool is open and nobody closed it."""
    closed: list[bool] = []
    monkeypatch.setattr(db, "close_pools", lambda: closed.append(True))
    monkeypatch.setattr("sys.argv", ["harvest", "--mode", "full"])

    def boom(*args, **kwargs):
        raise AttributeError("regression stand-in")

    monkeypatch.setattr(harvest, "run", boom)

    with pytest.raises(AttributeError):
        harvest.main()

    assert closed == [True]
