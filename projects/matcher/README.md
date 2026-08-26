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
| `POST` | `/v1/index/publications` | 202 + a run id. Fired by `themis-zora-harvest` on success |
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
`LLM_BASE_URL` points at, on every `match`, `repl` and `POST /v1/recommend` call. Against a local
endpoint that is a loopback connection; against a hosted API it is UZH personal data leaving the
university, and the difference is one environment variable with no visible signal either way.

`parsing/openai_compat.py` sends the student's query to the same endpoint. Retrieval and indexing
send nothing anywhere — only these two steps talk to an LLM, and both fall back to offline
implementations when `LLM_BASE_URL` is unset.

Not a recommendation either way: the deployment target is a UZH-hosted LibreChat endpoint, for
which this is a non-issue. It matters for development machines, where the convenient thing to
configure is a hosted API.

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
