"""Fixtures every member's tests may use.

This file sits at the repository root so pytest loads it for all six test
directories, and it imports nothing but `themis_shared`. That restriction is
load-bearing: if it reached into `themis_matcher`, then `uv sync --package
themis-zora && pytest projects/zora/tests` would fail while collecting this
file, and the whole point of the split -- each member's tests running on that
member's own dependency closure -- would be lost. Matcher-specific fixtures live
in projects/matcher/tests/conftest.py.

The Postgres fixture skips itself when DATABASE_URL is unset, which is what keeps
`pytest` runnable with no server, no container and no network. CI sets
DATABASE_URL against a pgvector service container, so the pgvector half of the
store contract really does run on every pull request.

It is also destructive -- fixtures built on it TRUNCATE between tests -- so it
refuses to run against a database whose name does not end in `_test`. Without
that guard, `pytest` in a shell that still has a development DATABASE_URL
exported silently wipes whatever is in it, which is merely annoying for a
rebuildable hash-fake index and expensive for a multi-hour ZORA harvest.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

from themis_shared import db, schema

_ALLOW_DESTRUCTIVE = "THEMIS_ALLOW_DESTRUCTIVE_TESTS"


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


@pytest.fixture(scope="session", autouse=True)
def _close_pools() -> None:
    """Release pooled connections at the end of the session."""
    yield
    db.close_pools()
