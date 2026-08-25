# CLAUDE.md — Thesis Matchmaking System (backend-core)

## Project summary

UZH master's project (15 ECTS, **graded academic work**) by Shayan, Nicolas, Gregory, and Ilya.
A RAG-based system that matches a student's natural-language research interests against (1) ZORA
publication data and (2) web-scraped open thesis positions, and recommends supervisors, research
groups, and open positions. The matchmaking core is the contribution and must work **standalone
first**; AI Buddy integration via MCP is a downstream possibility, never a dependency.

Because this is graded work: never fabricate results, citations, data, or evaluation numbers. When
a decision is unmade or a fact is unknown, say so explicitly.

## Current repo state (as of 2026-08-22)

`thesis_matchmaker` (src layout, `requires-python >=3.11`) — 10 packages, ~10,197 LOC,
317 test functions in 26 files (344 pass with a database, 308 pass / 36 skip offline).
**Per-package detail lives in a `README.md` inside each package; read those instead of expanding
this section.** Architecture diagram: [`docs/architecture.png`](docs/architecture.png)
(target state — the REST API and multi-signal ranking in it are not built yet).

| Package | Status | Concern |
|---|---|---|
| [`contracts/`](src/thesis_matchmaker/contracts/README.md) | implemented | Pydantic models every package speaks; imports nothing of ours |
| [`zora/`](src/thesis_matchmaker/zora/README.md) | implemented, running | DSpace REST harvester; **owns all writes** to `publication` |
| [`scraper/`](src/thesis_matchmaker/scraper/README.md) | ported, tested | Posting scraper over 103 UZH pages; **owns all writes** to `posting` |
| [`indexing/`](src/thesis_matchmaker/indexing/README.md) | implemented | JSONL → `Document` → content-hash diff → Postgres/pgvector |
| [`retrieval/`](src/thesis_matchmaker/retrieval/README.md) | implemented | Dual filtered queries + UZH-author pre-filter; **also holds the only ranking** |
| [`parsing/`](src/thesis_matchmaker/parsing/README.md) | implemented | Free text → `ParsedQuery`; rule-based baseline, optional LLM |
| [`synthesis/`](src/thesis_matchmaker/synthesis/README.md) | implemented | Grounded prose; weak matches short-circuit before any LLM call |
| [`pipeline/`](src/thesis_matchmaker/pipeline/README.md) | implemented, thin | Application-service functions the adapters call |
| [`adapters/`](src/thesis_matchmaker/adapters/README.md) | MCP done, REST design-only | Thin front doors; no business logic |

Not built: a **`ranking` package** (`pipeline/`'s docstring claims a rank step; in reality
ranking is `score = max(hit.score)` inside `VectorRetriever`).

The **web scraper now exists** — ported in from `Webscraping-Prototype` as
[`scraper/`](src/thesis_matchmaker/scraper/README.md), so `ThesisPosting` has a real producer for
the first time. Two of its three record kinds are stored and unread: `researcher_profile` (565
rows) and `application_process` (57) have tables but no consumer. Only `posting` reaches the
index.

**Repository-wide idiom — respect it.** `parsing/`, `indexing/`, `retrieval/`, `synthesis/` each
have `base.py` = `Protocol`, sibling modules = implementations, `__init__.py` = a
`build_*(settings)` factory selecting one from `config.Settings`. That is invariant 3 in code
form. Each also ships a real offline implementation (`HashEmbedder`, `FakeRetriever`,
`RuleBasedExtractor`, `TemplateSynthesizer`, `InMemoryVectorStore`) — not mocks, which is why CI
runs the whole pipeline with no model download, no database and no network.

Seam status. The **vector store is now decided: Postgres + pgvector** (cosine, HNSW) — not a
preference but a constraint of the deployment environment UZH Central Informatics confirmed on
2026-08-20. It stays behind the `VectorStore` protocol (`InMemoryVectorStore` is the second
implementation), but treat it as settled, not provisional. Still genuinely open per invariant 3:
embedding `BAAI/bge-m3` (`hash-fake` offline, 1024 dimensions — the width is baked into
`document.embedding vector(1024)`, so changing it is a migration) and the LLM (any
OpenAI-compatible endpoint; LibreChat prod, Ollama dev).

Entry points: `thesis-matchmaker` (`init-db`, `index --source --rebuild`, `match --top-k`),
`thesis-matchmaker-mcp` (`--stdio`), and `python -m thesis_matchmaker.zora.harvest`
(**no console script**).

**Gotcha:** `SOURCES_PATH` defaults to `data/samples`, so a bare `thesis-matchmaker index`
indexes the 50 checked-in sample documents (30 publications + 20 postings). The harvested
corpus lives in the `publication` table — **214,685 publications** as of 2026-08-21, of which
**91,673 (42.7%)** pass the UZH-author filter — a figure the `uzh_authors` ORCID conflation
inflates by 38,157 records; see the first known gap in
[`zora/README.md`](src/thesis_matchmaker/zora/README.md). `--source db` indexes only those: a publication
with no UZH author cannot yield a supervisor recommendation, and `retrieval/` already filtered it
out at query time, so embedding it was work spent on unreachable vectors.
`data/publications.jsonl` is a pre-Postgres artefact: nothing writes it and it is no
longer tracked.

Tooling: `uv` everywhere — `uv.lock` **is tracked and is what actually gets installed**, by CI
(`uv sync --locked`) and by the container image alike; pip is used nowhere. `pytest` (344 tests /
26 files; 36 need Postgres and skip without `DATABASE_URL`), `ruff` (line length 100, py311); both
live in a PEP 735 `dev` dependency group, not an extra. **One workflow**: `ci.yml` (ruff + pytest
on every PR, `dev` group only — never installs `mcp`/`embeddings`).
Deployment target is a **UZH Kubernetes cluster** pulling from a **private Harbor registry**,
with a **Postgres + pgvector** server; see [`docs/deployment.md`](docs/deployment.md). Images
are built by hand until Harbor access exists, and harvesting runs as a cluster job — never in
CI, and **never committing data back to the repo**.

Keep this table current as modules land; put the detail in the package README, not here.

## Architecture (agreed decisions — respect them)

**Modular monolith in a single monorepo.** Not microservices: batch ingestion pipelines writing to
shared storage don't need HTTP boundaries, and hard module seams inside one repo give decoupling
without multi-repo friction for a four-person, one-semester team.
**Per-component containerization is decided**: one image per deployable role
(harvester, indexer, serving adapter, posting scraper), each with its own
`docker/<role>/Dockerfile`. That does **not** imply one source tree per image — a
single distribution builds all of them, differing only in entrypoint and installed
extras. Splitting `src/` into a `projects/` workspace is still open, and the
scraper migration is what decides it. See the Images section of
[`docs/deployment.md`](docs/deployment.md).

Target layout (~6–8 packages). Names in the code drifted from this list; the mapping and what is
still missing:

- `common` — shared schemas / domain types (the contract between modules)
  → **shipped as `contracts/`**
- `ingestion` — **owns all writes**; sub-packages for ZORA and the scraper, plus a store layer;
  includes a scheduled ingest runner
  → **shipped as two peer packages, `zora/` and `scraper/`** (the cluster's CronJobs own
  scheduling). Whether they should sit under a shared `ingestion/` parent is **still open** and
  was deliberately not decided by the scraper port: the move would rename every
  `thesis_matchmaker.zora.*` import plus the harvester image's ENTRYPOINT, both CronJobs and
  `tests/zora/`, for no functional gain. Decide it together with the `projects/` workspace
  question, which is the same kind of question.
- `indexing` — builds the searchable index / embeddings from ingested data → shipped
- `retrieval` — semantic similarity search over the index; read-only → shipped
- `ranking` — multi-signal scoring over retrieved candidates; read-only
  → **not built.** Ranking is currently one line inside `VectorRetriever._group_by_person`
  (`score = max(hit.score)`). Keep the intent; the slot is between retrieve and synthesise
- `application service` — plain functions orchestrating retrieval → ranking → LLM synthesis;
  exposes the core use cases
  → **shipped as `pipeline/`** (plus `adapters/service.py`)
- Two thin adapter apps: **REST API** and **MCP adapter** — front doors over the
  application-service functions only
  → **MCP shipped; REST not built**

### Invariants

1. **Ingestion owns all writes.** Serving (retrieval / ranking / app-service / adapters) is
   strictly read-only. No write paths outside ingestion.
2. **Core exposes plain application-service functions.** Adapters (REST, MCP) call them; adapters
   contain no business logic, and core code never imports from adapters.
3. **Swappable seams behind interfaces**: embedding model, vector store, and LLM provider are
   **not finalized** — keep each replaceable without touching the rest. Do not hardcode a choice;
   if code later picks one, record what the code actually uses here and note it may change.
4. **Module boundaries map to team ownership** to minimize merge conflicts. Respect the seams.

## Tech stack

- **Python** unless a package explicitly states otherwise (README targets 3.11). State key
  library/version assumptions when they matter.
- Embedding model, vector store, LLM provider: undecided (see invariant 3).
- Project management: **GitHub Projects v2** (org-level), Issues as the atomic ticket unit,
  Iteration field for sprints, custom fields (Module, Priority, Assignee, Status).

## Git workflow (from CONTRIBUTING.md)

- Never commit to `main`; branch per task: `feature/…`, `fix/…`, `docs/…`.
- **AI-generated branches use the `ai/` prefix**; every AI PR needs a summary + reasoning and
  human review before merge. AI agents must not touch `.env`, config files, or dependency lock
  files without explicit human instruction.
- PRs into `main` require at least one review; imperative-mood commit messages.

## Data, scraping, and ethics

- **ZORA access is via the DSpace(-CRIS) REST API, not OAI-PMH** (decision confirmed with ZORA
  maintainers, 2026-07). Details below under "ZORA / DSpace REST API".
- Any scraping: respect robots.txt, terms of use, and rate limits; **cache raw responses** so
  ingestion is reproducible and re-runs don't re-hit sites.
- Researchers' names, affiliations, and publications are **personal data** — handle carefully and
  flag legal/ethical considerations rather than ignoring them.

### ZORA / DSpace REST API (from ZORA maintainers, 2026-07)

- ZORA runs **DSpace-CRIS**; full REST API per the DSpace RestContract:
  <https://github.com/DSpace/RestContract/tree/main>. Production entry point:
  `https://www.zora.uzh.ch/server/api`.
- **Search endpoint**: `GET /server/api/discover/search/objects` (docs:
  RestContract `search-endpoint.md`). Key parameters:
  - `scope` — UUID of a community or collection (browse the tree at
    `https://www.zora.uzh.ch/community-list`; UZH root community
    `323725a5-950d-4b89-8765-1b955e305664`).
  - `query` — a **Solr query** over metadata fields, e.g.
    `dc.date.accessioned:2025` (added since 2025), `dateIssued.year=2025`,
    `orcid:(0000-0002-0128-4602)`. Queryable fields: see
    `https://www.zora.uzh.ch/info/help`; per-record fields visible in the
    "Full Metadata" tab of a publication view.
- **Structure**: communities in the UZH tree = organizational units; publications belong only to
  **collections** — each leaf org unit (e.g. an institute) has an attached
  "Publications of …" collection.
- **Pagination**: responses are paginated with `next`/`last` links; `size` max **1000**
  (silently capped above that); `sort` supported.
- **Python client**: `dspace-rest-python` by The Library Code
  (<https://github.com/the-library-code/dspace-rest-python>) — requires token authentication and
  either PR #65 applied or current `main`.
- **No built-in incremental harvesting** (no OAI-PMH-style datestamp windows or deleted-record
  tombstones). We implement deltas ourselves: harvest **additions** via
  `dc.date.accessioned:[<last run> TO *]`; detect **updates/deletions** by periodically fetching
  the full (paginated) ID list and diffing against the last snapshot. `dc.date.accessioned` does
  not change on edit; whether a query-exposed `lastModified` field exists is **unverified** — do
  not assume it. The indexing layer's content-hash (checksum) diff remains the authoritative
  add/update/delete decision.

## Evaluation

- **Quantitative**: retrieval accuracy on known topics; ranking quality via precision@k, MRR,
  nDCG — against a gold/test set the team builds and can defend.
- **Qualitative**: usefulness of recommendations and interface usability.
- **Never fabricate evaluation numbers.**

## Working norms

- Default to concrete, runnable help: code, schemas, prompts, configs, eval scripts.
- For designs with trade-offs (chunking, ranking weights, vector DB, scraping approach, metrics):
  lay out realistic options with pros/cons and a reasoned recommendation — not one unexplained
  answer.
- Be direct about problems: bugs, weak eval designs, scope creep, unrealistic plans.
- Graded academic work: assist, explain, and review — analysis, decisions, and writing stay the
  team's. Favor understanding over copy-paste. When unsure, say so.