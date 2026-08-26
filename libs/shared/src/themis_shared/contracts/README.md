# contracts

The shared vocabulary of the system. Every other package speaks to its neighbours
through these Pydantic models rather than through raw dicts, so a change to what
"a publication" or "a match" means happens in exactly one place. In
[`docs/architecture.png`](../../../docs/architecture.png) these are the rounded
boxes on the right of the *Data Extraction* lane (`ZoraPublication`,
`ThesisPosting`) and the boxes threading through the *Retrieval + Generation* lane
(`ParsedQuery`, `SupervisorMatch`).

This package intentionally has **no dependencies on any sibling package**. It sits
at the bottom of the import graph; everything imports it, it imports nothing of
ours. Keeping it that way is what stops the module seams from collapsing.

**Every data model lives here — including the harvester's own output shapes.**
Until 2026-08-24 `zora/` kept a parallel set (its own `ZoraPublication`, its own
`AuthorAuthority`, and the only copies of the person and org-unit shapes) whose
docstring claimed field-alignment with this package. They drifted in three ways at
once: `title` optional on one side and required on the other, an
`owning_collection_uuid` that existed on only one, and a duplicated authority type
that nothing pinned together. `zora/` was also the only package importing nothing
from here. One model per shape is what makes that class of bug impossible rather
than merely documented; `zora/mapping.py` now holds the mapping and imports the
models.

## Role in the pipeline

```
ingestion ──writes──▶ ZoraPublication / ZoraPerson / ZoraOrgUnit / ThesisPosting
                                                  ──read by──▶ indexing
parsing   ──emits──▶  ParsedQuery                 ──read by──▶ retrieval
retrieval ──emits──▶  SupervisorMatch (+ Evidence) ──read by──▶ synthesis, adapters
```

Pure data. No I/O, no business logic, no imports from sibling packages.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `DegreeLevel` | `sources.py` | `StrEnum` of `bachelor` / `master` / `phd`. Shared by `ParsedQuery` and `ThesisPosting`, but **not symmetrically**: a query names one level, a posting is open to a list of them, so comparing them is an overlap test rather than equality. |
| `PostingStatus` | `sources.py` | `StrEnum` of `open` / `assigned` / `pending` / `private`. Departmental pages mark a topic taken rather than removing it. |
| `Supervisor` | `sources.py` | `name`, plus optional `email`, `profile_url`, `chair`. Only `name` is dependable: 96 of 264 scraped entries carry nothing else. |
| `AuthorAuthority` | `sources.py` | One author's typed identifier: `type` (`cris` \| `orcid`) and `id`. `cris` is a CRIS Person UUID that resolves in `person`; `orcid` is a bare ORCID with no local Person record, i.e. **unknown affiliation, not external**. The `Literal` is load-bearing — every candidate fix for the `uzh_authors` gap is expressed in terms of it. |
| `ZoraPublication` | `sources.py` | One ZORA publication, and exactly what the harvester writes as a `publication` row. 16 fields: `id`, `title`, `abstract`, `authors`, `uzh_authors`, `author_authority_map`, `year`, `keywords`, `department`, `owning_collection_uuid`, `language`, `publication_type`, `doi`, `url`, `accessioned`. |
| `ZoraPerson` | `sources.py` | One DSpace-CRIS Person entity: `uuid`, `display_name`, `family_name`, `given_name`, `orcid`, `handle`, `url`, `accessioned`. What a `cris`-typed `AuthorAuthority.id` resolves to. Carries **no affiliation** — upstream has none on these items. |
| `ZoraOrgUnit` | `sources.py` | One node of the UZH community tree: `uuid`, `name`, `parent_uuid`, `faculty_uuid`, `depth`, `handle`, `subject_id`, `collection_uuid`, `collection_name`. ZORA's OrgUnit *entity type* is empty upstream; communities are the org structure. |
| `ThesisPosting` | `sources.py` | One scraped open thesis position: `id`, `title`, `description`, `supervisors[]`, `faculty`, `department`, `degree_levels[]`, `status`, `keywords`, `language`, `url`, `listed_on`, `source_id`, `scraped_at`. |
| `ResearcherProfile` | `sources.py` | A researcher as their own department page describes them: `id`, `name`, `email`, `role`, `research_interest`, `research_field`, `research_group`, `bio`, `personal_website`, `profile_url`, `faculty`, `department`, `source_id`, `scraped_at`. Distinct from `ZoraPerson`: same kind of human, different source and different claim. |
| `ApplicationProcess` | `sources.py` | How to apply at one unit, for one level: `id`, `degree_level`, `description`, `relevant_links[]`, `url`, `faculty`, `department`, `source_ids[]`, `scraped_at`. |
| `ParsedQuery` | `retrieval.py` | A student's free-text interest turned into structure: `topics`, `keywords`, `degree_level`, `department`, `raw_query`. |
| `Evidence` | `retrieval.py` | One citable item backing a recommendation: `source_type` (`publication` \| `thesis_posting`), `source_id`, `title`, `url`, `year`. |
| `SupervisorMatch` | `retrieval.py` | One ranked person: `supervisor`, `department`, `score`, `matched_topics`, `publication_count`, `posting_count`, `evidence[]`. |

All thirteen are re-exported from `contracts/__init__.py`, so
`from thesis_matchmaker.contracts import ZoraPublication` is the intended import
path.

## Nullability follows the database

Where a column is nullable, the field is `| None`. That sounds obvious and was not
the case: `ZoraPublication.title`, `ThesisPosting.title` and `ThesisPosting.url`
were required `str`, so `indexing/sources.py` satisfied them with `or ""` when
reading rows — inventing an empty-string title rather than admitting the value was
missing. The models now say what the table says, and those coalesces are gone.

## Data flow

**Reads:** nothing. **Writes:** nothing.

The models are *validated* at three boundaries:

- `ZoraPublication` / `ZoraPerson` / `ZoraOrgUnit` — in `zora/mapping.py`, on the
  way from the harvester's normalized dicts into Postgres. A malformed record fails
  the run rather than reaching a table.
- `ZoraPublication` / `ThesisPosting` — in `indexing/sources.py`, both when
  `JsonlSourceReader` parses `data/samples` (a malformed line is counted and
  skipped, not fatal) and when `PostgresSourceReader` builds models from rows.
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

**Implemented and tested.** `tests/test_contracts.py` covers the source models
including `AuthorAuthority`'s `cris`/`orcid` discrimination, the two entity
mirrors, and the nullability above; `tests/zora/test_mapping.py` covers the
harvester's mapping onto them; nearly every other test file exercises them
indirectly. `ResearcherProfile`, `ApplicationProcess` and `ParsedQuery` have no
direct contract-level test.

Note that `Evidence.source_type` is a `Literal`, which keeps the two source kinds
from being spelled differently in different packages.

## Known gaps

- **`publication_count` is documented as a ranking signal but is not used as one.**
  `VectorRetriever` populates the field, but its score is `max(hit.score)` and
  nothing else. The docstring overstates the current behaviour.
- **`SupervisorMatch.matched_topics` is not actually computed.** The retriever
  copies `query.topics` wholesale into every match rather than intersecting the
  query's topics with what the matched documents are about. The field is currently
  decorative.
- **`uzh_authors` is wider than its name.** It holds authors carrying *any* DSpace
  authority, which includes ORCID-only co-authors of unknown affiliation — 38,157
  publications' worth. The field description says so now, and
  `author_authority_map` distinguishes the kinds, but the eligibility rule itself
  is still the any-authority one. See the Known gaps section of
  [`../zora/README.md`](../zora/README.md); fixing it is the next task.
- **`ThesisPosting.language` has no producer.** The column, this field and the
  reader all exist; `scraper/normalize.py::to_posting` never sets it, so it is
  always `None`.
- **No contract-level test pins the models against the SQL schema.** `schema.sql`
  is the other half of every shape here, and nothing fails if the two disagree —
  which is how `accessioned` lived as a column-only field, spliced onto rows after
  validation, until 2026-08-24.
- `ZoraPerson` and `ZoraOrgUnit` have **no consumer outside `zora/`** yet.
  `indexing.SourceReader` exposes only `publications()` and `postings()`, so
  nothing joins a publication to a person or an org unit at index or query time.
  The posting-linkage work is what will use them.
