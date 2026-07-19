"""
Main entrypoint for the ZORA harvester.

Usage:
    python -m thesis_matchmaker.zora.harvest --mode full
    python -m thesis_matchmaker.zora.harvest --mode full --since 2024-07-01
    python -m thesis_matchmaker.zora.harvest --mode full --limit 5
    python -m thesis_matchmaker.zora.harvest --mode incremental

Outputs:
    data/publications.jsonl       — flat per-publication records
    data/state.json               — incremental harvest watermark

full:        fetches every item currently in scope (optionally filtered by
             --since) and REPLACES output files entirely. This is what
             correctly reflects publications that were removed or corrected
             upstream, which incremental mode cannot detect.
incremental: fetches only items accessioned since the last successful run
             (per data/state.json) and merges them into the existing output
             files. Cheap, runs daily.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime

from . import config, normalize, output_schema, state, zora_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------



def load_existing_publications(path: str) -> dict[str, dict]:
    """Load existing publications keyed by id for deduplication."""
    if not os.path.exists(path):
        return {}
    publications: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            publications[record["id"]] = record
    return publications


def write_jsonl(records: list[dict], path: str, sort_key: str) -> None:
    """Write records to a JSONL file, sorted by sort_key for stable diffs."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ordered = sorted(records, key=lambda r: r.get(sort_key, ""))
    with open(path, "w", encoding="utf-8") as f:
        for record in ordered:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


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
    # - incremental: always uses the watermark from state.json
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
        raw_items.append(record)
        if record.get("accessioned"):
            last_accessioned_seen = record["accessioned"]

    logger.info("Fetched %d publication records", len(raw_items))

    if mode == "incremental" and not raw_items:
        logger.info("No new publications since last run — nothing to do")
        state.save_state(since, st.get("last_total_publications", 0), mode)
        return 0

    write_raw_dump(raw_items, mode)

    # --- Primary output: flat publications (publications.jsonl) ---
    new_publications = [
        output_schema.to_output(record) for record in raw_items
    ]

    if mode == "incremental":
        existing_pubs = load_existing_publications(config.PUBLICATIONS_PATH)
        new_count = 0
        for pub in new_publications:
            if pub["id"] not in existing_pubs:
                existing_pubs[pub["id"]] = pub
                new_count += 1
        logger.info(
            "%d genuinely new publications (of %d returned by API)",
            new_count,
            len(new_publications),
        )
        final_publications = list(existing_pubs.values())
    else:
        final_publications = new_publications

    write_jsonl(final_publications, config.PUBLICATIONS_PATH, sort_key="id")

    # --- Safety check ---
    new_total_pubs = len(final_publications)
    previous_total_pubs = st.get("last_total_publications", 0)

    if (
        previous_total_pubs > 0
        and new_total_pubs
        < previous_total_pubs * config.MIN_RETENTION_RATIO
    ):
        logger.error(
            "Safety check failed: new total (%d) is less than %.0f%% of previous "
            "total (%d). Aborting without writing — this smells like an auth "
            "failure or scope misconfiguration, not a real data change.",
            new_total_pubs,
            config.MIN_RETENTION_RATIO * 100,
            previous_total_pubs,
        )
        return 1

    # --- Validate primary output ---
    valid_count, errors = output_schema.validate_publications_jsonl(config.PUBLICATIONS_PATH)
    if errors:
        logger.error("Schema validation failed on %d records:", len(errors))
        for e in errors[:20]:
            logger.error("  %s", e)
        return 1

    state.save_state(last_accessioned_seen, new_total_pubs, mode)
    logger.info(
        "Done. %d publications in %s, all schema-valid.",
        valid_count,
        config.PUBLICATIONS_PATH,
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
            "always uses the watermark from state.json."
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
        logger.warning("--since is ignored in incremental mode (uses state.json watermark)")

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
