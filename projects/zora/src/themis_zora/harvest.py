"""
The ZORA harvest itself. Invoked as `themis-zora harvest` -- the argument parsing
and the flag validation live in cli.py; what a harvest *is* lives here.

This docstring is what `themis-zora harvest --help` prints, so it is written for
someone about to run one.

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

import logging
from collections.abc import Iterator

from themis_shared import schema

from . import (
    config,
    entities,
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
__all__ = ["publication_dump", "read_raw_dump", "run", "write_raw_dump"]


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


def publication_dump(dumps: dict[str, str]) -> str | None:
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
        publications = publications and publication_dump(dumps) is not None
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
            client_factory, mode, since_override, limit, publication_dump(dumps)
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
