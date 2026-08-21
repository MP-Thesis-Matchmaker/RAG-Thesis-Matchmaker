"""
Main entrypoint for the ZORA harvester.

Usage:
    python -m thesis_matchmaker.zora.harvest --mode full
    python -m thesis_matchmaker.zora.harvest --mode full --since 2024-07-01
    python -m thesis_matchmaker.zora.harvest --mode full --limit 5
    python -m thesis_matchmaker.zora.harvest --mode incremental

Outputs (all in the Postgres at DATABASE_URL):
    publication      — one row per publication
    harvest_state    — the incremental harvest watermark

A raw JSONL dump of each run is still written to data/raw/ so ingestion stays
reproducible without re-hitting ZORA.

full:        fetches every item currently in scope (optionally filtered by
             --since) and treats the result as an authoritative snapshot:
             publications missing from it are deleted. That is what reflects
             corrections and withdrawals upstream, which incremental mode cannot
             detect.
incremental: fetches only items accessioned since the last successful run (per
             harvest_state) and upserts them, deleting nothing. Cheap, runs
             daily.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime

from . import config, normalize, output_schema, state, store, zora_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Raw response cache
# ---------------------------------------------------------------------------


def write_raw_dump(raw_items: list[dict], mode: str) -> str:
    os.makedirs(config.RAW_DIR, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dump_path = os.path.join(config.RAW_DIR, f"{ts}_{mode}.jsonl")
    with open(dump_path, "w", encoding="utf-8") as f:
        for item in raw_items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return dump_path


# ---------------------------------------------------------------------------
# Main harvest logic
# ---------------------------------------------------------------------------


def run(mode: str, since_override: str | None = None, limit: int | None = None) -> int:
    """@return: process exit code (0 success, 1 aborted/failed)"""
    st = state.load_state()

    # Determine the "since" filter.
    # - incremental: always uses the watermark from harvest_state
    # - full: uses --since if provided, otherwise fetches everything
    if mode == "incremental":
        since = st.get("last_accessioned")
    else:
        since = since_override  # None means "fetch everything"

    logger.info("Starting %s harvest (since=%s, limit=%s)", mode, since, limit)

    if since is not None:
        logger.info(
            "NOTE: The dc.date.accessioned range query has not been tested "
            "against the live ZORA API. If zero results are returned with a "
            "since filter, the Solr query syntax may need adjusting."
        )

    client = zora_client.get_client()
    raw_items = []
    last_accessioned_seen = since

    for i, dso in enumerate(zora_client.iter_items(client, since=since)):
        if limit is not None and i >= limit:
            logger.info("Reached --limit %d, stopping", limit)
            break
        record = normalize.normalize_item(dso)
        if not record.get("handle"):
            logger.warning("Skipping item %d: no handle (uuid=%s)", i, record.get("uuid"))
            continue
        raw_items.append(record)
        if record.get("accessioned"):
            last_accessioned_seen = record["accessioned"]

    logger.info("Fetched %d publication records", len(raw_items))

    if mode == "incremental" and not raw_items:
        logger.info("No new publications since last run — nothing to do")
        # Re-persist the unchanged watermark/total purely to stamp
        # last_incremental_run_at: a run that legitimately found nothing still ran,
        # and the row should say so rather than looking like a skipped night.
        state.save_state(since, st.get("last_total_publications", 0), mode)
        return 0

    write_raw_dump(raw_items, mode)

    # to_output validates each record against ZoraPublication as it builds it, so
    # a malformed record fails here rather than after being written. accessioned
    # is carried alongside: it is not part of the published record shape, but the
    # row keeps it so the watermark can be recomputed from the data.
    rows = [
        {**output_schema.to_output(record), "accessioned": record.get("accessioned")}
        for record in raw_items
    ]

    # Upsert, prune (full mode only) and retention-check inside one transaction:
    # nothing is committed if the corpus shrank implausibly.
    result = store.write_harvest(
        rows,
        mode=mode,
        previous_total=st.get("last_total_publications", 0),
        min_retention_ratio=config.MIN_RETENTION_RATIO,
    )
    if result.aborted:
        logger.error("Nothing was written. Investigate before re-running.")
        return 1

    state.save_state(last_accessioned_seen, result.total, mode)
    logger.info(
        "Done. %d publications in the database (%d upserted, %d removed).",
        result.total,
        result.upserted,
        result.deleted,
    )
    return 0


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
        help="Max number of items to fetch. Useful for smoke testing.",
    )
    args = parser.parse_args()

    if args.since and args.mode == "incremental":
        logger.warning("--since is ignored in incremental mode (uses the harvest_state watermark)")

    try:
        exit_code = run(args.mode, since_override=args.since, limit=args.limit)
    except RuntimeError as exc:
        # Expected failure modes (auth, config) get a clean one-line message
        # in the Actions log instead of a full traceback. Anything else
        # (a real bug) still surfaces its traceback normally.
        logger.error(str(exc))
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
