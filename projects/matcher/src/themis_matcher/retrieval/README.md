# retrieval

Answers the question "which UZH researchers match this student's interests?" by
searching the vector index and grouping the hits into ranked people. This is the
middle of the *Retrieval + Generation* lane of
[`docs/architecture.png`](../../../../../docs/architecture.png): embed query → two
filtered top-k queries → group per person → rank.

**Read-only (invariant 1).** This package never writes to the index. It also holds
the only ranking logic in the system — see the note below.

## Role in the pipeline

```
ParsedQuery ──▶ VectorRetriever.retrieve
                     │
                     ├─ embed the query with the SAME model that built the index
                     │
                     ├─ query 1: source_type=publication + has_uzh_author=True [+ department]
                     └─ query 2: source_type=thesis_posting [+ department] [+ degree_level]
                     │
                     ▼  up to 2 × top_k ScoredHits
              _persons()  fan out each hit to the people it credits
                     ▼
              _group_by_person()  score = max(hit score); sort desc; truncate
                     ▼
              list[SupervisorMatch]  ──▶ synthesis / adapters
```

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `Retriever` | `base.py` | Protocol: `retrieve(query: ParsedQuery, top_k: int = 5) -> list[SupervisorMatch]`. |
| `VectorRetriever` | `vector.py` | The real implementation. Takes an `Embedder` and a `VectorStore`; knows nothing about which store it is. |
| `FakeRetriever` | `fake.py` | Three hard-coded matches, ignores the query entirely. Lets the CLI, the pipeline, and the MCP adapter run with no index present. |
| `build_retriever(settings)` | `__init__.py` | Factory. Imports `vector` lazily so the fake path opens no database connection. |

## Data flow

**Reads:** the `document` table, through the `VectorStore` protocol.
**Writes:** nothing.

### Why two queries instead of one

`degree_level` only exists on thesis postings. A single combined query filtered on
it would silently drop every publication whenever a student says "master's
thesis" — which is most of the time. So publications and postings are queried
separately, each with `top_k`, and the union (up to `2 × top_k` hits) is grouped
before the final truncation.

Filters applied:

| | publications | thesis postings |
|---|---|---|
| `source_type` | `publication` | `thesis_posting` |
| `department` | if the query names one | if the query names one |
| `degree_level` | — | *(not filtered directly -- see below)* |
| `has_uzh_author` | **`True`** *only when `MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR`* | — |
| `is_available` | — | **`True`** *unless `MATCHER_RETRIEVAL_REQUIRE_AVAILABLE_POSTING=false`* |
| `degree_<level>` | — | **`True`** if the query names one |

### Two eligibility rules, both applied here rather than at index time

A publication whose author list contains no registered UZH researcher cannot produce
a supervisor a student here could actually work with. Until 2026-08-25 that was
enforced twice and absolutely — `has_uzh_author=True` hardcoded into every
publication query, mirrored by a `WHERE` clause in `indexing/sources.py`. It is now
two settings, because "cannot supervise" and "is not worth showing" turned out to be
different claims.

| setting | default | effect |
|---|---|---|
| `MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR` | `false` | adds `has_uzh_author: True` to the publication query |
| `MATCHER_RETRIEVAL_RANKING_STRATEGY` | `uzh_first` | `uzh_first` sorts on `(has_uzh_affiliation, score)`; `score` on similarity alone |
| `MATCHER_RETRIEVAL_REQUIRE_AVAILABLE_POSTING` | `true` | adds `is_available: True` to the posting query |

The default is **permissive but demoted**: an external researcher is reachable and
always ranks below every UZH match. The second setting is inert while the first is
on — nothing unaffiliated survives the filter for a strategy to reorder.

Two consequences worth knowing:

- **Crediting falls back.** `_persons` credits a publication to its `uzh_authors`, or
  to `authors` when there are none. Without that fallback the permissive default
  would do nothing at all: an unaffiliated publication credits nobody, so it is
  grouped into nothing and discarded after being embedded and retrieved. 118,110 of
  the 123,022 unaffiliated publications name authors; the other 4,912 name nobody and
  stay unreachable, since there is no one to credit. A publication that *does* have
  UZH authors never falls back, so external co-authors on a UZH paper are not
  promoted to supervisors.
- **`SupervisorMatch` is no longer sorted by score.** Under `uzh_first` a
  lower-scored UZH supervisor precedes a higher-scored external one. `has_uzh_affiliation`
  says which is which; callers should not re-sort on `score` and expect the order back.

Indexing deliberately takes no position now — see the comment at the top of
`indexing/sources.py`. A `WHERE` clause there would make `MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR`
unflippable in practice: turning it off would return nothing extra until someone
re-embedded the corpus, hours of work triggered by an environment variable.

#### Availability: the same move, made for the posting side on 2026-08-26

`indexing/sources.py` used to drop assigned and private postings before they were
embedded, on the reasoning that availability is not eligibility: a topic already
taken cannot be a recommendation under any setting, so it belonged in a query rather
than in a knob. The conclusion was right and the location was wrong. Enforced at
index time it cost a re-index to revisit — the exact trap the UZH filter had just been
pulled out of — and the input is the unstable half of the record: a topic's status
changes on the source page between scrapes while its text does not.

So postings now follow publications. All 695 are embedded, each carrying
`is_available` (false for `assigned` and `private`; `pending` and a missing status
both count as available, because "not yet settled" and "the page did not say" are not
"taken"), and `MATCHER_RETRIEVAL_REQUIRE_AVAILABLE_POSTING` — on by default — decides whether
the rule applies. Cost of the reversal: 17 extra documents against 214,756
publications.

Two differences from the UZH knob, both deliberate:

- **No ranking counterpart.** There is no strategy that demotes taken topics instead
  of excluding them, so `false` puts them in results outright. That is why this one
  defaults to on and the other defaults to off.
- **No over-fetch.** `_FILTERED_OVERFETCH` exists because the UZH predicate discards
  well over half of what the HNSW scan returns; this one discards 17 of 695 postings.
  Widening `top_k` on the posting query to chase that would inflate `posting_count`
  per person for a recall problem two orders of magnitude smaller.

**Known gap.** `synthesis/` renders `"{posting_count} open thesis posting(s)"`, and
that word "open" is only guaranteed while this setting is on. With it off, a taken
topic is described as open. The wording is load-bearing against a hallucination seen
in `docs/example-run.md`, so it was left alone rather than weakened for a
non-default path — but flipping the setting without fixing the phrasing is a
correctness regression, not just a recall change.

#### Known gap: `MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR=true` under-returns

pgvector applies metadata filters **after** the HNSW scan (see the partial-index
comment in `schema.sql`), and the two partial indexes key on `source_type` only. Over
a full index roughly 43% of publications carry a UZH author, so a filtered query
returns about that fraction of the candidates it asked for and silently comes back
short of `top_k`. This did not bite before, because the indexing filter guaranteed
every row in the graph satisfied the predicate.

`VectorRetriever` compensates by over-fetching 4x when the filter is on
(`_FILTERED_OVERFETCH`). That is a mitigation, not a fix. The fix is a third partial
HNSW index whose predicate matches the filter, which means editing `schema.sql` — a
fingerprint change, so a full `init-db --reset` and re-harvest. Worth doing when
something else already forces a reset; not worth forcing one on its own, for an
opt-in path whose default is off.

### Fan-out and attribution

A posting credits its `supervisor`. A publication credits **every** entry in its
`uzh_authors` list, so a three-author UZH paper contributes to all three people's
scores. That is why `_persons` exists, and why a person's `publication_count` can
exceed the number of retrieved documents.

### Scoring

`_group_by_person` sets each person's `score` to the **maximum** hit score they
appear in — their single most relevant document — then sorts descending and
truncates to `top_k`. There is no aggregation across documents, no
publication-count boost, no recency weighting, and no department-affiliation
signal.

**The score is a cosine similarity over `[-1, 1]` and can be negative** — a maximum
over `[-1, 1]` values is a `[-1, 1]` value, so `SupervisorMatch.score` carries
`ScoredHit`'s range unchanged. It is not a percentage, and any threshold against it
(notably `MATCHER_SYNTHESIS_MIN_SCORE`) is in cosine units and should be chosen from
observed values rather than assumed. Why it was left signed instead of rescaled into
`[0, 1]`:
[`../indexing/README.md`](../indexing/README.md#what-the-score-is-and-why-it-is-not-0-1).

Measured 2026-08-28 — [`docs/score-calibration.md`](../../../../../docs/score-calibration.md).
Two results land here rather than in synthesis. **Scores are not comparable across
queries**: best-match scores ranged 0.605–0.734 by topic alone, tracking how densely the
corpus covers the query's neighbourhood rather than match quality, so the `score`
strategy's ordering is only meaningful *within* one query. And **postings score
systematically below publications** (best posting 0.564–0.652 against 0.605–0.734),
because 695 short advertisements are a thinner corpus than 214,756 abstracts — which
means `_group_by_person`'s `max` is not comparing like with like when a person has both.
Today the person key ensures nobody does; when that is fixed, this becomes live.

## Configuration

The subset of `MatcherSettings` this sub-package reads; the whole list is in
[the package README](../../../README.md#configuration).

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `embedding_model` | `MATCHER_EMBEDDING_MODEL` | `BAAI/bge-m3` | Must match the model that built the index; the manifest guard enforces this. |
| `database_url` | `DATABASE_URL` | `postgresql://matchmaker:matchmaker@localhost:5432/matchmaker` | Which Postgres to read the index from. |
| `retrieval_require_uzh_author` | `MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR` | `false` | Whether a publication needs a registered UZH author to be retrievable at all. See the eligibility section above, including why a filtered query can come back short. |
| `retrieval_require_available_posting` | `MATCHER_RETRIEVAL_REQUIRE_AVAILABLE_POSTING` | `true` | Whether a posting has to still be open. No ranking strategy softens this one — off puts assigned topics in the results outright. |
| `retrieval_ranking_strategy` | `MATCHER_RETRIEVAL_RANKING_STRATEGY` | `uzh_first` | `uzh_first` or `score`. Inert while the UZH-author filter is on. |

The first two rows are read indirectly: `build_retriever` calls
`indexing.build_embedder` and `indexing.build_store`. The last three it reads
itself — they were discussed at length above and missing from this table until
2026-08-27.

## Swappable seams

Follows the repository-wide idiom: `base.py` Protocol, implementations beside it,
`build_retriever(settings)` in `__init__.py`. `FakeRetriever` is not a test mock —
it is a first-class offline implementation, which is what lets someone clone the
repository and run `themis-matcher match` before indexing anything.

## Status

**Implemented and tested.** `projects/matcher/tests/test_vector_retriever.py` (15 tests) covers
ranking order, the degree-level filter, evidence back-references, the UZH-author
pre-filter, and multi-author credit. It runs against `InMemoryVectorStore`, so it
needs no database — the retriever depends only on the protocol, which is what
made the store swap a rename here rather than a rewrite.

One thing to be aware of: `Pipeline()` still defaults to `FakeRetriever`. The real
retriever is wired in by the callers (`cli.py`, `themis_gateway/service.py`) only when
`read_manifest(settings)` returns a row. A caller that forgets that check silently
serves fake results.

## Known gaps

- **This package contains the entire ranking implementation, and it is one line:
  `score = max(hit.score)`.** `CLAUDE.md`'s target layout lists a separate
  `ranking` package for multi-signal scoring (semantic similarity, publication
  frequency, open positions, department affiliation), and
  `pipeline/orchestrator.py`'s docstring already claims a rank step. Neither
  exists yet. Grouping and sorting inside `_group_by_person` is all there is.
- **The person key is an exact name string, so publications and postings almost
  never join. This is the biggest single thing the `ranking` package has to fix.**
  `_group_by_person` keys `by_person` on whatever `_persons` returns, and the two
  sources spell people differently: `"Davide Scaramuzza"` on a posting against
  `"Scaramuzza, D"` or `"Scaramuzza, Davide"` on a paper. Measured over the whole
  corpus on 2026-08-26 — **403 distinct supervisor names, 0 of them matching any of
  the 2,942 `uzh_authors`**. Three match a plain `authors` entry, and only via the
  unaffiliated fallback below, so a merge happens precisely where the UZH signal is
  absent: 3 of 403, 0.7%.

  The consequences run through everything above it. `publication_count` and
  `posting_count` are effectively never both non-zero, so a supervisor with an open
  position is never evidenced by their own papers and vice versa — which is most of
  what a student actually wants to know. Any multi-signal score combining "has
  publications here" with "has an open position" is therefore scoring a join that
  does not happen, and would look correct in review while ranking on one signal at
  a time. No choice of corpus or sample data hides this; it is a property of the
  key, not of the data volume.

  The fix is name normalisation, or an identity join through the `person` table —
  which exists, carries CRIS UUIDs, and is already the authority `uzh_authors` is
  derived from. `author_authority_map` gives publications a typed identifier;
  postings have none, so the posting side needs the harder half of the work.
  Belongs with `ranking` rather than here: it changes what a candidate *is*, not
  how one is scored.
- **`matched_topics` is not computed.** Every match receives a copy of
  `query.topics` rather than the topics that actually matched. The field looks
  informative and is not.
- **`publication_count` is populated but unused in scoring**, despite
  `contracts/retrieval.py` describing it as a ranking signal.
- **`department` matching is exact-string.** `parsing/` never populates the field
  from free text today, so the filter is effectively dormant; it will need
  normalisation (aliases, abbreviations) before it is useful.
- **`posting_count` is a fact about this query, not about the person.** It counts
  thesis postings retrieved for someone in this result set. The posting query is
  unthresholded -- it returns the nearest `top_k` postings whatever their distance
  -- so 0 means none of that person's reached the top-k, never that they have no
  open position. The renderers therefore print a posting clause only when it is
  non-zero and **must stay that way**: the earlier "no open position" text became
  "not currently accepting new students" about a named academic in the LLM's prose
  (see [`../../../../../docs/example-run.md`](../../../../../docs/example-run.md)).
- **A posting nobody is named on reaches nobody.** `_persons` fans a posting out to
  every entry in its `supervisors` list, so a posting with an empty list credits no
  one and never appears in a result. That is **63 of 247** scraped topics -- a
  quarter of the real corpus, invisible. `has_supervisor` is emitted into metadata
  so a future ranking pass can surface them another way; nothing does today.
- **`degree_level` filtering goes through booleans, not the field itself.** A
  posting can be open to several levels, and neither store can filter a list-valued
  metadata field, so the filter is `degree_<level>: True` against the companions
  `posting_to_document` emits. See [`../scraper/README.md`](../../../../../projects/scraper/README.md)
  for the measured distribution behind that.
