"""Schema application, as its own entry point.

`init-db` is the one command that needs nothing but the settings, the connection
pool and the DDL -- no embedding model, no retriever, no LLM client. It is
separated from `cli.py` because those two dependency sets are about to become
separate distributions, and because the cluster's init-db Job should run from an
image whose closure is pydantic plus psycopg rather than one that also carries
httpx, sentence-transformers and torch.

`cli.py` still exposes `init-db` as a subcommand and delegates here, so both
spellings do the same work.
"""

from __future__ import annotations

import argparse
import logging

from thesis_matchmaker import db, schema
from thesis_matchmaker.config import Settings, get_settings


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the flags `init-db` takes, wherever it is being spelled."""
    parser.add_argument(
        "--reset",
        action="store_true",
        help="DROP every table first, then recreate. Destroys all data. Needed after "
        "editing schema.sql, until the first harvest worth keeping exists.",
    )


def run(settings: Settings, *, reset: bool) -> None:
    """Apply the schema, reporting what changed."""
    try:
        result = schema.apply(settings.database_url, reset=reset)
    except schema.SchemaChangedError as exc:
        raise SystemExit(f"error: {exc}") from exc
    for name in result.dropped:
        print(f"dropped table {name}")
    if result.applied:
        print(f"schema applied ({result.fingerprint})")
    else:
        print(f"schema already up to date ({result.fingerprint})")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="thesis-matchmaker-init-db",
        description="Create the database schema (idempotent; safe to re-run).",
    )
    add_arguments(parser)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        run(get_settings(), reset=args.reset)
    finally:
        # The pool runs background worker threads; without this the process hangs
        # for the pool's stop timeout on the way out and complains.
        db.close_pools()


if __name__ == "__main__":
    main()
