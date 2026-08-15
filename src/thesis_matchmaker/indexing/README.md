# indexing

Turns the JSONL files that ingestion produces into a searchable vector index.
This is the *Ingestion + Indexing Pipeline* lane of
[`docs/architecture.png`](../../../docs/architecture.png): JSONL → `Document` →
content-hash diff → embed → ChromaDB, with `manifest.json` guarding against an
embedding-model mismatch.

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
                                   ChromaDB (data/index/) + manifest.json
```

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `Embedder` | `embedder.py` | Protocol: `model_name`, `embed_documents`, `embed_query`. The seam that keeps the embedding model swappable (invariant 3). |
| `HashEmbedder` | `embedder.py` | Deterministic sha256 word-sum fake, 64 dimensions, `model_name == "hash-fake"`. Keeps tests and CI offline and fast. |
| `SentenceTransformerEmbedder` | `embedder.py` | The real one. Lazy-loads the model, returns normalised vectors. Needs the `embeddings` extra (pulls torch). |
| `Document` | `documents.py` | What actually gets embedded: `id`, `text`, `metadata`, `content_hash`. |
| `zora_to_document` | `documents.py` | `ZoraRecord` → `Document`. |
| `posting_to_document` | `documents.py` | `ThesisPosting` → `Document`. |
| `VectorStore` | `store.py` | Protocol: `upsert`, `delete`, `existing_hashes`, `query`. The seam that keeps the vector store swappable. |
| `ScoredHit` | `store.py` | One retrieved document plus its similarity score. |
| `ChromaVectorStore` | `store.py` | Chroma `PersistentClient`, `hnsw:space=cosine`. |
| `Indexer` | `indexer.py` | `run()` performs the whole incremental index pass. |
| `IndexResult` | `indexer.py` | Counts returned by a run. |
| `ModelMismatchError` | `indexer.py` | Raised when the manifest's embedding model differs from the configured one. |
| `build_embedder` / `build_store` / `build_indexer` | `__init__.py` | Factories that pick implementations from `Settings`. |

## Data flow

**Reads:** `<sources_path>/publications.jsonl` and `<sources_path>/theses.jsonl`;
`<vector_store_path>/manifest.json`.

**Writes:** the Chroma collection under `<vector_store_path>`, and `manifest.json`
(`{embedding_model, document_count, sources_dir}`).

### Two decisions worth knowing before changing anything here

**No chunking. One record = one embedding.** Title, abstract/description, and
keywords are joined into a single text blob and embedded whole. Publication
abstracts are short enough that this holds, and it keeps the id space identical to
the record space — a `Document.id` *is* a `ZoraRecord.id`, which is what makes the
content-hash diff and the evidence back-references trivial. Introducing chunking
would break that 1:1 assumption in `retrieval/` as well as here.

**Chroma metadata is scalar-only.** List and dict fields (`authors`,
`uzh_authors`, `author_authority_map`, `keywords`) are therefore stored as JSON
strings, which means **they cannot be filtered on**. The scalar
`has_uzh_author: bool` exists for exactly one reason: to give `retrieval/chroma.py`
something filterable that stands in for "this publication has at least one
registered UZH author". If you add a new list field, expect the same treatment.

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

`manifest.json` records which embedding model built the index. Running with a
different model raises `ModelMismatchError` rather than silently mixing vector
spaces, which would produce plausible-looking nonsense. Use `--rebuild` to start
over deliberately.

Malformed JSONL lines are counted and skipped, not fatal.

## Configuration

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `embedding_model` | `EMBEDDING_MODEL` | `BAAI/bge-m3` | Passing `hash-fake` selects `HashEmbedder`; anything else loads sentence-transformers. |
| `vector_store_path` | `VECTOR_STORE_PATH` | `data/index` | Where Chroma persists. Gitignored. |
| `sources_path` | `SOURCES_PATH` | `data/samples` | Directory containing the JSONL files. |
| `collection_name` | `COLLECTION_NAME` | `matchmaker` | Chroma collection. |

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
outside this package should import `chromadb` or `sentence_transformers`.

The fake/real pairing is deliberate: `HashEmbedder` is not a mock, it is a real
deterministic implementation, which is why CI can run the full indexing and
retrieval tests without a model download.

## Status

**Implemented and well tested.** `tests/test_embedder.py` (5),
`tests/test_documents.py` (7), `tests/test_store.py` (5),
`tests/test_indexer.py` (6), `tests/test_factories.py` (3) — including the
incremental diff and the manifest guard.

## Known gaps

- **`ScoredHit.score` is documented as `[0, 1]` but is computed as
  `1.0 - cosine_distance`.** Cosine distance in Chroma ranges over `[0, 2]`, so
  the score is a cosine similarity in `[-1, 1]` and **can be negative**. Anything
  downstream that treats the score as a probability — including
  `SYNTHESIS_MIN_SCORE` — is working with a wrong mental model.
- **Only publications have a real producer.** `theses.jsonl` exists only as
  hand-made sample data; no web scraper lives in `src/`. That work sits on the
  unmerged `origin/webscraping` branch, so the posting half of the index is
  synthetic today.
- List-valued metadata is unfilterable by construction (see above). Any future
  filter on keywords or authors needs another scalar companion field like
  `has_uzh_author`, or a different store.
- No chunking means a long document dilutes its own embedding. Fine for abstracts;
  a problem if full texts are ever indexed.
