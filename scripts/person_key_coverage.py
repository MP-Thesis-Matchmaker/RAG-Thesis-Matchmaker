#!/usr/bin/env python
"""Measure how often the person key actually joins the two sources.

`retrieval/identity.py` resolves a posting's supervisor to a paper's author, and
over the whole corpus it resolves 105 of 403 supervisor names (2026-09-27). That number is an
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

`--given-names` runs a different, query-free audit instead: how much of the
first-token key rests on initials, and how often the given names it groups
together agree, omit, expand or contradict one another. It needs only
`DATABASE_URL` -- no model, no index:

    uv run --package themis-matcher python scripts/person_key_coverage.py --given-names

Its examples print raw author spellings. They are personal data: read them in
the terminal, do not paste them into committed docs.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from itertools import combinations, zip_longest

from themis_matcher.config import get_settings
from themis_matcher.indexing import read_manifest
from themis_matcher.retrieval import build_retriever, identity
from themis_shared import db
from themis_shared.contracts import ParsedQuery
from themis_shared.names import fold_ascii, strip_titles

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

# The same author set as `_AUTHORS_SQL`, with how many papers credit each
# spelling -- summed, these are author credits, not distinct publications.
_AUTHOR_CREDITS_SQL = """
SELECT author, count(*) AS credits
FROM publication, unnest(uzh_authors) AS author
GROUP BY author
"""

# Ordered by severity; a comparison reports the worst position it finds.
_CLASSES = ("equal", "omission", "expansion", "full-conflict", "letter-conflict")
_CONFLICTS = ("full-conflict", "letter-conflict")
_EXAMPLES = 8


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


def given_tokens(name: str, family_len: int = 1) -> list[str]:
    """Every folded given token, where `key_of` keeps only the first.

    Read the way `key_of` reads the name, so position 0 is always the key's
    `given`: a usable comma splits family from given, anything else is natural
    order with the last `family_len` tokens as the family.
    """
    cleaned = strip_titles(name)
    family, comma, given = cleaned.partition(",")
    if comma and fold_ascii(family).split() and fold_ascii(given).split():
        return fold_ascii(given).split()
    return fold_ascii(cleaned.replace(",", " ")).split()[:-family_len]


def compare(a: list[str], b: list[str]) -> str:
    """The worst position-by-position relation between two given-token lists."""
    worst = 0
    for x, y in zip_longest(a, b):
        if x is None or y is None:
            rank = 1  # omission: "Andrea B." against "Andrea"
        elif x == y:
            rank = 0
        elif x[0] != y[0]:
            rank = 4  # letter-conflict: "M. A." against "M. B."
        elif len(x) == 1 or len(y) == 1:
            rank = 2  # expansion: "D." against "Davide"
        else:
            rank = 3  # full-conflict: "Felix" against "Flurin"
        worst = max(worst, rank)
    return _CLASSES[worst]


def _worst(classes) -> str:
    return max(classes, key=_CLASSES.index, default="equal")


def _readings(name: str) -> list[identity.PersonKey]:
    """The keys `posting_key` would try for a posting name, before anchor lookup."""
    cleaned = strip_titles(name)
    if "," in cleaned:
        key = identity.key_of(name)
        if key is not None:
            return [key]
        cleaned = cleaned.replace(",", " ")
    return identity.candidates(cleaned)


def _raw_later_tokens(spelling: str) -> list[str]:
    """Given tokens after the first, lowercased but *not* accent-folded (F2's view)."""
    _, comma, given = strip_titles(spelling).partition(",")
    return re.findall(r"[^\W\d_]+", given.lower())[1:] if comma else []


def _given_name_audit(dsn: str) -> None:
    anchors: dict[identity.PersonKey, set[str]] = defaultdict(set)
    credits: dict[str, int] = {}
    with db.get_pool(dsn).connection() as conn:
        for author, n in conn.execute(_AUTHOR_CREDITS_SQL):
            key = identity.key_of(author)
            if key:
                anchors[key].add(author)
                credits[author] = n
        names = [row[0] for row in conn.execute(_SUPERVISORS_SQL)]

    anchor_set = set(anchors)
    merged = {name: key for name in names if (key := identity.resolve(name, anchor_set))}
    spelling_tokens = {s: given_tokens(s) for spellings in anchors.values() for s in spellings}

    # Worst relation among the spellings one anchor key already groups together.
    internal = {
        key: _worst(
            compare(spelling_tokens[a], spelling_tokens[b])
            for a, b in combinations(sorted(spellings), 2)
        )
        for key, spellings in anchors.items()
    }

    print("--- M0 sanity: must equal the recorded 403 / 2,411 / 105 ---")
    print(f"  distinct supervisor names        {len(names)}")
    print(f"  anchor keys from uzh_authors     {len(anchor_set)}")
    print(f"  resolved                         {len(merged)}")
    f2 = sorted(
        [
            key
            for key, spellings in anchors.items()
            if any(
                x != y and len(x) > 1 and len(y) > 1
                for a, b in combinations(spellings, 2)
                # Shortest wins on purpose: a missing middle name is not F2's conflict.
                for x, y in zip(_raw_later_tokens(a), _raw_later_tokens(b), strict=False)
            )
        ],
        key=lambda k: (k.family, k.given),
    )
    print(f"  F2 reproduction (recorded: 4)    {len(f2)}")
    print("    definition: two spellings of one key differ in a later full given token,")
    print("    compared lowercased without accent folding")
    for key in f2[: _EXAMPLES * 2]:
        later = sorted({t for s in anchors[key] for t in _raw_later_tokens(s)})
        print(f"    {key.given}|{key.family:24} {later}")

    print("\n--- M1 anchor side ---")
    initial_keys = [k for k in anchor_set if len(k.given) == 1]
    initial_spellings = [s for s, t in spelling_tokens.items() if len(t[0]) == 1]
    full_by_family: dict[str, set[str]] = defaultdict(set)
    for k in anchor_set:
        if len(k.given) > 1:
            full_by_family[k.family].add(k.given[0])
    shadowed = [k for k in initial_keys if k.given in full_by_family.get(k.family, set())]
    print(f"  anchor keys with an initial given        {len(initial_keys)} / {len(anchor_set)}")
    print(f"  uzh_authors spellings, initial first     {len(initial_spellings)} / {len(credits)}")
    print(
        f"  author credits on those spellings        {sum(credits[s] for s in initial_spellings)}"
    )
    print(f"    / all uzh author credits               {sum(credits.values())}")
    print(f"  initial keys with a same-letter full key {len(shadowed)}")
    print("    (same family, e.g. 'Smith, A.' beside 'Smith, Andreas': one person split,")
    print("    or two people -- a name cannot say which)")

    print("\n--- M2 posting side ---")
    posting_tokens = {name: given_tokens(name) for name in names}
    print(
        f"  first given token is an initial          "
        f"{sum(1 for t in posting_tokens.values() if t and len(t[0]) == 1)} / {len(names)}"
    )
    print(
        f"  any given token is an initial            "
        f"{sum(1 for t in posting_tokens.values() if any(len(x) == 1 for x in t))} / {len(names)}"
    )

    # Posting name against every spelling of the anchor it merged into, then
    # the anchor's own internal relation: a merge is only as clean as both.
    against: dict[str, str] = {}
    combined: dict[str, str] = {}
    for name, key in merged.items():
        tokens = given_tokens(name, len(key.family.split()))
        against[name] = _worst(compare(tokens, spelling_tokens[s]) for s in anchors[key])
        combined[name] = _worst([against[name], internal[key]])

    print("\n--- M3 the current merges, posting name vs anchor spellings ---")
    table = Counter(
        ("initial" if len(merged[n].given) == 1 else "full", cls) for n, cls in against.items()
    )
    print(f"  {'first token':12}" + "".join(f"{c:>16}" for c in _CLASSES))
    for kind in ("initial", "full"):
        print(f"  {kind:12}" + "".join(f"{table[(kind, c)]:>16}" for c in _CLASSES))

    print("\n--- M4 refused today, would merge if an initial could expand ---")
    by_family: dict[str, list[identity.PersonKey]] = defaultdict(list)
    for k in anchor_set:
        by_family[k.family].append(k)
    directions: Counter[str] = Counter()
    fan_out: Counter[str] = Counter()
    for name in names:
        if name in merged:
            continue
        compatible = {
            (
                a,
                "posting initial -> full anchor"
                if len(r.given) == 1
                else "full posting -> initial anchor",
            )
            for r in _readings(name)
            for a in by_family.get(r.family, [])
            if a.given != r.given
            and a.given[0] == r.given[0]
            and (len(a.given) == 1) != (len(r.given) == 1)
        }
        if compatible:
            for direction in {d for _, d in compatible}:
                directions[direction] += 1
            fan_out["exactly one anchor" if len(compatible) == 1 else "several anchors"] += 1
    for label in ("posting initial -> full anchor", "full posting -> initial anchor"):
        print(f"  {label:40} {directions[label]}")
    for label in ("exactly one anchor", "several anchors"):
        print(f"    {label:38} {fan_out[label]}")

    print("\n--- M5 anchor keys whose own spellings disagree ---")
    multi = {k: c for k, c in internal.items() if len(anchors[k]) > 1}
    print(f"  keys with 2+ spellings                   {len(multi)} / {len(anchor_set)}")
    print(f"  {'class':18}{'keys':>8}{'spellings':>12}{'credits':>10}")
    for cls in _CLASSES:
        keys = [k for k, c in multi.items() if c == cls]
        spellings = [s for k in keys for s in anchors[k]]
        print(
            f"  {cls:18}{len(keys):>8}{len(spellings):>12}{sum(credits[s] for s in spellings):>10}"
        )
    for cls in ("letter-conflict", "full-conflict", "expansion"):
        keys = sorted(
            (k for k, c in multi.items() if c == cls),
            key=lambda k: -sum(credits[s] for s in anchors[k]),
        )
        if keys:
            print(f"  top {cls}:")
            for k in keys[:_EXAMPLES]:
                shown = ", ".join(f"{s} ({credits[s]})" for s in sorted(anchors[k]))
                print(f"    {shown[:110]}")

    print("\n--- M6 the current merges under 'same evidence, no expansion, no conflict' ---")
    counts = Counter(combined.values())
    print("  worst of (posting vs spellings, spellings vs each other):")
    for cls in _CLASSES:
        print(f"    {cls:18} {counts[cls]}")
    kept = counts["equal"]
    print(f"  kept, equal only                         {kept} / {len(merged)}")
    kept += counts["omission"]
    print(f"  kept, if a missing middle is allowed     {kept}")
    kept += counts["expansion"]
    print(f"  kept, if a middle initial may expand too {kept}")
    lost = sum(counts[c] for c in _CONFLICTS)
    via_anchor = sum(
        1 for n in merged if combined[n] in _CONFLICTS and against[n] not in _CONFLICTS
    )
    print(f"  lost to a conflict under any reading     {lost}")
    print(f"    of which only the anchor's spellings conflict {via_anchor}")
    initial_merges = sum(1 for k in merged.values() if len(k.given) == 1)
    print(f"  initial-to-initial merges among the {len(merged)}   {initial_merges}")
    refused = sum(1 for c in internal.values() if c in _CONFLICTS)
    print(f"  anchor keys the rule would refuse        {refused} / {len(anchor_set)}")


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
    parser.add_argument(
        "--given-names",
        action="store_true",
        help="audit initials and given-name agreement instead (no model or index needed)",
    )
    args = parser.parse_args()

    settings = get_settings()
    if args.given_names:
        try:
            _given_name_audit(settings.database_url)
        finally:
            db.close_pools()
        return
    _check_index(settings)

    # The factory, not a hand-built VectorRetriever: the ranking reads the
    # per-source thresholds from settings, and a hand-built one silently ranked on
    # raw score at the constructor's 0.0 defaults.
    retriever = build_retriever(settings)
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
