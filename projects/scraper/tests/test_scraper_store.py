"""Tests for the scraper write path: postings, profiles and application processes.

These need a real Postgres, so they skip unless DATABASE_URL points at a database
whose name ends in _test (see tests/conftest.py).

The behaviour worth the most attention here is the *scope* of the prune. The harvester
can safely delete anything absent from a full run, because a ZORA harvest either sees
the whole corpus or has failed. A scrape run legitimately covers one source out of 103,
so an unscoped delete would silently drop the other 102 sources' records -- and would
look like a successful run while doing it.
"""

from __future__ import annotations

import pytest

from themis_scraper import store
from themis_shared import db


def _topic(topic_id: str, source_id: str, **overrides) -> dict:
    base = {
        "topic_id": topic_id,
        "title": f"Topic {topic_id}",
        "status": "open",
        "degree_level": "Bachelor, Master",
        "date_of_listing": None,
        "research_area": "Graph Learning",
        "supervisors": [{"name": "A. Example", "contact_url": "https://example.org/a"}],
        "topic_description": "Representation learning on graphs.",
        "source_link": f"https://example.org/{source_id}",
        "source_id": source_id,
        "scraped_at": "2026-08-04T11:18:40.961327+00:00",
    }
    base.update(overrides)
    return base


def _person(name: str, source_id: str) -> dict:
    return {
        "name": name,
        "email": f"{name.replace(' ', '.').lower()}@example.org",
        "role": "Professor",
        "research_interest": "Graphs",
        "research_field": "Informatics",
        "bio": None,
        "personal_website": None,
        "_profile_url": f"https://example.org/{source_id}/{name}",
        "source_id": source_id,
        "scraped_at": "2026-08-04T11:18:40.961327+00:00",
    }


def _process(source_id: str, degree: str = "Master") -> dict:
    return {
        "degree_level": degree,
        "process_description": "Email the coordinator.",
        "relevant_links": [{"url": "https://example.org/apply", "description": "Form"}],
        "source_url": f"https://example.org/{source_id}/apply",
        "source_id": source_id,
        "scraped_at": "2026-08-04T11:18:40.961327+00:00",
    }


def _dataset(
    *,
    topics=(),
    people=(),
    processes=(),
    faculty="Example Faculty",
    unit="Example Institute",
) -> dict:
    """The nesting `dataset.py` builds, which is what `store.write_dataset` flattens."""
    return {
        "faculties": {
            "EX": {
                "faculty": faculty,
                "faculty_code": "EX",
                "process": [],
                "units": {
                    "ex--1": {
                        "unit": unit,
                        "people": list(people),
                        "process": list(processes),
                        "concrete_topics": list(topics),
                        "groups": {},
                    }
                },
            }
        }
    }


def _truncate(dsn: str) -> None:
    with db.connection(dsn) as conn:
        conn.execute("TRUNCATE posting")
        conn.execute("TRUNCATE researcher_profile")
        conn.execute("TRUNCATE application_process")


@pytest.fixture()
def clean_db(dsn: str) -> str:
    """Empty before and after, like conftest.py's pg_store.

    Leaving rows behind is not a private mess: the indexer's source reader reads
    this table, so a leftover posting shows up in another module's assertions.
    See tests/integration/test_source_reader_contract.py.
    """
    _truncate(dsn)
    yield dsn
    _truncate(dsn)


def test_write_stores_all_three_record_kinds(clean_db: str) -> None:
    result = store.write_dataset(
        _dataset(
            topics=[_topic("t1", "src--1")],
            people=[_person("A Example", "src--1")],
            processes=[_process("src--1")],
        ),
        dsn=clean_db,
    )
    assert (result.postings, result.profiles, result.processes) == (1, 1, 1)
    assert result.total == 3
    assert store.posting_count(dsn=clean_db) == 1


def test_degree_levels_round_trip_as_an_array(clean_db: str) -> None:
    """ "Bachelor, Master" has to survive as two values, not one string."""
    store.write_dataset(_dataset(topics=[_topic("t1", "src--1")]), dsn=clean_db)
    with db.connection(clean_db) as conn:
        row = conn.execute("SELECT degree_levels FROM posting WHERE id = 't1'").fetchone()
    assert sorted(row[0]) == ["bachelor", "master"]


def test_degree_levels_are_queryable_by_overlap(clean_db: str) -> None:
    """The reason the column is text[] and not text."""
    store.write_dataset(
        _dataset(
            topics=[
                _topic("both", "src--1"),
                _topic("master_only", "src--1", degree_level="Master"),
            ]
        ),
        dsn=clean_db,
    )
    with db.connection(clean_db) as conn:
        bachelor = conn.execute(
            "SELECT id FROM posting WHERE degree_levels && %s ORDER BY id", (["bachelor"],)
        ).fetchall()
        master = conn.execute(
            "SELECT id FROM posting WHERE degree_levels && %s ORDER BY id", (["master"],)
        ).fetchall()
    assert [r[0] for r in bachelor] == ["both"]
    assert [r[0] for r in master] == ["both", "master_only"]


def test_rewriting_a_source_replaces_only_its_own_rows(clean_db: str) -> None:
    """The prune is scoped to the sources a run covered.

    This is the test that matters: an unscoped delete would pass every other
    assertion in this file and still destroy 102 sources' data in production.
    """
    store.write_dataset(
        _dataset(topics=[_topic("a1", "src--a"), _topic("a2", "src--a")]), dsn=clean_db
    )
    store.write_dataset(_dataset(topics=[_topic("b1", "src--b")]), dsn=clean_db)
    assert store.posting_count(dsn=clean_db) == 3

    # src--a comes back with one topic gone. src--b was not in this run at all.
    result = store.write_dataset(_dataset(topics=[_topic("a1", "src--a")]), dsn=clean_db)
    assert result.pruned == 1
    with db.connection(clean_db) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM posting ORDER BY id").fetchall()]
    assert ids == ["a1", "b1"]


def test_rerunning_the_same_source_is_idempotent(clean_db: str) -> None:
    data = _dataset(topics=[_topic("t1", "src--1")], people=[_person("A Example", "src--1")])
    store.write_dataset(data, dsn=clean_db)
    again = store.write_dataset(data, dsn=clean_db)
    assert again.pruned == 0
    assert store.posting_count(dsn=clean_db) == 1


def test_an_updated_topic_overwrites_rather_than_duplicating(clean_db: str) -> None:
    store.write_dataset(_dataset(topics=[_topic("t1", "src--1")]), dsn=clean_db)
    store.write_dataset(
        _dataset(topics=[_topic("t1", "src--1", title="Renamed", status="taken")]),
        dsn=clean_db,
    )
    with db.connection(clean_db) as conn:
        row = conn.execute("SELECT title, status FROM posting WHERE id = 't1'").fetchone()
    assert row == ("Renamed", "assigned")


def test_a_faculty_scope_process_has_no_department(clean_db: str) -> None:
    data = _dataset()
    data["faculties"]["EX"]["process"] = [_process("fac--1", degree="Bachelor")]
    store.write_dataset(data, dsn=clean_db)
    with db.connection(clean_db) as conn:
        row = conn.execute(
            "SELECT faculty, department, degree_level FROM application_process"
        ).fetchone()
    assert row == ("Example Faculty", None, "bachelor")


def test_group_records_are_pooled_into_their_unit(clean_db: str) -> None:
    """Chair groups are a second level of nesting; downstream they are the unit's."""
    data = _dataset(topics=[_topic("unit1", "src--1")])
    data["faculties"]["EX"]["units"]["ex--1"]["groups"] = {
        "g1": {"concrete_topics": [_topic("grp1", "src--1")], "people": [], "process": []}
    }
    store.write_dataset(data, dsn=clean_db)
    with db.connection(clean_db) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM posting ORDER BY id").fetchall()]
    assert ids == ["grp1", "unit1"]


def test_an_empty_dataset_writes_and_prunes_nothing(clean_db: str) -> None:
    """A run whose every source failed must not be read as "delete everything"."""
    store.write_dataset(_dataset(topics=[_topic("t1", "src--1")]), dsn=clean_db)
    result = store.write_dataset({"faculties": {}}, dsn=clean_db)
    assert (result.total, result.pruned) == (0, 0)
    assert store.posting_count(dsn=clean_db) == 1
