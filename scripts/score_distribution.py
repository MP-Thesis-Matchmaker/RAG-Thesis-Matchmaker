#!/usr/bin/env python
"""Measure the retrieval score distribution over the real index.

Exists to answer one question with evidence rather than assumption: what value
should `MATCHER_SYNTHESIS_MIN_SCORE` have? `ScoredHit.score` is a cosine
similarity over `[-1, 1]` (see `indexing/store.py`), so a threshold against it is
in cosine units and cannot be guessed from what "looks like a percentage".

Two distributions are reported per query, and they answer different questions:

* **corpus-wide** -- one aggregate over every row in `document`, computed in SQL.
  Exact, not sampled. This is what says how much of the index is *negative*, which
  is the open question about whether the signed range carries anything at all.
* **retrieved head** -- the top-k the retriever would actually see. A threshold
  never encounters anything else, so this is the region the value comes from.

The default queries are **probes for a distribution, not a gold set**. They are
not evaluation data, no relevance judgement is attached to them, and nothing here
may be reported as retrieval accuracy. Pass your own as arguments.

`--control` is what actually locates the threshold. On-topic probes alone show
where good answers sit; they say nothing about where *bad* ones sit, and the
threshold is the line between the two. The controls are out-of-domain questions
this corpus cannot answer, so their retrieved head is the highest score the system
produces when the right answer is "nobody". A defensible threshold sits above that
and below the weakest on-topic head -- the run prints both ends and the gap.

**The first run's results and their analysis are written up in
`docs/score-calibration.md`.** Read that before interpreting new output; three of
its findings change how the table below should be read:

* No document scored below zero against any of nine queries (lowest: 0.115). The
  signed range is real but unoccupied on this corpus.
* Postings score systematically below publications at the top (best posting
  0.56-0.65, best publication 0.61-0.73) while separating from noise nearly three
  times better in relative terms. One threshold covers both, and
  `SupervisorMatch.score` is a max over one person's documents, so a value tuned
  on publications silently deletes supervisors whose only evidence is an open
  position. The posting column sets the ceiling.
* `--top-k 100` is the top 0.05% of 214,756 publications but the top 14% of 695
  postings. The two head rows are not the same slice; `max` and `#5` are
  comparable across them, `min`/`p50`/`p90` are not.

Read-only: `SELECT` only, no writes, no schema changes (invariant 1).

Needs `DATABASE_URL` pointing at the built index, and the real embedding model:

    uv run --package themis-matcher --extra embeddings python scripts/score_distribution.py
"""

from __future__ import annotations

import argparse
import sys

from themis_matcher.config import get_settings
from themis_matcher.indexing import build_embedder, build_store, read_manifest
from themis_shared import db

# Plausible student phrasings spread across faculties, so the numbers are not all
# drawn from one corner of the corpus. Nothing more is claimed for them.
_PROBES = [
    "retrieval-augmented generation and misinformation detection",
    "machine learning for medical imaging",
    "sustainable finance and climate risk",
    "computational linguistics for Swiss German",
    "neural mechanisms of memory consolidation",
]

# Out-of-domain: everyday practical questions no UZH research group works on, and
# phrased the way nobody writes an abstract. They are the noise floor -- whatever
# the head reaches here is what the system scores when the honest answer is
# "nobody". Deliberately not merely *obscure* topics: UZH spans medicine, law,
# theology, economics, vetsuisse and the sciences, so an obscure academic subject
# would still find a genuine neighbour and would measure the wrong thing.
_CONTROLS = [
    "how long to proof a sourdough starter at room temperature",
    "steps to patch a bicycle inner tube by the roadside",
    "assembly instructions for a flat-pack bookcase",
    "charcoal grill temperature settings for chicken thighs",
]

# `1 - (embedding <=> v)` is the same expression PgVectorStore._QUERY_TEMPLATE
# selects. Repeated here rather than imported because this needs it inside
# aggregates the store's query shape has no room for.
_SCORE = "1 - (embedding <=> %(vector)s::vector)"

_CORPUS_SQL = f"""
SELECT source_type,
       count(*)                                     AS n,
       count(*) FILTER (WHERE {_SCORE} < 0)         AS negative,
       min({_SCORE})                                AS lo,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY {_SCORE}) AS p50,
       percentile_cont(0.90) WITHIN GROUP (ORDER BY {_SCORE}) AS p90,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY {_SCORE}) AS p99,
       max({_SCORE})                                AS hi
FROM document
GROUP BY source_type
ORDER BY source_type
"""


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted ascending list."""
    if not values:
        return float("nan")
    index = min(len(values) - 1, max(0, round(fraction * (len(values) - 1))))
    return values[index]


def _check_index(settings) -> None:
    """Refuse to produce numbers that would be meaningless if reported.

    Same three-way comparison `Indexer._check_manifest` makes. A threshold
    calibrated against a mismatched model, or against `hash-fake`, would look
    exactly like a real measurement and be worth nothing.
    """
    if settings.embedding_model == "hash-fake":
        sys.exit(
            "refusing to run with MATCHER_EMBEDDING_MODEL=hash-fake: its scores are "
            "arbitrary by construction, so a threshold derived from them is noise."
        )
    manifest = read_manifest(settings)
    if manifest is None:
        sys.exit("no index has been built (index_manifest is empty). Run `themis-matcher index`.")
    if manifest.embedding_model != settings.embedding_model:
        sys.exit(
            f"the index was built with '{manifest.embedding_model}' but "
            f"MATCHER_EMBEDDING_MODEL is '{settings.embedding_model}'. Vectors from "
            "different models are not comparable; the scores would be meaningless."
        )
    print(f"index: {manifest.document_count} documents, {manifest.embedding_model}\n")


def _corpus_rows(dsn: str, vector: list[float]) -> list[tuple]:
    with db.connection(dsn) as conn:
        return conn.execute(_CORPUS_SQL, {"vector": db.to_vector_literal(vector)}).fetchall()


def _report(
    query: str, dsn: str, store, vector: list[float], top_k: int, corpus: bool = True
) -> dict[str, dict[str, float]]:
    """Print one query's two distributions; return the head figures for the band.

    `corpus` is skippable because the corpus-wide aggregate is a full sequential
    scan and says nothing new for a control query -- the tail of an out-of-domain
    query is the same tail, and only its head is interesting.
    """
    print(f'query: "{query}"')

    if corpus:
        print("  corpus-wide (every row, exact):")
        header = f"{'source_type':<16}{'n':>9}{'negative':>12}{'min':>9}"
        print(f"    {header}{'p50':>9}{'p90':>9}{'p99':>9}{'max':>9}")
        for source_type, n, negative, lo, p50, p90, p99, hi in _corpus_rows(dsn, vector):
            share = f"{negative} ({negative / n:.2%})" if n else "0"
            print(
                f"    {source_type:<16}{n:>9}{share:>12}"
                f"{lo:>9.3f}{p50:>9.3f}{p90:>9.3f}{p99:>9.3f}{hi:>9.3f}"
            )

    # `max` is what decides whether the answer degrades to _no_strong_match at all
    # (llm.py returns it only when *nothing* clears the threshold), and `#5` is the
    # weakest document the retriever's own top_k=5 would surface -- so those two
    # bracket what SupervisorMatch.score can actually be. p50 of a top-100 slice
    # brackets nothing: it is the top 0.05% of publications and the top 14% of
    # postings, which is why the band below is not computed from it.
    print(f"  retrieved head (top {top_k}):")
    columns = f"{'source_type':<16}{'returned':>9}{'min':>9}{'p50':>9}{'p90':>9}{'#5':>9}{'max':>9}"
    print(f"    {columns}")
    head: dict[str, dict[str, float]] = {}
    for source_type in ("publication", "thesis_posting"):
        hits = store.query(vector, top_k=top_k, filters={"source_type": source_type})
        if not hits:
            print(f"    {source_type:<16}{0:>9}")
            continue
        scores = sorted(hit.score for hit in hits)
        fifth = scores[-5] if len(scores) >= 5 else scores[0]
        head[source_type] = {"fifth": fifth, "max": scores[-1]}
        print(
            f"    {source_type:<16}{len(scores):>9}"
            f"{scores[0]:>9.3f}{_percentile(scores, 0.50):>9.3f}"
            f"{_percentile(scores, 0.90):>9.3f}{fifth:>9.3f}{scores[-1]:>9.3f}"
        )
    print()
    return head


def _band(
    signal: list[dict[str, dict[str, float]]], noise: list[dict[str, dict[str, float]]]
) -> None:
    """Print the admissible threshold band, per source type and combined.

    The threshold has two distinct jobs and they want different numbers, so both
    ends are reported rather than collapsed into one recommendation:

    * `_no_strong_match` fires only when *nothing* clears the threshold, so the
      whole-answer behaviour is governed by each query's best score. The band is
      "above the best an out-of-domain query manages, below the worst an on-topic
      query manages".
    * Above the weakest on-topic `#5`, the threshold also starts trimming
      candidates *inside* result sets that are fine. That is a different decision
      and usually not the one being made here.
    """
    print("admissible band (cosine units):")
    for source_type in ("publication", "thesis_posting"):
        best = [q[source_type]["max"] for q in signal if source_type in q]
        worst_ok = [q[source_type]["fifth"] for q in signal if source_type in q]
        floor = [q[source_type]["max"] for q in noise if source_type in q]
        if not best or not floor:
            continue
        lo, hi = max(floor), min(best)
        verdict = f"{lo:.3f} .. {hi:.3f}" if lo < hi else f"EMPTY ({lo:.3f} >= {hi:.3f})"
        print(f"  {source_type:<16}{verdict}")
        print(
            f"  {'':<16}out-of-domain best {max(floor):.3f} | on-topic worst best "
            f"{min(best):.3f} | trims good sets above {min(worst_ok):.3f}"
        )

    both = [(q[s]["max"], s) for q in signal for s in ("publication", "thesis_posting") if s in q]
    noise_both = [q[s]["max"] for q in noise for s in ("publication", "thesis_posting") if s in q]
    if both and noise_both:
        lo, hi = max(noise_both), min(m for m, _ in both)
        print(
            f"\n  combined: {lo:.3f} .. {hi:.3f}"
            if lo < hi
            else f"\n  combined: EMPTY ({lo:.3f} >= {hi:.3f})"
        )
        print(
            "  One threshold covers both source types and SupervisorMatch.score is a\n"
            "  max over one person's documents, so the lower of the two ceilings binds.\n"
            "  Postings are the lower one: a value tuned on publications deletes exactly\n"
            "  the supervisors whose evidence is an open position."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("queries", nargs="*", help="probe queries; defaults to a built-in set")
    parser.add_argument("--top-k", type=int, default=100, help="head size per source type")
    parser.add_argument(
        "--control",
        action="store_true",
        help="also probe out-of-domain queries and print the admissible threshold band",
    )
    args = parser.parse_args()

    settings = get_settings()
    _check_index(settings)

    embedder = build_embedder(settings)
    store = build_store(settings)
    dsn = settings.database_url
    signal: list[dict[str, dict[str, float]]] = []
    noise: list[dict[str, dict[str, float]]] = []
    try:
        for query in args.queries or _PROBES:
            signal.append(_report(query, dsn, store, embedder.embed_query(query), args.top_k))
        if args.control:
            print("--- out-of-domain controls: the score when the answer is nobody ---\n")
            for query in _CONTROLS:
                head = _report(
                    query, dsn, store, embedder.embed_query(query), args.top_k, corpus=False
                )
                noise.append(head)
    finally:
        db.close_pools()

    if noise:
        _band(signal, noise)
    else:
        print(
            "No band computed: re-run with --control. On-topic probes show where good\n"
            "answers sit and say nothing about where bad ones sit, and the threshold is\n"
            "the line between them. MATCHER_SYNTHESIS_MIN_SCORE is in cosine units."
        )


if __name__ == "__main__":
    main()
