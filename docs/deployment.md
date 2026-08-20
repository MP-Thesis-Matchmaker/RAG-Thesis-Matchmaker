# Deployment

Target environment, confirmed with UZH Central Informatics on 2026-08-20:

- **Kubernetes cluster**, pulling images from a **private UZH Harbor registry**
- **Postgres** server with the **pgvector** extension available

Everything below that is not yet filled in is marked `TODO(ci)` — a value we do
not have yet. Nothing here is guessed: an unverified registry hostname or DSN in
a committed manifest is worse than an obvious blank.

## What runs where

| Component | Runtime | Trigger |
|---|---|---|
| `migrate` | one-shot `Job` | before every rollout |
| `thesis-matchmaker-mcp` | `Deployment` + `Service` | always on, HTTP at `/mcp` |
| `thesis_matchmaker.zora.harvest` | `CronJob` | incremental daily, full weekly |
| `thesis-matchmaker index` | `CronJob` | after each harvest |

Harvesting is a **cluster** concern. It is never run in GitHub Actions, and
harvest output is never committed to git — the repository is source code, not a
datastore.

## Local equivalent

`docker-compose.yml` mirrors the cluster: a `pgvector/pgvector` container, the
migrate one-shot, and the harvester as a manually invoked job.

```bash
docker compose up -d postgres
docker compose run --rm migrate
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
| `PERSONAL_API_TOKEN_FILE` | ZORA API token path | `Secret`, mounted as a file |
| `LLM_BASE_URL` / `LLM_API_KEY` | LibreChat / AI Buddy gateway | `Secret` |
| `EMBEDDING_MODEL` | `BAAI/bge-m3`, or `hash-fake` offline | `ConfigMap` |
| `MCP_HOST` / `MCP_PORT` | must be `0.0.0.0` in a container | `ConfigMap` |

## Open questions for Central Informatics

Blocking for deployment, not for local development against a Postgres container.

1. **Is `CREATE EXTENSION vector` permitted for our database role?** It normally
   requires superuser unless the extension is marked trusted. If it is not, the
   extension has to be pre-created for us. **Ask this first** — it is the most
   likely blocker, and `migrations/001_*.sql` opens with that statement.
2. **pgvector version** (HNSW needs >= 0.5.0; `halfvec` needs >= 0.7.0) and the
   Postgres major version.
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

## Registry

`docker/zora/Dockerfile` builds the image. Until Harbor details exist, it is
built and pushed by hand:

```bash
docker build -f docker/zora/Dockerfile -t TODO(ci)/TODO(ci)/thesis-matchmaker:<tag> .
docker push TODO(ci)/TODO(ci)/thesis-matchmaker:<tag>
```

There is deliberately **no** build-and-push workflow in `.github/workflows/`. The
previous one pushed to GHCR, which is the wrong registry for this deployment; a
replacement gets written when the values above are known, not before.
