"""One-off: re-normalize ORCID ids inside `publication.author_authority_map`.

Why this exists
---------------
`zora/normalize.py::_typed_authority` stripped DSpace's
`will be referenced::ORCID::` marker but stored whatever followed verbatim, so
four upstream corruptions reached Postgres -- full URLs, lowercase check digits,
a trailing full stop, and ids whose check digit had been stripped entirely. The
normalizer now handles all four; this repairs the rows harvested before it did.

Why a script and not SQL
------------------------
It calls `normalize._normalize_orcid`, the same function the harvester uses. A
hand-written SQL equivalent would have to re-implement the ISO 7064 checksum
rule, and any drift between the two would leave backfilled rows disagreeing with
freshly harvested ones. Sharing the function makes them identical by
construction.

Why a script and not a re-harvest
---------------------------------
The corruption is upstream and deterministic: a fresh harvest returns the same
bad values and fixes them with the same code. For 18 rows that is two hours of
API traffic to reach a result this produces in a second.

On invariant 1
--------------
`publication` is the harvester's table and nothing in the serving path may write
to it. This is an operator action in the same category as `init-db --reset` --
run by hand, never imported, and not a new write path. It changes no schema, so
no fingerprint changes and no reset is implied.

Idempotent: a canonical id normalizes to itself, so a second run reports 0
changes. Every edit is printed, so 20 corrections are auditable rather than a
silent bulk update.

Usage:
    python -m scripts.backfill_orcid_authorities [--apply]

Without --apply it is a dry run and writes nothing.
"""

from __future__ import annotations

import argparse
import logging

from psycopg.types.json import Jsonb

from thesis_matchmaker import db
from thesis_matchmaker.config import get_settings
from thesis_matchmaker.zora.normalize import _normalize_orcid

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Only rows that actually carry an orcid-typed authority; the jsonb path operator
# keeps this off a full 214k-row scan.
_SELECT = """
SELECT id, author_authority_map
FROM publication
WHERE author_authority_map @? '$.* ? (@.type == "orcid")'
ORDER BY id
"""

_UPDATE = "UPDATE publication SET author_authority_map = %(map)s WHERE id = %(id)s"


def _repaired(authority_map: dict) -> tuple[dict, list[tuple[str, str, str]]]:
    """The map with every orcid id canonicalised, plus a log of what changed.

    CRIS entries are copied through untouched: their ids are lowercase-hex UUIDs
    joining to `person.uuid`, and the ORCID pipeline uppercases.
    """
    out: dict = {}
    changes: list[tuple[str, str, str]] = []
    for name, authority in authority_map.items():
        if not isinstance(authority, dict) or authority.get("type") != "orcid":
            out[name] = authority
            continue
        raw = authority.get("id") or ""
        fixed = _normalize_orcid(raw)
        out[name] = {**authority, "id": fixed}
        if fixed != raw:
            changes.append((name, raw, fixed))
    return out, changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the repairs. Without it this is a dry run.",
    )
    args = parser.parse_args()
    dsn = get_settings().database_url

    scanned = 0
    edits: list[tuple[str, str, str, str]] = []
    updates: list[dict] = []

    with db.connection(dsn) as conn:
        for pub_id, authority_map in conn.execute(_SELECT).fetchall():
            scanned += 1
            repaired, changes = _repaired(authority_map or {})
            if not changes:
                continue
            updates.append({"id": pub_id, "map": Jsonb(repaired)})
            edits.extend((pub_id, name, raw, fixed) for name, raw, fixed in changes)

        for pub_id, name, raw, fixed in edits:
            logger.info("%s  %s\n    %s\n -> %s", pub_id, name, raw, fixed)

        logger.info(
            "%d publication(s) scanned, %d entr%s to repair across %d row(s)",
            scanned,
            len(edits),
            "y" if len(edits) == 1 else "ies",
            len(updates),
        )

        if not updates:
            logger.info("Nothing to do.")
        elif not args.apply:
            logger.info("Dry run -- re-run with --apply to write these.")
        else:
            with conn.transaction():
                conn.cursor().executemany(_UPDATE, updates)
            logger.info("Wrote %d row(s).", len(updates))

    db.close_pools()


if __name__ == "__main__":
    main()
