![Python](https://img.shields.io/badge/python-3.11-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-in%20development-orange)

# RAG-Thesis-Matchmaker

Finds thesis supervisors and open thesis positions at the University of Zurich.
A student describes their interests in plain language, and the system searches
UZH publications (ZORA) and scraped thesis postings, ranks the researchers
behind them, and answers with the evidence for each suggestion.

## How it fits askUZH

UZH's AI Buddy is becoming [askUZH](https://www.ai-buddy.uzh.ch/en.html), an
assistant for the whole university. This project is built to plug into it as a
tool: we run our own standalone MCP server, askUZH points its agent at that
URL, calls our structured tool, and writes the final answer itself. Nothing
from this repository gets merged into askUZH, and the system also works on its
own through the CLI.

![AI Buddy Architecture](docs/architecture/ai_buddy_architecture.svg)

![Architecture](docs/architecture.png)

*Target state. The REST API, the web scraper, the application-process summaries,
and the multi-signal ranking box are not implemented yet — the per-package
READMEs linked under [Layout](#layout) say what actually exists.*

## How it works

1. **Ingestion.** ZORA harvesting and departmental web scraping produce
   publication and thesis-posting records, validated against the shared pydantic
   contracts in `src/thesis_matchmaker/contracts`. Harvesting is live and writes
   rows into the `publication` table —
   [`python -m thesis_matchmaker.zora.harvest`](docs/zora-harvester.md), roughly
   22.5k records for the faculty scope currently configured. The scraper is
   **not built yet**, so thesis postings are still the synthetic
   `theses.jsonl` sample in `data/samples/`.
2. **Indexing.** Records are embedded (BGE-M3, swappable; a deterministic
   `hash-fake` stand-in keeps tests and CI offline) and upserted into a
   Postgres table with a pgvector column, incrementally via a content-hash
   diff. Create the schema first with `thesis-matchmaker init-db`.
3. **Query.** Free text is parsed into topics, degree level, and department,
   by a rule-based parser offline or any OpenAI-compatible LLM when one is
   configured. The query is embedded with the same model, matched against
   publications and postings, grouped per UZH researcher, and ranked.
4. **Answer.** Two tools: one returns the ranked researchers with evidence as
   structured data (what askUZH uses), one writes a grounded recommendation in
   prose. An offline template keeps everything runnable without any LLM, and a
   match that clears no score threshold is answered deterministically rather
   than handed to a model. Both are served by the MCP adapter.

## Quickstart

Needs [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```
uv sync                           # installs exactly what uv.lock pins, dev tooling included
cp .env.example .env              # optional, everything runs offline by default
docker compose up -d postgres     # Postgres + pgvector on localhost:5432
uv run thesis-matchmaker init-db  # creates the schema
uv run thesis-matchmaker index    # indexes the 50 checked-in samples
uv run thesis-matchmaker match "I want a master's thesis in NLP on RAG"
```

**`--source` decides what you are searching.** It defaults to `SOURCES_PATH`,
which is `data/samples` — right for a fresh clone, but only 50 documents. Once
the harvester has run, the corpus is in the `publication` table and wants
`thesis-matchmaker index --source db`. Nothing warns you about the difference.

Optional extras: `uv sync --extra embeddings` adds the real embedding model
(pulls in torch), `uv sync --extra mcp` adds the MCP server. Ask for both in one
command if you want both — unlike `pip install`, `uv sync` makes the environment
*match* what you named, so it uninstalls whatever you left out. All
configuration is documented in
`.env.example`; to use an LLM, point `LLM_BASE_URL` at any OpenAI-compatible
endpoint (LibreChat in production, or a local Ollama during development).

Real example output is in [docs/example-run.md](docs/example-run.md).

Other entry points:

```
thesis-matchmaker-mcp                                    # MCP server, HTTP on :8000/mcp
python -m thesis_matchmaker.zora.harvest --mode full     # ZORA harvest
```

## Layout

Everything lives under `src/thesis_matchmaker/`. Each package has its own README
with its public API, data flow, configuration, and known gaps.

| Package | What it does |
|---|---|
| [`contracts/`](src/thesis_matchmaker/contracts/README.md) | The Pydantic models every other package speaks. Imports nothing of ours. |
| [`zora/`](src/thesis_matchmaker/zora/README.md) | Harvests ZORA via the DSpace REST API. Owns all writes to source data. |
| [`indexing/`](src/thesis_matchmaker/indexing/README.md) | JSONL → `Document` → content-hash diff → Postgres/pgvector. No chunking. |
| [`retrieval/`](src/thesis_matchmaker/retrieval/README.md) | Filtered semantic search, UZH-author pre-filter, grouping per person. |
| [`parsing/`](src/thesis_matchmaker/parsing/README.md) | Free text → topics, degree level, department. |
| [`synthesis/`](src/thesis_matchmaker/synthesis/README.md) | Grounded prose answers, with an offline template fallback. |
| [`pipeline/`](src/thesis_matchmaker/pipeline/README.md) | The application-service functions the adapters call. |
| [`adapters/`](src/thesis_matchmaker/adapters/README.md) | MCP server. A REST API is planned, not built. |

Plus `cli.py`, `config.py` (pydantic-settings), and `llm.py` (OpenAI-compatible
client).

One idiom runs through `parsing`, `indexing`, `retrieval`, and `synthesis`:
`base.py` defines a `Protocol`, sibling modules implement it, and `__init__.py`
exposes a `build_*(settings)` factory that picks one. Each also ships a real
offline implementation — `HashEmbedder`, `FakeRetriever`, `RuleBasedExtractor`,
`TemplateSynthesizer` — which is why the whole pipeline runs in CI with no model
download and no network.

## Development

```
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

CI (`ci.yml`) runs both on every pull request, installing with `uv sync --locked`
so it gets the versions in `uv.lock` and not whatever has been released since —
the `dev` group only, so the `mcp` and `embeddings` code paths are not exercised
there. It is the only workflow: container images are built by hand until the UZH
Harbor registry is wired up, and harvesting runs in the cluster, never in CI. See
[docs/deployment.md](docs/deployment.md).

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow.

Two known inconsistencies worth fixing at some point: the MIT badge and
`license = { text = "MIT" }` have no `LICENSE` file behind them, and
`docker/zora/Dockerfile` builds on Python 3.12 while the badge and
`requires-python` say 3.11.

## Contributors

* Shayan Sooratgar
* Nicolas Peyer
* Gregory Frommelt
* Ilya Kruchenetskiy
