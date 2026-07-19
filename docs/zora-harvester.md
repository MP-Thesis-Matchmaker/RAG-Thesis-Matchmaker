# ZORA Harvester

The ZORA harvester fetches publication metadata from [ZORA](https://www.zora.uzh.ch) (UZH's institutional repository) and outputs clean, structured records for the Thesis Matchmaker RAG pipeline.

## Quick Start

### One-shot harvest (development)

```bash
# Set your ZORA API token
export PERSONAL_API_TOKEN_FILE=token.secret

# Smoke test — fetch 5 records
python -m thesis_matchmaker.zora.harvest --mode full --limit 5

# Full harvest (all of UZH, ~238K records, ~2 hours)
python -m thesis_matchmaker.zora.harvest --mode full

# Incremental harvest (new records since last run)
python -m thesis_matchmaker.zora.harvest --mode incremental
```

### Continuous scheduler (production)

```bash
python -m thesis_matchmaker.zora.scheduler
```

Runs forever, checking periodically whether a harvest is due. Handles SIGTERM/SIGINT gracefully.

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `INCREMENTAL_INTERVAL_HOURS` | `24` | Hours between incremental harvests |
| `FULL_INTERVAL_HOURS` | `168` (weekly) | Hours between full harvests |
| `POLL_INTERVAL_SECONDS` | `3600` | How often to check if a harvest is due |

### Via GitHub Actions (manual trigger)

1. Go to the **Actions** tab
2. Select **"ZORA Harvest"** from the sidebar
3. Click **"Run workflow"**
4. Choose mode, optionally set a `since` date or `limit`

This commits the results directly to the repo.

## Docker

The Docker image defaults to one-shot harvest mode:

```bash
# Build
docker build -t zora-harvester .

# One-shot harvest
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/token.secret:/app/token.secret:ro" \
  -e PERSONAL_API_TOKEN_FILE=/app/token.secret \
  zora-harvester --mode full --limit 5
```

For continuous operation, override the command:

```bash
docker run --rm \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/token.secret:/app/token.secret:ro" \
  -e PERSONAL_API_TOKEN_FILE=/app/token.secret \
  zora-harvester \
  python -m thesis_matchmaker.zora.scheduler
```

## Output Format

The sole deliverable is `data/publications.jsonl` — one JSON object per line:

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

**Key fields for the RAG system:**
- **`title` + `abstract`** — embedded for semantic search
- **`department`** — enables filtering by department
- **`uzh_authors`** — UZH-affiliated researchers (potential supervisors)
- **`author_authority_map`** — maps each author to their CRIS Person UUID (or `null` for external co-authors)

## Module Layout

```
src/thesis_matchmaker/zora/
├── config.py           # constants — API endpoint, field names, paths
├── zora_client.py      # thin wrapper around dspace_rest_client
├── normalize.py        # raw DSpace item → flat publication dict
├── output_schema.py    # THE file to edit when output shape changes
├── state.py            # harvest watermark (data/state.json)
├── harvest.py          # one-shot harvest orchestrator (Docker ENTRYPOINT)
├── scheduler.py        # continuous operation loop (deployment override)
└── schema/
    └── zora_publication.schema.json
```