"""Applies `schema.sql` to a Postgres database, idempotently.

Deliberately not a migration framework. There is no deployed database and no
harvested data yet, so there is nothing to preserve and therefore no reason to
express schema changes as deltas: `schema.sql` holds the whole schema and is
edited in place. One file also reads far better than a pile of numbered
fragments when someone -- a teammate, an examiner -- wants to know what the
database looks like.

What this does buy over running `psql < schema.sql` by hand is unattended,
idempotent application: the Kubernetes Job runs before every rollout, and CI
runs it before every test job. Neither has a human at a prompt.

The one hazard of the single-file approach is silent drift -- editing the file
while a database still holds the previous version, with `CREATE TABLE IF NOT
EXISTS` quietly doing nothing. So the file's fingerprint is stored alongside it
and a mismatch is refused loudly rather than ignored.

**When to switch to numbered migrations:** the first time a database holds data
that would be painful to recreate -- concretely, the first real ZORA harvest into
the UZH Postgres, which is a multi-hour job against a server we do not control.
At that point `--reset` stops being an acceptable answer, `schema.sql` becomes
`001_initial.sql`, and every later change is a delta.

That trigger has now half fired, and it is worth being precise about why only half.
A local database does hold a full 214,685-record harvest, and adding
`index_manifest.max_seq_length` did force exactly the refuse-or-reset choice this
module exists to force. `--reset` stayed acceptable only because
`zora/harvest.py --from-dump` replays `data/raw/`'s cache into an empty database in
seconds without re-fetching anything -- which makes the raw cache load-bearing here,
not merely an ingestion-reproducibility nicety. The next schema change against a
database with no replayable dump behind it -- the UZH server, once harvested -- is
where numbered migrations stop being optional.
"""

from __future__ import annotations

import hashlib
import logging
from importlib import resources

from psycopg import sql
from pydantic import BaseModel

from thesis_matchmaker import db

logger = logging.getLogger(__name__)

# Tracks which version of schema.sql is in place. Created by this module rather
# than by schema.sql itself, since it is the thing that describes schema.sql.
_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    id          int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    fingerprint text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""

_RECORD_VERSION = """
INSERT INTO schema_version (id, fingerprint) VALUES (1, %s)
ON CONFLICT (id) DO UPDATE SET fingerprint = EXCLUDED.fingerprint, applied_at = now()
"""


class SchemaChangedError(RuntimeError):
    """schema.sql no longer matches what this database had applied.

    Raised instead of silently doing nothing, which is what `CREATE TABLE IF NOT
    EXISTS` would do. Resolving it means recreating the database from the current
    file, which destroys its contents -- so it is the caller's decision, not ours.
    """


class ApplyResult(BaseModel):
    """What `apply` did, for the CLI to report and tests to assert on."""

    applied: bool
    fingerprint: str
    dropped: list[str] = []


def schema_sql() -> str:
    """The schema definition.

    Package data rather than a repository-root file, so it survives `pip install`
    and is present in the container image the Kubernetes Job runs.
    """
    return resources.files("thesis_matchmaker").joinpath("schema.sql").read_text(encoding="utf-8")


def fingerprint(sql_text: str | None = None) -> str:
    """Short sha256 of the schema definition."""
    text = sql_text if sql_text is not None else schema_sql()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def applied_fingerprint(dsn: str) -> str | None:
    """The fingerprint this database has applied, or None if it has none."""
    with db.connection(dsn) as conn:
        conn.execute(_VERSION_TABLE)
        row = conn.execute("SELECT fingerprint FROM schema_version WHERE id = 1").fetchone()
    return row[0] if row else None


_TABLES_QUERY = "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"


def existing_tables(dsn: str) -> list[str]:
    """Every table in the current schema, sorted."""
    with db.connection(dsn) as conn:
        return sorted(row[0] for row in conn.execute(_TABLES_QUERY).fetchall())


def drop_all_tables(dsn: str) -> list[str]:
    """Drop every table in the current schema. Returns what was dropped.

    Extensions are deliberately left alone: `CREATE EXTENSION vector` may well
    need privileges we do not have on the UZH server (see docs/deployment.md), so
    dropping one we could not recreate would be unrecoverable. Dropping the
    database or `SCHEMA public` wholesale would take the extension with it.
    """
    with db.connection(dsn) as conn:
        names = sorted(row[0] for row in conn.execute(_TABLES_QUERY).fetchall())
        if names:
            conn.execute(
                sql.SQL("DROP TABLE {} CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(name) for name in names)
                )
            )
            logger.warning("dropped %d table(s): %s", len(names), ", ".join(names))
    return names


def apply(dsn: str, *, reset: bool = False) -> ApplyResult:
    """Bring `dsn` to the schema in `schema.sql`.

    Unchanged database: does nothing. Empty database: applies the schema. Database
    holding a different version: raises `SchemaChangedError` unless `reset` was
    asked for, in which case every table is dropped first.
    """
    sql_text = schema_sql()
    want = fingerprint(sql_text)

    dropped: list[str] = []
    if reset:
        dropped = drop_all_tables(dsn)

    have = applied_fingerprint(dsn)
    if have == want:
        return ApplyResult(applied=False, fingerprint=want, dropped=dropped)
    if have is not None:
        raise SchemaChangedError(
            f"schema.sql has changed ({have} -> {want}) but this database still has "
            f"{have} applied. Re-run with --reset to drop every table and recreate "
            "from the current file. That DESTROYS ALL DATA in the database."
        )

    # No fingerprint, but tables all the same: somebody applied SQL by hand, or
    # this database predates init-db. Applying schema.sql would die on the first
    # CREATE TABLE, so say what is actually wrong instead.
    unmanaged = [name for name in existing_tables(dsn) if name != "schema_version"]
    if unmanaged:
        raise SchemaChangedError(
            f"this database already holds tables ({', '.join(unmanaged)}) but has no "
            "recorded schema fingerprint, so init-db did not create them. Re-run with "
            "--reset to drop them and recreate from schema.sql. That DESTROYS ALL DATA "
            "in the database."
        )

    with db.connection(dsn) as conn:
        conn.execute(sql_text)
        conn.execute(_RECORD_VERSION, (want,))
    logger.info("applied schema %s", want)
    return ApplyResult(applied=True, fingerprint=want, dropped=dropped)
