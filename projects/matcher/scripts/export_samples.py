"""
Regenerate `data/samples/` from the harvested corpus.

The samples are what a fresh clone indexes: `SOURCES_PATH` defaults to
`data/samples`, so `themis-matcher index` with no arguments reads them, and CI's
offline job runs the whole pipeline against nothing else. They therefore have to
parse against the current contracts -- and in 2026-08 they silently stopped,
because `ZoraPublication` gained typed authorities and the checked-in file kept
bare strings. `JsonlSourceReader` counts invalid lines and carries on, so the
offline corpus quietly became 20 documents instead of 50 with nothing to say so.

This script exists so the next contract change is a re-run rather than an
archaeology session, and `projects/matcher/tests/test_sample_data.py` is the
alarm that says when to re-run it.

Two things are deliberate.

**Records are built by `PostgresSourceReader`, not by SQL written here.** Ids are
chosen in SQL, then the reader is iterated and filtered. That is a full streaming
pass over the corpus to keep 30 rows, which is a few seconds and worth it: the
exported objects come out of exactly the code path production uses, so the
mapping cannot drift -- which is the bug being fixed.

**Email addresses are removed, from the structured field and from free text
alike.** 503 of 695 postings carry a `supervisor.email` and 336 distinct real
addresses are involved; separately, some departments write a contact address into
the posting body. `contracts/sources.py` already states the position: the address
"travels no further than the record carrying it", and a git repository is
travelling further. Nulling the field while leaving `hise@ifi.uzh.ch` in the
description would have been the appearance of care rather than care.

Redaction is the one place these files are not verbatim scraper output. It is
marked in the text (`[email removed]`) rather than silently blanked, so nobody
mistakes a redacted description for what the page said.

Reads Postgres only. Writes nothing to any table -- invariant 1 holds.

Usage:
    python projects/matcher/scripts/export_samples.py [--dry-run]
    python projects/matcher/scripts/export_samples.py --out /tmp/samples
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from themis_matcher.indexing.sources import (
    PUBLICATIONS_FILE,
    THESES_FILE,
    PostgresSourceReader,
)
from themis_shared import db
from themis_shared.config import get_settings
from themis_shared.contracts import ThesisPosting, ZoraPublication

# Four topics that exist on both sides, so a query can match a publication and a
# posting rather than only ever one kind. Measured over the corpus: each has
# 18-67 postings and 1,800-4,000 UZH-authored publications behind it.
CLUSTERS = {
    "machine learning / NLP": r"(machine learning|neural|language model|nlp|deep learning)",
    "climate / environment": r"(climate|ecolog|biodiversit|environmental)",
    "economics / behaviour": r"(economic|market|behaviou?ral|incentive)",
    "medical imaging": r"(imaging|mri|radiolog|segmentation|tomograph)",
}

# 24 topical + 6 edge cases = 30 publications; 14 + 6 = 20 postings. The totals
# are held at 30 and 20 because docs/deployment.md extrapolates its whole
# throughput table from "the 50 checked-in samples", and changing the count would
# invalidate a measurement rather than just a sentence.
PUBLICATION_QUOTAS = dict.fromkeys(CLUSTERS, 6)
# Not uniform: medical imaging has only 18 postings in the corpus against 62-67
# for the others, so taking four of them would over-represent it.
POSTING_QUOTAS = {
    "machine learning / NLP": 4,
    "climate / environment": 4,
    "economics / behaviour": 3,
    "medical imaging": 3,
}

# One record each, so the awkward paths are represented rather than assumed. The
# comment on each is what it buys; a sample set that is all happy-path hides
# exactly the bugs a sample set is for.
PUBLICATION_EDGE_CASES = {
    # Embedding falls back to title alone.
    "no abstract": "abstract IS NULL OR abstract = ''",
    # Without one of these, RETRIEVAL_REQUIRE_UZH_AUTHOR cannot be exercised
    # offline at all -- every record would pass the filter either way.
    "no UZH author": "coalesce(array_length(uzh_authors, 1), 0) = 0",
    # The CRIS-vs-ORCID distinction the typed authority exists to carry. Written
    # with jsonb_each rather than a LIKE over ::text because a LIKE pattern needs
    # a literal `%`, and psycopg reads that as a placeholder wherever the query
    # also takes parameters -- which every query here does.
    "ORCID-only authority": (
        "EXISTS (SELECT 1 FROM jsonb_each(author_authority_map) a "
        "        WHERE a.value ->> 'type' = 'orcid') "
        "AND NOT EXISTS (SELECT 1 FROM jsonb_each(author_authority_map) a "
        "                WHERE a.value ->> 'type' = 'cris')"
    ),
    "no authority at all": "author_authority_map = '{}'::jsonb",
    "no keywords": "coalesce(array_length(keywords, 1), 0) = 0",
    "not in English": "language IS NOT NULL AND language <> 'eng'",
}

POSTING_EDGE_CASES = {
    # These two are why RETRIEVAL_REQUIRE_AVAILABLE_POSTING is flippable.
    "assigned": "status = 'assigned'",
    "private": "status = 'private'",
    "no status": "status IS NULL",
    # data/samples/README.md's own complaint about the fixtures these replace:
    # a quarter of real topics name nobody, and half take two degree levels.
    "no supervisor": "supervisors = '[]'::jsonb",
    "two degree levels": "array_length(degree_levels, 1) > 1",
    "no description": "description IS NULL OR description = ''",
}


# Newest first, and deterministic either way -- a re-run against an unchanged
# database has to reproduce the same file, or every regeneration is a diff nobody
# can review. Publications sort by year because a matchmaking demo answered with
# 2008 papers is a demo about people who may have left; ZORA handles happen to
# ascend with age, so plain `ORDER BY id` quietly selected the oldest corner of
# the corpus. Posting ids are sha1 and carry no such meaning, and `listed_on` is
# often null, so those stay on id: arbitrary but stable.
_ORDER = {
    "publication": "year DESC NULLS LAST, id DESC",
    "posting": "id",
}


def _pick(dsn: str, table: str, where: str, limit: int, exclude: set[str]) -> list[str]:
    """Ids matching `where`, capped, in the table's deterministic order."""
    sql = f"SELECT id FROM {table} WHERE ({where})"  # noqa: S608 - predicates are literals above
    params: list[object] = []
    if exclude:
        sql += " AND id <> ALL(%s)"
        params.append(list(exclude))
    sql += f" ORDER BY {_ORDER[table]} LIMIT %s"
    params.append(limit)
    with db.connection(dsn) as conn:
        return [row[0] for row in conn.execute(sql, params).fetchall()]


def _topical(
    dsn: str, table: str, quotas: dict[str, int], extra: str, exclude: set[str]
) -> list[str]:
    """The quota of ids from each topic, skipping any already chosen."""
    chosen: list[str] = []
    seen = set(exclude)
    for topic, wanted in quotas.items():
        where = f"(title || ' ' || coalesce({_body(table)}, '')) ~* '{CLUSTERS[topic]}'"
        if extra:
            where += f" AND {extra}"
        ids = _pick(dsn, table, where, wanted, seen)
        if len(ids) < wanted:
            print(f"  warning: {topic} yielded {len(ids)}/{wanted} from {table}")
        chosen.extend(ids)
        seen.update(ids)
    return chosen


def _body(table: str) -> str:
    return "abstract" if table == "publication" else "description"


def _edge_cases(dsn: str, table: str, cases: dict[str, str], exclude: set[str]) -> list[str]:
    chosen: list[str] = []
    seen = set(exclude)
    for label, where in cases.items():
        ids = _pick(dsn, table, where, 1, seen)
        if not ids:
            # Loud rather than silent: a sample set that quietly lost its only
            # unavailable posting is one where a retrieval flag stops being
            # testable and nobody finds out.
            print(f"  warning: no {table} matches edge case {label!r}")
            continue
        chosen.extend(ids)
        seen.update(ids)
    return chosen


# Deliberately broad. This guards a privacy decision, so over-matching costs a
# marker in a sample description and under-matching publishes somebody's address.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
REDACTED = "[email removed]"


def _redact(text: str | None) -> str | None:
    return None if text is None else _EMAIL.sub(REDACTED, text)


def _strip_emails(posting: ThesisPosting) -> ThesisPosting:
    """A copy with every email removed -- field and free text. See the docstring."""
    return posting.model_copy(
        update={
            "supervisors": [s.model_copy(update={"email": None}) for s in posting.supervisors],
            "title": _redact(posting.title),
            "description": _redact(posting.description),
        }
    )


def _strip_publication_emails(publication: ZoraPublication) -> ZoraPublication:
    """The same for abstracts. Rare in ZORA, but the guarantee should not depend on that."""
    return publication.model_copy(
        update={"title": _redact(publication.title), "abstract": _redact(publication.abstract)}
    )


def _write(path: Path, records: Iterable[object]) -> int:
    lines = [record.model_dump_json() + "\n" for record in records]
    path.write_text("".join(lines), encoding="utf-8")
    return len(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--database-url", help="default: the DATABASE_URL setting")
    parser.add_argument("--out", default="data/samples", help="default: data/samples")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the selection without writing"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    dsn = args.database_url or settings.database_url

    try:
        print("selecting publications")
        # Topical records need both an abstract to embed and a UZH author to be
        # attributable to a supervisor; the edge cases deliberately have neither
        # requirement, which is what makes them edge cases.
        pub_topical = _topical(
            dsn,
            "publication",
            PUBLICATION_QUOTAS,
            "coalesce(array_length(uzh_authors, 1), 0) > 0 AND coalesce(abstract, '') <> ''",
            set(),
        )
        pub_edge = _edge_cases(dsn, "publication", PUBLICATION_EDGE_CASES, set(pub_topical))
        pub_ids = set(pub_topical) | set(pub_edge)

        print("selecting postings")
        post_topical = _topical(dsn, "posting", POSTING_QUOTAS, "", set())
        post_edge = _edge_cases(dsn, "posting", POSTING_EDGE_CASES, set(post_topical))
        post_ids = set(post_topical) | set(post_edge)

        print(f"  {len(pub_ids)} publications, {len(post_ids)} postings")
        if args.dry_run:
            for label, ids in (("publication", sorted(pub_ids)), ("posting", sorted(post_ids))):
                print(f"\n{label}:")
                for chosen in ids:
                    print(f"  {chosen}")
            return 0

        # One streaming pass each, through the reader production uses. Slower than
        # a targeted SELECT and the whole point: these objects are built by the
        # same code, so the export cannot drift from what the indexer reads.
        print("reading through PostgresSourceReader (one pass over the corpus)")
        reader = PostgresSourceReader(dsn=dsn)
        publications: list[ZoraPublication] = [
            _strip_publication_emails(record)
            for record in reader.publications()
            if record.id in pub_ids
        ]
        postings: list[ThesisPosting] = [
            _strip_emails(record) for record in reader.postings() if record.id in post_ids
        ]

        publications.sort(key=lambda record: record.id)
        postings.sort(key=lambda record: record.id)

        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        written_pubs = _write(out / PUBLICATIONS_FILE, publications)
        written_posts = _write(out / THESES_FILE, postings)
        print(f"wrote {written_pubs} publications and {written_posts} postings to {out}")

        missing = (pub_ids - {p.id for p in publications}) | (post_ids - {p.id for p in postings})
        if missing:
            print(f"warning: {len(missing)} selected id(s) never came back from the reader")
            return 1
    finally:
        db.close_pools()
    return 0


if __name__ == "__main__":
    sys.exit(main())
