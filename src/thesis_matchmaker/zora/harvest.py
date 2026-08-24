"""
Main entrypoint for the ZORA harvester.

Usage:
    python -m thesis_matchmaker.zora.harvest --mode full
    python -m thesis_matchmaker.zora.harvest --mode full --since 2024-07-01
    python -m thesis_matchmaker.zora.harvest --mode full --limit 5
    python -m thesis_matchmaker.zora.harvest --mode incremental
    python -m thesis_matchmaker.zora.harvest --no-persons --no-org-units
    python -m thesis_matchmaker.zora.harvest --mode full --from-dump data/raw/<ts>_full.jsonl

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
             only the validate/upsert half of the pipeline. Implies
             --no-persons --no-org-units: the whole point is not to touch the
             API, and one dump holds one kind of record.

An entity step that fails stops the run *before* the publication step. A full
publication harvest costs hours; if the API is refusing requests or the community
tree walk broke, finding out now beats finding out then.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator

from thesis_matchmaker import db

from . import config, entities, mapping, normalize, store, zora_client
from .raw_dump import read_raw_dump, write_raw_dump

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Re-exported: the dump helpers live in raw_dump.py now (entities.py needs them
# too, and this module imports entities.py), but this is where callers look.
__all__ = ["main", "read_raw_dump", "run", "write_raw_dump"]


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
        min_retention_ratio=config.MIN_RETENTION_RATIO,
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
    from_dump: str | None = None,
    persons: bool = True,
    org_units: bool = True,
    publications: bool = True,
) -> int:
    """Harvest the enabled capabilities, entities first. @return: exit code."""
    # Enforced here rather than only in argparse, because "--from-dump makes no API
    # request" has to hold for every caller of run(), not just the command line.
    # Scoped to a run that is actually replaying publications: with
    # --no-publications the dump is irrelevant and the mirrors can still refresh.
    if from_dump and publications and (persons or org_units):
        logger.warning("--from-dump implies --no-persons --no-org-units (no API request is made)")
        persons = org_units = False

    # One client for the whole run, built on first use: three steps that each
    # authenticated separately would pay for it three times, and a --from-dump
    # replay must not build one at all.
    client = None

    def client_factory():
        nonlocal client
        if client is None:
            client = zora_client.get_client()
        return client

    for enabled, label, step in (
        (persons, "person", entities.harvest_persons),
        (org_units, "org unit", entities.harvest_org_units),
    ):
        if not enabled:
            logger.info("Skipping the %s mirror", label)
            continue
        result = step(client_factory(), limit)
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

    if not publications:
        logger.info("Skipping the publication harvest")
        return 0

    return _harvest_publications(client_factory, mode, since_override, limit, from_dump)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
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
        default=None,
        metavar="PATH",
        help=(
            "Replay a raw publication dump from data/raw/ instead of fetching from "
            "ZORA. Its records were already normalized when they were written, so "
            "only the validate/upsert half of the pipeline re-runs -- no API token "
            "needed, and no second request for data already on disk. Use this after "
            "a run that fetched successfully but failed to write. Implies "
            "--no-persons --no-org-units."
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

    # `run()` applies the --from-dump implication itself, so it holds for every
    # caller rather than only for this one.
    if args.since and args.mode == "incremental":
        logger.warning("--since is ignored in incremental mode (uses the harvest_state watermark)")
    if args.since and args.from_dump:
        logger.warning("--since is ignored with --from-dump (the filter was applied at fetch time)")
    if args.no_publications and (args.since or args.from_dump):
        logger.warning("--since/--from-dump are ignored with --no-publications")

    try:
        exit_code = run(
            args.mode,
            since_override=args.since,
            limit=args.limit,
            from_dump=args.from_dump,
            persons=persons,
            org_units=org_units,
            publications=publications,
        )
    except RuntimeError as exc:
        # Expected failure modes (auth, config, a broken tree walk) get a clean
        # one-line message in the Actions log instead of a full traceback.
        # Anything else (a real bug) still surfaces its traceback normally.
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
