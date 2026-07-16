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

## How it works

1. **Ingestion.** ZORA harvesting and departmental web scraping produce
   `publications.jsonl` and `theses.jsonl`, validated against the shared
   pydantic contracts in `src/thesis_matchmaker/contracts`. (Harvesting works;
   the scraping is still being set up. `data/samples/` holds working examples.)
2. **Indexing.** Records are embedded (BGE-M3, swappable; a deterministic
   `hash-fake` stand-in keeps tests and CI offline) and upserted into a
   ChromaDB index, incrementally via a content-hash diff.
3. **Query.** Free text is parsed into topics, degree level, and department,
   by a rule-based parser offline or any OpenAI-compatible LLM when one is
   configured. The query is embedded with the same model, matched against
   publications and postings, grouped per UZH researcher, and ranked.
4. **Answer.** Two tools: one returns the ranked researchers with evidence as
   structured data (what askUZH uses), one writes a grounded recommendation in
   prose. An offline template keeps everything runnable without any LLM. The
   MCP server exposing these is currently in review.

## Quickstart

Needs Python 3.11+.

```
pip install -e ".[dev]"
cp .env.example .env        # optional, everything runs offline by default
thesis-matchmaker index
thesis-matchmaker match "I want a master's thesis in NLP on RAG"
```

Optional extras: `.[embeddings]` installs the real embedding model (pulls in
torch), `.[mcp]` installs the MCP server. All configuration is documented in
`.env.example`; to use an LLM, point `LLM_BASE_URL` at any OpenAI-compatible
endpoint (LibreChat in production, or a local Ollama during development).

Real example output is in [docs/example-run.md](docs/example-run.md).

## Layout

`src/thesis_matchmaker/`: `contracts` (shared data shapes), `parsing` (query
to structured fields), `indexing` (embedder, vector store, indexer),
`retrieval` (semantic search and ranking), `synthesis` (written answers),
`pipeline` (ties the steps together), `cli`.

## Development

```
ruff check . && ruff format --check .
pytest
```

CI runs both on every pull request. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the workflow.

## Contributors

* Shayan Sooratgar
* Nicolas Peyer
* Gregory Frommelt
* Ilya Kruchenetskiy
