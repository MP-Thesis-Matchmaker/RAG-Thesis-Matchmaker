"""
Main entrypoint for the ZORA harvester.

Usage:
    python -m themis_zora.harvest --mode full
    python -m themis_zora.harvest --mode full --since 2024-07-01
    python -m themis_zora.harvest --mode full --limit 5
    python -m themis_zora.harvest --mode incremental
    python -m themis_zora.harvest --no-persons --no-org-units
    python -m themis_zora.harvest --mode full --from-dump data/raw/<ts>_full.jsonl
    python -m themis_zora.harvest --from-dump data/raw/<ts>_persons.jsonl

Outputs (all in the Postgres at DATABASE_URL):
    person           — one row per DSpace-CRIS Person entity (~2,000)
    org_unit         — one row per community of the UZH tree (~500)
    publication      — one row per publication
    harvest_state    — the incremental harvest watermark

A raw JSONL dump of each step is written to data/raw/ so ingestion stays
reproducible without re-hitting ZORA.

One run harvests three things, in this order: **persons, then org units, then
publications**. The entity mirrors come first because they are what a publication's
author authorities and owning collection resolve *against*, and because they are
cheap -- a few pages each against hours for the publications. They are steps of a
harvest rather than a job of their own, so there is no separate schedule and no
separate entrypoint; `--no-persons` / `--no-org-units` / `--no-publications` opt out
of any of the three.

Entity steps are always full snapshots (no watermark, nothing incremental about
2,000 rows). `--mode` and `--since` describe the publication step only.

full:        fetches every item currently in scope (optionally filtered by
             --since) and treats the result as an authoritative snapshot:
             publications missing from it are deleted. That is what reflects
             corrections and withdrawals upstream, which incremental mode cannot
             detect (dc.date.accessioned does not change on edit).
incremental: fetches only items accessioned since the last successful run (per
             harvest_state) and upserts them, deleting nothing. Cheap, runs
             daily.
--from-dump: skips ZORA entirely and replays a raw dump written by an earlier
             run. The records in it were already normalized, so this re-runs
             only the validate/upsert half of the pipeline. Repeatable, once per
             kind: which step a dump feeds is read off its filename (or stated
             with --dump-kind). One rule covers every combination --

                 if any dump is given, no API request is made; a step with a
                 dump replays from it, a step without one is skipped.

             So a lone <ts>_full.jsonl replays publications and touches neither
             mirror, exactly as before, while a run that died partway can hand
             back every dump it managed to write.

An entity step that fails stops the run *before* the publication step. A full
publication harvest costs hours; if the API is refusing requests or the community
tree walk broke, finding out now beats finding out then.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator

from themis_shared import db, schema

from . import (
    config,
    entities,
    index_trigger,
    mapping,
    normalize,
    raw_dump,
    store,
    zora_client,
)
from .raw_dump import read_raw_dump, write_raw_dump

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Re-exported: the dump helpers live in raw_dump.py now (entities.py needs them
# too, and this module imports entities.py), but this is where callers look.
__all__ = ["main", "read_raw_dump", "run", "write_raw_dump"]


def _as_dump_map(from_dump: str | dict[str, str] | None) -> dict[str, str]:
    """Normalize the `from_dump` argument into `{kind: path}`.

    A bare string is the single-dump form -- every caller before dumps became
    repeatable passed one, and a lone publication dump is still the common case --
    so its kind is read off the filename rather than assumed to be a publication
    dump. That way `run(from_dump="<ts>_persons.jsonl")` routes correctly instead
    of silently feeding persons to the publication validator.
    """
    if from_dump is None:
        return {}
    if isinstance(from_dump, str):
        return {raw_dump.dump_kind(from_dump): from_dump}
    return dict(from_dump)


def _because(replaying: bool) -> str:
    """Why a step was skipped, appended to the skip message.

    A skipped step is unremarkable when its own flag turned it off, and worth
    explaining when it was a consequence of replaying dumps -- that is the case
    somebody re-reads the log over.
    """
    return " (replaying dumps, and none feeds it)" if replaying else ""


def _publication_dump(dumps: dict[str, str]) -> str | None:
    """The publication dump among `dumps`, if any.

    `full` and `incremental` are two names for one step: which of them wrote the
    dump says how *that* run was invoked, not what this one should do with it.
    """
    for kind in raw_dump.PUBLICATION_KINDS:
        if kind in dumps:
            return dumps[kind]
    return None


def _harvest_publications(
    client_factory,
    mode: str,
    since_override: str | None,
    limit: int | None,
    from_dump: str | None,
) -> int:
    """The publication half of a run. @return: process exit code."""
    st = store.load_state()

    # Determine the "since" filter.
    # - incremental: always uses the watermark from harvest_state
    # - full: uses --since if provided, otherwise fetches everything
    since = st.last_accessioned if mode == "incremental" else since_override

    logger.info("Starting %s publication harvest (since=%s, limit=%s)", mode, since, limit)

    # Both branches yield the same thing -- normalized records -- which is what
    # lets the loop below, the limit, the handle guard and the watermark stay
    # written once. On the dump path normalization already happened, at fetch
    # time, and `since` was whatever filter that run used: it is not re-applied
    # here, so it only describes the watermark this run resumes from.
    if from_dump:
        logger.info("Replaying %s -- no ZORA request will be made", from_dump)
        records: Iterator[dict] = read_raw_dump(from_dump)
    else:
        if since is not None:
            logger.info(
                "NOTE: The dc.date.accessioned range query has not been tested "
                "against the live ZORA API. If zero results are returned with a "
                "since filter, the Solr query syntax may need adjusting."
            )
        records = (
            normalize.normalize_item(dso)
            for dso in zora_client.iter_items(client_factory(), since=since)
        )

    raw_items = []
    last_accessioned_seen = since

    for i, record in enumerate(records):
        if limit is not None and i >= limit:
            logger.info("Reached --limit %d, stopping", limit)
            break
        if not record.get("handle"):
            logger.warning("Skipping item %d: no handle (uuid=%s)", i, record.get("uuid"))
            continue
        raw_items.append(record)
        if record.get("accessioned"):
            last_accessioned_seen = record["accessioned"]

    logger.info(
        "%s %d publication records",
        "Replayed" if from_dump else "Fetched",
        len(raw_items),
    )

    if mode == "incremental" and not raw_items:
        logger.info("No new publications since last run — nothing to do")
        # Re-persist the unchanged watermark/total purely to stamp
        # last_incremental_run_at: a run that legitimately found nothing still ran,
        # and the row should say so rather than looking like a skipped night.
        store.save_state(since, st.last_total_publications, mode)
        return 0

    if from_dump:
        # The source file already *is* the raw cache. Writing a second copy under
        # a new timestamp would double ~50 MB on disk and make it ambiguous which
        # dump a later replay should use.
        logger.info("Not writing a raw dump: --from-dump replays an existing one")
    else:
        write_raw_dump(raw_items, mode)

    # to_publication validates each record against ZoraPublication as it builds it,
    # so a malformed record fails here rather than after being written. accessioned
    # is part of that model, so the validated row is exactly what gets stored.
    rows = [mapping.to_publication(record) for record in raw_items]

    # Upsert, prune (full mode only) and retention-check inside one transaction:
    # nothing is committed if the corpus shrank implausibly.
    result = store.write_harvest(
        rows,
        mode=mode,
        previous_total=st.last_total_publications,
        min_retention_ratio=config.ZoraSettings.ZORA_MIN_RETENTION_RATIO,
    )
    if result.aborted:
        logger.error("Nothing was written. Investigate before re-running.")
        return 1

    store.save_state(last_accessioned_seen, result.total, mode)
    logger.info(
        "Done. %d publications in the database (%d upserted, %d removed).",
        result.total,
        result.upserted,
        result.deleted,
    )
    return 0


def run(
    mode: str,
    since_override: str | None = None,
    limit: int | None = None,
    from_dump: str | dict[str, str] | None = None,
    persons: bool = True,
    org_units: bool = True,
    publications: bool = True,
) -> int:
    """Harvest the enabled capabilities, entities first. @return: exit code.

    @param from_dump: a `{kind: path}` mapping, or a bare path for the publication
        step -- the single-dump form every existing caller already passes, kept
        working rather than migrated.
    """
    # First, and before any request: every path below writes to Postgres -- a
    # --from-dump replay included -- so a database whose schema predates this code
    # has to be caught in one round-trip rather than by an UndefinedTable after the
    # fetching is already paid for.
    schema.require_current(config.get_settings().database_url)

    dumps = _as_dump_map(from_dump)

    # Enforced here rather than only in argparse, because "a dump means no API
    # request" has to hold for every caller of run(), not just the command line.
    # A step with a dump replays it; a step without one is skipped, which is what
    # keeps the promise absolute instead of per-flag. The old "--from-dump implies
    # --no-persons --no-org-units" rule is the publication-only case of this.
    # `replaying` is what the skip messages below use to say *why* a step was
    # skipped: "you disabled it" and "you are replaying dumps and gave none for it"
    # are different situations for whoever reads the log.
    replaying = bool(dumps)
    if replaying:
        persons = persons and raw_dump.PERSONS in dumps
        org_units = org_units and raw_dump.ORG_UNITS in dumps
        publications = publications and _publication_dump(dumps) is not None
        if not (persons or org_units or publications):
            logger.error(
                "Replaying %s, but none of them feeds an enabled step. Nothing to do.",
                ", ".join(sorted(dumps.values())),
            )
            return 1

    # One client for the whole run, built on first use: three steps that each
    # authenticated separately would pay for it three times, and a --from-dump
    # replay must not build one at all.
    client = None

    def client_factory():
        nonlocal client
        if client is None:
            client = zora_client.get_client()
        return client

    for enabled, kind, label, step in (
        (persons, raw_dump.PERSONS, "person", entities.harvest_persons),
        (org_units, raw_dump.ORG_UNITS, "org unit", entities.harvest_org_units),
    ):
        if not enabled:
            logger.info("Skipping the %s mirror%s", label, _because(replaying))
            continue
        dump = dumps.get(kind)
        # No client at all on the replay path, so "no API request" is enforced by
        # there being nothing to make one with.
        result = step(None if dump else client_factory(), limit, dump)
        if result.aborted:
            # The store refused the snapshot (see its empty-snapshot rail). Stop
            # here rather than spending hours on publications during a run that
            # already went wrong.
            logger.error("The %s mirror was not written. Aborting before publications.", label)
            return 1
        logger.info(
            "%s mirror done. %d rows (%d upserted, %d removed).",
            label.capitalize(),
            result.total,
            result.upserted,
            result.deleted,
        )

    if publications:
        exit_code = _harvest_publications(
            client_factory, mode, since_override, limit, _publication_dump(dumps)
        )
        if exit_code != 0:
            return exit_code
    else:
        logger.info("Skipping the publication harvest%s", _because(replaying))

    # Last, and unconditionally on success: eligibility is derived from columns
    # this run may have just rewritten, and from a `person` mirror it may have
    # just refreshed. Running it here rather than inside the publication step is
    # what makes `--no-publications` worth doing on its own -- a mirror refresh
    # alone can change which authors qualify across the whole existing corpus.
    store.reconcile_uzh_authors()
    return 0


def main() -> None:
    # prog is pinned rather than left to sys.argv[0]: the fallback is the script
    # filename, so `python -m themis_zora.harvest --help` announces itself as
    # "harvest.py" -- a name that appears nowhere a reader can act on. Both
    # spellings now print the console script, which is the declared entry point
    # and what the container runs.
    parser = argparse.ArgumentParser(prog="themis-zora-harvest", description=__doc__)
    parser.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "ISO date (e.g. 2024-07-01). Only items accessioned on or after "
            "this date are fetched. Only applies to full mode — incremental "
            "always uses the watermark from harvest_state."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of items to fetch per step. Useful for smoke testing.",
    )
    parser.add_argument(
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
    parser.add_argument(
        "--dump-kind",
        choices=raw_dump.KINDS,
        default=None,
        help=(
            "Which step the --from-dump file feeds, when its name does not say "
            "(a renamed or hand-copied dump). Only valid with a single --from-dump."
        ),
    )
    parser.add_argument(
        "--no-persons",
        action="store_true",
        help="Skip refreshing the `person` mirror.",
    )
    parser.add_argument(
        "--no-org-units",
        action="store_true",
        help="Skip refreshing the `org_unit` mirror.",
    )
    parser.add_argument(
        "--no-publications",
        action="store_true",
        help="Skip the publication harvest, refreshing only the entity mirrors.",
    )
    args = parser.parse_args()

    persons = not args.no_persons
    org_units = not args.no_org_units
    publications = not args.no_publications

    if not (persons or org_units or publications):
        parser.error("all three capabilities are disabled — nothing to harvest")

    dumps: dict[str, str] = {}
    paths = args.from_dump or []
    if args.dump_kind and len(paths) != 1:
        parser.error("--dump-kind names the kind of one dump; pass exactly one --from-dump")
    for path in paths:
        try:
            kind = args.dump_kind or raw_dump.dump_kind(path)
        except RuntimeError as exc:
            parser.error(str(exc))
        if kind in dumps:
            parser.error(f"two {kind} dumps given ({dumps[kind]} and {path}); a step replays one")
        dumps[kind] = path
    # A dump for a step its own flag switched off is a contradiction, not a
    # precedence question -- say so rather than quietly honouring one of the two.
    for kind, disabled, flag in (
        (raw_dump.PERSONS, args.no_persons, "--no-persons"),
        (raw_dump.ORG_UNITS, args.no_org_units, "--no-org-units"),
    ):
        if kind in dumps and disabled:
            parser.error(f"{dumps[kind]} is a {kind} dump but {flag} was given")
    if _publication_dump(dumps) and args.no_publications:
        parser.error("a publication dump was given but --no-publications was too")

    # `run()` applies the --from-dump implication itself, so it holds for every
    # caller rather than only for this one.
    if args.since and args.mode == "incremental":
        logger.warning("--since is ignored in incremental mode (uses the harvest_state watermark)")
    if args.since and args.from_dump:
        logger.warning("--since is ignored with --from-dump (the filter was applied at fetch time)")
    if args.no_publications and args.since:
        logger.warning("--since is ignored with --no-publications")

    try:
        exit_code = run(
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
            # asking the matcher to re-read a table this run did not touch is
            # work for nothing. This is what replaces the "index after each
            # harvest" CronJob that was never written -- a schedule can only
            # guess when a harvest finished, and this knows.
            index_trigger.trigger_index(config.get_settings())
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

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
