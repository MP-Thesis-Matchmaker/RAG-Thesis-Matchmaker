"""Harvest steps for the two ZORA entity mirrors: `person` and `org_unit`.

Called by `harvest.py` at the start of every harvest run, before publications.
**Deliberately not runnable**: no argparse, no `main`, no `__main__` guard. These
mirrors are not a job of their own -- they are the part of a harvest that resolves
who a publication's authors are and which org unit it belongs to, so they refresh
with the harvest rather than on a schedule of their own.

Every run is a full snapshot: fetch everything, upsert, prune what disappeared
upstream. There is no watermark and no incremental mode, because at ~2,000 persons
and ~500 org units the whole population costs a few pages of requests -- the
machinery `harvest.py` needs for 215k publications would be pure overhead here. The
one safety rail lives in `store.py`: an empty snapshot never overwrites a non-empty
table.
"""

from __future__ import annotations

import logging

from . import mapping, normalize, store, zora_client
from .raw_dump import ORG_UNITS, PERSONS, read_raw_dump, write_raw_dump

logger = logging.getLogger(__name__)


def _collect(source, kind: str, limit: int | None, from_dump: str | None) -> list[dict]:
    """Normalized records for one mirror, from the API or from a dump.

    Both halves of every step reduce to this: a stream of normalized records,
    capped by `limit`, cached to `data/raw/` unless it came from there. Written
    once so the fetch and the replay cannot drift into behaving differently --
    `--limit` in particular applies to both, which is what makes a replay usable
    as a smoke test.

    No second dump is written on the replay path: the source file already *is*
    the cache, and copying it under a new timestamp would only make it ambiguous
    which one a later replay should use. Same reasoning as the publication step.
    """
    records = []
    for i, record in enumerate(source):
        if limit is not None and i >= limit:
            logger.info("Reached --limit %d, stopping the %s step", limit, kind)
            break
        records.append(record)

    label = "person" if kind == PERSONS else "org unit"
    logger.info("%s %d %s records", "Replayed" if from_dump else "Fetched", len(records), label)
    if from_dump:
        logger.info("Not writing a raw dump: this run replayed an existing one")
    else:
        write_raw_dump(records, kind)
    return records


def harvest_persons(
    client, limit: int | None = None, from_dump: str | None = None
) -> store.EntityWriteResult:
    """Snapshot the DSpace-CRIS Person entities into the `person` table.

    @param from_dump: replay this dump instead of calling ZORA. `client` is then
        unused and callers pass None, so "no API request" is structural rather
        than a promise.
    """
    if from_dump:
        logger.info("Replaying %s -- no ZORA request will be made", from_dump)
        source = read_raw_dump(from_dump)
    else:
        source = (normalize.normalize_person(dso) for dso in zora_client.iter_persons(client))

    records = _collect(source, PERSONS, limit, from_dump)
    return store.write_persons([mapping.to_person(record) for record in records])


def harvest_org_units(
    client, limit: int | None = None, from_dump: str | None = None
) -> store.EntityWriteResult:
    """Snapshot the UZH community tree into the `org_unit` table.

    @param from_dump: as for `harvest_persons`.
    """
    if from_dump:
        logger.info("Replaying %s -- no ZORA request will be made", from_dump)
        source = read_raw_dump(from_dump)
    else:
        walk = zora_client.iter_org_tree(client)
        source = (
            normalize.normalize_org_unit(community, parent, depth, faculty, collections)
            for community, parent, depth, faculty, collections in walk
        )

    records = _collect(source, ORG_UNITS, limit, from_dump)
    return store.write_org_units([mapping.to_org_unit(record) for record in records])
