"""Database writes for scraped postings, researcher profiles and application processes.

This module is the only writer of the `posting`, `researcher_profile` and
`application_process` tables (invariant 1: ingestion owns all writes). Serving code
reads them through `indexing.sources.PostgresSourceReader`, never from here.

It replaces the prototype's SQLite mirror, which rebuilt `extracted_data.sqlite` from
the JSON on every run. Two things changed with it, both deliberate:

**Writes are per-source replaces, not a global rebuild.** A run can cover a single
source (`--only ifi--5`), so deleting everything first would drop 102 other sources'
records. Each source's rows are upserted, then any row still carrying a source_id this
run covered but absent from its output is deleted -- the same contract
`dataset.upsert_source` has with the JSON, expressed in SQL.

**There is no retention guard**, unlike `zora/store.py`'s. The harvester's guard exists
because a full ZORA harvest either sees the whole corpus or has failed; here a run
legitimately covers one source, so "the row count dropped" carries no signal. Losing a
source's rows is prevented by scoping the delete instead, and a source that failed to
extract never reaches this module -- `validate.py` flags it and `report.py` exits
non-zero.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel

from themis_shared import db
from themis_shared.config import get_settings
from themis_shared.contracts import ApplicationProcess, ResearcherProfile, ThesisPosting

from . import normalize

logger = logging.getLogger(__name__)

_UPSERT_POSTING = """
INSERT INTO posting (
    id, title, description, supervisors, faculty, department, degree_levels,
    status, keywords, language, url, listed_on, source_id, scraped_at, stored_at
)
VALUES (
    %(id)s, %(title)s, %(description)s, %(supervisors)s, %(faculty)s, %(department)s,
    %(degree_levels)s, %(status)s, %(keywords)s, %(language)s, %(url)s, %(listed_on)s,
    %(source_id)s, %(scraped_at)s, now()
)
ON CONFLICT (id) DO UPDATE SET
    title         = EXCLUDED.title,
    description   = EXCLUDED.description,
    supervisors   = EXCLUDED.supervisors,
    faculty       = EXCLUDED.faculty,
    department    = EXCLUDED.department,
    degree_levels = EXCLUDED.degree_levels,
    status        = EXCLUDED.status,
    keywords      = EXCLUDED.keywords,
    language      = EXCLUDED.language,
    url           = EXCLUDED.url,
    listed_on     = EXCLUDED.listed_on,
    source_id     = EXCLUDED.source_id,
    scraped_at    = EXCLUDED.scraped_at,
    stored_at     = now()
"""

_UPSERT_PROFILE = """
INSERT INTO researcher_profile (
    id, name, email, role, research_interest, research_field, research_group,
    bio, personal_website, profile_url, faculty, department, source_id,
    scraped_at, stored_at
)
VALUES (
    %(id)s, %(name)s, %(email)s, %(role)s, %(research_interest)s, %(research_field)s,
    %(research_group)s, %(bio)s, %(personal_website)s, %(profile_url)s, %(faculty)s,
    %(department)s, %(source_id)s, %(scraped_at)s, now()
)
ON CONFLICT (id) DO UPDATE SET
    name              = EXCLUDED.name,
    email             = EXCLUDED.email,
    role              = EXCLUDED.role,
    research_interest = EXCLUDED.research_interest,
    research_field    = EXCLUDED.research_field,
    research_group    = EXCLUDED.research_group,
    bio               = EXCLUDED.bio,
    personal_website  = EXCLUDED.personal_website,
    profile_url       = EXCLUDED.profile_url,
    faculty           = EXCLUDED.faculty,
    department        = EXCLUDED.department,
    source_id         = EXCLUDED.source_id,
    scraped_at        = EXCLUDED.scraped_at,
    stored_at         = now()
"""

_UPSERT_PROCESS = """
INSERT INTO application_process (
    id, degree_level, description, relevant_links, url, faculty, department,
    source_ids, scraped_at, stored_at
)
VALUES (
    %(id)s, %(degree_level)s, %(description)s, %(relevant_links)s, %(url)s,
    %(faculty)s, %(department)s, %(source_ids)s, %(scraped_at)s, now()
)
ON CONFLICT (id) DO UPDATE SET
    degree_level   = EXCLUDED.degree_level,
    description    = EXCLUDED.description,
    relevant_links = EXCLUDED.relevant_links,
    url            = EXCLUDED.url,
    faculty        = EXCLUDED.faculty,
    department     = EXCLUDED.department,
    source_ids     = EXCLUDED.source_ids,
    scraped_at     = EXCLUDED.scraped_at,
    stored_at      = now()
"""

# Anti-join rather than `id <> ALL(%s)`, for the reason zora/store.py's prune spells
# out. Scoped to the source_ids this run covered, so a single-source run cannot touch
# another source's rows.
_PRUNE = """
DELETE FROM {table}
WHERE source_id = ANY(%(source_ids)s)
  AND NOT EXISTS (
      SELECT 1 FROM unnest(%(keep)s::text[]) AS kept(id)
      WHERE kept.id = {table}.id
  )
"""

# application_process carries source_ids (plural, because the scraper consolidates
# several pages into one entry), so its scope test is array overlap.
_PRUNE_PROCESS = """
DELETE FROM application_process
WHERE source_ids && %(source_ids)s
  AND NOT EXISTS (
      SELECT 1 FROM unnest(%(keep)s::text[]) AS kept(id)
      WHERE kept.id = application_process.id
  )
"""


class ScrapeWriteResult(BaseModel):
    """What a write did, for the CLI to report and tests to assert on."""

    postings: int = 0
    profiles: int = 0
    processes: int = 0
    pruned: int = 0

    @property
    def total(self) -> int:
        return self.postings + self.profiles + self.processes


def _dsn(dsn: str | None) -> str:
    """The scraper's own Settings owns scraper knobs; the DSN belongs to the system."""
    return dsn if dsn is not None else get_settings().database_url


def _posting_params(posting: ThesisPosting) -> dict:
    return {
        "id": posting.id,
        "title": posting.title,
        "description": posting.description,
        # jsonb wants a JSON string. ensure_ascii=False keeps umlauts legible in psql,
        # which is where anyone debugging a scraped record will be looking.
        "supervisors": json.dumps(
            [s.model_dump() for s in posting.supervisors], ensure_ascii=False
        ),
        "faculty": posting.faculty,
        "department": posting.department,
        "degree_levels": [level.value for level in posting.degree_levels],
        "status": posting.status.value if posting.status else None,
        "keywords": posting.keywords,
        "language": posting.language,
        "url": posting.url,
        "listed_on": posting.listed_on,
        "source_id": posting.source_id,
        "scraped_at": posting.scraped_at,
    }


def _profile_params(profile: ResearcherProfile) -> dict:
    return profile.model_dump()


def _process_params(process: ApplicationProcess) -> dict:
    return {
        "id": process.id,
        "degree_level": process.degree_level.value if process.degree_level else None,
        "description": process.description,
        "relevant_links": json.dumps(process.relevant_links, ensure_ascii=False),
        "url": process.url,
        "faculty": process.faculty,
        "department": process.department,
        "source_ids": process.source_ids,
        "scraped_at": process.scraped_at,
    }


def write_dataset(data: dict, dsn: str | None = None) -> ScrapeWriteResult:
    """Flatten the scraper's dataset and write all three record kinds.

    One transaction: a run that dies part way leaves the tables as they were rather
    than holding a mix of two runs' records.
    """
    postings, profiles, processes = normalize.iter_records(data)

    # Which sources this run had anything to say about. The prunes are scoped to
    # these, so a source absent from `data` keeps its rows.
    posting_sources = sorted({p.source_id for p in postings if p.source_id})
    profile_sources = sorted({p.source_id for p in profiles if p.source_id})
    process_sources = sorted({sid for p in processes for sid in p.source_ids})

    result = ScrapeWriteResult()
    with db.connection(_dsn(dsn)) as conn, conn.transaction():
        cur = conn.cursor()
        if postings:
            cur.executemany(_UPSERT_POSTING, [_posting_params(p) for p in postings])
            result.postings = len(postings)
        if profiles:
            cur.executemany(_UPSERT_PROFILE, [_profile_params(p) for p in profiles])
            result.profiles = len(profiles)
        if processes:
            cur.executemany(_UPSERT_PROCESS, [_process_params(p) for p in processes])
            result.processes = len(processes)

        pruned = 0
        for table, sources, keep in (
            ("posting", posting_sources, [p.id for p in postings]),
            ("researcher_profile", profile_sources, [p.id for p in profiles]),
        ):
            if not sources:
                continue
            cur.execute(_PRUNE.format(table=table), {"source_ids": sources, "keep": keep})
            pruned += cur.rowcount or 0
        if process_sources:
            cur.execute(
                _PRUNE_PROCESS,
                {"source_ids": process_sources, "keep": [p.id for p in processes]},
            )
            pruned += cur.rowcount or 0
        result.pruned = pruned

    logger.info(
        "wrote %d postings, %d profiles, %d processes (%d stale rows removed)",
        result.postings,
        result.profiles,
        result.processes,
        result.pruned,
    )
    return result


def posting_count(dsn: str | None = None) -> int:
    """Rows in `posting`. Used by the run report and by the indexer's sanity check."""
    with db.connection(_dsn(dsn)) as conn:
        row = conn.execute("SELECT count(*) FROM posting").fetchone()
    return int(row[0]) if row else 0
