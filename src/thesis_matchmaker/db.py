"""Postgres connection handling, shared by indexing and retrieval.

One pool per DSN, created lazily. Pools are keyed by DSN rather than kept in a
single module-level global, so tests can point at a scratch database without
reaching in to reset shared state.

There is no pgvector type adapter here on purpose. Vectors are only ever sent to
Postgres, never read back (`<=>` returns a float), so they go over the wire as a
text literal with an explicit `::vector` cast. That keeps the wire format
visible in the SQL and avoids depending on which adapter version registers which
Python type.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

# Everything that means "the database did not cooperate". PoolTimeout is not a
# psycopg.Error, so callers that want to degrade gracefully rather than crash
# need both -- hence one tuple to catch instead of two imports at every site.
DB_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error, PoolTimeout)

# Waiting the pool default of 30 s to discover that Postgres is unreachable
# makes the CLI feel broken. Fail in a few seconds and say so instead.
_CONNECT_TIMEOUT = 5.0

_pools: dict[tuple[str, int], ConnectionPool] = {}


def get_pool(dsn: str, max_size: int = 5) -> ConnectionPool:
    """Return the pool for this DSN, opening it on first use."""
    key = (dsn, max_size)
    pool = _pools.get(key)
    if pool is None:
        pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=max_size,
            timeout=_CONNECT_TIMEOUT,
            open=True,
        )
        _pools[key] = pool
    return pool


@contextmanager
def connection(dsn: str, max_size: int = 5) -> Iterator[psycopg.Connection]:
    """A pooled connection. Commits on clean exit, rolls back on exception."""
    with get_pool(dsn, max_size).connection() as conn:
        yield conn


def close_pools() -> None:
    """Close every open pool. For test teardown and clean process shutdown."""
    while _pools:
        _, pool = _pools.popitem()
        pool.close()


def to_vector_literal(vector: list[float]) -> str:
    """Render a vector in pgvector's text input format, e.g. '[0.1,-0.2]'."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"
