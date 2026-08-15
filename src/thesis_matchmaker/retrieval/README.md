# retrieval

Answers the question "which UZH researchers match this student's interests?" by
searching the vector index and grouping the hits into ranked people. This is the
middle of the *Retrieval + Generation* lane of
[`docs/architecture.png`](../../../docs/architecture.png): embed query → two
filtered top-k queries → group per person → rank.

**Read-only (invariant 1).** This package never writes to the index. It also holds
the only ranking logic in the system — see the note below.

## Role in the pipeline

```
ParsedQuery ──▶ ChromaRetriever.retrieve
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
| `ChromaRetriever` | `chroma.py` | The real implementation. Takes an `Embedder` and a `VectorStore`. |
| `FakeRetriever` | `fake.py` | Three hard-coded matches, ignores the query entirely. Lets the CLI, the pipeline, and the MCP adapter run with no index present. |
| `build_retriever(settings)` | `__init__.py` | Factory. Imports `chroma` lazily so the fake path never pulls in `chromadb`. |

## Data flow

**Reads:** the Chroma collection, through the `VectorStore` protocol.
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
| `degree_level` | — | if the query names one |
| `has_uzh_author` | **`True`** | — |

### The UZH-author pre-filter

A publication whose author list contains no registered UZH researcher cannot
produce a valid supervisor recommendation — the people on it do not work here.
`has_uzh_author=True` removes those records at query time rather than filtering
them out afterwards, so they never consume a top-k slot.

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

## Configuration

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `embedding_model` | `EMBEDDING_MODEL` | `BAAI/bge-m3` | Must match the model that built the index; the manifest guard enforces this. |
| `vector_store_path` | `VECTOR_STORE_PATH` | `data/index` | Which index to read. |
| `collection_name` | `COLLECTION_NAME` | `matchmaker` | Which collection to read. |

`build_retriever` reads these indirectly, by calling `indexing.build_embedder` and
`indexing.build_store`.

## Swappable seams

Follows the repository-wide idiom: `base.py` Protocol, implementations beside it,
`build_retriever(settings)` in `__init__.py`. `FakeRetriever` is not a test mock —
it is a first-class offline implementation, which is what lets someone clone the
repository and run `thesis-matchmaker match` before indexing anything.

## Status

**Implemented and tested.** `tests/test_chroma_retriever.py` (6 tests) covers
ranking order, the degree-level filter, evidence back-references, the UZH-author
pre-filter, and multi-author credit.

One thing to be aware of: `Pipeline()` still defaults to `FakeRetriever`. The real
retriever is wired in by the callers (`cli.py`, `adapters/service.py`) only when
`data/index/manifest.json` exists. A caller that forgets that check silently
serves fake results.

## Known gaps

- **This package contains the entire ranking implementation, and it is one line:
  `score = max(hit.score)`.** `CLAUDE.md`'s target layout lists a separate
  `ranking` package for multi-signal scoring (semantic similarity, publication
  frequency, open positions, department affiliation), and
  `pipeline/orchestrator.py`'s docstring already claims a rank step. Neither
  exists yet. Grouping and sorting inside `_group_by_person` is all there is.
- **`matched_topics` is not computed.** Every match receives a copy of
  `query.topics` rather than the topics that actually matched. The field looks
  informative and is not.
- **`publication_count` is populated but unused in scoring**, despite
  `contracts/retrieval.py` describing it as a ranking signal.
- **Score range**: `ScoredHit.score` is `1.0 - cosine_distance`, so it lives in
  `[-1, 1]` and can be negative — see
  [`../indexing/README.md`](../indexing/README.md). Any threshold set against it
  (notably `SYNTHESIS_MIN_SCORE`) should be chosen from observed values, not
  assumed to be a percentage.
- **`department` matching is exact-string.** `parsing/` never populates the field
  from free text today, so the filter is effectively dormant; it will need
  normalisation (aliases, abbreviations) before it is useful.
- The posting half of the index is synthetic sample data, so `has_open_position`
  is not yet meaningful against real UZH postings.
