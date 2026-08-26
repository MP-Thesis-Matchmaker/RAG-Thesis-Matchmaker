"""Index-run bookkeeping, against a real Postgres.

The single-active rule is a partial unique index rather than application code, so
these tests need the real database -- an in-memory stand-in would be testing the
stand-in. They skip without DATABASE_URL, like every other pgvector test.
"""

from __future__ import annotations

import threading

import pytest

from themis_matcher.indexing.indexer import IndexResult
from themis_matcher.indexing.runs import IndexRunInProgress, IndexRunStore
from themis_shared import db
from themis_shared.contracts import IndexRunKind, IndexRunState


@pytest.fixture()
def runs(dsn: str) -> IndexRunStore:
    """An empty index_run table, emptied again afterwards."""
    with db.connection(dsn) as conn:
        conn.execute("TRUNCATE index_run")
    store = IndexRunStore(dsn=dsn)
    yield store
    with db.connection(dsn) as conn:
        conn.execute("TRUNCATE index_run")


def _age_heartbeat(dsn: str, run_id: int, seconds: int) -> None:
    with db.connection(dsn) as conn:
        conn.execute(
            "UPDATE index_run SET heartbeat_at = now() - make_interval(secs => %s) WHERE id = %s",
            (seconds, run_id),
        )


def test_start_records_a_running_run(runs: IndexRunStore) -> None:
    run = runs.start(IndexRunKind.publication, "postgres")

    assert run.state is IndexRunState.running
    assert run.kind is IndexRunKind.publication
    assert run.source == "postgres"
    assert run.finished_at is None
    assert runs.get(run.id) == run


def test_second_run_is_refused_and_names_the_first(runs: IndexRunStore) -> None:
    """The whole reason the table has a partial unique index."""
    first = runs.start(IndexRunKind.publication, "postgres")

    with pytest.raises(IndexRunInProgress) as caught:
        runs.start(IndexRunKind.thesis_posting, "postgres")

    assert caught.value.run_id == first.id
    assert len(runs.recent()) == 1


def test_finishing_releases_the_slot(runs: IndexRunStore) -> None:
    first = runs.start(IndexRunKind.publication, "postgres")
    runs.succeed(first.id, IndexResult(embedded=3, skipped=1, deleted=2))

    done = runs.get(first.id)
    assert done is not None
    assert done.state is IndexRunState.succeeded
    assert (done.embedded, done.skipped, done.deleted) == (3, 1, 2)
    assert done.finished_at is not None

    second = runs.start(IndexRunKind.thesis_posting, "postgres")
    assert second.state is IndexRunState.running


def test_failing_releases_the_slot_and_keeps_the_reason(runs: IndexRunStore) -> None:
    first = runs.start(IndexRunKind.publication, "postgres")
    runs.fail(first.id, "connection lost")

    failed = runs.get(first.id)
    assert failed is not None
    assert failed.state is IndexRunState.failed
    assert failed.error == "connection lost"

    runs.start(IndexRunKind.thesis_posting, "postgres")


def test_a_run_that_stops_breathing_is_reaped(dsn: str, runs: IndexRunStore) -> None:
    """A killed process must not hold the slot forever."""
    stale = runs.start(IndexRunKind.publication, "postgres")
    _age_heartbeat(dsn, stale.id, runs.heartbeat_timeout_s + 60)

    assert runs.reap_stale() == [stale.id]

    reaped = runs.get(stale.id)
    assert reaped is not None
    assert reaped.state is IndexRunState.failed
    assert reaped.error is not None and "heartbeat" in reaped.error
    assert runs.active() is None


def test_start_reaps_before_claiming(dsn: str, runs: IndexRunStore) -> None:
    """Recovery is automatic; nobody has to notice and edit the table by hand."""
    stale = runs.start(IndexRunKind.publication, "postgres")
    _age_heartbeat(dsn, stale.id, runs.heartbeat_timeout_s + 60)

    fresh = runs.start(IndexRunKind.thesis_posting, "postgres")

    assert fresh.id != stale.id
    assert runs.active() == fresh


def test_heartbeat_keeps_a_slow_run_alive(dsn: str, runs: IndexRunStore) -> None:
    """A cold full index takes days but breathes every chunk; it must not be reaped."""
    run = runs.start(IndexRunKind.publication, "postgres")
    _age_heartbeat(dsn, run.id, runs.heartbeat_timeout_s + 60)

    runs.heartbeat(run.id)

    assert runs.reap_stale() == []
    assert runs.active() is not None


def test_recent_is_newest_first(runs: IndexRunStore) -> None:
    first = runs.start(IndexRunKind.publication, "postgres")
    runs.succeed(first.id, IndexResult())
    second = runs.start(IndexRunKind.thesis_posting, "postgres")

    assert [run.id for run in runs.recent()] == [second.id, first.id]


def test_get_is_none_for_an_unknown_run(runs: IndexRunStore) -> None:
    assert runs.get(999_999) is None


def test_only_one_of_many_racing_starts_wins(runs: IndexRunStore) -> None:
    """Sequential refusal proves the constraint exists; this proves it is a race-free one.

    The endpoints hand each accepted trigger to its own thread, and the harvester
    and the scraper can POST within the same second. If the guard were a
    check-then-insert in Python rather than a unique index, this is the test that
    would catch it.
    """
    started: list[int] = []
    refused: list[int | None] = []
    barrier = threading.Barrier(8)

    def claim() -> None:
        barrier.wait()
        try:
            started.append(runs.start(IndexRunKind.publication, "postgres").id)
        except IndexRunInProgress as exc:
            refused.append(exc.run_id)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(started) == 1, f"{len(started)} runs claimed the slot at once"
    assert len(refused) == 7
    assert [run.id for run in runs.recent()] == started
