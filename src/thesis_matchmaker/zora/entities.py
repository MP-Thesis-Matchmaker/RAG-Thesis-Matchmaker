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
from .raw_dump import write_raw_dump

logger = logging.getLogger(__name__)

PERSONS = "persons"
ORG_UNITS = "orgunits"


def harvest_persons(client, limit: int | None = None) -> store.EntityWriteResult:
    """Snapshot the DSpace-CRIS Person entities into the `person` table."""
    records = []
    for i, dso in enumerate(zora_client.iter_persons(client)):
        if limit is not None and i >= limit:
            logger.info("Reached --limit %d, stopping the person fetch", limit)
            break
        records.append(normalize.normalize_person(dso))

    logger.info("Fetched %d person records", len(records))
    write_raw_dump(records, PERSONS)
    return store.write_persons([mapping.to_person(record) for record in records])


def harvest_org_units(client, limit: int | None = None) -> store.EntityWriteResult:
    """Snapshot the UZH community tree into the `org_unit` table."""
    records = []
    walk = zora_client.iter_org_tree(client)
    for i, (community, parent_uuid, depth, faculty_uuid, collections) in enumerate(walk):
        if limit is not None and i >= limit:
            logger.info("Reached --limit %d, stopping the org-unit walk", limit)
            break
        records.append(
            normalize.normalize_org_unit(community, parent_uuid, depth, faculty_uuid, collections)
        )

    logger.info("Fetched %d org-unit records", len(records))
    write_raw_dump(records, ORG_UNITS)
    return store.write_org_units([mapping.to_org_unit(record) for record in records])
