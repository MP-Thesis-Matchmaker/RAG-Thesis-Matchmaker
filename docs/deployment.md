# Deployment

Target environment, confirmed with UZH Central Informatics on 2026-08-20:

- **Kubernetes cluster**, pulling images from a **private UZH Harbor registry**
- **Postgres** server with the **pgvector** extension available

Everything below that is not yet filled in is marked `TODO(ci)` — a value we do
not have yet. Nothing here is guessed: an unverified registry hostname or DSN in
a committed manifest is worse than an obvious blank.

## What runs where

| Component | Runtime | Trigger | Manifest |
|---|---|---|---|
| `init-db` | one-shot `Job` | before every rollout | [`k8s/init-db-job.yaml`](../k8s/init-db-job.yaml) |
| `thesis_matchmaker.zora.harvest` | `CronJob` | incremental daily, full weekly | [`k8s/zora-harvest-*.yaml`](../k8s/) |
| `thesis-matchmaker index` | `CronJob` | after each harvest | **none** — needs an `[embeddings]` image |
| `thesis-matchmaker-mcp` | `Deployment` + `Service` | always on, HTTP at `/mcp` | **none** — needs an `[mcp]` image |

The last two rows are the plan, not the state. See **Images** below for what
blocks them, and [`k8s/README.md`](../k8s/README.md) for what the committed
manifests still need filled in.

Harvesting is a **cluster** concern. It is never run in GitHub Actions, and
harvest output is never committed to git — the repository is source code, not a
datastore.

### Repository size

That rule arrived late. `data/publications.jsonl` was tracked for most of the
project and reached 47 MB before the corpus moved into the `publication` table;
it and `data/state.json` are now untracked and gitignored. Untracking only stops
the growth — **every one of those blobs is still in git history**, so a fresh
clone still pays for them.

Excising them means rewriting history (`git filter-repo`), force-pushing, and
every collaborator re-cloning. That is a deliberate, coordinated decision for the
whole team, not a cleanup to slip into a branch, and it is deliberately **not**
done yet.

## Images

**One image per deployable role, not one image for everything.** Four are planned;
`docker/<role>/Dockerfile` is where each lives.

| Image | Role | Runtime | Extras it needs | Exists |
|---|---|---|---|---|
| `docker/zora/` | ZORA harvester | `CronJob` | none | **yes** |
| `docker/indexer/` | build the vector index | `CronJob` | `[embeddings]` | no |
| `docker/serving/` | MCP adapter | `Deployment` | `[embeddings]`, `[mcp]` | no |
| — | posting scraper | `CronJob` | its own set | no — still a separate repo |

The split is not tidiness. `sentence-transformers` pulls in torch, which takes the
image from roughly 200 MB to a few GB; a harvester pod that imports neither would
otherwise pull all of it on every scheduled run. The posting scraper is the sharper
case: it is `requests` / `beautifulsoup4` / `PyYAML` / `pypdf` / `openai`, disjoint
from this repo's `httpx` / `psycopg` / `dspace-rest-client` apart from pydantic and
dotenv. One image for both means each ships the other's dependency tree.

The indexer and the serving adapter both need `[embeddings]` — the query has to be
embedded with the same model as the corpus — so splitting them buys lifecycle
separation rather than size: one is a batch job that writes and exits, the other is
a long-lived read-only process. That is reason enough to version and roll them
independently.

**`init-db` shares the harvester image**, and `docker-compose.yml` builds it for
both. That is correct rather than a shortcut: `init-db` is not a role, it is this
same distribution applying its own schema, and it needs no extras. It moves to a
shared image if `src/` is ever split.

**Images install from `uv.lock`, never from version ranges.** The Dockerfile copies
the lockfile in and runs `uv sync --locked --no-default-groups --no-editable`, so
rebuilding the same commit gives the same dependency set, and the `dev` group —
pytest, ruff — never reaches the artefact. uv itself is pinned in the
`COPY --from=ghcr.io/astral-sh/uv:0.11.17` line, because it is the tool that reads
the lock. That pin is a tag rather than a digest, which is a weaker guarantee than
it looks: a re-pushed tag would go unnoticed.

### Why `src/` is still one tree

Several images do not require several source trees: one distribution can produce
all four, differing only in entrypoint and installed extras. So the flat
`src/thesis_matchmaker/` layout and the per-role `docker/` layout are not in
conflict, they are answering different questions.

Whether to go further — a `uv` workspace of `projects/zora`, `projects/scraper`,
`projects/serving`, `projects/shared`, each its own distribution — is **open**. It
would make invariant 4 mechanical instead of social: a harvester image that never
installs `retrieval` cannot import it, and the boundary stops depending on review.
It also means four `pyproject.toml` files, a regenerated `uv.lock`, every import
path rewritten, and the whole test suite relocated.

**Trigger for deciding: the scraper migration.** With one producer and a prototype,
the seam between them is a guess; with two real producers it is observable. Not
before.

## Local equivalent

`docker-compose.yml` mirrors the cluster: a `pgvector/pgvector` container, the
init-db one-shot, and the harvester as a manually invoked job.

```bash
docker compose up -d postgres
docker compose run --rm init-db
docker compose run --rm harvester --mode incremental   # = what the CronJob does
```

For real timing locally, the timer belongs to the host, not the app — the same
two schedules as the CronJobs:

```cron
0 1 * * 1      cd <repo> && docker compose run --rm harvester --mode full
0 1 * * 0,2-6  cd <repo> && docker compose run --rm harvester --mode incremental
```

## Configuration

| Variable | Purpose | Source in the cluster |
|---|---|---|
| `DATABASE_URL` | Postgres DSN | `Secret` — `TODO(ci)` |
| `ZORA_UZH_API_KEY_FILE` | ZORA API token path | `Secret`, mounted as a file |
| `ZORA_UZH_API_KEY` | ZORA API token, inline | local only — the file above wins |
| `LLM_BASE_URL` / `LLM_API_KEY` | LibreChat / AI Buddy gateway | `Secret` |
| `EMBEDDING_MODEL` | `BAAI/bge-m3`, or `hash-fake` offline | `ConfigMap` |
| `MCP_HOST` / `MCP_PORT` | must be `0.0.0.0` in a container | `ConfigMap` |

## Open questions for Central Informatics

Blocking for deployment, not for local development against a Postgres container.

1. **Is `CREATE EXTENSION vector` permitted for our database role?** It normally
   requires superuser unless the extension is marked trusted. If it is not, the
   extension has to be pre-created for us. **Ask this first** — it is the most
   likely blocker, and `src/thesis_matchmaker/schema.sql` opens with that
   statement.
2. **pgvector version** and the Postgres major version. This is not idle
   curiosity: HNSW needs >= 0.5.0, and **`hnsw.iterative_scan` needs >= 0.8.0**.
   That setting is what stops a filtered vector search from silently returning
   fewer rows than asked for, so on an older pgvector the retrieval quality
   depends entirely on the partial indexes in `schema.sql`. The code tolerates
   its absence; the evaluation numbers would not be comparable.
3. **Own database, or a schema inside a shared one?** Affects migration scoping
   and `search_path`.
4. **Connection limit** for our role — sets the `ConnectionPool` max size.
5. **TLS**: required `sslmode`, and any CA certificate we must mount.
6. **How credentials reach the pod**: plain k8s `Secret`, or an external secret
   store / sealed-secrets setup.
7. **Harbor**: registry hostname, project name, robot-account credentials, and
   whether pushes come from GitHub Actions (needs egress plus stored secrets) or
   from a UZH-side runner.
8. **Backup and retention** policy for the harvested publication data — this is
   personal data (researcher names and affiliations), so retention is a legal
   question as much as an operational one.
9. **Storage for the raw-response cache.** Each harvest writes one JSONL dump so
   ingestion is reproducible without re-hitting ZORA. The committed CronJobs mount
   an `emptyDir`, which discards it on pod exit — so that property is currently
   lost in the cluster. Is a PVC available, and under which storage class? The
   alternative is moving the cache into Postgres as a `jsonb` table and dropping
   the volume question entirely.
10. **Resource requests and limits.** Unset on every container, because nothing
   has been profiled in a cluster. Is there a `LimitRange` in the namespace, and
   what is a reasonable ceiling for a job that processes ~22,541 records?

## Known limitations of our own tooling

Not questions for Central Informatics — things we owe ourselves.

- **`schema.sql` has no migration path.** `schema.py` fingerprints the file's raw
  text and refuses to run when the stored fingerprint differs, which is a strong
  guarantee that no DDL edit goes unapplied. The cost is that *comments are part of
  the schema's identity*: editing a comment demands `init-db --reset`, which DROPs
  every table. Hashing normalized SQL instead would fix it, and costs one reset, so
  it should ride along with a reset that is happening anyway.
- **No image is pushed anywhere automatically**, and no manifest is applied
  automatically. Both are deliberate until Harbor exists, and both are manual work
  in the meantime.
- **The Python version the image runs is never tested.** CI runs the suite on 3.11;
  the image is `python:3.12-slim`; `requires-python` says only `>=3.11`. `uv.lock`
  resolves for both, so nothing is broken today — but the interpreter production
  actually uses is the one no test has ever executed against. Closing it means
  either a 3.12 leg in the CI matrix or agreeing on a single version everywhere.
  Left open deliberately: picking the target interpreter is a deployment decision,
  not a tooling one.

## Registry

Each image is built from its own `docker/<role>/Dockerfile` (see **Images**).
Only the harvester's exists so far. Until Harbor details exist, it is built and
pushed by hand:

```bash
docker build -f docker/zora/Dockerfile -t TODO(ci)/TODO(ci)/thesis-matchmaker:<tag> .
docker push TODO(ci)/TODO(ci)/thesis-matchmaker:<tag>
```

One Harbor repository per image, so the request to Central Informatics should ask
for a **project** rather than a single repository.

There is deliberately **no** build-and-push workflow in `.github/workflows/`. The
previous one pushed to GHCR, which is the wrong registry for this deployment; a
replacement gets written when the values above are known, not before.
