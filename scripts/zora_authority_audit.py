"""
Measure the candidate `uzh_authors` eligibility rules against the harvested
corpus, so the rule is chosen from data rather than from the estimates in
zora/README.md's Known-gaps table.

Reads Postgres only -- no ZORA API, no token. Writes nothing to any source
table; the one thing it creates is a session-scoped TEMP table, dropped before
it exits, so invariant 1 ("ingestion owns all writes") holds.

Requires a database with the current schema.sql applied AND a completed
`harvest --mode full`: the questions below are about `author_authority_map`,
`owning_collection_uuid` and the `person` mirror, none of which exist in a
pre-2026-08-24 database.

Usage:
    themis-init-db --reset          # if the schema is stale
    python -m themis_zora.harvest --mode full
    python -m scripts.zora_authority_audit
"""

from __future__ import annotations

from themis_shared import db
from themis_shared.config import get_settings

WIDTH = 70

# The five rules under test, as SQL predicates over one (publication, author)
# pair. `aa` is the temp table built below; `p` the publication; `ou` the org
# unit its owning collection belongs to.
#
# Rule 1 is today's behaviour and exists as the baseline. Rule 3 is the new
# candidate -- an ORCID-typed authority whose ORCID resolves in `person.orcid`
# IS a UZH researcher; ZORA holds a Person record for them and DSpace simply
# failed to link this item to it. Rules 4 and 5 are zora/README.md's
# "sole-authored in an org-unit collection" heuristic, included to settle
# whether its arithmetic gap (4,571 publications claimed as ~5,800) is an
# undocumented dissertation clause.
_SOLE = "(cardinality(p.authors) = 1 AND ou.collection_uuid IS NOT NULL)"
# position() rather than ILIKE '%dissertation%': psycopg parses a bare % in the
# SQL as a placeholder prefix and rejects the query outright ("only '%s', '%b',
# '%t' are allowed as placeholders, got '%d'"). No wildcards, no escaping.
_DISS = "(position('dissertation' in lower(coalesce(p.publication_type, ''))) > 0)"

# Every predicate is closed with `IS TRUE`, which is not decoration. An author
# with no authority at all has kind NULL, so `kind = 'cris'` is NULL, and a
# predicate that NEGATES an unguarded rule -- section D2's "sole-author but not
# already eligible" -- evaluates to NULL and silently drops exactly the rows it
# exists to count. `IS TRUE` collapses NULL to false once, here, so no caller
# has to remember.
_CRIS_OR_RESOLVED_ORCID = "((aa.kind = 'cris' OR (aa.kind = 'orcid' AND aa.orcid_hit)) IS TRUE)"

RULES: list[tuple[str, str]] = [
    ("1  any authority (today)", "(aa.kind IS NOT NULL)"),
    ("2  cris-typed only", "((aa.kind = 'cris') IS TRUE)"),
    ("3  cris, or orcid resolving in person", _CRIS_OR_RESOLVED_ORCID),
    ("4  rule 3, or sole-author in org collection", f"({_CRIS_OR_RESOLVED_ORCID} OR {_SOLE})"),
    ("5  rule 4, minus dissertations", f"({_CRIS_OR_RESOLVED_ORCID} OR ({_SOLE} AND NOT {_DISS}))"),
]

# A well-formed ORCID is four groups of four, the last character a digit or X.
# 20 upstream values are not -- lowercase-x checksums, a trailing period,
# truncated groups -- which is why `_typed_authority` classifies by DSpace's
# marker and never by the id's shape.
ORCID_RE = r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$"

_BUILD = """
CREATE TEMP TABLE aa AS
SELECT p.id                                          AS pub_id,
       a.name                                        AS name,
       a.ord::int                                    AS ord,
       (p.author_authority_map -> a.name) ->> 'type' AS kind,
       (p.author_authority_map -> a.name) ->> 'id'   AS auth_id,
       false                                         AS cris_hit,
       false                                         AS orcid_hit
FROM publication p
CROSS JOIN LATERAL unnest(p.authors) WITH ORDINALITY AS a(name, ord)
"""

# Resolution is done as two UPDATEs rather than joins in the SELECT above so the
# expensive part runs once and every later query is a scan of `aa`. ORCIDs are
# compared upper-cased and trimmed: a lowercase-x checksum is a formatting
# defect, not a different person.
_RESOLVE = [
    "UPDATE aa SET cris_hit = true FROM person pe WHERE aa.kind = 'cris' AND pe.uuid = aa.auth_id",
    "UPDATE aa SET orcid_hit = true FROM person pe "
    "WHERE aa.kind = 'orcid' AND upper(btrim(pe.orcid)) = upper(btrim(aa.auth_id))",
    "CREATE INDEX ON aa (pub_id)",
    "CREATE INDEX ON aa (name)",
    "ANALYZE aa",
]

# Every rule query joins the same three relations; `t` exposes one boolean
# column per rule so the counts below are a single pass.
_RULE_SOURCE = """
FROM aa
JOIN publication p ON p.id = aa.pub_id
LEFT JOIN org_unit ou ON ou.collection_uuid = p.owning_collection_uuid
"""


def head(title: str) -> None:
    print()
    print("=" * WIDTH)
    print(title)
    print("=" * WIDTH)


def rows(conn, sql: str, params: dict | None = None) -> list[tuple]:
    # `params or {}` would be wrong: an empty dict still makes psycopg scan the
    # SQL for placeholders, so any literal % in the query becomes an error.
    return conn.execute(sql, params).fetchall()


def one(conn, sql: str, params: dict | None = None):
    return rows(conn, sql, params)[0][0]


def pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:5.1f}%" if whole else "    --"


def section_a(conn) -> None:
    head("A. AUTHORITY INVENTORY")

    total = one(conn, "SELECT count(*) FROM publication")
    persons = one(conn, "SELECT count(*) FROM person")
    org_units = one(conn, "SELECT count(*) FROM org_unit")
    print(f"  publications              {total:>9,}")
    print(f"  person mirror rows        {persons:>9,}")
    print(f"  org_unit mirror rows      {org_units:>9,}")

    any_auth = one(
        conn,
        "SELECT count(DISTINCT pub_id) FROM aa WHERE kind IS NOT NULL",
    )
    stored = one(conn, "SELECT count(*) FROM publication WHERE cardinality(uzh_authors) > 0")
    print()
    print(
        f"  with >=1 authority of any kind (from the map)  {any_auth:>9,}  {pct(any_auth, total)}"
    )
    print(f"  with cardinality(uzh_authors) > 0 (as stored)  {stored:>9,}  {pct(stored, total)}")
    if any_auth != stored:
        print("  ^ these differ: the stored array no longer equals 'any authority'.")

    head("A2. DISTINCT AUTHOR NAMES BY AUTHORITY KIND")
    print("  A name is counted once across the whole corpus. 'both' means the")
    print("  same name carries a CRIS UUID on one record and an ORCID on another.")
    print()
    kinds = rows(
        conn,
        """
        SELECT count(*) FILTER (WHERE has_cris AND NOT has_orcid),
               count(*) FILTER (WHERE has_orcid AND NOT has_cris),
               count(*) FILTER (WHERE has_cris AND has_orcid),
               count(*) FILTER (WHERE NOT has_cris AND NOT has_orcid),
               count(*)
        FROM (
            -- coalesce is load-bearing: for a name whose every authority is
            -- NULL, `kind = 'cris'` is NULL rather than false, so bool_or
            -- returns NULL and `NOT has_cris AND NOT has_orcid` is NULL too --
            -- the no-authority names silently vanish from all four buckets.
            SELECT name,
                   coalesce(bool_or(kind = 'cris'), false)  AS has_cris,
                   coalesce(bool_or(kind = 'orcid'), false) AS has_orcid
            FROM aa GROUP BY name
        ) t
        """,
    )[0]
    labels = ["cris only", "orcid only", "seen both ways", "no authority at all", "TOTAL names"]
    for label, value in zip(labels, kinds, strict=True):
        print(f"  {label:<24s}{value:>9,}")


def section_b(conn) -> None:
    head("B. DO THE AUTHORITY IDS RESOLVE AGAINST THE MIRRORS?")
    print("  The two loose ends zora/README.md leaves open. A cris id that does")
    print("  not resolve may name a non-Person entity type; a resolving ORCID is")
    print("  the signal that was never measured.")
    print()

    for kind, column, hit in (
        ("cris", "person.uuid", "cris_hit"),
        ("orcid", "person.orcid", "orcid_hit"),
    ):
        total, resolved = rows(
            conn,
            f"SELECT count(DISTINCT auth_id), count(DISTINCT auth_id) FILTER (WHERE {hit}) "
            f"FROM aa WHERE kind = %(kind)s",
            {"kind": kind},
        )[0]
        print(
            f"  distinct {kind:<5s} ids {total:>9,}   resolving in {column:<14s}"
            f" {resolved:>9,}  {pct(resolved, total)}"
        )

    print()
    print("  Unresolved samples (10 each):")
    for kind, hit in (("cris", "cris_hit"), ("orcid", "orcid_hit")):
        sample = rows(
            conn,
            f"SELECT DISTINCT auth_id, name FROM aa WHERE kind = %(kind)s AND NOT {hit} LIMIT 10",
            {"kind": kind},
        )
        print(f"    -- {kind} --")
        for auth_id, name in sample:
            print(f"       {auth_id:<40s} {name}")

    head("B2. MALFORMED ORCID AUTHORITY VALUES")
    bad = rows(
        conn,
        "SELECT DISTINCT auth_id, name FROM aa "
        "WHERE kind = 'orcid' AND auth_id !~ %(re)s ORDER BY auth_id",
        {"re": ORCID_RE},
    )
    print(f"  {len(bad)} distinct values do not match {ORCID_RE}")
    for auth_id, name in bad[:25]:
        print(f"    {auth_id:<40s} {name}")


def section_c(conn) -> None:
    head("C. CANDIDATE RULES")
    print("  publications = at least one author passes the rule.")
    print("  names        = distinct author names the rule presents as eligible.")
    print()

    selects = ",\n".join(
        f"count(DISTINCT pub_id) FILTER (WHERE r{i}), count(DISTINCT name) FILTER (WHERE r{i})"
        for i in range(1, len(RULES) + 1)
    )
    predicates = ",\n".join(f"{sql} AS r{i}" for i, (_, sql) in enumerate(RULES, start=1))
    result = rows(
        conn,
        f"SELECT {selects} FROM (SELECT aa.pub_id, aa.name, {predicates} {_RULE_SOURCE}) t",
    )[0]

    print(f"  {'rule':<46s}{'publications':>14s}{'names':>10s}")
    print("  " + "-" * (WIDTH - 2))
    for i, (label, _) in enumerate(RULES):
        print(f"  {label:<46s}{result[2 * i]:>14,}{result[2 * i + 1]:>10,}")


def section_d(conn) -> None:
    head("D. DOES THE COLLECTION CLAUSE DISCRIMINATE?")
    print("  ZORA is UZH's own repository, so nearly every item should sit in")
    print("  some 'Publications of X' collection. If it does, the clause selects")
    print("  ~everything and sole-authorship is doing all the work in rule 4.")
    print()

    total, no_uuid, joins = rows(
        conn,
        """
        SELECT count(*),
               count(*) FILTER (WHERE p.owning_collection_uuid IS NULL),
               count(*) FILTER (WHERE ou.collection_uuid IS NOT NULL)
        FROM publication p
        LEFT JOIN org_unit ou ON ou.collection_uuid = p.owning_collection_uuid
        """,
    )[0]
    print(f"  publications                                   {total:>9,}")
    print(f"  with no owning_collection_uuid                 {no_uuid:>9,}  {pct(no_uuid, total)}")
    print(f"  joining an org_unit collection                 {joins:>9,}  {pct(joins, total)}")

    head("D2. WHAT RULE 4 ADDS OVER RULE 3, BY ORG UNIT")
    added = rows(
        conn,
        f"""
        SELECT coalesce(ou.name, '(no org unit)') AS unit,
               count(DISTINCT aa.pub_id) AS pubs,
               count(DISTINCT aa.name) AS names,
               count(DISTINCT aa.pub_id) FILTER (WHERE {_DISS}) AS dissertations
        {_RULE_SOURCE}
        WHERE {_SOLE} AND NOT {_CRIS_OR_RESOLVED_ORCID}
        GROUP BY unit ORDER BY pubs DESC LIMIT 10
        """,
    )
    print(f"  {'org unit':<44s}{'pubs':>8s}{'names':>8s}{'diss.':>8s}")
    print("  " + "-" * (WIDTH - 2))
    for unit, pubs, names, diss in added:
        print(f"  {unit[:43]:<44s}{pubs:>8,}{names:>8,}{diss:>8,}")


def section_e(conn) -> None:
    head("E. FACE VALIDITY -- DO THESE LOOK LIKE UZH SUPERVISORS?")
    print("  15 random eligible names per rule, with a department they publish")
    print("  in. Eyeball these: rule 1 should show obvious foreign co-authors.")

    for i in (1, 2, 3, 5):
        label, predicate = RULES[i - 1]
        sample = rows(
            conn,
            f"""
            SELECT aa.name, min(p.department)
            {_RULE_SOURCE}
            WHERE {predicate}
            GROUP BY aa.name ORDER BY random() LIMIT 15
            """,
        )
        print()
        print(f"  -- rule {label} --")
        for name, department in sample:
            print(f"     {name[:40]:<42s}{department or '(no department)'}")


def main() -> None:
    dsn = get_settings().database_url
    with db.connection(dsn) as conn:
        print(f"Auditing {dsn.rsplit('@', 1)[-1]}")
        print("Building the author-level temp table (one pass over publication)...")
        conn.execute(_BUILD)
        for statement in _RESOLVE:
            conn.execute(statement)
        print(f"  {one(conn, 'SELECT count(*) FROM aa'):,} (publication, author) pairs")

        try:
            section_a(conn)
            section_b(conn)
            section_c(conn)
            section_d(conn)
            section_e(conn)
        finally:
            conn.execute("DROP TABLE IF EXISTS aa")
    # Without this the pooled worker threads outlive main() and psycopg prints
    # "couldn't stop thread 'pool-1-worker-0'" over the report.
    db.close_pools()
    print()


if __name__ == "__main__":
    main()
