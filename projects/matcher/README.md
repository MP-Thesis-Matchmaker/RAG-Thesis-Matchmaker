# themis-matcher

The matching engine: everything between a student's sentence and a ranked list of
supervisors, and since 2026-08-26 the HTTP service that fronts it. Sub-packages
each have their own README, and the four seam packages follow the same shape —
`base.py` is a `Protocol`, sibling modules are implementations, and `__init__.py`
exposes a `build_*(settings)` factory that picks one from
`themis_shared.config.Settings`.

| Sub-package | What it does |
|---|---|
| [`indexing/`](src/themis_matcher/indexing/README.md) | JSONL or Postgres → `Document` → content-hash diff → pgvector |
| [`retrieval/`](src/themis_matcher/retrieval/README.md) | Filtered vector queries, grouping per person, and the only ranking there is |
| [`parsing/`](src/themis_matcher/parsing/README.md) | Free text → `ParsedQuery`; rule-based by default, LLM optional |
| [`synthesis/`](src/themis_matcher/synthesis/README.md) | Grounded prose; weak matches short-circuit before any LLM call |
| [`pipeline/`](src/themis_matcher/pipeline/README.md) | Application-service functions the API and CLI call |
| [`api/`](src/themis_matcher/api/) | The HTTP front door: match, recommend, and the index triggers. Routes only; no logic |

Every seam ships a real offline implementation — `HashEmbedder`,
`InMemoryVectorStore`, `FakeRetriever`, `RuleBasedExtractor`,
`TemplateSynthesizer` — not mocks. That is what lets CI run the whole pipeline
end to end with no model download, no database and no network.

## Install and run

```bash
uv sync --package themis-matcher                  # offline implementations only
uv sync --package themis-matcher --extra embeddings   # real BGE-M3 (pulls torch)

uv run themis-matcher index --source db
uv run themis-matcher match "NLP thesis on retrieval-augmented generation"
uv run themis-matcher repl
uv run themis-matcher serve                       # the HTTP API, default :8100
```

`themis-matcher init-db` also works, delegating to the command
[`themis-shared`](../../libs/shared/README.md) owns.

## The HTTP API

| Method | Path | What |
|---|---|---|
| `GET` | `/v1/health` | Liveness. Touches no database, so a Postgres blip does not cycle pods |
| `GET` | `/v1/index/status` | The manifest, or 409 `index_not_built` |
| `POST` | `/v1/match` | Ranked `SupervisorMatch` list. What the gateway's `find_researchers` calls |
| `POST` | `/v1/recommend` | Grounded prose. What `recommend_supervisors` calls |
| `POST` | `/v1/index/publications` | 202 + a run id. Fired by `themis-zora harvest` on success |
| `POST` | `/v1/index/postings` | 202 + a run id. Fired by `themis-scraper` on success |
| `GET` | `/v1/index/runs`, `/v1/index/runs/{id}` | Run history and one run's state |

Four things about it are deliberate.

**Triggers return a receipt, not a result.** A cold full index is measured in days
under the cluster's CPU quota; even a warm one is minutes. The run happens in a
background thread and the caller polls. The slot is claimed on the request thread,
though, so a 202 is never a 409 in disguise.

**There is no rebuild endpoint.** `--rebuild` truncates the `document` table —
215,451 rows that cost days of CPU to embed — and stays CLI-only, needing a human
with shell access rather than one POST.

**Only one run at a time, enforced by Postgres.** A partial unique index on
`index_run` refuses the second; two concurrent runs would interleave their upserts
and their orphan sweeps. Every committed chunk bumps a heartbeat, so a run whose
process died is reaped rather than holding the slot forever.

**Every route is `def`, never `async def`.** Nothing underneath is async — the
psycopg pool is synchronous, `encode` blocks, `LLMClient.chat` is a blocking
`httpx.post` — so Starlette's threadpool is where they belong. Note the ceiling
this implies: `db.get_pool` caps at `max_size=5`, so five concurrent
database-touching requests is the limit before `PoolTimeout`.

## What leaves this process

Worth stating plainly, because it is easy to miss and the pipeline does not warn.

`synthesis/llm.py` puts the retrieved candidates into the prompt: **supervisor and author names,
publication titles, abstracts and posting descriptions**. All of it goes to whatever
`MATCHER_LLM_BASE_URL` points at, on every `match`, `repl` and `POST /v1/recommend` call. Against a local
endpoint that is a loopback connection; against a hosted API it is UZH personal data leaving the
university, and the difference is one environment variable with no visible signal either way.

`parsing/openai_compat.py` sends the student's query to the same endpoint. Retrieval and indexing
send nothing anywhere — only these two steps talk to an LLM, and both fall back to offline
implementations when `MATCHER_LLM_BASE_URL` is unset.

Not a recommendation either way: the deployment target is a UZH-hosted LibreChat endpoint, for
which this is a non-issue. It matters for development machines, where the convenient thing to
configure is a hosted API.

## Configuration

`MatcherSettings` in [`config.py`](src/themis_matcher/config.py), a `MATCHER_`-prefixed
subclass of the shared `Settings`. Seventeen variables, every one read by this
package and no other — which is why they live here rather than in `themis-shared`.
The sub-package READMEs below repeat the handful each of them uses; this is the
whole list.

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `llm_base_url` | `MATCHER_LLM_BASE_URL` | unset | OpenAI-compatible endpoint for query parsing and prose synthesis. Unset selects the offline rule-based parser and the template synthesiser. |
| `llm_model` | `MATCHER_LLM_MODEL` | `llama3.1` | Model name sent to that endpoint. |
| `llm_api_key` | `MATCHER_LLM_API_KEY` | unset | Bearer token for it. |
| `llm_reasoning_effort` | `MATCHER_LLM_REASONING_EFFORT` | unset | `none` / `low` / `medium` / `high`, for reasoning models only. Leave unset otherwise: some servers reject fields they do not know. |
| `synthesis_min_score` | `MATCHER_SYNTHESIS_MIN_SCORE` | `0.0` | Below this, the answer says there is no strong match instead of overselling a weak one. **In cosine units, not a percentage** — the score is a cosine similarity over `[-1, 1]`. Measured and deliberately left inert: [`docs/score-calibration.md`](../../docs/score-calibration.md) found no single value that does not delete posting-backed supervisors. Meaningless with `hash-fake`, whose scores are arbitrary. |
| `embedding_model` | `MATCHER_EMBEDDING_MODEL` | `BAAI/bge-m3` | `hash-fake` selects the deterministic offline embedder — no download, no network. |
| `embedding_max_seq_length` | `MATCHER_EMBEDDING_MAX_SEQ_LENGTH` | `1024` | Token cap before embedding. **Changing it invalidates every vector in the index**, and `document.embedding` is `vector(1024)`. |
| `embedding_batch_size` | `MATCHER_EMBEDDING_BATCH_SIZE` | `16` | Documents per forward pass. Bounds the attention buffer with the cap above; cannot substitute for it. |
| `embedding_device` | `MATCHER_EMBEDDING_DEVICE` | auto-detect | Passed to torch verbatim. Set `cpu` on a Mac if a run dies with no traceback at all. |
| `index_chunk_size` | `MATCHER_INDEX_CHUNK_SIZE` | `1000` | Documents embedded and committed per round trip. Keeps peak memory flat and makes an interrupted run resumable. |
| `sources_path` | `MATCHER_SOURCES_PATH` | `data/samples` | Where the indexer reads from. `db` indexes the harvested tables instead. |
| `retrieval_require_uzh_author` | `MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR` | `false` | Whether a publication needs a registered UZH author to be retrievable. Flipping it needs no re-index. |
| `retrieval_require_available_posting` | `MATCHER_RETRIEVAL_REQUIRE_AVAILABLE_POSTING` | `true` | Whether a posting has to still be open. No ranking strategy softens this one. |
| `retrieval_ranking_strategy` | `MATCHER_RETRIEVAL_RANKING_STRATEGY` | `uzh_first` | `uzh_first` or `score`. A `Literal`, so a typo fails at load naming the valid values. |
| `api_host` | `MATCHER_API_HOST` | `127.0.0.1` | Bind address for `themis-matcher serve`. The image sets `0.0.0.0`. |
| `api_port` | `MATCHER_API_PORT` | `8100` | Port. 8100 so it does not collide with the gateway's 8000 on a laptop. |
| `index_run_heartbeat_timeout_s` | `MATCHER_INDEX_RUN_HEARTBEAT_TIMEOUT_S` | `900` | How long an index run may go without committing a chunk before its slot is released. Bounds the gap between chunks, not the run. |

Two more arrive inherited and stay **unprefixed**: `DATABASE_URL` and
`MATCHER_BASE_URL`. They are the shared floor's — more than one member reads each —
and a `validation_alias` pins them so the `MATCHER_` prefix cannot rename them.
`MATCHER_BASE_URL` is not `MATCHER_` in the prefix sense; it is the matcher's
address, and the gateway, harvester and scraper set the same variable.

> A stale unprefixed name is **silent**: `extra="ignore"` means `EMBEDDING_MODEL`
> is not rejected, just not read, and the default quietly applies.
> `get_settings()` logs a warning for the pre-2026-08-27 spellings.

## Dependencies

`fastapi` and `uvicorn` are core, not an extra: CI's `offline` and `pgvector` jobs
install with `--all-packages` and no extras, so an extra would leave the API tests
uncollectable there — and one image serves both the API and the batch indexer, so
they are installed either way.

`httpx` is used by `llm.py`. `sentence-transformers` and `torch` sit behind the
`embeddings` extra, and this is the one distribution where `torch` is a direct
dependency, which is what makes the workspace root's CPU-wheel override apply. See
the comments in `pyproject.toml`.

## Not here

Ranking is one line inside `retrieval/vector.py` — `score = max(hit.score)` in
`_group_by_person`, plus a two-level sort for `uzh_first`. The separate `ranking`
package the architecture calls for does not exist yet. The slot is between
retrieve and synthesise.

**What it has to fix first is the person key, not the score.** `_group_by_person`
groups on an exact name string, and the two sources spell people differently:
`"Davide Scaramuzza"` on a posting against `"Scaramuzza, D"` on a paper. Measured
2026-08-26 — 403 distinct supervisor names, **0** matching any of the 2,942
`uzh_authors`. So `publication_count` and `posting_count` are effectively never
both non-zero, and a multi-signal score combining "publishes here" with "has an
open position" would be scoring a join that never happens. Full detail and the
proposed fix in
[`retrieval/README.md`](src/themis_matcher/retrieval/README.md#known-gaps).
