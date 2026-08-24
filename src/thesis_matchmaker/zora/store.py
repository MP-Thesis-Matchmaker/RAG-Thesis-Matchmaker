"""Database writes for harvested publications and the harvest watermark.

This module is the only writer of the `publication` and `harvest_state` tables
(invariant 1: ingestion owns all writes). Serving code reads them through
`indexing.sources.PostgresSourceReader`, never from here.

Replaces the file I/O that used to live in `harvest.py` and a `state.py` shim: a
47 MB `data/publications.jsonl` rewritten in full on every run, plus a
`state.json` watermark, both committed into git by a CI bot.

The watermark was the worse of the two. It had to be persisted on whatever disk
the harvester happened to be given, which is why that CI workflow committed it
back into the repository -- a resume point coupled to git history, and unable to
survive two concurrent runs at all.

The retention safety check gets strictly better in the process. On disk it had to
count, then write, then validate -- so a failure left a half-trusted file behind.
Here the whole harvest is one transaction that is rolled back if the corpus
shrank implausibly, which is what that check was always reaching for.
"""

from __future__ import annotations

import logging
from datetime import datetime

from psycopg.types.json import Jsonb
from pydantic import BaseModel

from thesis_matchmaker import db
from thesis_matchmaker.config import get_settings

logger = logging.getLogger(__name__)

_UPSERT = """
INSERT INTO publication (
    id, doi, title, abstract, authors, uzh_authors, author_authority_map,
    year, publication_type, department, owning_collection_uuid, language,
    keywords, url, accessioned, harvested_at
)
VALUES (
    %(id)s, %(doi)s, %(title)s, %(abstract)s, %(authors)s, %(uzh_authors)s,
    %(author_authority_map)s, %(year)s, %(publication_type)s, %(department)s,
    %(owning_collection_uuid)s, %(language)s, %(keywords)s, %(url)s,
    %(accessioned)s, now()
)
ON CONFLICT (id) DO UPDATE SET
    doi                    = EXCLUDED.doi,
    title                  = EXCLUDED.title,
    abstract               = EXCLUDED.abstract,
    authors                = EXCLUDED.authors,
    uzh_authors            = EXCLUDED.uzh_authors,
    author_authority_map   = EXCLUDED.author_authority_map,
    year                   = EXCLUDED.year,
    publication_type       = EXCLUDED.publication_type,
    department             = EXCLUDED.department,
    owning_collection_uuid = EXCLUDED.owning_collection_uuid,
    language               = EXCLUDED.language,
    keywords               = EXCLUDED.keywords,
    url                    = EXCLUDED.url,
    accessioned            = EXCLUDED.accessioned,
    harvested_at           = now()
"""

# Prune everything the authoritative snapshot did not contain.
#
# Written as an anti-join against `unnest`, not as `id <> ALL(%(kept)s)`. They mean
# the same thing, but `<> ALL(array)` re-scans the array for every candidate row,
# so at real corpus size (~215k kept ids against ~22k rows) it degenerates into
# billions of comparisons inside the transaction holding the write lock.
# `unnest` gives the planner a relation it can hash, which turns the same question
# into one pass. NOT EXISTS rather than NOT IN because NOT IN yields NULL -- and
# so deletes nothing at all -- if a single id in the set is NULL.
_PRUNE = """
DELETE FROM publication p
WHERE NOT EXISTS (
    SELECT 1 FROM unnest(%(kept)s::text[]) AS kept(id) WHERE kept.id = p.id
)
"""

_LOAD_STATE = """
SELECT last_accessioned, last_total_publications, last_run_at,
       last_incremental_run_at, last_full_run_at
FROM harvest_state WHERE id = 1
"""


class HarvestWriteResult(BaseModel):
    """Outcome of one harvest write."""

    total: int
    upserted: int
    deleted: int
    aborted: bool = False


class HarvestState(BaseModel):
    """The single `harvest_state` row: where the next incremental run resumes."""

    # Text rather than a datetime because it is fed back verbatim into a Solr
    # range query -- schema.sql says the same thing about the column. The three
    # timestamps below are the opposite case: nothing queries them, they exist to
    # be read by a person asking when a harvest last committed, so they stay typed.
    last_accessioned: str | None = None
    last_total_publications: int = 0
    last_run_at: datetime | None = None
    last_incremental_run_at: datetime | None = None
    last_full_run_at: datetime | None = None


class _RetentionAbort(Exception):
    """Internal: unwinds the transaction when the corpus shrank implausibly."""


def _dsn(dsn: str | None) -> str:
    return dsn if dsn is not None else get_settings().database_url


def _params(row: dict) -> dict:
    """One output record as query parameters.

    Lists map onto Postgres `text[]`, which psycopg adapts directly; the author
    authority map is a map, so it stays jsonb.
    """
    return {
        **{
            key: row.get(key)
            for key in (
                "id",
                "doi",
                "title",
                "abstract",
                "year",
                "publication_type",
                "department",
                "owning_collection_uuid",
                "language",
                "url",
                "accessioned",
            )
        },
        "authors": list(row.get("authors") or []),
        "uzh_authors": list(row.get("uzh_authors") or []),
        "keywords": list(row.get("keywords") or []),
        "author_authority_map": Jsonb(row.get("author_authority_map") or {}),
    }


def write_harvest(
    rows: list[dict],
    *,
    mode: str,
    previous_total: int,
    min_retention_ratio: float,
    dsn: str | None = None,
) -> HarvestWriteResult:
    """Upsert a harvest, atomically, subject to the retention check.

    A **full** harvest is an authoritative snapshot, so publications missing from
    it are deleted -- that is how corrections and withdrawals upstream ever take
    effect. An **incremental** harvest only ever saw new items, so it must not
    delete anything.

    If the resulting corpus is smaller than `min_retention_ratio` of
    `previous_total`, nothing is committed: that pattern means an auth failure
    returning an empty-but-200 response or a bad scope far more often than it
    means UZH lost most of its publications overnight.
    """
    target = _dsn(dsn)
    ids = [row["id"] for row in rows]
    result = HarvestWriteResult(total=previous_total, upserted=0, deleted=0, aborted=True)

    with db.connection(target) as conn:
        try:
            with conn.transaction():
                if rows:
                    conn.cursor().executemany(_UPSERT, [_params(row) for row in rows])

                deleted = 0
                if mode == "full":
                    cursor = conn.execute(_PRUNE, {"kept": ids})
                    deleted = cursor.rowcount

                total = conn.execute("SELECT count(*) FROM publication").fetchone()[0]

                if previous_total > 0 and total < previous_total * min_retention_ratio:
                    logger.error(
                        "Retention check failed: %d publications after this harvest is "
                        "less than %.0f%% of the previous %d. Rolling back -- this looks "
                        "like an auth failure or a scope misconfiguration, not a real "
                        "data change.",
                        total,
                        min_retention_ratio * 100,
                        previous_total,
                    )
                    raise _RetentionAbort

                result = HarvestWriteResult(
                    total=total, upserted=len(rows), deleted=deleted, aborted=False
                )
        except _RetentionAbort:
            pass

    return result


def publication_count(dsn: str | None = None) -> int:
    with db.connection(_dsn(dsn)) as conn:
        return conn.execute("SELECT count(*) FROM publication").fetchone()[0]


# ---------------------------------------------------------------------------
# Entity mirrors: person and org_unit
# ---------------------------------------------------------------------------

_PERSON_UPSERT = """
INSERT INTO person (
    uuid, display_name, family_name, given_name, orcid, handle, url,
    accessioned, harvested_at
)
VALUES (
    %(uuid)s, %(display_name)s, %(family_name)s, %(given_name)s, %(orcid)s,
    %(handle)s, %(url)s, %(accessioned)s, now()
)
ON CONFLICT (uuid) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    family_name  = EXCLUDED.family_name,
    given_name   = EXCLUDED.given_name,
    orcid        = EXCLUDED.orcid,
    handle       = EXCLUDED.handle,
    url          = EXCLUDED.url,
    accessioned  = EXCLUDED.accessioned,
    harvested_at = now()
"""

_ORG_UNIT_UPSERT = """
INSERT INTO org_unit (
    uuid, name, parent_uuid, faculty_uuid, depth, handle, subject_id,
    collection_uuid, collection_name, harvested_at
)
VALUES (
    %(uuid)s, %(name)s, %(parent_uuid)s, %(faculty_uuid)s, %(depth)s,
    %(handle)s, %(subject_id)s, %(collection_uuid)s, %(collection_name)s, now()
)
ON CONFLICT (uuid) DO UPDATE SET
    name            = EXCLUDED.name,
    parent_uuid     = EXCLUDED.parent_uuid,
    faculty_uuid    = EXCLUDED.faculty_uuid,
    depth           = EXCLUDED.depth,
    handle          = EXCLUDED.handle,
    subject_id      = EXCLUDED.subject_id,
    collection_uuid = EXCLUDED.collection_uuid,
    collection_name = EXCLUDED.collection_name,
    harvested_at    = now()
"""

# Same anti-join shape as _PRUNE above, and for the same reasons -- see the
# comment there. The tables are three orders of magnitude smaller, but the
# pattern is already paid for.
_ENTITY_PRUNE = """
DELETE FROM {table} t
WHERE NOT EXISTS (
    SELECT 1 FROM unnest(%(kept)s::text[]) AS kept(uuid) WHERE kept.uuid = t.uuid
)
"""


class EntityWriteResult(BaseModel):
    """Outcome of one entity snapshot write."""

    total: int
    upserted: int
    deleted: int
    aborted: bool = False


def _write_entity_snapshot(table: str, upsert: str, rows: list[dict], dsn: str | None):
    """Replace a mirror table with an authoritative snapshot, atomically.

    Every run is a full snapshot -- these mirrors have no incremental mode --
    so anything missing from `rows` is pruned. One safety rail instead of the
    retention ratio: an empty snapshot against a non-empty table is refused,
    because "the API returned nothing" means a broken walk or an auth failure
    far more often than ZORA deleting every researcher or org unit.
    """
    kept = [row["uuid"] for row in rows]

    with db.connection(_dsn(dsn)) as conn:
        with conn.transaction():
            existing = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if not rows and existing > 0:
                logger.error(
                    "Refusing to commit an empty %s snapshot over %d existing rows -- "
                    "this looks like a failed fetch, not a real deletion of everything.",
                    table,
                    existing,
                )
                return EntityWriteResult(total=existing, upserted=0, deleted=0, aborted=True)

            if rows:
                conn.cursor().executemany(upsert, rows)
            deleted = conn.execute(_ENTITY_PRUNE.format(table=table), {"kept": kept}).rowcount
            total = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    return EntityWriteResult(total=total, upserted=len(rows), deleted=deleted, aborted=False)


def write_persons(rows: list[dict], dsn: str | None = None) -> EntityWriteResult:
    """Snapshot-replace the `person` mirror. Rows are validated ZoraPerson dumps."""
    return _write_entity_snapshot("person", _PERSON_UPSERT, rows, dsn)


def write_org_units(rows: list[dict], dsn: str | None = None) -> EntityWriteResult:
    """Snapshot-replace the `org_unit` mirror. Rows are validated ZoraOrgUnit dumps."""
    return _write_entity_snapshot("org_unit", _ORG_UNIT_UPSERT, rows, dsn)


def load_state(dsn: str | None = None) -> HarvestState:
    """Where the next incremental harvest resumes from.

    A missing row is not an error: it is a database that has never been harvested,
    and the field defaults say what that means -- no watermark, nothing counted.
    That is why the first run on a fresh deployment is necessarily a full one.
    """
    with db.connection(_dsn(dsn)) as conn:
        row = conn.execute(_LOAD_STATE).fetchone()
    if row is None:
        return HarvestState()
    return HarvestState(
        last_accessioned=row[0],
        last_total_publications=row[1],
        last_run_at=row[2],
        last_incremental_run_at=row[3],
        last_full_run_at=row[4],
    )


def save_state(
    last_accessioned: str | None,
    total_publications: int,
    mode: str,
    dsn: str | None = None,
) -> None:
    """Record the watermark and stamp this run.

    A full run supersedes an incremental one, so it stamps both columns. Nothing
    reads them now that the CronJobs own the cadence, but a full run that left
    last_incremental_run_at pointing at last week would make the row lie about
    when a harvest last committed.
    """
    # Both branches below have to agree on that rule. The very first run on a fresh
    # deployment is an INSERT rather than an UPDATE, and it is a *full* harvest
    # precisely because no state exists yet -- so the INSERT is exactly where
    # forgetting the incremental stamp would go unnoticed.
    stamped = ["last_full_run_at", "last_incremental_run_at"]
    if mode != "full":
        stamped = ["last_incremental_run_at"]

    columns = ", ".join(stamped)
    values = ", ".join(["now()"] * len(stamped))
    updates = ", ".join(f"{column} = now()" for column in stamped)

    with db.connection(_dsn(dsn)) as conn:
        conn.execute(
            f"""
            INSERT INTO harvest_state (id, last_accessioned, last_total_publications,
                                       last_run_at, {columns})
            VALUES (1, %s, %s, now(), {values})
            ON CONFLICT (id) DO UPDATE SET
                last_accessioned        = EXCLUDED.last_accessioned,
                last_total_publications = EXCLUDED.last_total_publications,
                last_run_at             = now(),
                {updates}
            """,
            (last_accessioned, total_publications),
        )
