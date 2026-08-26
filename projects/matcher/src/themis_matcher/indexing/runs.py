"""Index-run bookkeeping: who is running, who finished, who died.

Two jobs, and the first one is correctness rather than reporting. Two concurrent
index runs interleave their upserts and their orphan sweeps, so the second has to
be refused. That refusal is enforced by a partial unique index in `schema.sql`
(`index_run_single_active`) rather than by a lock held here: the database is the
only place both API replicas and a CLI run can see, and a `pg_advisory_lock`
would have to hold one of five pooled connections for the hours a cold run takes.

The second job is telling a dead run from a slow one. `index_manifest` only gains
a row once a run has *finished*, so a process killed mid-run leaves no trace
there while having already written documents. Every committed chunk bumps
`heartbeat_at`; a `running` row that has stopped breathing is reaped, which also
releases the single-active slot it would otherwise hold forever.
"""

from __future__ import annotations

import logging

from psycopg import errors as pg_errors

from themis_matcher.indexing.indexer import IndexResult
from themis_shared import db
from themis_shared.contracts import IndexRun, IndexRunKind, IndexRunState

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id, kind, state, source, embedded, skipped, deleted, truncated, "
    "invalid_lines, error, started_at, heartbeat_at, finished_at"
)


class IndexRunInProgress(RuntimeError):
    """Another run holds the single-active slot.

    Carries the id of the run that holds it, so a caller can report which one
    rather than only that there was one.
    """

    def __init__(self, run_id: int | None) -> None:
        self.run_id = run_id
        super().__init__(
            f"an index run is already in progress (run {run_id})"
            if run_id is not None
            else "an index run is already in progress"
        )


def _to_run(row: tuple) -> IndexRun:
    return IndexRun(
        id=row[0],
        kind=IndexRunKind(row[1]),
        state=IndexRunState(row[2]),
        source=row[3],
        embedded=row[4],
        skipped=row[5],
        deleted=row[6],
        truncated=row[7],
        invalid_lines=row[8],
        error=row[9],
        started_at=row[10],
        heartbeat_at=row[11],
        finished_at=row[12],
    )


class IndexRunStore:
    """Reads and writes the `index_run` table."""

    def __init__(self, dsn: str, heartbeat_timeout_s: int = 900) -> None:
        self.dsn = dsn
        self.heartbeat_timeout_s = heartbeat_timeout_s

    def start(self, kind: IndexRunKind, source: str) -> IndexRun:
        """Claim the single-active slot, or raise `IndexRunInProgress`.

        Stale runs are reaped first: a crashed process must not lock out
        indexing until somebody notices and edits the table by hand.
        """
        self.reap_stale()
        try:
            with db.connection(self.dsn) as conn:
                row = conn.execute(
                    f"INSERT INTO index_run (kind, state, source) "
                    f"VALUES (%s, %s, %s) RETURNING {_COLUMNS}",
                    (kind.value, IndexRunState.running.value, source),
                ).fetchone()
        except pg_errors.UniqueViolation as exc:
            raise IndexRunInProgress(self._active_id()) from exc
        assert row is not None  # RETURNING on a successful INSERT always yields a row
        logger.info("index run %d started (kind=%s source=%s)", row[0], kind.value, source)
        return _to_run(row)

    def heartbeat(self, run_id: int) -> None:
        """Say the run is still alive. Called per committed chunk."""
        with db.connection(self.dsn) as conn:
            conn.execute(
                "UPDATE index_run SET heartbeat_at = now() WHERE id = %s AND state = %s",
                (run_id, IndexRunState.running.value),
            )

    def succeed(self, run_id: int, result: IndexResult) -> None:
        with db.connection(self.dsn) as conn:
            conn.execute(
                "UPDATE index_run SET state = %s, embedded = %s, skipped = %s, deleted = %s, "
                "truncated = %s, invalid_lines = %s, heartbeat_at = now(), finished_at = now() "
                "WHERE id = %s",
                (
                    IndexRunState.succeeded.value,
                    result.embedded,
                    result.skipped,
                    result.deleted,
                    result.truncated,
                    result.invalid_lines,
                    run_id,
                ),
            )
        logger.info("index run %d succeeded (%s)", run_id, result.model_dump())

    def fail(self, run_id: int, error: str) -> None:
        """Record a failure.

        The message is truncated: it comes from an exception whose text we do not
        control, and a run row is not the place for a megabyte of driver output.
        """
        with db.connection(self.dsn) as conn:
            conn.execute(
                "UPDATE index_run SET state = %s, error = %s, heartbeat_at = now(), "
                "finished_at = now() WHERE id = %s",
                (IndexRunState.failed.value, error[:2000], run_id),
            )
        logger.warning("index run %d failed: %s", run_id, error)

    def get(self, run_id: int) -> IndexRun | None:
        with db.connection(self.dsn) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM index_run WHERE id = %s", (run_id,)
            ).fetchone()
        return _to_run(row) if row else None

    def recent(self, limit: int = 20) -> list[IndexRun]:
        with db.connection(self.dsn) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM index_run ORDER BY started_at DESC, id DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [_to_run(row) for row in rows]

    def active(self) -> IndexRun | None:
        """The run holding the single-active slot, if any."""
        with db.connection(self.dsn) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM index_run WHERE state = %s",
                (IndexRunState.running.value,),
            ).fetchone()
        return _to_run(row) if row else None

    def reap_stale(self) -> list[int]:
        """Fail any run whose heartbeat has gone quiet, and return their ids.

        The timeout has to exceed the gap between two committed chunks, not the
        length of a run: a cold full index takes days but breathes every chunk.
        """
        with db.connection(self.dsn) as conn:
            rows = conn.execute(
                "UPDATE index_run SET state = %s, error = %s, finished_at = now() "
                "WHERE state = %s AND heartbeat_at < now() - make_interval(secs => %s) "
                "RETURNING id",
                (
                    IndexRunState.failed.value,
                    "no heartbeat; the process running this index run went away",
                    IndexRunState.running.value,
                    self.heartbeat_timeout_s,
                ),
            ).fetchall()
        reaped = [row[0] for row in rows]
        if reaped:
            logger.warning("reaped %d stale index run(s): %s", len(reaped), reaped)
        return reaped

    def _active_id(self) -> int | None:
        active = self.active()
        return active.id if active else None
