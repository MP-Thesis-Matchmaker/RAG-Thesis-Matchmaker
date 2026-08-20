"""Schema migrations: versioned .sql files applied in filename order.

Deliberately small. The alternative was Alembic, but its value is autogenerating
migrations by diffing SQLAlchemy models, and this codebase has no ORM -- every
migration would be hand-written anyway, so Alembic would only contribute an
ordering mechanism and a second config surface.

Applying is idempotent, which is what makes it safe as a Kubernetes pre-rollout
Job and as `docker compose run --rm migrate`.
"""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

from thesis_matchmaker import db

logger = logging.getLogger(__name__)

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def available_migrations() -> list[tuple[str, str]]:
    """Every shipped migration as (version, sql), in filename order.

    Migrations are package data rather than a top-level directory so they
    survive `pip install` and are present in the container image.
    """
    root = resources.files("thesis_matchmaker.migrations")
    files = sorted((f for f in root.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
    return [(Path(f.name).stem, f.read_text(encoding="utf-8")) for f in files]


def applied_versions(dsn: str) -> set[str]:
    with db.connection(dsn) as conn:
        conn.execute(_TRACKING_TABLE)
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def run(dsn: str) -> list[str]:
    """Apply every unapplied migration. Returns the versions applied."""
    done = applied_versions(dsn)
    applied: list[str] = []
    for version, sql in available_migrations():
        if version in done:
            continue
        # One transaction per migration, so a failure half way through the set
        # leaves the schema at the last complete version rather than in between.
        with db.connection(dsn) as conn:
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        logger.info("applied migration %s", version)
        applied.append(version)
    return applied
