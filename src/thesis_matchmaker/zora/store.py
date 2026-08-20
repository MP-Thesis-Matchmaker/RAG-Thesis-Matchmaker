"""Database writes for harvested publications and the harvest watermark.

This module is the only writer of the `publication` and `harvest_state` tables
(invariant 1: ingestion owns all writes). Serving code reads them through
`indexing.sources.PostgresSourceReader`, never from here.

Replaces the file I/O that used to live in `harvest.py` and `state.py`: a 45 MB
`data/publications.jsonl` rewritten in full on every run, plus a `state.json`
watermark, both committed into git by a CI bot.

The retention safety check gets strictly better in the process. On disk it had to
count, then write, then validate -- so a failure left a half-trusted file behind.
Here the whole harvest is one transaction that is rolled back if the corpus
shrank implausibly, which is what that check was always reaching for.
"""

from __future__ import annotations

import logging

from psycopg.types.json import Jsonb
from pydantic import BaseModel

from thesis_matchmaker import db
from thesis_matchmaker.config import get_settings

logger = logging.getLogger(__name__)

_UPSERT = """
INSERT INTO publication (
    id, doi, title, abstract, authors, uzh_authors, author_authority_map,
    year, publication_type, department, language, keywords, url, accessioned,
    harvested_at
)
VALUES (
    %(id)s, %(doi)s, %(title)s, %(abstract)s, %(authors)s, %(uzh_authors)s,
    %(author_authority_map)s, %(year)s, %(publication_type)s, %(department)s,
    %(language)s, %(keywords)s, %(url)s, %(accessioned)s, now()
)
ON CONFLICT (id) DO UPDATE SET
    doi                  = EXCLUDED.doi,
    title                = EXCLUDED.title,
    abstract             = EXCLUDED.abstract,
    authors              = EXCLUDED.authors,
    uzh_authors          = EXCLUDED.uzh_authors,
    author_authority_map = EXCLUDED.author_authority_map,
    year                 = EXCLUDED.year,
    publication_type     = EXCLUDED.publication_type,
    department           = EXCLUDED.department,
    language             = EXCLUDED.language,
    keywords             = EXCLUDED.keywords,
    url                  = EXCLUDED.url,
    accessioned          = EXCLUDED.accessioned,
    harvested_at         = now()
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
                    cursor = conn.execute("DELETE FROM publication WHERE id <> ALL(%s)", (ids,))
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


def load_state(dsn: str | None = None) -> dict:
    """The harvest watermark, in the shape the previous state.json had.

    Timestamps come back as ISO strings rather than datetimes so that callers
    which used to read the JSON file keep working unchanged.
    """
    with db.connection(_dsn(dsn)) as conn:
        row = conn.execute(_LOAD_STATE).fetchone()
    if row is None:
        return {
            "last_accessioned": None,
            "last_total_publications": 0,
            "last_run_at": None,
            "last_incremental_run_at": None,
            "last_full_run_at": None,
        }
    return {
        "last_accessioned": row[0],
        "last_total_publications": row[1],
        "last_run_at": row[2].isoformat() if row[2] else None,
        "last_incremental_run_at": row[3].isoformat() if row[3] else None,
        "last_full_run_at": row[4].isoformat() if row[4] else None,
    }


def save_state(
    last_accessioned: str | None,
    total_publications: int,
    mode: str,
    dsn: str | None = None,
) -> None:
    """Record the watermark and stamp this run.

    A full run supersedes an incremental one, so it stamps both -- otherwise the
    in-process scheduler fires an incremental immediately after a full harvest.
    """
    # A full run stamps the incremental column too, so the scheduler does not fire
    # an incremental immediately after one. Both branches below have to agree on
    # that: the very first run on a fresh deployment is an INSERT, and it is a
    # *full* harvest precisely because no state exists yet.
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
