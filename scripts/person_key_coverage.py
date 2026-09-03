#!/usr/bin/env python
"""Measure how often the person key actually joins the two sources.

`retrieval/identity.py` resolves a posting's supervisor to a paper's author, and
over the whole corpus it resolves 99 of 403 supervisor names. That number is an
**upper bound on who could ever merge**, not a prediction of what a query
returns: `VectorRetriever.retrieve` fetches `top_k` postings and `top_k`
publications, so a merge needs the same person to surface in both slices at once.
At the default `top_k=5` that is rare, and this script is what stops the corpus
figure from being reported as if it were the retrieval figure.

Two numbers per `top_k`:

* **corpus ceiling** -- supervisor names resolvable against every `uzh_authors`
  string in the index. Computed once, independent of any query.
* **per-query merges** -- matches actually returned with `publication_count` and
  `posting_count` both non-zero. This is what a student would see.

The queries are the same probes `scripts/score_distribution.py` uses. They are
**not a gold set**: no relevance judgement is attached, and nothing here may be
reported as retrieval accuracy. They exist to sample the corpus from several
faculties at once.

**Results and analysis: `docs/person-key-resolution.md`.**

Read-only: `SELECT` only, no writes, no schema changes (invariant 1).

Needs `DATABASE_URL` pointing at the built index, and the real embedding model:

    uv run --package themis-matcher --extra embeddings python scripts/person_key_coverage.py
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from themis_matcher.config import get_settings
from themis_matcher.indexing import build_embedder, build_store, read_manifest
from themis_matcher.retrieval import identity
from themis_matcher.retrieval.vector import VectorRetriever
from themis_shared import db
from themis_shared.contracts import ParsedQuery

_PROBES = [
    "retrieval-augmented generation and misinformation detection",
    "machine learning for medical imaging",
    "sustainable finance and climate risk",
    "computational linguistics for Swiss German",
    "neural mechanisms of memory consolidation",
]

_SUPERVISORS_SQL = """
SELECT DISTINCT s->>'name' AS name
FROM posting, jsonb_array_elements(supervisors) AS s
WHERE coalesce(s->>'name', '') <> ''
"""

_AUTHORS_SQL = """
SELECT DISTINCT unnest(uzh_authors) AS author
FROM publication
WHERE uzh_authors <> '{}'
"""


def _check_index(settings) -> None:
    """Refuse to produce numbers that would be meaningless if reported."""
    if settings.embedding_model == "hash-fake":
        sys.exit(
            "refusing to run with MATCHER_EMBEDDING_MODEL=hash-fake: its scores are "
            "arbitrary by construction, so the retrieved sets would be too."
        )
    manifest = read_manifest(settings)
    if manifest is None:
        sys.exit("no index has been built (index_manifest is empty). Run `themis-matcher index`.")
    if manifest.embedding_model != settings.embedding_model:
        sys.exit(
            f"the index was built with '{manifest.embedding_model}' but "
            f"MATCHER_EMBEDDING_MODEL is '{settings.embedding_model}'."
        )


def _corpus_ceiling(dsn: str) -> tuple[int, int, int]:
    """(supervisors, resolvable, anchor keys) over the whole corpus."""
    anchors: dict[identity.PersonKey, set[str]] = defaultdict(set)
    with db.get_pool(dsn).connection() as conn:
        for (author,) in conn.execute(_AUTHORS_SQL):
            key = identity.key_of(author)
            if key:
                anchors[key].add(author)
        names = [row[0] for row in conn.execute(_SUPERVISORS_SQL)]

    anchor_set = set(anchors)
    resolvable = sum(1 for name in names if identity.resolve(name, anchor_set))
    return len(names), resolvable, len(anchor_set)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("queries", nargs="*", help="probe queries; defaults to a built-in set")
    parser.add_argument(
        "--top-k",
        type=int,
        nargs="+",
        default=[5, 20, 50],
        help="retrieval widths to compare (default: 5 20 50)",
    )
    args = parser.parse_args()

    settings = get_settings()
    _check_index(settings)

    embedder = build_embedder(settings)
    store = build_store(settings)
    retriever = VectorRetriever(
        embedder=embedder,
        store=store,
        require_uzh_author=settings.retrieval_require_uzh_author,
        require_available_posting=settings.retrieval_require_available_posting,
        ranking_strategy=settings.retrieval_ranking_strategy,
    )
    queries = args.queries or _PROBES

    try:
        total, resolvable, anchors = _corpus_ceiling(settings.database_url)
        print("--- corpus ceiling (query-independent) ---")
        print(f"  distinct supervisor names        {total}")
        print(f"  anchor keys from uzh_authors     {anchors}")
        print(f"  resolvable against the corpus    {resolvable}  ({100 * resolvable / total:.1f}%)")
        print("\n  This is who COULD merge. What follows is who does.\n")

        for top_k in args.top_k:
            print(f"--- top_k = {top_k} ---")
            merged_total = matches_total = 0
            for query in queries:
                matches = retriever.retrieve(ParsedQuery(topics=[query]), top_k=top_k)
                merged = [m for m in matches if m.publication_count and m.posting_count]
                merged_total += len(merged)
                matches_total += len(matches)
                names = ", ".join(m.supervisor for m in merged) or "-"
                print(f"  {len(merged):2}/{len(matches):3}  {query[:52]:52}  {names[:60]}")
            share = 100 * merged_total / matches_total if matches_total else 0.0
            print(
                f"  {merged_total} of {matches_total} returned matches are "
                f"cross-source ({share:.1f}%)\n"
            )
    finally:
        db.close_pools()


if __name__ == "__main__":
    main()
