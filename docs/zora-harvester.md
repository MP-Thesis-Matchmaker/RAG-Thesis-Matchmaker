# ZORA Harvester

The ZORA harvester fetches publication metadata from [ZORA](https://www.zora.uzh.ch) (UZH's
institutional repository) and writes clean, structured rows into Postgres for the Thesis Matchmaker
RAG pipeline.

## Quick Start

The harvester writes to the database, so the schema has to exist first:

```bash
# Local Postgres with pgvector, plus the schema
docker compose up -d postgres
docker compose run --rm init-db

export DATABASE_URL=postgresql://matchmaker:matchmaker@localhost:5432/matchmaker
# Either the token inline, or a file holding it (the file wins if you set both).
export ZORA_UZH_API_KEY=<your ZORA personal API token>

# Smoke test — fetch 5 records
python -m thesis_matchmaker.zora.harvest --mode full --limit 5

# Full harvest (all of UZH, ~238K records, ~2 hours)
python -m thesis_matchmaker.zora.harvest --mode full

# Incremental harvest (records accessioned since the watermark)
python -m thesis_matchmaker.zora.harvest --mode incremental
```

`--since <ISO date>` narrows a **full** harvest to items accessioned on or after that date. It is
ignored in incremental mode, which always uses the `harvest_state` watermark.

## Replaying a harvest without re-fetching

Every run writes its records to `data/raw/<timestamp>_<mode>.jsonl` **before** touching Postgres.
That ordering is what makes a failed write cheap to recover from: the two hours of ZORA requests are
already on disk, so replay the dump instead of repeating them.

```bash
python -m thesis_matchmaker.zora.harvest --mode full \
    --from-dump data/raw/20260101T120000Z_full.jsonl
```

The records in a dump were already normalized at fetch time, so a replay re-runs only the second
half of the pipeline — schema validation, upsert, prune, retention check, watermark. Consequences
worth knowing:

- **No ZORA API token is required.** No client is built, so `ZORA_UZH_API_KEY` can be unset.
- **No second dump is written.** The source file already is the cache; copying it under a new
  timestamp would only make it ambiguous which one to replay next.
- **`--since` is ignored** (with a warning). Whatever filter produced the dump was applied when it
  was fetched; the flag cannot retroactively narrow a file.
- **`--mode full` still prunes.** A replayed full harvest is treated as an authoritative snapshot
  exactly like a fetched one, so publications absent from the dump are deleted — subject to the
  same retention rail.
- **`--limit` still applies**, which makes it a fast way to smoke-test the write path against a
  scratch database.

## Scheduled harvest (production)

A scheduled harvest is one container invocation with a `--mode` flag — nothing more. The timing
belongs to whatever runs the container, not to the application, so in the cluster it is a Kubernetes
`CronJob` and locally it is the same invocation:

```bash
docker compose run --rm harvester --mode incremental
```

The schedules, and the two crontab lines that mirror them on a dev machine, are in
[`deployment.md`](deployment.md) — they are not repeated here so there is one place to change them.

There used to be an in-process poll loop (`zora/scheduler.py`) that decided *when* to harvest from
inside the process. That was the right answer while the only alternative was a CI cron pushing data
into git. The cluster owns scheduling now, so it is gone, and its one non-obvious rule — "full wins
when both are due" — is expressed declaratively by putting the two CronJobs on disjoint days.

## Docker

There is no `Dockerfile` at the repository root — the image is built from `docker/zora/Dockerfile`,
and `docker-compose.yml` builds the same image for both `init-db` and `harvester`:

```bash
# Build
docker build -f docker/zora/Dockerfile -t zora-harvester .

# One-shot harvest. DATABASE_URL is required: without it there is nowhere to write.
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --network host \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/token.secret:/app/token.secret:ro" \
  -e ZORA_UZH_API_KEY_FILE=/app/token.secret \
  -e DATABASE_URL=postgresql://matchmaker:matchmaker@localhost:5432/matchmaker \
  zora-harvester --mode full --limit 5
```

The `data/` mount is **only** the raw-response cache (`data/raw/`). Harvest output goes to the
database; nothing is written back into the repository checkout.

In practice prefer `docker compose`, which already wires the network, the database URL, and the
token mount:

```bash
docker compose run --rm harvester --mode incremental
```

## Output

A run writes three tables (schema in `src/thesis_matchmaker/schema.sql`, all written
only by `zora/store.py` — invariant 1): `person` and `org_unit` first, as full
snapshots, then `publication`. Skip any of them with `--no-persons`,
`--no-org-units`, `--no-publications`.

Each harvested publication becomes one row shaped exactly like
`contracts.ZoraPublication`, which `mapping.to_publication` validates on the way
in, so as JSON one row looks like:

```json
{
  "id": "20.500.14742/31317",
  "title": "Small scale entry versus acquisitions...",
  "abstract": "We consider a reduced form model...",
  "authors": ["Aydemir, Zava", "Schmutzler, Armin"],
  "uzh_authors": ["Schmutzler, Armin"],
  "author_authority_map": {
    "Aydemir, Zava": null,
    "Schmutzler, Armin": "f45b3ec1-cf2a-43ae-85d4-528afff07a40"
  },
  "year": 2008,
  "publication_type": "article",
  "department": "Department of Economics",
  "language": "eng",
  "keywords": ["330 Economics", "Economics and Econometrics"],
  "doi": "10.1016/j.jebo.2004.11.017",
  "url": "https://www.zora.uzh.ch/handle/20.500.14742/31317"
}
```

`authors`, `uzh_authors` and `keywords` are `text[]` columns; `author_authority_map` is `jsonb`.
The table also carries `accessioned` and `harvested_at`, which are not part of the output schema.

**Key fields for the RAG system:**
- **`title` + `abstract`** — embedded for semantic search
- **`department`** — enables filtering by department
- **`uzh_authors`** — UZH-affiliated researchers (potential supervisors)
- **`author_authority_map`** — maps each author to their CRIS Person UUID (or `null` for external co-authors)

The incremental watermark is `harvest_state.last_accessioned`, a single row — not a file. The
indexer reads the `publication` table with `thesis-matchmaker index --source db`.

## Module Layout

```
src/thesis_matchmaker/zora/
├── config.py           # constants — API endpoint, field names, raw-cache path
├── zora_client.py      # thin wrapper around dspace_rest_client
├── normalize.py        # raw DSpace item → flat dict (publications, persons, org units)
├── mapping.py          # flat dict → validated contracts model (edit for shape changes)
├── raw_dump.py         # the data/raw/ JSONL cache, written per step
├── entities.py         # the person + org_unit mirror steps (NOT runnable on its own)
├── store.py            # the only writer: publication, harvest_state, person, org_unit
└── harvest.py          # one-shot harvest orchestrator (Docker ENTRYPOINT)
```

The data models are **not** in this package — they live in
`src/thesis_matchmaker/contracts/sources.py` (`ZoraPublication`, `ZoraPerson`,
`ZoraOrgUnit`, `AuthorAuthority`), so the harvester and the indexer cannot drift
apart.
