"""What the writers write is what the indexer reads.

These four tests are the only ones that span three packages at once: `zora` and
`scraper` own the write paths, `indexing` owns `PostgresSourceReader`, and the
table definitions they agree on live in `schema.sql`. That makes the contract
nobody's property in particular -- which is why it lives here rather than in
either writer's own suite, where it would force that package to depend on the
matcher just to be tested.

The helpers are deliberately duplicated from tests/test_zora_store.py and
tests/test_scraper_store.py rather than imported. Importing them would put an
edge back between the suites that this file exists to remove.

Needs a real Postgres, so it skips unless DATABASE_URL names a database ending
in _test (see tests/conftest.py).
"""

from __future__ import annotations

import pytest

from themis_matcher.indexing.sources import PostgresSourceReader
from themis_scraper import store as scraper_store
from themis_shared import db
from themis_shared.contracts import AuthorAuthority
from themis_zora import store as zora_store

_RATIO = 0.5


# --- publication side, from tests/test_zora_store.py ---


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
def clean_publications(dsn: str) -> str:
    with db.connection(dsn) as conn:
        conn.execute("TRUNCATE publication")
        conn.execute("TRUNCATE person")
        conn.execute("TRUNCATE org_unit")
        conn.execute("DELETE FROM harvest_state")
    return dsn


def _write(dsn: str, rows: list[dict], *, mode: str, previous_total: int = 0):
    return zora_store.write_harvest(
        rows, mode=mode, previous_total=previous_total, min_retention_ratio=_RATIO, dsn=dsn
    )


# --- posting side, from tests/test_scraper_store.py ---


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


def _dataset(*, topics=(), faculty="Example Faculty", unit="Example Institute") -> dict:
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
                        "people": [],
                        "process": [],
                        "concrete_topics": list(topics),
                        "groups": {},
                    }
                },
            }
        }
    }


def _truncate_postings(dsn: str) -> None:
    with db.connection(dsn) as conn:
        conn.execute("TRUNCATE posting")
        conn.execute("TRUNCATE researcher_profile")
        conn.execute("TRUNCATE application_process")


@pytest.fixture()
def clean_postings(dsn: str) -> str:
    """Empty before and after: a leftover posting shows up in another module's asserts."""
    _truncate_postings(dsn)
    yield dsn
    _truncate_postings(dsn)


# --- the contract ---


def test_postgres_source_reader_returns_what_the_harvester_wrote(clean_publications: str) -> None:
    """The read side of the same table, which is what the indexer consumes."""
    _write(clean_publications, [_row("zora:1"), _row("zora:2")], mode="full")
    reader = PostgresSourceReader(dsn=clean_publications)
    records = list(reader.publications())
    assert [r.id for r in records] == ["zora:1", "zora:2"]
    assert records[0].uzh_authors == ["A. Müller"]
    assert records[0].author_authority_map == {
        "A. Müller": AuthorAuthority(type="cris", id="uuid-1"),
        "X. External": None,
    }
    assert records[0].keywords == ["retrieval", "german"]
    assert reader.invalid_records == 0


def test_postgres_source_reader_reads_publications_without_a_uzh_author(
    clean_publications: str,
) -> None:
    """Indexing takes no position on UZH authorship; retrieval decides.

    This asserted the opposite until 2026-08-25. Filtering in SQL made
    `MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR` unflippable in practice: turning it off would
    return nothing extra until someone re-embedded the corpus. Reading everything
    costs ~2.3x the embedding work once and makes the rule a setting.
    """
    _write(
        clean_publications,
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
    reader = PostgresSourceReader(dsn=clean_publications)
    records = list(reader.publications())

    assert [r.id for r in records] == ["zora:1", "zora:2"]
    # Read faithfully, so retrieval can tell the two apart: the empty list is what
    # `documents.py` turns into `has_uzh_author: False`, and what the retriever's
    # fallback keys on when it credits `authors` instead.
    assert records[1].uzh_authors == []
    assert records[1].authors == ["X. External", "Y. Elsewhere"]


def test_supervisors_round_trip_with_their_profile_links(clean_postings: str) -> None:
    """contact_url is one of three keys a profile link hides behind."""
    scraper_store.write_dataset(_dataset(topics=[_topic("t1", "src--1")]), dsn=clean_postings)
    postings = list(PostgresSourceReader(clean_postings).postings())
    assert len(postings) == 1
    assert [s.name for s in postings[0].supervisors] == ["A. Example"]
    assert postings[0].supervisors[0].profile_url == "https://example.org/a"
    assert postings[0].supervisors[0].email is None


def test_every_posting_is_offered_to_the_indexer_regardless_of_status(clean_postings: str) -> None:
    """Availability is decided at retrieval now, not here.

    This reader used to drop assigned and private topics so they were never embedded.
    They are embedded now, carrying `is_available: False`, and
    `retrieval_require_available_posting` -- on by default -- is what keeps them out of
    results. The point of moving it is that flipping that setting needs no re-index.
    """
    scraper_store.write_dataset(
        _dataset(
            topics=[
                _topic("open1", "src--1"),
                _topic("taken1", "src--1", status="taken"),
                _topic("private1", "src--1", status="private"),
                _topic("silent1", "src--1", status=None),
            ]
        ),
        dsn=clean_postings,
    )
    assert scraper_store.posting_count(dsn=clean_postings) == 4
    offered = {p.id for p in PostgresSourceReader(clean_postings).postings()}
    assert offered == {"open1", "taken1", "private1", "silent1"}
