"""
Run this once, manually, before trusting anything in fields.py's field
name constants.

Fetches a handful of real ZORA records and prints every metadata field key
actually present, with one example value each — plus an explicit check
against the field names this pipeline assumes.

Usage:
    export ZORA_UZH_API_KEY=<your ZORA personal API token>
    # or: export ZORA_UZH_API_KEY_FILE=/path/to/your/token.secret
    python projects/zora/scripts/zora_inspect_fields.py [n_items]

Note: this needs real network access to www.zora.uzh.ch, so it will not run
inside a sandboxed environment without that access — run it locally or as
a one-off GitHub Actions job.
"""

from __future__ import annotations

import sys
from collections import defaultdict

from themis_zora import fields, zora_client


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    client = zora_client.get_client()
    field_examples: dict[str, str] = {}
    author_field_authorities: dict[str, list[str | None]] = defaultdict(list)

    count = 0
    for dso in zora_client.iter_items(client):
        for field, values in dso.metadata.items():
            if field not in field_examples and values:
                example = values[0].get("value", "")
                field_examples[field] = (example[:80] + "...") if len(example) > 80 else example

        author_entries = dso.metadata.get(fields.FIELD_AUTHOR, [])
        for entry in author_entries:
            author_field_authorities[dso.uuid].append(entry.get("authority"))

        count += 1
        if count >= n:
            break

    print(f"\nInspected {count} real ZORA items.\n")

    print("=" * 70)
    print("ALL METADATA FIELDS FOUND (field -> one example value)")
    print("=" * 70)
    for field in sorted(field_examples):
        print(f"  {field:45s} {field_examples[field]}")

    print()
    print("=" * 70)
    print("ASSUMED FIELDS IN fields.py — present or missing?")
    print("=" * 70)
    assumed = {
        "FIELD_TITLE": fields.FIELD_TITLE,
        "FIELD_AUTHOR": fields.FIELD_AUTHOR,
        "FIELD_ABSTRACT": fields.FIELD_ABSTRACT,
        "FIELD_DATE_ISSUED": fields.FIELD_DATE_ISSUED,
        "FIELD_DATE_ACCESSIONED": fields.FIELD_DATE_ACCESSIONED,
        "FIELD_TYPE": fields.FIELD_TYPE,
        "FIELD_DOI": fields.FIELD_DOI,
        "FIELD_URI": fields.FIELD_URI,
        "FIELD_SUBJECT_DDC": fields.FIELD_SUBJECT_DDC,
        "FIELD_SCOPUS_SUBJECTS": fields.FIELD_SCOPUS_SUBJECTS,
        "FIELD_SUBJECT": fields.FIELD_SUBJECT,
    }
    for name, field in assumed.items():
        status = "FOUND" if field in field_examples else "MISSING — check fields.py"
        print(f"  {name:25s} ({field:30s}) {status}")

    print()
    print("=" * 70)
    print("ORCID CANDIDATE FIELDS")
    print("=" * 70)
    for field in fields.FIELD_ORCID_CANDIDATES:
        status = "FOUND" if field in field_examples else "not present"
        print(f"  {field:30s} {status}")

    print()
    print("=" * 70)
    print("PER-AUTHOR AUTHORITY KEYS (uzh.contributor.author entries)")
    print("=" * 70)
    print(
        "If these are non-null UUIDs, per-author identity is resolvable via "
        "entities and aggregate.py's single-author-only ORCID limitation can "
        "likely be lifted. If they're all null, name-string matching (with "
        "its known limitations) is the only option for now.\n"
    )
    for uuid, authorities in list(author_field_authorities.items())[:n]:
        print(f"  item {uuid}: {authorities}")


if __name__ == "__main__":
    main()
