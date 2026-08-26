"""Fixtures for the matcher's own tests.

These two were in the repository-root conftest until the workspace split, where
they could not stay: both need `themis_matcher`, and the root conftest is loaded
for zora's and the scraper's tests too, neither of which installs the matcher.

Scoping `_forget_endpoint_capabilities` here is a small correction rather than
just a relocation. It is autouse, so at the root it was clearing the matcher's
LLM cache before and after every zora and scraper test as well -- work those
tests neither needed nor knew about.

`dsn` comes from the root conftest.
"""

from __future__ import annotations

import pytest

from themis_matcher import llm
from themis_matcher.indexing.store import PgVectorStore


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


@pytest.fixture()
def pg_store(dsn: str) -> PgVectorStore:
    """An empty PgVectorStore. Emptied again afterwards so tests do not leak rows."""
    store = PgVectorStore(dsn=dsn)
    store.clear()
    yield store
    store.clear()
