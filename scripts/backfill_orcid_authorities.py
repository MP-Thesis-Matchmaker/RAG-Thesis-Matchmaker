"""One-off: repair ORCID authorities inside `publication.author_authority_map`.

Why this exists
---------------
Two rounds of the same class of defect, both repaired here.

`zora/normalize.py::_typed_authority` first stripped DSpace's
`will be referenced::ORCID::` marker but stored whatever followed verbatim, so
four upstream corruptions reached Postgres -- full URLs, lowercase check digits,
a trailing full stop, and ids whose check digit had been stripped entirely. The
normalizer now handles all four; this repairs the rows harvested before it did.

It then also treated an *unmarked* value as a CRIS Person id unconditionally.
Where upstream omitted the marker on a plain ORCID (`20.500.14742/59205` does
exactly this), that produced a phantom UZH researcher: eligible for supervisor
recommendations while joining to nothing in `person`. The classifier now lets
shape decide unmarked values, and this re-types the rows written before it did.

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
from thesis_matchmaker.zora.normalize import _normalize_orcid, _typed_authority

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Both repair candidates: rows carrying an orcid-typed authority (ids to
# canonicalise) and rows whose cris-typed id is ORCID-shaped (entries to re-type).
# The jsonb path operator keeps this off a full 214k-row scan; `like_regex` accepts
# a lowercase check digit because canonicalisation has not happened yet.
_SELECT = """
SELECT id, author_authority_map
FROM publication
WHERE author_authority_map @? '$.* ? (@.type == "orcid")'
   OR author_authority_map @? '$.* ? (@.type == "cris" && @.id like_regex
        "^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9Xx]?$")'
ORDER BY id
"""

_UPDATE = "UPDATE publication SET author_authority_map = %(map)s WHERE id = %(id)s"


def _repaired(authority_map: dict) -> tuple[dict, list[tuple[str, str, str]]]:
    """The map with orcid ids canonicalised and mistyped cris entries re-typed.

    Two different repairs, because the two entry kinds carry different histories.

    An **orcid** entry has already had its marker stripped, so the stored id is a
    payload and nothing more; it is canonicalised, never re-typed. Re-running the
    classifier on it would be wrong precisely where it matters: a malformed payload
    such as `not-an-orcid` would fail the shape test and get demoted to `cris`,
    turning a known non-UZH author into a phantom researcher.

    A **cris** entry is the opposite: that branch never modified the value, so the
    stored id *is* the raw authority DSpace sent, and re-running `_typed_authority`
    on it reproduces exactly what a fresh harvest would now produce. That is what
    catches the unmarked bare ORCIDs the old fall-through filed as Person ids.
    Genuine UUIDs come back `cris` byte-for-byte -- the classifier tests a
    normalised throwaway and stores the original.
    """
    out: dict = {}
    changes: list[tuple[str, str, str]] = []
    for name, authority in authority_map.items():
        if not isinstance(authority, dict):
            out[name] = authority
            continue

        raw = authority.get("id") or ""
        if authority.get("type") == "orcid":
            fixed = _normalize_orcid(raw)
            out[name] = {**authority, "id": fixed}
            if fixed != raw:
                changes.append((name, raw, fixed))
            continue

        retyped = _typed_authority(raw) or authority
        out[name] = retyped
        if retyped != authority:
            changes.append(
                (name, f"{authority['type']} {raw}", f"{retyped['type']} {retyped['id']}")
            )
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
