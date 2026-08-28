# Retrieval score calibration

First empirical characterisation of what `ScoredHit.score` actually looks like on the
harvested corpus, and what follows for `MATCHER_SYNTHESIS_MIN_SCORE`.

**Measured 2026-08-28.** Produced by
[`scripts/score_distribution.py`](../scripts/score_distribution.py); every number below
is that script's output, not an estimate.

> **This is not an evaluation.** The queries are probes for a *distribution*. No
> relevance judgements are attached to any result, no gold set exists yet, and nothing
> here may be reported as retrieval accuracy, precision@k, MRR or nDCG. What it measures
> is the geometry of the score, which is a prerequisite for evaluation, not a substitute
> for it.

## Why this exists

`MATCHER_SYNTHESIS_MIN_SCORE` gates whether `LLMSynthesizer` presents a candidate as a
match or degrades to `_no_strong_match` (`synthesis/llm.py:81`). Its default was `0.0`,
which is inert, and there was no basis for any other value: `ScoredHit.score` is a
cosine similarity in `[-1, 1]` (see
[`indexing/README.md`](../projects/matcher/src/themis_matcher/indexing/README.md#what-the-score-is-and-why-it-is-not-0-1)),
so a threshold against it is in cosine units and cannot be read off as a percentage.

## Method

Two distributions per query, answering different questions.

**Corpus-wide** — one SQL aggregate over every row of `document`, grouped by
`source_type`. Exact, not sampled: `percentile_cont` plus `min`, `max`, and a count of
scores below zero. This characterises the *background* — what an arbitrary document
scores.

**Retrieved head** — `store.query(top_k=100)` per `source_type`. Reports `min`, `p50`,
`p90`, `#5` (fifth-best) and `max`. `#5` and `max` are the load-bearing columns:
`SupervisorMatch.score` is a max over one person's retrieved documents
(`retrieval/vector.py:198`) drawn from a `top_k=5` query, so those two bracket what a
threshold actually encounters. `p50` of a top-100 slice brackets nothing — see the
matched-slice caveat under *Threats to validity*.

**Two query classes.** Five *on-topic* probes spread across faculties, and four
*out-of-domain controls*. The controls are the methodological core: on-topic probes show
where good answers sit and say nothing about where bad ones sit, and a threshold is the
line between the two. Controls are everyday practical questions no research group works
on, deliberately not obscure *academic* subjects — UZH spans medicine, law, theology,
economics, vetsuisse and the sciences, so an obscure discipline would still find a
genuine neighbour and would measure the wrong thing.

The **admissible band** is then

```
max over controls of (best score)  ≤  threshold  ≤  min over on-topic of (best score)
```

because `_no_strong_match` fires only when *nothing* clears the threshold. A second,
tighter ceiling — `min over on-topic of #5` — marks where the threshold stops merely
detecting hopeless queries and starts trimming candidates inside result sets that are
fine.

## Index under test

| | |
|---|---|
| documents | 214,756 publications + 695 thesis postings |
| embedding model | `BAAI/bge-m3`, 1024-dim, unit-normalised |
| token window | 1024 (`MATCHER_EMBEDDING_MAX_SEQ_LENGTH`) |
| store | Postgres + pgvector, cosine, HNSW |
| manifest guard | passed — the run refuses a model/manifest mismatch and refuses `hash-fake` |

## Results

### On-topic probes — corpus-wide

| query | type | min | p50 | p90 | p99 | max | negative |
|---|---|---|---|---|---|---|---|
| RAG + misinformation | pub | 0.198 | 0.377 | 0.431 | 0.479 | 0.611 | 0 |
| | post | 0.270 | 0.401 | 0.460 | 0.506 | 0.564 | 0 |
| ML for medical imaging | pub | 0.137 | 0.408 | 0.483 | 0.547 | 0.690 | 0 |
| | post | 0.262 | 0.415 | 0.500 | 0.563 | 0.585 | 0 |
| sustainable finance | pub | 0.136 | 0.330 | 0.398 | 0.483 | 0.734 | 0 |
| | post | 0.200 | 0.360 | 0.435 | 0.501 | 0.652 | 0 |
| comp. ling. Swiss German | pub | 0.147 | 0.369 | 0.449 | 0.537 | 0.717 | 0 |
| | post | 0.268 | 0.384 | 0.455 | 0.517 | 0.598 | 0 |
| memory consolidation | pub | 0.115 | 0.350 | 0.424 | 0.496 | 0.707 | 0 |
| | post | 0.215 | 0.370 | 0.440 | 0.531 | 0.640 | 0 |

### On-topic probes — retrieved head (top 100)

| query | type | min | p50 | p90 | #5 | max |
|---|---|---|---|---|---|---|
| RAG + misinformation | pub | 0.522 | 0.537 | 0.570 | 0.596 | **0.605** |
| | post | 0.452 | 0.467 | 0.501 | 0.513 | **0.564** |
| ML for medical imaging | pub | 0.610 | 0.621 | 0.647 | 0.659 | 0.690 |
| | post | 0.485 | 0.510 | 0.557 | 0.573 | 0.585 |
| sustainable finance | pub | 0.601 | 0.625 | 0.671 | 0.689 | 0.734 |
| | post | 0.421 | 0.445 | 0.490 | 0.529 | 0.652 |
| comp. ling. Swiss German | pub | 0.610 | 0.630 | 0.659 | 0.684 | 0.717 |
| | post | 0.443 | 0.463 | 0.505 | 0.538 | 0.598 |
| memory consolidation | pub | 0.561 | 0.585 | 0.628 | 0.667 | 0.707 |
| | post | 0.429 | 0.454 | 0.522 | 0.542 | 0.640 |

Bold marks the weakest best-match — the signal ceiling.

### Out-of-domain controls — retrieved head (top 100)

| query | type | min | p50 | p90 | #5 | max |
|---|---|---|---|---|---|---|
| sourdough proofing | pub | 0.412 | 0.424 | 0.448 | 0.460 | 0.484 |
| | post | 0.281 | 0.298 | 0.327 | 0.335 | 0.364 |
| bicycle inner tube | pub | 0.441 | 0.454 | 0.478 | 0.488 | 0.504 |
| | post | 0.325 | 0.345 | 0.379 | 0.390 | 0.413 |
| flat-pack bookcase | pub | 0.417 | 0.431 | 0.457 | 0.462 | 0.491 |
| | post | 0.327 | 0.348 | 0.388 | 0.402 | **0.431** |
| charcoal grill | pub | 0.433 | 0.448 | 0.486 | 0.501 | **0.542** |
| | post | 0.288 | 0.305 | 0.349 | 0.359 | 0.373 |

Bold marks the strongest false best-match — the noise floor.

### Derived bands

| | noise floor | signal ceiling | band | trims good sets above |
|---|---|---|---|---|
| publications | 0.542 | 0.605 | **0.063** | 0.596 |
| thesis postings | 0.431 | 0.564 | **0.133** | 0.513 |
| **combined** | **0.542** | **0.564** | **0.022** | 0.513 |

## Findings

### F1 — The negative half of the range is empty

Zero of 214,756 publications and zero of 695 postings scored below `0.0` against any of
the nine queries. The lowest observed score anywhere was **0.115**.

This is bge-m3's anisotropy, measured rather than assumed: dense retrievers of this
family place all embeddings in a narrow cone, so "unrelated" bottoms out around 0.12–0.20
and never becomes opposition. The signed `[-1, 1]` range is real and reachable in
principle; on this corpus nothing occupies its lower half.

Consequence for the design decision recorded in
[`indexing/README.md`](../projects/matcher/src/themis_matcher/indexing/README.md#what-the-score-is-and-why-it-is-not-0-1):
keeping the score signed rather than clamping at zero cost nothing, and it is now known
to have gained nothing in practice either. The reversibility argument stands — a clamp is
irreversible at the storage layer — but it protects an empty region. Re-check after any
re-embed or model change.

### F2 — Retrieval separates on-topic from out-of-domain

Every control's best match falls below every on-topic query's best match, in both source
types. Publications: controls reach 0.484–0.542, on-topic 0.605–0.734. Postings: controls
0.364–0.431, on-topic 0.564–0.652. There is no overlap.

The system does know the difference. A threshold is therefore a coherent mechanism here,
which was not obvious before measuring.

### F3 — The two source types have incompatible operating ranges

| | controls reach | on-topic reach | relative margin |
|---|---|---|---|
| publications | 0.542 | 0.605 | 11.6% |
| postings | 0.431 | 0.564 | 30.9% |

Postings separate nearly three times better in relative terms, yet score *lower* in
absolute terms at the top. The publication noise floor (0.542) sits only 0.022 below the
posting signal ceiling (0.564).

Postings also occupy a visibly narrower band overall — corpus spread (max − min) is
0.294–0.452 for postings against 0.413–0.598 for publications, and their corpus median is
*higher* (≈0.386 vs ≈0.367) despite their lower peak. They are 695 short advertisements
in one genre; publications are 214,756 abstracts across every faculty.

### F4 — The combined band is 0.022 wide and can only shrink

The floor is a `max` over controls; the ceiling is a `min` over probes. Adding queries can
only raise the floor and lower the ceiling. **0.022 is therefore an upper bound on the
true band, established from nine queries.**

It is already fragile. The charcoal-grill control reached 0.542 while its three siblings
sat at 0.484 / 0.504 / 0.491 — a single query consumed 0.038 of margin, nearly twice what
survives. A tenth query plausibly closes the band entirely.

### F5 — Every admissible single threshold trims postings and no publications

Trimming begins at `#5`: 0.596 for publications, 0.513 for postings. The combined band
`[0.542, 0.564]` lies entirely inside that interval. So any legal single value is, by
construction, in the region that cuts postings while leaving publications untouched.

At a representative 0.55:

| | fifth-best per query | trimmed |
|---|---|---|
| publications | 0.596 / 0.659 / 0.689 / 0.684 / 0.667 | none |
| postings | 0.513 / 0.573 / 0.529 / 0.538 / 0.542 | **4 of 5 queries** |

The retriever fetches five postings per query; at 0.55 one or two survive.

This matters beyond recall. Because the person key never joins the two sources
([`retrieval/README.md`](../projects/matcher/src/themis_matcher/retrieval/README.md#known-gaps)
— 403 supervisor names, 0 matching any of 2,942 `uzh_authors`), posting-people and
publication-people are disjoint entries, and `SupervisorMatch.score` is whichever source
that person has. A single threshold in the admissible band therefore **removes
supervisors with advertised open positions and keeps every publication-backed name** —
deleting the most actionable half of the output.

### F6 — Scores are not comparable across queries

Best-match scores ranged 0.605 to 0.734 across on-topic publications, a 0.129 spread
driven only by which topic was asked. That is roughly six times the width of the combined
admissible band. Any single global cut is lenient on topics the corpus covers densely and
strict on topics it covers thinly.

## Interpretation

The source-type asymmetry in F3 and the query variance in F6 have one cause, and it is
structural rather than a defect in the data.

**Cosine similarity is not calibrated across corpora of different size or breadth.** The
nearest neighbour of an arbitrary query among 214,756 documents is closer than the nearest
among 695, purely from sampling density — with enough documents, something is always
superficially close. The charcoal-grill query reaching 0.542 against publications while
reaching only 0.373 against postings is that effect, not a statement about grilling
research at UZH.

The same mechanism produces F6: score magnitude tracks how densely the corpus covers the
query's neighbourhood, not how good the match is. "Sustainable finance" peaks at 0.734 and
"RAG + misinformation" at 0.605 because ZORA holds far more of the former, not because the
former is better matched.

An absolute cosine threshold is therefore a weak instrument for this problem. It is being
asked to straddle two corpora whose noise floors differ by 0.111, across queries whose
magnitudes vary by 0.129.

## Recommendation

**Two thresholds, not one.** Not yet applied — recorded here for the decision.

| | admissible | trims above | proposed | margin |
|---|---|---|---|---|
| publications | 0.542 – 0.605 | 0.596 | **0.57** | ±0.027 |
| thesis postings | 0.431 – 0.564 | 0.513 | **0.48** | ±0.041 |

Both block every control, pass every on-topic query, and trim nothing inside a good result
set. Neither balances on a knife edge, and both have room to absorb the band contraction
F4 predicts.

The cost is a code change rather than a config value. `SupervisorMatch` does not record
which source produced its `score`. Given the join defect it is currently inferable from
`publication_count > 0`, but that inference becomes silently wrong the moment the person
key is fixed — so the field should be added rather than the inference relied on.

**Interim state: `synthesis_min_score` stays `0.0`.** An inert guard is preferable to one
whose value is inside a 0.022 band measured from nine queries, and preferable to one that
deletes supervisors with open positions.

## Threats to validity

- **n = 9.** Five probes, four controls. Enough to establish F1–F3 and to bound F4; not
  enough to fix a value. The bands are upper bounds.
- **The probes are not a gold set.** They were chosen to spread across faculties and to be
  plausible student phrasings. No relevance judgement is attached to any retrieved
  document, so nothing here measures whether the top results are *correct* — only how
  their scores are distributed.
- **The control class is an assumption.** "No UZH group works on sourdough proofing" is a
  judgement, not a verified fact. If a control accidentally has genuine neighbours, its
  best score is signal and the floor is overestimated. The charcoal-grill outlier is the
  candidate to check.
- **Mismatched head slices.** `--top-k 100` is the top 0.047% of publications but the top
  14.4% of postings. `max` and `#5` are comparable across the two; `min`, `p50` and `p90`
  are not, and no conclusion above rests on them.
- **No corpus aggregate for the controls.** The run skips the full scan for control
  queries, so their background distribution is unknown and control scores cannot be
  normalised against their own corpus. This blocks the most promising follow-up below.
- **One model, one index state.** Every number is specific to `BAAI/bge-m3` at a 1024-token
  window over this corpus. A re-embed invalidates all of it.

## Reproducing

```
uv run --package themis-matcher --extra embeddings python scripts/score_distribution.py --control
```

Requires `DATABASE_URL` pointing at a built index and the `embeddings` extra. Read-only —
`SELECT` only, no writes (invariant 1). The script refuses `hash-fake` and refuses a
model/manifest mismatch, because either would produce numbers that look like measurements
and are not.

Pass your own queries as positional arguments to replace the on-topic probes.

## Open questions and next measurements

1. **Does the combined band survive more queries?** Extend to ~20 probes and ~10 controls.
   F4 predicts it collapses. If it does, the two-threshold recommendation is evidenced
   rather than argued; if it holds, a single value becomes defensible.
2. **Does query-relative scoring beat an absolute threshold?** The obvious normalisation is
   `(score − corpus_p50) / (corpus_p90 − corpus_p50)`, which would divide out the density
   effect behind F3 and F6. On the on-topic probes this yields 3.76 / 4.35 / 4.22 / 4.82 /
   5.94 — a *wider* relative spread than the raw scores, so it is **not** obviously an
   improvement, and it cannot be tested at all without the controls' corpus statistics
   (see *Threats*). Requires the script to keep the corpus scan for controls.
   A simpler relative rule — "keep matches within δ of this query's best" — sidesteps both
   problems and needs no per-query aggregate. Neither is implemented; both belong with the
   unbuilt `ranking` package rather than with synthesis.
3. **Is the charcoal-grill control genuinely out of domain?** It scored 0.058 above its
   nearest sibling and single-handedly sets the publication floor. Inspect what it actually
   retrieved.
4. **Do the bands move once the person key is fixed?** Merging posting-people with
   publication-people changes which source supplies each `SupervisorMatch.score`, which is
   the quantity thresholded. F5's asymmetry is partly an artefact of the join not
   happening.

## See also

- [`indexing/README.md`](../projects/matcher/src/themis_matcher/indexing/README.md#what-the-score-is-and-why-it-is-not-0-1)
  — what the score is, and why it was left signed rather than rescaled into `[0, 1]`
- [`synthesis/README.md`](../projects/matcher/src/themis_matcher/synthesis/README.md)
  — where the threshold is applied, and why only `LLMSynthesizer` honours it
- [`retrieval/README.md`](../projects/matcher/src/themis_matcher/retrieval/README.md#known-gaps)
  — the person-key defect behind F5