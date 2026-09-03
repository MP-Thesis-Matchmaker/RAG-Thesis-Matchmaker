# indexing

Turns the records that ingestion produces into a searchable vector index.
This is the *Ingestion + Indexing Pipeline* lane of
[`docs/architecture.png`](../../../../../docs/architecture.png): JSONL → `Document` →
content-hash diff → embed → Postgres/pgvector, with an `index_manifest` row
guarding against an embedding-model mismatch.

This is the last package in the write path. Everything downstream of it —
`retrieval/`, `pipeline/`, and `themis_gateway` — is strictly read-only (invariant 1).

## Role in the pipeline

```
SourceReader (publication table, or JSONL files)
                                 └─▶ zora_to_document / posting_to_document
                                            │
                                            ▼  Document(id, text, metadata, content_hash)
                                     Indexer.run
                                            │  compare content_hash against the store
                                            ├── changed → embed → upsert
                                            └── missing → delete
                                            ▼
                          Postgres: document + index_manifest (pgvector)
```

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `Embedder` | `embedder.py` | Protocol: `model_name`, `dimensions`, `max_seq_length`, `last_truncated`, `embed_documents`, `embed_query`. The seam that keeps the embedding model swappable (invariant 3). |
| `HashEmbedder` | `embedder.py` | Deterministic sha256 word-sum fake, `EMBEDDING_DIM` dimensions, `model_name == "hash-fake"`. No token window, so `max_seq_length is None`. Keeps tests and CI offline. |
| `SentenceTransformerEmbedder` | `embedder.py` | The real one. Lazy-loads the model, caps input at `max_seq_length` tokens, counts what that truncated, returns normalised vectors. Needs the `embeddings` extra (pulls torch). |
| `Document` | `documents.py` | What actually gets embedded: `id`, `text`, `metadata`, `content_hash`. |
| `prepare_text` | `documents.py` | Strips markup and collapses whitespace before the text is hashed and embedded. |
| `SourceReader` | `sources.py` | Protocol: `publications()`, `postings()`, `label`, `invalid_records`. Where records come from. |
| `PostgresSourceReader` | `sources.py` | Reads the harvested `publication` table, **UZH-authored publications only**. What a deployed indexer uses. |
| `JsonlSourceReader` | `sources.py` | Reads `publications.jsonl` / `theses.jsonl`. Still needed: `data/samples` is fixture data and CI runs without a database. |
| `zora_to_document` | `documents.py` | `ZoraPublication` → `Document`. |
| `posting_to_document` | `documents.py` | `ThesisPosting` → `Document`. |
| `VectorStore` | `store.py` | Protocol: `upsert`, `delete`, `existing_hashes`, `query`, `read_manifest`, `write_manifest`, `clear`. |
| `ScoredHit` | `store.py` | One retrieved document plus its similarity score. |
| `IndexManifest` | `store.py` | Which model built the index, its width and token window, the document count, and how many documents were truncated. |
| `PgVectorStore` | `store.py` | Postgres + pgvector. Cosine distance (`<=>`), jsonb containment filters, per-batch commits. |
| `InMemoryVectorStore` | `store.py` | Exhaustive-scan implementation for offline runs. Not a mock: it is the ground truth the approximate index is checked against. |
| `Indexer` | `indexer.py` | `run()` performs the whole incremental index pass. |
| `IndexResult` | `indexer.py` | Counts returned by a run. |
| `ModelMismatchError` | `indexer.py` | Raised when the manifest's embedding model differs from the configured one. |
| `build_embedder` / `build_store` / `build_indexer` | `__init__.py` | Factories that pick implementations from `Settings`. |
| `read_manifest(settings)` | `__init__.py` | `IndexManifest | None`. `None` means "nothing indexed yet"; an unreachable database raises instead, so a dead Postgres never masquerades as an empty one. |

## Data flow

**Reads:** whatever the `SourceReader` is pointed at — the `publication` table
with `--source db`, or `<sources_path>/publications.jsonl` and `theses.jsonl`
otherwise; plus the `index_manifest` row.

`--source db` reads **every row of both tables** — all 214,756 publications and all
695 postings. Neither query filters any more, and both stopped for the same reason:
an eligibility rule enforced here can only be revisited by re-embedding the corpus,
which turns an environment variable into hours of work. The publication filter went
on 2026-08-25 (`MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR`), the posting one on 2026-08-26
(`MATCHER_RETRIEVAL_REQUIRE_AVAILABLE_POSTING`); `sources.py` carries both arguments in full,
including what each one used to be.

**Writes:** the `document` table and the `index_manifest` row, both in the
Postgres at `DATABASE_URL`.

### Two decisions worth knowing before changing anything here

**No chunking. One record = one embedding.** Title, abstract/description, and
keywords are joined into a single text blob and embedded whole. Publication
abstracts are short enough that this holds, and it keeps the id space identical to
the record space — a `Document.id` *is* a `ZoraPublication.id`, which is what makes the
content-hash diff and the evidence back-references trivial. Introducing chunking
would break that 1:1 assumption in `retrieval/` as well as here.

**`source_type` is a real column; everything else is `jsonb`.** The column is not
denormalisation for speed — it is what makes the two partial HNSW indexes usable
at all. A partial index is only considered when its predicate is the *same
expression* as the query's `WHERE` clause, and Postgres cannot prove that
`metadata @> '{"source_type": "publication"}'` implies
`metadata ->> 'source_type' = 'publication'`. With the filter in jsonb only, both
HNSW indexes were dead weight that no query could ever reach. `source_type` is
still written into the jsonb blob as well, so a `ScoredHit` carries it without a
special case; the duplication is one short string per row and it stays inside
`PgVectorStore`.

**Metadata is one `jsonb` column, and filters are flat equality.** List and dict
fields (`authors`, `uzh_authors`, `author_authority_map`, `keywords`) are stored
as themselves — under Chroma they had to be JSON-encoded strings, and that
workaround is gone. What has *not* changed is the filter API: `query(filters=...)`
takes a flat mapping and becomes one `metadata @> '{...}'` containment predicate.
So `has_uzh_author: bool` still exists, not as a store limitation but because
"has at least one registered UZH author" has to be a scalar to be expressible in
that API. A new list field is storable and readable, but not filterable without
its own scalar companion.

### Text preparation and the token window

`documents.py` runs `prepare_text` over each part before joining: HTML tags out,
entities unescaped, whitespace runs collapsed. Tags go before the unescape, so an
escaped `&lt;p&gt;` — text *about* a tag — is not turned into a real tag and then
stripped. Measured over the 214,685-record harvest: 329 abstracts carry tags, 579
carry entities, 2,300 carry runs of three or more whitespace characters. Because
preparation happens before the emptiness filter, a part that is nothing but markup
drops out instead of contributing a blank line. It is inside the `content_hash`, so
changing it re-indexes the documents it affects.

The embedder caps input at `embedding_max_seq_length` tokens (default **1024**),
which is a hard tokenizer cut: keep the first N tokens, drop the tail.

**Why there is a cap at all.** bge-m3 ships `max_seq_length: 8192`. The attention
buffer is `batch × heads × seq²`, and `encode()` batches longest-first, so the
single longest abstract in the corpus — a 30,052-char dissertation summary at 6,822
tokens — sized the very first batch at `32 × 16 × 6822² × 4 B` = **88.77 GiB**, and
the run died two seconds in. The cap is what makes memory bounded rather than a
function of the worst record in the corpus.

**Why 1024.** Over a 2% sample the token counts are p50 240, p95 632, p99 905. A
1024 cap truncates **0.69%** of documents and costs about 1% of the average
document's tokens. 2048 would truncate only 0.04%, but its worst-case buffer is
4.29 GiB — over the cluster's 4 GiB namespace quota on its own, before bge-m3's
2.27 GB of weights. The quota picks 1024. What gets cut is also the least useful
part: the long documents are dissertation summaries whose opening states the topic
and whose tail is methods and results detail.

`IndexResult.truncated` and `index_manifest.truncated_docs` record how many
documents hit the cap, so the figure is reproducible rather than estimated.

**Two things deliberately not done.**

*Stop-word removal* is a sparse-retrieval technique (BM25, TF-IDF) where each term
is an independent dimension and function words are noise. A transformer's
self-attention uses them for syntax, negation and relation, so removing `not`
inverts a meaning rather than trimming filler; and the tokenizer is SentencePiece
subword over 100+ languages, in a corpus that is heavily German/English mixed, so
`the` is not even a cleanly removable unit. It would also not have fixed the crash:
stop words are ~30–40% of tokens, so 6,822 → ~4,300 still asks for 35 GiB.

*Chunking* is the textbook alternative to truncation, and is rejected because of
how ranking works downstream, not because of cost — see the 1:1 argument above, and
note that the retrieval unit is a **person**, scored `max(hit.score)` over their
publications. Splitting one 30k-char dissertation into sixty windows would give that
author sixty chances at the top spot, so long-form and prolific researchers would
outrank concise ones for reasons unrelated to topical fit. It would need a ranking
redesign to be safe, to buy correctness for 0.69% of documents.

### Streaming and resumability

`Indexer.run` streams: it reads each record, diffs it, and buffers changed documents
until `index_chunk_size` (default 1000) are ready, then embeds and commits that
chunk and drops it. It does **not** collect the corpus first.

Eagerly, a 215k-record run held every `Document` plus every vector before its first
write — `.tolist()` alone materialises 2.2e8 Python floats, roughly 7 GB, against a
4 GiB namespace quota — and any failure during the hours of embedding discarded the
whole run. Chunking bounds peak memory and makes `PgVectorStore`'s per-batch commits
reachable, which is what actually delivers the resumability that store claims: rows
already written match on content hash next time and are skipped, so an interrupted
run continues instead of restarting.

Three orderings matter, and are commented in the code because the eager version
satisfied them by accident: `reader.invalid_records` is only final once the reader is
drained; `removed` needs the complete id set, so deletion waits for the last chunk;
and `skipped` is `total - embedded`, since there is no document list to measure.

### Incremental indexing

`Indexer.run` asks the store for `{id: content_hash}` of everything it already
holds, then:

- `changed` = documents whose hash differs from the stored one, or that are new →
  embedded and upserted. Unchanged documents are never re-embedded.
- `removed` = stored ids absent from the current input → deleted.

The `content_hash` is a sha256 over `json.dumps({text, metadata}, sort_keys=True)`,
so a metadata-only change (a corrected department, a newly added language) also
triggers a re-index. This makes a full snapshot replay idempotent and makes the
hash — not the harvester — the authoritative add/update/delete decision.

The `index_manifest` row records which embedding model built the index and how
wide its vectors are. A different model raises `ModelMismatchError` rather than
silently mixing vector spaces, which would produce plausible-looking nonsense;
`--rebuild` starts over deliberately. A different *width* is a harder failure: the
`document.embedding` column is `vector(1024)`, so changing model width needs a
migration that alters the column, and the error says so.

The manifest also records `max_seq_length`, under the same guard and for a subtler
reason: changing the token window changes every vector but **no** document's content
hash, so a plain re-index would skip everything and leave the index holding two
incompatible generations of vector at once.

Malformed JSONL lines are counted and skipped, not fatal.

### What the score is, and why it is not `[0, 1]`

`ScoredHit.score` is a **cosine similarity over `[-1, 1]`, and it can be negative.**
pgvector's `<=>` returns cosine *distance* over `[0, 2]` (exactly as Chroma did), so
`PgVectorStore`'s `1 - (embedding <=> v)` is the similarity, not a normalised one;
`InMemoryVectorStore` returns the same quantity from `_cosine`. Vectors are
unit-normalised (`normalize_embeddings=True`, redundant with bge-m3's own final
module), so the whole range is genuinely reachable rather than an artefact.

Until 2026-08-28 the `Field` description claimed `[0, 1]`, which is where the
confusion came from — a percentage-shaped number invites a percentage-shaped
threshold, and `MATCHER_SYNTHESIS_MIN_SCORE` is not one. **It is in cosine units.**

Two transforms into `[0, 1]` were considered and rejected:

| | effect | why not |
|---|---|---|
| affine, `(cos + 1) / 2` | lossless, reversible as `2s - 1`, order-preserving | compresses the band: an *orthogonal* — i.e. irrelevant — document reads `0.5`, so a displayed score never looks low even when it should |
| clamp, `max(0, cos)` | keeps the number readable | irreversible, and at the storage layer. Collapses "unrelated" and "opposed" into one value that no downstream layer can ever separate again |

Nothing in the system needs `[0, 1]` anyway: `VectorRetriever._rank`'s sorts, the
`max()` in `_group_by_person` and the `>=` in `synthesis/llm.py` are all
sign-agnostic. So the score stays signed and the documentation was corrected to
match the code, rather than the reverse. `test_store_contract.py` pins both
endpoints — a rescale or a clamp keeps every ordering and every other store test
green, so nothing else would catch one.

#### What the first measurement found (2026-08-28)

Nine queries — five on-topic probes, four out-of-domain controls — against the full
215,451-document index. Full data, method and analysis:
[`docs/score-calibration.md`](../../../../../docs/score-calibration.md). Reproduce with
[`scripts/score_distribution.py --control`](../../../../../scripts/score_distribution.py).
The four results that bear on this section:

- **The negative region is empty.** Not one row scored below zero against any query;
  the lowest observed score was `0.115`. bge-m3's anisotropy, measured rather than
  assumed. This does not reverse the decision above — a clamp would still be
  irreversible for no gain — but the reversibility argument is now known to protect an
  empty region. Re-check after any re-embed or model change.
- **Retrieval does separate signal from noise.** Out-of-domain controls peak at 0.542
  (publications) and 0.431 (postings); on-topic queries bottom out at 0.605 and 0.564.
  No overlap, so a threshold is a coherent mechanism here.
- **The two source types have incompatible ranges.** The publication noise floor sits
  0.022 below the posting signal ceiling, so the admissible band for a *single*
  threshold is `[0.542, 0.564]` — and it lies entirely inside the region that trims
  postings while leaving publications untouched. Since the person key never joins the
  two sources, that deletes supervisors with advertised open positions.
- **Absolute cosine is a weak instrument here.** Best-match scores vary 0.605–0.734 by
  topic alone, tracking corpus density rather than match quality. Query-relative
  scoring belongs with `ranking`, not here.

`MATCHER_SYNTHESIS_MIN_SCORE` was therefore **retired and split in two** —
`MATCHER_SYNTHESIS_MIN_SCORE_PUBLICATION` at 0.57 and `..._POSTING` at 0.48, each
mid-band with room either side. `SupervisorMatch.score_source` records which one
applies to a given person.

## Configuration

The subset of `MatcherSettings` this sub-package reads; the whole list is in
[the package README](../../../README.md#configuration).

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `embedding_model` | `MATCHER_EMBEDDING_MODEL` | `BAAI/bge-m3` | Passing `hash-fake` selects `HashEmbedder`; anything else loads sentence-transformers. |
| `database_url` | `DATABASE_URL` | `postgresql://matchmaker:matchmaker@localhost:5432/matchmaker` | Postgres holding `document` and `index_manifest`. Create the schema with `themis-init-db`. |
| `sources_path` | `MATCHER_SOURCES_PATH` | `data/samples` | Default `--source`. A directory of JSONL files, or `db` for the harvested table. |
| `embedding_max_seq_length` | `MATCHER_EMBEDDING_MAX_SEQ_LENGTH` | `1024` | Token cap per document. Recorded in the manifest and guarded: changing it needs `--rebuild`. |
| `embedding_batch_size` | `MATCHER_EMBEDDING_BATCH_SIZE` | `16` | Documents per forward pass. Bounds the attention buffer together with the cap; cannot replace it. |
| `embedding_device` | `MATCHER_EMBEDDING_DEVICE` | unset | Torch device the model loads onto; unset means auto-detect. Deliberately *not* manifest-guarded -- unlike the token cap it changes neither the model nor which text is embedded. Set `cpu` on a Mac if a run dies with no message: auto-detect picks `mps`, and a short-of-memory `mps` load aborts the process instead of raising. |
| `index_chunk_size` | `MATCHER_INDEX_CHUNK_SIZE` | `1000` | Documents embedded and committed per round trip. Lower it to cut peak memory. |

> **Watch out:** `sources_path` still defaults to `data/samples`, so a bare
> `themis-matcher index` indexes the sample rows. The real harvest now lives in
> Postgres: use `themis-matcher index --source db`, or set `MATCHER_SOURCES_PATH=db`.
> The output line reports which source was used, so at least it is visible.

## Swappable seams

Follows the repository-wide idiom: `Protocol` definitions, concrete
implementations, and a `build_*(settings)` factory in `__init__.py`. Three seams
live here — the **embedding model** (`Embedder`), the **vector store**
(`VectorStore`) and the **record source** (`SourceReader`). The first is still
open per invariant 3; the vector store is now decided (Postgres + pgvector, a
constraint of the deployment environment) but stays behind the protocol.

The fake/real pairing is deliberate: `HashEmbedder` is not a mock, it is a real
deterministic implementation, which is why CI can run the full indexing and
retrieval tests without a model download.

## Status

**Implemented and well tested.** `projects/matcher/tests/test_embedder.py`,
`projects/matcher/tests/test_documents.py`, `projects/matcher/tests/test_store_contract.py`,
`projects/matcher/tests/test_indexer.py`, `projects/matcher/tests/test_factories.py` — including the incremental
diff, the manifest guard and the vector-width guard.

`test_store_contract.py` is parametrised over both implementations. The in-memory
parameters always run; the pgvector parameters run whenever `DATABASE_URL` points
at a Postgres with the extension, which CI always does via a
`pgvector/pgvector:pg16` service container.

## Known gaps

- **Filtered HNSW recall is not verified at corpus scale by the test suite.**
  pgvector applies `WHERE` after the index scan, which is why `schema.sql` creates
  one partial HNSW index per `source_type` and why `PgVectorStore._tune` sets
  `hnsw.iterative_scan = strict_order` (pgvector >= 0.8 — the version on the UZH
  server is an open question, so a missing GUC is tolerated rather than fatal). At
  test-fixture sizes Postgres picks a sequential scan anyway, so the contract test
  proves the API, not the query plan. Check it with `EXPLAIN ANALYZE` against the
  real corpus.

  The selectivity half of this argument has now flipped twice, and the second flip
  turned it into a live defect. While `--source db` filtered to UZH-authored
  publications, every row in a DB-sourced index satisfied
  `metadata @> '{"has_uzh_author": true}'`, so the predicate was unselective and
  merely wasteful. Since 2026-08-25 the source is unfiltered — 53,545 of 214,756
  publications carry a UZH author — so with `MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR=true` that
  predicate discards ~75% of the candidates the HNSW scan returns, **after** the
  scan, which is precisely the under-return that `schema.sql`'s partial-index comment
  describes. `VectorRetriever` over-fetches 4x to compensate; the real fix is a third
  partial index matching the predicate, and it needs a schema reset. The two existing
  partial HNSW indexes on `source_type` are unaffected and still do real work, since
  the index holds both publications and postings.
- **Both halves of the index now have real producers.** `themis-zora` writes
  `publication`, `themis-scraper` writes `posting`, and `PostgresSourceReader` reads
  both. `theses.jsonl` stays because the offline path and CI need a source with no
  database behind it -- but note those 20 fixtures are unrepresentative of scraped
  reality in two ways: each names exactly one supervisor and exactly one degree
  level, where a quarter of real topics name nobody and half are open to two.
- **Postings carry three boolean companions**, `degree_bachelor` /
  `degree_master` / `degree_phd`, for the reason the next bullet gives. They are
  the `has_uzh_author` pattern applied to a second list-valued field, and they are
  what a level-filtered posting query actually matches on.
- **`is_available` is the same pattern applied to a rule rather than a field.**
  Retrieval wants "not assigned and not private", and the filter API has no negation
  and no `IN` list, so the predicate is evaluated once at index time and stored as a
  boolean. Note that a status-less posting carries no `status` key at all (`_build`
  drops `None`), so equality on `status` could not have expressed it either.
- List-valued metadata is unfilterable by construction (see above). Any future
  filter on keywords or authors needs another scalar companion field like
  `has_uzh_author`, or a different store.
- **A long document is truncated, not chunked, and 0.69% of the corpus is.** One
  record is still one embedding, so anything past `embedding_max_seq_length` tokens
  is dropped rather than diluted. Measured and recorded per run
  (`index_manifest.truncated_docs`), and defensible for abstracts — the affected
  records are dissertation summaries losing their methods tail. It would stop being
  defensible if full texts were ever indexed, and that is the point at which the
  chunking-versus-ranking argument above has to be reopened.
