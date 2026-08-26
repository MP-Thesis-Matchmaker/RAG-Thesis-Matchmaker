"""Tests for the ZORA write path: publication rows and the harvest watermark.

These need a real Postgres, so they skip unless DATABASE_URL points at a
database whose name ends in _test (see tests/conftest.py).
"""

from __future__ import annotations

import pytest

from themis_shared import db
from themis_zora import store

_RATIO = 0.5


def _row(pub_id: str, **overrides) -> dict:
    base = {
        "id": pub_id,
        "doi": f"10.1000/{pub_id}",
        "title": f"Paper {pub_id}",
        "abstract": "We study dense retrieval.",
        "authors": ["A. Müller", "X. External"],
        "uzh_authors": ["A. Müller"],
        "author_authority_map": {
            "A. Müller": {"type": "cris", "id": "uuid-1"},
            "X. External": None,
        },
        "year": 2024,
        "publication_type": "article",
        "department": "Department of Informatics",
        "owning_collection_uuid": "coll-uuid-1",
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
        conn.execute("TRUNCATE person")
        conn.execute("TRUNCATE org_unit")
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


# --- Entity mirrors: person and org_unit ---


def _person(uuid: str, **overrides) -> dict:
    base = {
        "uuid": uuid,
        "display_name": f"Person {uuid}",
        "family_name": "Person",
        "given_name": uuid,
        "orcid": f"0000-0000-0000-{uuid[-4:].zfill(4)}",
        "handle": f"20.500.14742/{uuid}",
        "url": f"https://www.zora.uzh.ch/handle/20.500.14742/{uuid}",
        "accessioned": "2025-12-08T16:28:41Z",
    }
    base.update(overrides)
    return base


def _org_unit(uuid: str, **overrides) -> dict:
    base = {
        "uuid": uuid,
        "name": f"Institute {uuid}",
        "parent_uuid": "root",
        "faculty_uuid": "faculty-1",
        "depth": 2,
        "handle": f"20.500.14742/{uuid}",
        "subject_id": None,
        "collection_uuid": f"coll-{uuid}",
        "collection_name": f"Publications of Institute {uuid}",
    }
    base.update(overrides)
    return base


def test_write_persons_snapshot_replaces(clean_db: str) -> None:
    first = store.write_persons([_person("p1"), _person("p2")], dsn=clean_db)
    assert (first.total, first.upserted, first.deleted, first.aborted) == (2, 2, 0, False)

    # The next snapshot no longer contains p2: it must be pruned.
    second = store.write_persons([_person("p1", orcid="0000-0001-0001-0001")], dsn=clean_db)
    assert (second.total, second.deleted, second.aborted) == (1, 1, False)

    with db.connection(clean_db) as conn:
        row = conn.execute("SELECT orcid FROM person WHERE uuid = 'p1'").fetchone()
    assert row[0] == "0000-0001-0001-0001"


def test_write_persons_refuses_empty_snapshot_over_existing_rows(clean_db: str) -> None:
    store.write_persons([_person("p1")], dsn=clean_db)
    result = store.write_persons([], dsn=clean_db)
    assert result.aborted is True
    with db.connection(clean_db) as conn:
        assert conn.execute("SELECT count(*) FROM person").fetchone()[0] == 1


def test_write_persons_empty_snapshot_on_empty_table_is_fine(clean_db: str) -> None:
    result = store.write_persons([], dsn=clean_db)
    assert result.aborted is False
    assert result.total == 0


def test_write_org_units_snapshot_replaces(clean_db: str) -> None:
    first = store.write_org_units(
        [
            _org_unit("root", parent_uuid=None, faculty_uuid=None, depth=0, collection_uuid=None),
            _org_unit("faculty-1", parent_uuid="root", depth=1),
            _org_unit("inst-1"),
        ],
        dsn=clean_db,
    )
    assert (first.total, first.aborted) == (3, False)

    second = store.write_org_units(
        [
            _org_unit("root", parent_uuid=None, faculty_uuid=None, depth=0, collection_uuid=None),
            _org_unit("faculty-1", parent_uuid="root", depth=1),
        ],
        dsn=clean_db,
    )
    assert (second.total, second.deleted) == (2, 1)


def test_write_org_units_is_idempotent(clean_db: str) -> None:
    rows = [_org_unit("inst-1"), _org_unit("inst-2")]
    store.write_org_units(rows, dsn=clean_db)
    again = store.write_org_units(rows, dsn=clean_db)
    assert (again.total, again.deleted, again.aborted) == (2, 0, False)


def test_reconcile_uzh_authors_keeps_only_cris_backed_authors(clean_db: str) -> None:
    """The rule is recomputed from what is stored, with author order preserved.

    This is what lets an eligibility change reach the existing corpus without a
    re-harvest: `authors` plus `author_authority_map` are a complete input.
    """
    _write(
        clean_db,
        [
            _row(
                "mixed",
                authors=["Cris, Clara", "Orcid, Otto", "Nobody, Nina", "Cris, Carl"],
                # Deliberately wrong on the way in -- the any-authority rule this
                # replaces would have written exactly this.
                uzh_authors=["Cris, Clara", "Orcid, Otto", "Cris, Carl"],
                author_authority_map={
                    "Cris, Clara": {"type": "cris", "id": "uuid-clara"},
                    "Orcid, Otto": {"type": "orcid", "id": "0000-0002-1825-0097"},
                    "Nobody, Nina": None,
                    "Cris, Carl": {"type": "cris", "id": "uuid-carl"},
                },
            ),
            _row(
                "orcid-only",
                authors=["Orcid, Olga"],
                uzh_authors=["Orcid, Olga"],
                author_authority_map={
                    "Orcid, Olga": {"type": "orcid", "id": "0000-0003-1111-1111"}
                },
            ),
        ],
        mode="full",
    )

    assert store.reconcile_uzh_authors(dsn=clean_db) == 2

    with db.connection(clean_db) as conn:
        rows = dict(conn.execute("SELECT id, uzh_authors FROM publication").fetchall())

    # Order follows `authors`, not the jsonb key order the map would have given.
    assert rows["mixed"] == ["Cris, Clara", "Cris, Carl"]
    assert rows["orcid-only"] == []


def test_reconcile_uzh_authors_resolves_an_orcid_against_the_person_mirror(
    clean_db: str,
) -> None:
    """An ORCID-typed authority still counts when it names a harvested Person.

    DSpace's marker is a statement about the *record* -- "this item is not linked
    to a local Person" -- not about the human. Where the mirror holds someone with
    that ORCID, they are a UZH researcher whatever the record says. `normalize`
    cannot see this (no database at fetch time), which is why it lands here.
    """
    store.write_persons(
        [
            {
                "uuid": "uuid-known",
                "display_name": "Known, Kim",
                "family_name": "Known",
                "given_name": "Kim",
                "orcid": "0000-0002-1825-0097",
                "handle": None,
                "url": None,
                "accessioned": None,
            }
        ],
        dsn=clean_db,
    )
    _write(
        clean_db,
        [
            _row(
                "resolves",
                authors=["Known, Kim", "Stranger, Sam"],
                uzh_authors=[],
                author_authority_map={
                    # Same person, referenced by ORCID rather than by Person UUID.
                    "Known, Kim": {"type": "orcid", "id": "0000-0002-1825-0097"},
                    # An ORCID the mirror has never seen: unknown affiliation.
                    "Stranger, Sam": {"type": "orcid", "id": "0000-0003-9999-9999"},
                },
            )
        ],
        mode="full",
    )

    store.reconcile_uzh_authors(dsn=clean_db)

    with db.connection(clean_db) as conn:
        eligible = conn.execute("SELECT uzh_authors FROM publication").fetchone()[0]

    assert eligible == ["Known, Kim"]


def test_reconcile_uzh_authors_is_idempotent(clean_db: str) -> None:
    """A second run changes nothing, because the UPDATE skips already-correct rows."""
    _write(clean_db, [_row("a"), _row("b")], mode="full")

    store.reconcile_uzh_authors(dsn=clean_db)
    assert store.reconcile_uzh_authors(dsn=clean_db) == 0


def test_reconcile_uzh_authors_leaves_authorless_publications_alone(clean_db: str) -> None:
    """No authors at all yields an empty array rather than a NULL or a crash."""
    _write(
        clean_db,
        [_row("empty", authors=[], uzh_authors=[], author_authority_map={})],
        mode="full",
    )

    store.reconcile_uzh_authors(dsn=clean_db)

    with db.connection(clean_db) as conn:
        assert conn.execute("SELECT uzh_authors FROM publication").fetchone()[0] == []
