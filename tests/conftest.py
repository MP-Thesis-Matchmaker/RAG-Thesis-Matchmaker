"""Shared fixtures.

The Postgres fixtures skip themselves when DATABASE_URL is unset, which is what
keeps `pytest` runnable with no server, no container and no network. CI sets
DATABASE_URL against a pgvector service container, so the pgvector half of the
store contract really does run on every pull request.

They are also destructive -- they TRUNCATE between tests -- so they refuse to run
against a database whose name does not end in `_test`. Without that guard,
`pytest` in a shell that still has a development DATABASE_URL exported silently
wipes whatever is in it, which is merely annoying for a rebuildable hash-fake
index and expensive for a multi-hour ZORA harvest.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

from thesis_matchmaker import db, llm, schema
from thesis_matchmaker.indexing.store import PgVectorStore

_ALLOW_DESTRUCTIVE = "THESIS_MATCHMAKER_ALLOW_DESTRUCTIVE_TESTS"


@pytest.fixture(scope="session")
def dsn() -> str:
    """A schema-applied Postgres to test against, or a skip if none is configured."""
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is not set; the pgvector tests need a real Postgres")

    database = urlsplit(configured).path.lstrip("/")
    if not database.endswith("_test") and not os.environ.get(_ALLOW_DESTRUCTIVE):
        pytest.skip(
            f"refusing to run destructive tests against database {database!r}: these "
            "fixtures TRUNCATE between tests. Point DATABASE_URL at a database whose "
            f"name ends in '_test', or set {_ALLOW_DESTRUCTIVE}=1 to override."
        )

    schema.apply(configured)
    return configured


@pytest.fixture(autouse=True)
def _forget_endpoint_capabilities() -> None:
    """Clear the learned "this endpoint refuses that field" cache between tests.

    It is module-level on purpose (one endpoint, many clients), which makes it
    leak across tests: one test teaching it that "m" refuses temperature would
    silently change what the next test's first request even contains.
    """
    llm._UNSUPPORTED_FIELDS.clear()
    yield
    llm._UNSUPPORTED_FIELDS.clear()


@pytest.fixture(scope="session", autouse=True)
def _close_pools() -> None:
    """Release pooled connections at the end of the session."""
    yield
    db.close_pools()


@pytest.fixture()
def pg_store(dsn: str) -> PgVectorStore:
    """An empty PgVectorStore. Emptied again afterwards so tests do not leak rows."""
    store = PgVectorStore(dsn=dsn)
    store.clear()
    yield store
    store.clear()
