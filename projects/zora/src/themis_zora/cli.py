"""Command line entry point.

One subcommand today, `harvest`, which fetches persons, org units and publications
from ZORA and writes them to Postgres. The shape is deliberate rather than
speculative: every member of this workspace is `themis-<member> <subcommand>`, so
the harvester is `themis-zora harvest` and not a console script that spells its own
subcommand in its name. See `themis_matcher.cli`, which this mirrors.

    themis-zora                       # what this instance is pointed at
    themis-zora harvest --mode full
    themis-zora harvest --mode full --since 2024-07-01
    themis-zora harvest --mode full --limit 5
    themis-zora harvest --mode incremental
    themis-zora harvest --no-persons --no-org-units
    themis-zora harvest --mode full --from-dump data/raw/<ts>_full.jsonl
    themis-zora harvest --from-dump data/raw/<ts>_persons.jsonl

`python -m themis_zora harvest ...` is the same program by its module name.

The harvest semantics -- what `--mode`, `--since` and `--from-dump` mean, and why
the three steps run in the order they do -- live in `harvest.py`'s docstring, next
to the code that implements them.
"""

from __future__ import annotations

import argparse
import logging
import sys

from themis_shared import db
from themis_zora import __version__, config, harvest, index_trigger, raw_dump, store

logger = logging.getLogger(__name__)


class _UsageError(Exception):
    """A flag combination argparse's own grammar cannot express.

    Raised by a handler and turned into `parser.error()` by `main`, so the
    handlers keep the uniform `(settings, args) -> int` signature while the
    message still arrives with the usage line and exit status 2 that a user
    expects from a bad invocation.
    """


def _collect_dumps(args: argparse.Namespace) -> dict[str, str]:
    """Map each `--from-dump` path to the harvest step it replays.

    Raises `_UsageError` for the combinations argparse cannot rule out: two dumps
    of one kind, `--dump-kind` against anything other than a single dump, or a
    dump for a step that a `--no-*` flag switched off. That last one is a
    contradiction rather than a precedence question, so it is refused rather than
    resolved silently in either direction.
    """
    dumps: dict[str, str] = {}
    paths = args.from_dump or []
    if args.dump_kind and len(paths) != 1:
        raise _UsageError("--dump-kind names the kind of one dump; pass exactly one --from-dump")
    for path in paths:
        try:
            kind = args.dump_kind or raw_dump.dump_kind(path)
        except RuntimeError as exc:
            raise _UsageError(str(exc)) from exc
        if kind in dumps:
            raise _UsageError(
                f"two {kind} dumps given ({dumps[kind]} and {path}); a step replays one"
            )
        dumps[kind] = path

    for kind, disabled, flag in (
        (raw_dump.PERSONS, args.no_persons, "--no-persons"),
        (raw_dump.ORG_UNITS, args.no_org_units, "--no-org-units"),
    ):
        if kind in dumps and disabled:
            raise _UsageError(f"{dumps[kind]} is a {kind} dump but {flag} was given")
    if harvest.publication_dump(dumps) and args.no_publications:
        raise _UsageError("a publication dump was given but --no-publications was too")
    return dumps


def _run_harvest(settings: config.ZoraSettings, args: argparse.Namespace) -> int:
    persons = not args.no_persons
    org_units = not args.no_org_units
    publications = not args.no_publications

    if not (persons or org_units or publications):
        raise _UsageError("all three capabilities are disabled — nothing to harvest")

    dumps = _collect_dumps(args)

    # `run()` applies the --from-dump implication itself, so it holds for every
    # caller rather than only for this one.
    if args.since and args.mode == "incremental":
        logger.warning("--since is ignored in incremental mode (uses the harvest_state watermark)")
    if args.since and args.from_dump:
        logger.warning("--since is ignored with --from-dump (the filter was applied at fetch time)")
    if args.no_publications and args.since:
        logger.warning("--since is ignored with --no-publications")

    exit_code = harvest.run(
        args.mode,
        since_override=args.since,
        limit=args.limit,
        from_dump=dumps,
        persons=persons,
        org_units=org_units,
        publications=publications,
    )
    if exit_code == 0 and publications:
        # Only on success, and only when publications were actually written:
        # asking the matcher to re-read a table this run did not touch is work
        # for nothing. This is what replaces the "index after each harvest"
        # CronJob that was never written -- a schedule can only guess when a
        # harvest finished, and this knows.
        index_trigger.trigger_index(settings)
    return exit_code


def _redacted_dsn(dsn: str) -> str:
    """The DSN without its password, safe to print. Mirrors themis_matcher.cli."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(dsn)
    if parts.password is None:
        return dsn
    netloc = f"{parts.username or ''}:***@{parts.hostname or ''}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _token_status(settings: config.ZoraSettings) -> str:
    """Whether a token is configured, without printing it and without raising.

    `api_token` is a property that raises when nothing is set, which is right for
    a harvest and wrong for a status line -- the whole point of the summary is to
    be readable on a pod that is misconfigured.
    """
    try:
        _ = settings.api_token  # noqa: F841 -- accessed for the exception, not the value
    except RuntimeError:
        return "not set - a harvest that reaches ZORA will refuse to start"
    return "set"


def _watermark(settings: config.ZoraSettings) -> str:
    """Where the next incremental run would resume, without crashing on a dead database."""
    try:
        state = store.load_state(settings.database_url)
    except db.DB_ERRORS as exc:
        return f"unknown - database unreachable ({exc.__class__.__name__})"
    if state.last_accessioned is None:
        return "none yet - the next incremental run harvests everything"
    return f"accessioned >= {state.last_accessioned} ({state.last_total_publications} publications)"


def _dispatch(settings: config.ZoraSettings, args: argparse.Namespace) -> int:
    if args.command == "harvest":
        return _run_harvest(settings, args)

    # No subcommand: say what this instance is pointed at. The same shape as
    # themis_matcher.cli's else-branch, and the reason a bare run is not an
    # error -- in a container it is the fastest way to see which database and
    # which matcher a pod actually got.
    print("themis-zora")
    print(f"  dspace api:   {settings.ZORA_DSPACE_API_URL}")
    print(f"  api token:    {_token_status(settings)}")
    print(f"  database:     {_redacted_dsn(settings.database_url)}")
    print(f"  matcher:      {settings.matcher_base_url or 'unset - index trigger is skipped'}")
    print(f"  watermark:    {_watermark(settings)}")
    print("try: themis-zora harvest --mode incremental")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="themis-zora",
        description="ZORA harvester: publications, persons and org units into Postgres.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    harvest_parser = subparsers.add_parser(
        "harvest",
        help="fetch persons, org units and publications, and write them to Postgres",
        description=harvest.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    harvest_parser.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    harvest_parser.add_argument(
        "--since",
        default=None,
        help=(
            "ISO date (e.g. 2024-07-01). Only items accessioned on or after "
            "this date are fetched. Only applies to full mode — incremental "
            "always uses the watermark from harvest_state."
        ),
    )
    harvest_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of items to fetch per step. Useful for smoke testing.",
    )
    harvest_parser.add_argument(
        "--from-dump",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "Replay a raw dump from data/raw/ instead of fetching from ZORA. Its "
            "records were already normalized when they were written, so only the "
            "validate/upsert half of the pipeline re-runs -- no API token needed, "
            "and no second request for data already on disk. Use this after a run "
            "that fetched successfully but failed to write. Repeatable, once per "
            "kind; the step a dump feeds comes from its filename. Any dump at all "
            "means no API request is made, so steps without one are skipped."
        ),
    )
    harvest_parser.add_argument(
        "--dump-kind",
        choices=raw_dump.KINDS,
        default=None,
        help=(
            "Which step the --from-dump file feeds, when its name does not say "
            "(a renamed or hand-copied dump). Only valid with a single --from-dump."
        ),
    )
    harvest_parser.add_argument(
        "--no-persons",
        action="store_true",
        help="Skip refreshing the `person` mirror.",
    )
    harvest_parser.add_argument(
        "--no-org-units",
        action="store_true",
        help="Skip refreshing the `org_unit` mirror.",
    )
    harvest_parser.add_argument(
        "--no-publications",
        action="store_true",
        help="Skip the publication harvest, refreshing only the entity mirrors.",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = config.get_settings()

    try:
        exit_code = _dispatch(settings, args)
    except _UsageError as exc:
        # On the subparser, so the usage line shown is the one the user got wrong.
        subparsers.choices.get(args.command, parser).error(str(exc))
    except (RuntimeError, *db.DB_ERRORS) as exc:
        # Expected failure modes (auth, config, a broken tree walk, an unreachable
        # or out-of-date database) get a clean one-line message in the Actions log
        # instead of a full traceback. Anything else (a real bug) still surfaces
        # its traceback normally. psycopg errors are in the list because they are
        # operator conditions too: `raw_dump.py` already translates OSError for the
        # same reason, but the store layer has no such translation of its own.
        logger.error(str(exc))
        exit_code = 1
    finally:
        # The pool runs background worker threads; without this the process
        # hangs for the pool's stop timeout on the way out and complains. In
        # `finally` rather than after the except, because a crash mid-harvest is
        # exactly when the pool is open -- and that is the path that hung.
        db.close_pools()

    # Explicitly, unlike themis_matcher.cli, which returns None and lets the
    # interpreter exit 0. A CronJob decides whether a harvest succeeded from this
    # status alone: swallow it and a run that wrote nothing reports green.
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
