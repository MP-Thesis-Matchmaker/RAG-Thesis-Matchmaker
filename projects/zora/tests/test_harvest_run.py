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

from fake_dso import FakeDSO
from themis_shared import db, schema
from themis_zora import config, entities, fields, harvest, store, zora_client


def _dso(handle: str, *, accessioned: str = "2026-01-01T00:00:00Z") -> FakeDSO:
    return FakeDSO(
        handle=handle,
        uuid=f"uuid-{handle}",
        fields={
            fields.FIELD_TITLE: [f"Title {handle}"],
            fields.FIELD_AUTHOR: ["Doe, Jane"],
            fields.FIELD_DATE_ACCESSIONED: [accessioned],
        },
        authorities={fields.FIELD_AUTHOR: ["cris-person-1"]},
    )


class _Spy:
    """Records the calls `run()` makes into the store and the entity steps."""

    def __init__(self, state: store.HarvestState) -> None:
        self.state = state
        self.write_calls: list[dict] = []
        self.save_calls: list[tuple] = []
        # Which steps ran, in order. `run()` promises persons -> org units ->
        # publications -> reconcile, and the order is the point: the mirrors are
        # what a publication's authorities and owning collection resolve against,
        # and the reconcile derives eligibility from whatever the run just wrote.
        self.steps: list[str] = []
        self.entity_limits: list[int | None] = []
        # Which dump each entity step was handed, if any -- the replay path passes
        # a path and no client, so this is where "no API request" is observable.
        self.entity_dumps: list[str | None] = []
        self.entity_clients: list[object] = []
        self.aborted_step: str | None = None

    def load_state(self, dsn: str | None = None) -> store.HarvestState:
        return self.state

    def write_harvest(self, rows, **kwargs) -> store.HarvestWriteResult:
        self.steps.append("publications")
        self.write_calls.append({"rows": rows, **kwargs})
        return store.HarvestWriteResult(
            total=len(rows), upserted=len(rows), deleted=0, aborted=False
        )

    def save_state(self, last_accessioned, total, mode, dsn=None) -> None:
        self.save_calls.append((last_accessioned, total, mode))

    def reconcile_uzh_authors(self, dsn: str | None = None) -> int:
        self.steps.append("reconcile")
        return 0

    def _entity_step(self, name: str):
        def step(client, limit=None, from_dump=None) -> store.EntityWriteResult:
            self.steps.append(name)
            self.entity_limits.append(limit)
            self.entity_dumps.append(from_dump)
            self.entity_clients.append(client)
            return store.EntityWriteResult(
                total=2, upserted=2, deleted=0, aborted=self.aborted_step == name
            )

        return step


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch, tmp_path) -> _Spy:
    """Wire `run()` up to fake ZORA items, fake entity steps and an in-memory store.

    The entity steps are stubbed rather than disabled, so every publication test
    below still exercises the real default path -- all three capabilities on.
    """
    s = _Spy(store.HarvestState(last_accessioned="2025-06-01", last_total_publications=22541))
    monkeypatch.setattr(store, "load_state", s.load_state)
    monkeypatch.setattr(store, "write_harvest", s.write_harvest)
    monkeypatch.setattr(store, "save_state", s.save_state)
    # Every store call `run()` makes has to be stubbed, not just the ones a test
    # asserts on: an unstubbed one opens a real connection to `settings.database_url`,
    # which is a live database on a developer machine and nothing at all in CI. That
    # is exactly how this arrived -- `reconcile_uzh_authors` joined `run()` without
    # joining the spy, so 21 offline tests passed locally against the real corpus
    # (writing to it) and failed in CI with a five-second pool timeout.
    monkeypatch.setattr(store, "reconcile_uzh_authors", s.reconcile_uzh_authors)
    monkeypatch.setattr(entities, "harvest_persons", s._entity_step("persons"))
    monkeypatch.setattr(entities, "harvest_org_units", s._entity_step("org_units"))
    monkeypatch.setattr(zora_client, "get_client", lambda: object())
    monkeypatch.setattr(
        zora_client,
        "iter_items",
        lambda client, since=None: iter([_dso("123/1"), _dso("123/2", accessioned="2026-02-02")]),
    )
    monkeypatch.setenv("ZORA_DATA_DIR", str(tmp_path))
    # The preflight is the one part of `run()` that needs a real database. It has
    # its own tests in tests/test_schema.py; here it would only mean every
    # orchestration test required Postgres.
    monkeypatch.setattr(schema, "require_current", lambda dsn: None)
    return s


# ---------------------------------------------------------------------------
# Orchestration: three capabilities, entities first
# ---------------------------------------------------------------------------


def test_default_run_harvests_all_three_entities_first(spy: _Spy) -> None:
    assert harvest.run("full") == 0
    assert spy.steps == ["persons", "org_units", "publications", "reconcile"]


def test_each_capability_can_be_opted_out(spy: _Spy) -> None:
    assert harvest.run("full", persons=False) == 0
    assert spy.steps == ["org_units", "publications", "reconcile"]

    spy.steps.clear()
    assert harvest.run("full", org_units=False) == 0
    assert spy.steps == ["persons", "publications", "reconcile"]

    spy.steps.clear()
    assert harvest.run("full", publications=False) == 0
    assert spy.steps == ["persons", "org_units", "reconcile"]


def test_no_publications_leaves_the_watermark_untouched(spy: _Spy) -> None:
    """Only the publication step owns harvest_state."""
    assert harvest.run("full", publications=False) == 0
    assert spy.write_calls == []
    assert spy.save_calls == []


def test_limit_applies_to_the_entity_steps_too(spy: _Spy) -> None:
    assert harvest.run("full", limit=5) == 0
    assert spy.entity_limits == [5, 5]


def test_the_reconcile_runs_last_on_success_and_not_at_all_on_failure(spy: _Spy) -> None:
    """Eligibility is derived from what the run just wrote, so it cannot run early.

    `--no-publications` still reconciles -- a refreshed `person` mirror alone can
    change which authors qualify across the existing corpus, which is what makes
    that flag worth using on its own. An aborted step, by contrast, means the inputs
    are not what the run intended, so nothing is derived from them.
    """
    assert harvest.run("full", publications=False) == 0
    assert spy.steps[-1] == "reconcile"

    spy.steps.clear()
    spy.aborted_step = "org_units"
    assert harvest.run("full") == 1
    assert "reconcile" not in spy.steps


def test_an_aborted_entity_snapshot_stops_before_publications(spy: _Spy) -> None:
    """The expensive half must not run once a cheap step already went wrong."""
    spy.aborted_step = "persons"

    assert harvest.run("full") == 1
    assert spy.steps == ["persons"]
    assert spy.write_calls == []
    assert spy.save_calls == []


def test_an_aborted_org_unit_snapshot_also_stops_the_run(spy: _Spy) -> None:
    spy.aborted_step = "org_units"

    assert harvest.run("full") == 1
    assert spy.steps == ["persons", "org_units"]
    assert spy.write_calls == []


def test_an_entity_step_failure_surfaces_as_a_clean_exit(
    spy: _Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken tree walk raises RuntimeError; main turns it into exit 1, no traceback."""

    def boom(client, limit=None, from_dump=None):
        raise RuntimeError("Failed to fetch .../subcommunities page 0")

    monkeypatch.setattr(entities, "harvest_org_units", boom)
    monkeypatch.setattr(db, "close_pools", lambda: None)
    monkeypatch.setattr("sys.argv", ["harvest", "--mode", "full"])

    with pytest.raises(SystemExit) as exc:
        harvest.main()

    assert exc.value.code == 1
    assert spy.write_calls == []


def test_main_maps_the_no_flags_onto_run(spy: _Spy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "close_pools", lambda: None)
    monkeypatch.setattr("sys.argv", ["harvest", "--mode", "full", "--no-persons", "--no-org-units"])

    with pytest.raises(SystemExit) as exc:
        harvest.main()

    assert exc.value.code == 0
    assert spy.steps == ["publications", "reconcile"]


def test_disabling_all_three_is_a_usage_error(spy: _Spy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "close_pools", lambda: None)
    monkeypatch.setattr(
        "sys.argv",
        ["harvest", "--no-persons", "--no-org-units", "--no-publications"],
    )

    with pytest.raises(SystemExit) as exc:
        harvest.main()

    # argparse.error() exits 2, and nothing ran.
    assert exc.value.code == 2
    assert spy.steps == []


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

    dumps = os.listdir(config.get_settings().raw_dir)
    assert len(dumps) == 1 and dumps[0].endswith("_full.jsonl")
    with open(os.path.join(config.get_settings().raw_dir, dumps[0]), encoding="utf-8") as f:
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
        "author_authority_map": {"Doe, Jane": {"type": "cris", "id": "cris-person-1"}},
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
    # mapping.to_publication ran, so the normalized keys were renamed.
    assert rows[0]["publication_type"] == "article"
    assert rows[0]["url"] == "https://www.zora.uzh.ch/id/eprint/123/1"
    # accessioned is part of the validated model now, not spliced on afterwards.
    assert rows[0]["accessioned"] == "2026-01-01T00:00:00Z"
    assert spy.write_calls[0]["previous_total"] == 22541


def test_a_lone_publication_dump_runs_only_the_publication_step(
    spy: _Spy, dump: str, monkeypatch
) -> None:
    """A replay exists to avoid the API; fetching 2,500 entity records would defeat it.

    The general rule is "a step with a dump replays it, a step without one is
    skipped". This is its most common case, and the one that has to keep behaving
    exactly as it did before dumps became repeatable.
    """
    monkeypatch.setattr(db, "close_pools", lambda: None)
    monkeypatch.setattr("sys.argv", ["harvest", "--mode", "full", "--from-dump", dump])

    with pytest.raises(SystemExit) as exc:
        harvest.main()

    assert exc.value.code == 0
    assert spy.steps == ["publications", "reconcile"]


# ---------------------------------------------------------------------------
# Several dumps at once: routing by kind
# ---------------------------------------------------------------------------


@pytest.fixture
def persons_dump(tmp_path) -> str:
    path = tmp_path / "20260825T091305Z_persons.jsonl"
    path.write_text(
        json.dumps({"uuid": "p1", "display_name": "Doe, Jane"}) + "\n", encoding="utf-8"
    )
    return str(path)


def test_a_persons_dump_replays_only_that_mirror(spy: _Spy, persons_dump: str) -> None:
    """The case the stranded 2,018-record dump could not be loaded for."""
    assert harvest.run("full", from_dump=persons_dump) == 0
    assert spy.steps == ["persons", "reconcile"]
    # The path reached the step, and no client was built for it.
    assert spy.entity_dumps == [persons_dump]
    assert spy.entity_clients == [None]


def test_two_dumps_replay_two_steps_and_skip_the_third(
    spy: _Spy, persons_dump: str, dump: str
) -> None:
    assert harvest.run("full", from_dump={"persons": persons_dump, "full": dump}) == 0
    assert spy.steps == ["persons", "publications", "reconcile"]


def test_dumps_that_feed_no_enabled_step_is_a_failure_not_a_silent_pass(
    spy: _Spy, persons_dump: str
) -> None:
    """Exit 1 rather than 0: the operator asked for work that did not happen."""
    assert harvest.run("full", from_dump=persons_dump, persons=False) == 1
    assert spy.steps == []


def test_a_dump_for_a_disabled_step_is_a_usage_error(
    spy: _Spy, persons_dump: str, monkeypatch
) -> None:
    monkeypatch.setattr(db, "close_pools", lambda: None)
    monkeypatch.setattr("sys.argv", ["harvest", "--from-dump", persons_dump, "--no-persons"])

    with pytest.raises(SystemExit) as exc:
        harvest.main()

    assert exc.value.code == 2
    assert spy.steps == []


def test_two_dumps_of_one_kind_is_a_usage_error(spy: _Spy, tmp_path, monkeypatch) -> None:
    """A step replays one file, so two candidates is a question only the operator can settle."""
    monkeypatch.setattr(db, "close_pools", lambda: None)
    first = _write_dump(tmp_path / "a_full.jsonl", [_record("123/1")])
    second = _write_dump(tmp_path / "b_full.jsonl", [_record("123/2")])
    monkeypatch.setattr("sys.argv", ["harvest", "--from-dump", first, "--from-dump", second])

    with pytest.raises(SystemExit) as exc:
        harvest.main()

    assert exc.value.code == 2


def test_an_unroutable_name_is_rejected_and_dump_kind_fixes_it(
    spy: _Spy, tmp_path, monkeypatch
) -> None:
    """A renamed or hand-copied dump: guessing would defer the failure, not avoid it."""
    monkeypatch.setattr(db, "close_pools", lambda: None)
    path = _write_dump(tmp_path / "copy.jsonl", [_record("123/1")])

    monkeypatch.setattr("sys.argv", ["harvest", "--from-dump", path])
    with pytest.raises(SystemExit) as exc:
        harvest.main()
    assert exc.value.code == 2
    assert spy.steps == []

    monkeypatch.setattr("sys.argv", ["harvest", "--from-dump", path, "--dump-kind", "full"])
    with pytest.raises(SystemExit) as exc:
        harvest.main()
    assert exc.value.code == 0
    assert spy.steps == ["publications", "reconcile"]


def test_dump_kind_needs_exactly_one_dump(spy: _Spy, tmp_path, monkeypatch) -> None:
    """One override cannot name the kind of two files."""
    monkeypatch.setattr(db, "close_pools", lambda: None)
    first = _write_dump(tmp_path / "a_full.jsonl", [_record("123/1")])
    second = _write_dump(tmp_path / "b_persons.jsonl", [_record("123/2")])
    monkeypatch.setattr(
        "sys.argv",
        ["harvest", "--from-dump", first, "--from-dump", second, "--dump-kind", "full"],
    )

    with pytest.raises(SystemExit) as exc:
        harvest.main()

    assert exc.value.code == 2


def test_from_dump_does_not_write_another_raw_dump(spy: _Spy, dump: str) -> None:
    """The source file already *is* the cache; re-dumping it would just duplicate it."""
    assert harvest.run("full", from_dump=dump) == 0
    assert (
        not os.path.exists(config.get_settings().raw_dir)
        or os.listdir(config.get_settings().raw_dir) == []
    )


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
    # Named for what it tests, not for a harvest step, so the kind is explicit.
    assert harvest.run("full", from_dump={"full": path}) == 0
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
    assert harvest.run("full", from_dump={"full": path}) == 0
    assert [row["id"] for row in spy.write_calls[0]["rows"]] == ["123/1", "123/3"]


def test_from_dump_tolerates_blank_lines(spy: _Spy, tmp_path) -> None:
    path = tmp_path / "dump.jsonl"
    path.write_text(
        json.dumps(_record("123/1")) + "\n\n" + json.dumps(_record("123/2")) + "\n",
        encoding="utf-8",
    )
    assert harvest.run("full", from_dump={"full": str(path)}) == 0
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
        "sys.argv", ["harvest", "--mode", "full", "--from-dump", str(tmp_path / "nope_full.jsonl")]
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
