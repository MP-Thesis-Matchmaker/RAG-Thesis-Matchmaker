# indexing

Turns the JSONL files that ingestion produces into a searchable vector index.
This is the *Ingestion + Indexing Pipeline* lane of
[`docs/architecture.png`](../../../docs/architecture.png): JSONL → `Document` →
content-hash diff → embed → Postgres/pgvector, with an `index_manifest` row
guarding against an embedding-model mismatch.

This is the last package in the write path. Everything downstream of it —
`retrieval/`, `pipeline/`, `adapters/` — is strictly read-only (invariant 1).

## Role in the pipeline

```
data/<source>/publications.jsonl ─┐
data/<source>/theses.jsonl       ─┴─▶ zora_to_document / posting_to_document
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
| `Embedder` | `embedder.py` | Protocol: `model_name`, `dimensions`, `embed_documents`, `embed_query`. The seam that keeps the embedding model swappable (invariant 3). |
| `HashEmbedder` | `embedder.py` | Deterministic sha256 word-sum fake, `EMBEDDING_DIM` dimensions, `model_name == "hash-fake"`. Keeps tests and CI offline. |
| `SentenceTransformerEmbedder` | `embedder.py` | The real one. Lazy-loads the model, returns normalised vectors. Needs the `embeddings` extra (pulls torch). |
| `Document` | `documents.py` | What actually gets embedded: `id`, `text`, `metadata`, `content_hash`. |
| `zora_to_document` | `documents.py` | `ZoraRecord` → `Document`. |
| `posting_to_document` | `documents.py` | `ThesisPosting` → `Document`. |
| `VectorStore` | `store.py` | Protocol: `upsert`, `delete`, `existing_hashes`, `query`, `read_manifest`, `write_manifest`, `clear`. |
| `ScoredHit` | `store.py` | One retrieved document plus its similarity score. |
| `IndexManifest` | `store.py` | Which model built the index, its width, and the document count. |
| `PgVectorStore` | `store.py` | Postgres + pgvector. Cosine distance (`<=>`), jsonb containment filters, per-batch commits. |
| `InMemoryVectorStore` | `store.py` | Exhaustive-scan implementation for offline runs. Not a mock: it is the ground truth the approximate index is checked against. |
| `Indexer` | `indexer.py` | `run()` performs the whole incremental index pass. |
| `IndexResult` | `indexer.py` | Counts returned by a run. |
| `ModelMismatchError` | `indexer.py` | Raised when the manifest's embedding model differs from the configured one. |
| `build_embedder` / `build_store` / `build_indexer` | `__init__.py` | Factories that pick implementations from `Settings`. |
| `read_manifest(settings)` | `__init__.py` | `IndexManifest | None`. `None` means "nothing indexed yet"; an unreachable database raises instead, so a dead Postgres never masquerades as an empty one. |

## Data flow

**Reads:** `<sources_path>/publications.jsonl` and `<sources_path>/theses.jsonl`;
the `index_manifest` row.

**Writes:** the `document` table and the `index_manifest` row, both in the
Postgres at `DATABASE_URL`.

### Two decisions worth knowing before changing anything here

**No chunking. One record = one embedding.** Title, abstract/description, and
keywords are joined into a single text blob and embedded whole. Publication
abstracts are short enough that this holds, and it keeps the id space identical to
the record space — a `Document.id` *is* a `ZoraRecord.id`, which is what makes the
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

Malformed JSONL lines are counted and skipped, not fatal.

## Configuration

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `embedding_model` | `EMBEDDING_MODEL` | `BAAI/bge-m3` | Passing `hash-fake` selects `HashEmbedder`; anything else loads sentence-transformers. |
| `database_url` | `DATABASE_URL` | `postgresql://matchmaker:matchmaker@localhost:5432/matchmaker` | Postgres holding `document` and `index_manifest`. Apply the schema with `thesis-matchmaker migrate`. |
| `sources_path` | `SOURCES_PATH` | `data/samples` | Directory containing the JSONL files. |

> **Watch out:** `sources_path` defaults to `data/samples`, **not** `data/`. The
> real 22,541-row harvest lives at `data/publications.jsonl`, so
> `thesis-matchmaker index` indexes 30 sample rows unless you run
> `thesis-matchmaker index --source data` or set `SOURCES_PATH=data`. Nothing
> warns you about this.

## Swappable seams

Follows the repository-wide idiom: `Protocol` definitions, concrete
implementations, and a `build_*(settings)` factory in `__init__.py`. Two seams
live here — the **embedding model** (`Embedder`) and the **vector store**
(`VectorStore`) — and both are named in invariant 3 as not-yet-final. Nothing
outside this package should import `psycopg` or `sentence_transformers`.

The fake/real pairing is deliberate: `HashEmbedder` is not a mock, it is a real
deterministic implementation, which is why CI can run the full indexing and
retrieval tests without a model download.

## Status

**Implemented and well tested.** `tests/test_embedder.py`,
`tests/test_documents.py`, `tests/test_store_contract.py`,
`tests/test_indexer.py`, `tests/test_factories.py` — including the incremental
diff, the manifest guard and the vector-width guard.

`test_store_contract.py` is parametrised over both implementations. The in-memory
parameters always run; the pgvector parameters run whenever `DATABASE_URL` points
at a Postgres with the extension, which CI always does via a
`pgvector/pgvector:pg16` service container.

## Known gaps

- **`ScoredHit.score` is documented as `[0, 1]` but is computed as
  `1 - cosine_distance`.** pgvector's `<=>` returns cosine distance over `[0, 2]`,
  exactly as Chroma did, so the score is a cosine similarity in `[-1, 1]` and
  **can be negative**. Anything downstream that treats it as a probability —
  including `SYNTHESIS_MIN_SCORE` — is working with a wrong mental model. The
  migration did not change this; it only moved where it is computed.
- **Filtered HNSW recall is not verified at corpus scale by the test suite.**
  pgvector applies `WHERE` after the index scan, which is why migration 001
  creates one partial HNSW index per `source_type` and why `PgVectorStore._tune`
  sets `hnsw.iterative_scan = strict_order` (pgvector >= 0.8 — the version on the
  UZH server is an open question, so a missing GUC is tolerated rather than
  fatal). At test-fixture sizes Postgres picks a sequential scan anyway, so the
  contract test proves the API, not the query plan. Check the plan with
  `EXPLAIN ANALYZE` against the real corpus, and note that a sequential scan is
  the *correct* plan when the filter is unselective — at present ~78% of the
  corpus is `source_type = 'publication'` with a UZH author.
- **A full build embeds everything before it writes anything.** `Indexer.run`
  calls `embed_documents` on the whole changed set, then upserts. Writes are
  batched and committed per batch, so a crashed run is resumable via the
  content-hash diff — but the embedding step holds every vector in memory first
  (22,541 x 1024 floats is a few hundred MB of Python objects, and the full ZORA
  corpus is ten times that). Interleaving embed and upsert per batch is the fix
  when the corpus grows past one department.
- **Only publications have a real producer.** `theses.jsonl` exists only as
  hand-made sample data; no web scraper lives in `src/`. That work sits on the
  unmerged `origin/webscraping` branch, so the posting half of the index is
  synthetic today.
- List-valued metadata is unfilterable by construction (see above). Any future
  filter on keywords or authors needs another scalar companion field like
  `has_uzh_author`, or a different store.
- No chunking means a long document dilutes its own embedding. Fine for abstracts;
  a problem if full texts are ever indexed.
