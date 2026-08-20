# contracts

The shared vocabulary of the system. Every other package speaks to its neighbours
through these Pydantic models rather than through raw dicts, so a change to what
"a publication" or "a match" means happens in exactly one place. In
[`docs/architecture.png`](../../../docs/architecture.png) these are the rounded
boxes on the right of the *Data Extraction* lane (`ZoraRecord`, `ThesisPosting`)
and the boxes threading through the *Retrieval + Generation* lane (`ParsedQuery`,
`SupervisorMatch`).

This package intentionally has **no dependencies on any sibling package**. It sits
at the bottom of the import graph; everything imports it, it imports nothing of
ours. Keeping it that way is what stops the module seams from collapsing.

## Role in the pipeline

```
ingestion ──writes──▶ ZoraRecord / ThesisPosting ──read by──▶ indexing
parsing   ──emits──▶  ParsedQuery                ──read by──▶ retrieval
retrieval ──emits──▶  SupervisorMatch (+ Evidence) ──read by──▶ synthesis, adapters
```

Pure data. No I/O, no business logic, no imports from sibling packages.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `DegreeLevel` | `sources.py` | `StrEnum` of `bachelor` / `master` / `phd`. Shared by `ParsedQuery` and `ThesisPosting` so a query's level can be compared to a posting's. |
| `ZoraRecord` | `sources.py` | One ZORA publication after normalisation. 13 fields: `id`, `title`, `abstract`, `authors`, `uzh_authors`, `author_authority_map`, `year`, `keywords`, `department`, `language`, `publication_type`, `doi`, `url`. |
| `ThesisPosting` | `sources.py` | One scraped open thesis position: `id`, `title`, `description`, `supervisor`, `department`, `degree_level`, `keywords`, `language`, `url`, `scraped_at`. |
| `ParsedQuery` | `retrieval.py` | A student's free-text interest turned into structure: `topics`, `keywords`, `degree_level`, `department`, `raw_query`. |
| `Evidence` | `retrieval.py` | One citable item backing a recommendation: `source_type` (`publication` \| `thesis_posting`), `source_id`, `title`, `url`, `year`. |
| `SupervisorMatch` | `retrieval.py` | One ranked person: `supervisor`, `department`, `score`, `matched_topics`, `publication_count`, `has_open_position`, `evidence[]`. |

All six are re-exported from `contracts/__init__.py`, so
`from thesis_matchmaker.contracts import ZoraRecord` is the intended import path.

## Data flow

**Reads:** nothing. **Writes:** nothing.

The models are *validated* at two boundaries:

- `ZoraRecord` / `ThesisPosting` — when `indexing/indexer.py` parses the JSONL
  files produced by ingestion. A malformed line is counted and skipped, not fatal.
- `ParsedQuery` — when `parsing/openai_compat.py` validates an LLM's JSON output.
  A `ValidationError` there triggers the rule-based fallback rather than an error
  reaching the user.

## Configuration

None. This package reads no settings and no environment variables.

## Swappable seams

None by design. These models *are* the fixed point that makes the other seams
swappable (invariant 3): the embedder, vector store, and LLM provider can all be
replaced precisely because none of them leak their own types across a module
boundary.

## Status

**Implemented and tested.** Covered by `tests/test_contracts.py`, and exercised
indirectly by nearly every other test file.

Note that `Evidence.source_type` is a `Literal`, which is what forces
`indexing/store.py` to build its filter values through a small typed helper rather
than passing bare strings around.

## Known gaps

- **`publication_count` is documented as a ranking signal but is not used as one.**
  `VectorRetriever` populates the field, but its score is `max(hit.score)` and
  nothing else. The docstring overstates the current behaviour.
- **`SupervisorMatch.matched_topics` is not actually computed.** The retriever
  copies `query.topics` wholesale into every match rather than intersecting the
  query's topics with what the matched documents are about. The field is currently
  decorative.
- **`ZoraRecord.title` is a required `str`, while the harvester's own output model
  (`zora/output_schema.py::ZoraPublication`) declares `title: str | None`.** The
  two were meant to be field-identical so the indexer could validate harvested
  JSONL directly. A harvested record with a missing title therefore passes
  harvest-time validation and fails at index time. See
  [`../zora/README.md`](../zora/README.md).
- `ThesisPosting` has no producer in this repository yet — see
  [`../indexing/README.md`](../indexing/README.md) on the missing scraper.
