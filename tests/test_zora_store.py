"""Tests for the ZORA write path: publication rows and the harvest watermark.

These need a real Postgres, so they skip unless DATABASE_URL points at a
database whose name ends in _test (see tests/conftest.py).
"""

from __future__ import annotations

import pytest

from thesis_matchmaker import db
from thesis_matchmaker.indexing.sources import PostgresSourceReader
from thesis_matchmaker.zora import store

_RATIO = 0.5


def _row(pub_id: str, **overrides) -> dict:
    base = {
        "id": pub_id,
        "doi": f"10.1000/{pub_id}",
        "title": f"Paper {pub_id}",
        "abstract": "We study dense retrieval.",
        "authors": ["A. Müller", "X. External"],
        "uzh_authors": ["A. Müller"],
        "author_authority_map": {"A. Müller": "uuid-1", "X. External": None},
        "year": 2024,
        "publication_type": "article",
        "department": "Department of Informatics",
        "language": "eng",
        "keywords": ["retrieval", "german"],
        "url": f"https://www.zora.uzh.ch/{pub_id}",
        "accessioned": "2026-07-17T09:04:55Z",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def clean_db(dsn: str) -> str:
    with db.connection(dsn) as conn:
        conn.execute("TRUNCATE publication")
        conn.execute("DELETE FROM harvest_state")
    return dsn


def _write(dsn: str, rows: list[dict], *, mode: str, previous_total: int = 0):
    return store.write_harvest(
        rows, mode=mode, previous_total=previous_total, min_retention_ratio=_RATIO, dsn=dsn
    )


def test_full_harvest_writes_rows(clean_db: str) -> None:
    result = _write(clean_db, [_row("zora:1"), _row("zora:2")], mode="full")
    assert result.aborted is False
    assert result.total == 2
    assert result.upserted == 2
    assert store.publication_count(clean_db) == 2


def test_upsert_overwrites_by_id(clean_db: str) -> None:
    _write(clean_db, [_row("zora:1")], mode="full")
    _write(clean_db, [_row("zora:1", title="Corrected title")], mode="full", previous_total=1)
    with db.connection(clean_db) as conn:
        titles = conn.execute("SELECT title FROM publication").fetchall()
    assert [t[0] for t in titles] == ["Corrected title"]


def test_full_harvest_prunes_publications_it_no_longer_sees(clean_db: str) -> None:
    """A full harvest is authoritative: withdrawals upstream have to take effect."""
    _write(clean_db, [_row(f"zora:{i}") for i in range(4)], mode="full")
    result = _write(clean_db, [_row("zora:0"), _row("zora:1")], mode="full", previous_total=4)
    assert result.deleted == 2
    assert result.total == 2


def test_full_harvest_prune_scales_to_a_large_kept_set(clean_db: str) -> None:
    """Pins the prune semantics across the anti-join rewrite.

    The original `id <> ALL(%s)` evaluated the kept-id array once per row, which
    is quadratic and became untenable at the real corpus size (~215k kept ids
    against ~22k rows). The replacement has to agree with it exactly: every row
    present in the kept set survives, every row absent from it goes.
    """
    _write(clean_db, [_row(f"zora:{i}") for i in range(50)], mode="full")
    kept = [_row(f"zora:{i}") for i in range(25)] + [_row(f"zora:new-{i}") for i in range(2000)]
    result = _write(clean_db, kept, mode="full", previous_total=50)
    assert result.aborted is False
    assert result.deleted == 25
    assert result.total == 2025


def test_full_harvest_with_nothing_kept_prunes_everything(clean_db: str) -> None:
    """The empty-kept-set edge case, which the two DELETE formulations could differ on.

    `id <> ALL('{}')` is TRUE for every row, so an empty full harvest wipes the
    table. That only survives the retention check when there was nothing to lose
    (previous_total = 0), but the semantics must not change silently.
    """
    _write(clean_db, [_row("zora:1"), _row("zora:2")], mode="full")
    result = _write(clean_db, [], mode="full", previous_total=0)
    assert result.aborted is False
    assert result.deleted == 2
    assert store.publication_count(clean_db) == 0


def test_incremental_harvest_never_deletes(clean_db: str) -> None:
    """An incremental run only ever saw new items, so absence proves nothing."""
    _write(clean_db, [_row(f"zora:{i}") for i in range(4)], mode="full")
    result = _write(clean_db, [_row("zora:99")], mode="incremental", previous_total=4)
    assert result.deleted == 0
    assert result.total == 5


def test_implausible_shrink_is_rolled_back_entirely(clean_db: str) -> None:
    """The retention check now leaves nothing half-written, unlike the file version."""
    _write(clean_db, [_row(f"zora:{i}") for i in range(10)], mode="full")
    result = _write(clean_db, [_row("zora:0")], mode="full", previous_total=10)
    assert result.aborted is True
    # Every one of the ten survives: the transaction was rolled back, so neither
    # the deletes nor the upsert landed.
    assert store.publication_count(clean_db) == 10


def test_state_roundtrip_and_full_run_stamps_both_modes(clean_db: str) -> None:
    empty = store.load_state(clean_db)
    # A never-harvested database is not an error, it is the defaults -- which is
    # what makes the first run on a fresh deployment necessarily a full one.
    assert isinstance(empty, store.HarvestState)
    assert empty.last_accessioned is None
    assert empty.last_total_publications == 0

    store.save_state("2026-07-17T09:04:55Z", 22541, "incremental", dsn=clean_db)
    after_incremental = store.load_state(clean_db)
    assert after_incremental.last_accessioned == "2026-07-17T09:04:55Z"
    assert after_incremental.last_total_publications == 22541
    assert after_incremental.last_full_run_at is None

    store.save_state("2026-08-20T00:00:00Z", 22600, "full", dsn=clean_db)
    after_full = store.load_state(clean_db)
    assert after_full.last_full_run_at is not None
    # A full run supersedes an incremental one, so it stamps both -- otherwise the
    # row would claim the last incremental harvest was older than it is.
    assert after_full.last_incremental_run_at is not None


def test_postgres_source_reader_returns_what_the_harvester_wrote(clean_db: str) -> None:
    """The read side of the same table, which is what the indexer consumes."""
    _write(clean_db, [_row("zora:1"), _row("zora:2")], mode="full")
    reader = PostgresSourceReader(dsn=clean_db)
    records = list(reader.publications())
    assert [r.id for r in records] == ["zora:1", "zora:2"]
    assert records[0].uzh_authors == ["A. Müller"]
    assert records[0].author_authority_map == {"A. Müller": "uuid-1", "X. External": None}
    assert records[0].keywords == ["retrieval", "german"]
    assert reader.invalid_records == 0


def test_first_ever_run_is_full_and_still_stamps_both_modes(clean_db: str) -> None:
    """The regression: a fresh deployment has no state row, so its first harvest is
    a full one *inserting* the row rather than updating it. The INSERT and UPDATE
    paths are written separately, so this is where the two can disagree about
    stamping the incremental column."""
    store.save_state("2026-08-20T00:00:00Z", 22541, "full", dsn=clean_db)
    state = store.load_state(clean_db)
    assert state.last_full_run_at is not None
    assert state.last_incremental_run_at is not None


def test_incremental_run_does_not_stamp_the_full_column(clean_db: str) -> None:
    store.save_state("2026-08-20T00:00:00Z", 22541, "incremental", dsn=clean_db)
    state = store.load_state(clean_db)
    assert state.last_incremental_run_at is not None
    assert state.last_full_run_at is None


def test_postgres_source_reader_skips_publications_without_a_uzh_author(clean_db: str) -> None:
    """The index only holds what could actually produce a supervisor recommendation.

    A publication whose author list contains no registered UZH researcher cannot
    produce one -- nobody on it works here -- so it is filtered out in SQL rather
    than embedded and then discarded by retrieval's query-time pre-filter.
    """
    _write(
        clean_db,
        [
            _row("zora:1"),
            _row(
                "zora:2",
                authors=["X. External", "Y. Elsewhere"],
                uzh_authors=[],
                author_authority_map={"X. External": None, "Y. Elsewhere": None},
            ),
        ],
        mode="full",
    )
    reader = PostgresSourceReader(dsn=clean_db)
    assert [r.id for r in reader.publications()] == ["zora:1"]
