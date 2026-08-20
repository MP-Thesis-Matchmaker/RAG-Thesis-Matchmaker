"""Shared fixtures.

The Postgres fixtures skip themselves when DATABASE_URL is unset, which is what
keeps `pytest` runnable with no server, no container and no network. CI sets
DATABASE_URL against a pgvector service container, so the pgvector half of the
store contract really does run on every pull request.
"""

from __future__ import annotations

import os

import pytest

from thesis_matchmaker import db, migrate
from thesis_matchmaker.indexing.store import PgVectorStore


@pytest.fixture(scope="session")
def dsn() -> str:
    """A migrated Postgres to test against, or a skip if none is configured."""
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        pytest.skip("DATABASE_URL is not set; the pgvector tests need a real Postgres")
    migrate.run(configured)
    return configured


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
