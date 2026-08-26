# CLAUDE.md — Thesis Matchmaking System (backend-core)

## Project summary

UZH master's project (15 ECTS, **graded academic work**) by Shayan, Nicolas, Gregory, and Ilya.
A RAG-based system that matches a student's natural-language research interests against (1) ZORA
publication data and (2) web-scraped open thesis positions, and recommends supervisors, research
groups, and open positions. The matchmaking core is the contribution and must work **standalone
first**; AI Buddy integration via MCP is a downstream possibility, never a dependency.

Because this is graded work: never fabricate results, citations, data, or evaluation numbers. When
a decision is unmade or a fact is unknown, say so explicitly.

## Current repo state (as of 2026-08-26)

A **five-member `uv` workspace**, every member `requires-python >=3.11`, ~11,750 LOC across the
five `src/` trees, 481 tests in 33 files (433 pass / 48 skip without `DATABASE_URL`). The
workspace root has **no `[project]` table** — it is virtual, which is why a bare `uv sync`
installs nothing and errors; always `--all-packages` or `--package themis-<x>`.
**Per-member detail lives in the member's `README.md`; read those instead of expanding this
section.** Architecture diagram: [`docs/architecture.png`](docs/architecture.png)
(target state — the REST API and multi-signal ranking in it are not built yet).

| Member | Import root | Status | Concern |
|---|---|---|---|
| [`libs/shared/`](libs/shared/README.md) | `themis_shared` | implemented | The floor everything stands on: [`contracts/`](libs/shared/src/themis_shared/contracts/README.md) (**every** data model), `config`, `db`, `schema` + `schema.sql`, `initdb`. Imports no other member |
| [`projects/zora/`](projects/zora/README.md) | `themis_zora` | implemented, running | DSpace REST harvester; **owns all writes** to `publication`, `person`, `org_unit` |
| [`projects/scraper/`](projects/scraper/README.md) | `themis_scraper` | implemented, tested | Posting scraper over 103 UZH pages; **owns all writes** to `posting` |
| [`projects/matcher/`](projects/matcher/README.md) | `themis_matcher` | implemented | The engine, in five sub-packages — [`indexing/`](projects/matcher/src/themis_matcher/indexing/README.md), [`retrieval/`](projects/matcher/src/themis_matcher/retrieval/README.md) (**also holds the only ranking**), [`parsing/`](projects/matcher/src/themis_matcher/parsing/README.md), [`synthesis/`](projects/matcher/src/themis_matcher/synthesis/README.md), [`pipeline/`](projects/matcher/src/themis_matcher/pipeline/README.md) — plus `llm.py` and `cli.py` |
| [`projects/gateway/`](projects/gateway/README.md) | `themis_gateway` | MCP done, REST design-only | Thin front door; no business logic, no model, no database. Reaches the matcher over HTTP from `service.py`; imports no other member |

The dependency graph is a star: **every member depends on `themis-shared` and on nothing else of
ours.** `themis-gateway` used to also depend on `themis-matcher`; since 2026-08-26 it calls the
matcher over HTTP instead (`/v1/match`, `/v1/recommend`), which is what removed the last cross-edge.
CI's `boundaries` job installs each member **alone**, so that is now enforced rather than merely
described: a stray `from themis_matcher import ...` in the gateway fails with `ModuleNotFoundError`.
The shared wire models live in [`themis_shared.contracts.api`](libs/shared/src/themis_shared/contracts/api.py),
so both ends stay typed without either importing the other.

Not built: a **`ranking` package.** Ranking is one line inside
`themis_matcher.retrieval`'s `VectorRetriever._group_by_person` (`score = max(hit.score)`).

**The first thing that package has to fix is the person key, not the score.**
`_group_by_person` groups on an exact name string, and the two sources spell people
differently — `"Davide Scaramuzza"` on a posting against `"Scaramuzza, D"` on a paper. Measured
2026-08-26: **403 distinct supervisor names, 0 matching any of the 2,942 `uzh_authors`**; 3 match a
plain `authors` entry, and only through the unaffiliated fallback, so a merge happens exactly where
the UZH signal is absent. So `publication_count` and `posting_count` are effectively never both
non-zero, a supervisor with an open position is never evidenced by their own papers, and any
multi-signal score combining the two would be scoring a join that does not happen — while looking
correct in review. The fix is name normalisation or an identity join through the `person` table
(which already carries the CRIS UUIDs `uzh_authors` derives from); postings carry no identifier at
all, so that side is the harder half. Detail:
[`retrieval/README.md`](projects/matcher/src/themis_matcher/retrieval/README.md).

Two of the scraper's three record kinds are stored and unread: `researcher_profile` (569 rows) and
`application_process` (45) have tables but no consumer. Only `posting` reaches the index.

**Matcher-wide idiom — respect it.** `themis_matcher`'s `parsing/`, `indexing/`, `retrieval/`,
`synthesis/` each have `base.py` = `Protocol`, sibling modules = implementations, `__init__.py` = a
`build_*(settings)` factory selecting one from `themis_shared.config.Settings`. That is invariant 3
in code form. Each also ships a real offline implementation (`HashEmbedder`, `FakeRetriever`,
`RuleBasedExtractor`, `TemplateSynthesizer`, `InMemoryVectorStore`) — not mocks, which is why CI's
`offline` job runs the whole pipeline with no model download, no database and no network.

Seam status. The **vector store is now decided: Postgres + pgvector** (cosine, HNSW) — not a
preference but a constraint of the deployment environment UZH Central Informatics confirmed on
2026-08-20. It stays behind the `VectorStore` protocol (`InMemoryVectorStore` is the second
implementation), but treat it as settled, not provisional. Still genuinely open per invariant 3:
embedding `BAAI/bge-m3` (`hash-fake` offline, 1024 dimensions — the width is baked into
`document.embedding vector(1024)`, so changing it is a migration) and the LLM (any
OpenAI-compatible endpoint; LibreChat prod, Ollama dev).

Entry points — one console script per member: `themis-init-db` (shared), `themis-matcher`
(`init-db`, `index --source --rebuild`, `match --top-k`, `repl`, `serve --host --port`),
`themis-gateway-mcp`
(`--stdio`), `themis-zora-harvest`, and `themis-scraper`. The last two also answer to
`python -m themis_zora.harvest` and `python -m themis_scraper`. `themis-matcher init-db`
delegates to `themis_shared.initdb`, so the two spellings cannot drift.

**Gotcha:** `SOURCES_PATH` defaults to `data/samples`, so a bare `themis-matcher index`
indexes the 50 checked-in sample documents (30 publications + 20 postings). The harvested
corpus lives in the `publication` table — **214,756 publications** as of 2026-08-25, of which
**53,545 (24.9%)** carry a UZH author, naming **2,942** distinct researchers. That figure was
**91,734 (42.7%)** and 58,218 names until 2026-08-25, when
`uzh_authors` stopped admitting ORCID-only authorities — 38,190 records whose authors DSpace never
linked to a local Person. Those publications are still indexed and still retrievable; they are
ranked below CRIS-backed candidates rather than excluded. See
[`zora/README.md`](projects/zora/README.md). `--source db` indexes **all** of them,
plus **all 695 postings**. It briefly indexed only the UZH-authored ones (2026-08-21 to
08-25); that filter is gone because it made `RETRIEVAL_REQUIRE_UZH_AUTHOR` unflippable —
turning it off would have returned nothing extra until someone re-embedded the corpus. Eligibility
is now a retrieval-time setting, with `RETRIEVAL_RANKING_STRATEGY=uzh_first` demoting
unaffiliated researchers rather than excluding them. The posting side made the same move on
2026-08-26: it used to index only the 678 available topics, and now indexes the 15 `assigned` and
2 `private` ones too, flagged `is_available: false` and excluded by
`RETRIEVAL_REQUIRE_AVAILABLE_POSTING` (on by default) instead of by a `WHERE` clause.
`data/publications.jsonl` is a pre-Postgres artefact: nothing writes it and it is no longer
tracked.

**Schema reset performed (2026-08-25, fingerprint `3d4f0475bf80`).** The reset stamped
`135ac01a09be`; the recorded value changed without any DDL change when `schema.py` started
fingerprinting normalized DDL instead of raw file text, so a database applied before that
commit reads as stale and is not. `schema.sql` gained the
`person` and `org_unit` entity mirrors (refreshed at the start of every `zora.harvest` run —
persons, then org units, then publications; `--no-persons` / `--no-org-units` /
`--no-publications` opt out), plus `publication.owning_collection_uuid` and a typed
`author_authority_map` (`{"type": "cris"|"orcid", "id": ...}` — the CRIS-vs-ORCID distinction the
known `uzh_authors` gap needed). The local database was reset and re-harvested from the API:
**2,018 persons, 497 org units, 214,756 publications**, every one carrying
`owning_collection_uuid`. Postings were restored from the scraper's response cache (695 rows, no
re-fetch). **The re-index has since run:** the `document` table holds **215,451** rows
(214,756 publications + 695 postings), embedded with the real `BAAI/bge-m3`, zero null embeddings.

Two consequences. Old raw dumps predate the new fields, so `--from-dump` cannot rebuild this
corpus; only an API harvest can. And the **posting-side follow-up will need its own reset** —
the "one reset for everything" plan did not survive, because the entity mirrors were needed
before those changes were designed. Details:
[`zora/README.md`](projects/zora/README.md).

Tooling: `uv` everywhere — a **single root `uv.lock` covering all five members**, tracked, and what
actually gets installed by CI (`uv sync --locked --all-packages`, or `--package themis-<x>`) and by
the container images alike; pip is used nowhere. One `.venv`, at the root: `--package X` *replaces*
its contents rather than making a second environment. `pytest` (481 tests / 33 files; 48 need
Postgres and skip without `DATABASE_URL`) and `ruff` (line length 100, py311) are configured
**only in the root `pyproject.toml`** — which fixes pytest's rootdir at the repo root, so always
invoke it from there. `ruff` lives in the root `dev` group; `pytest` is repeated in every member's
`dev` group so `--package X` still yields a runnable environment.

**One workflow file, five jobs** — `ci.yml`: `offline` (all members, no network or database),
`scraper` (`--package themis-scraper --extra scraping`), `pgvector` (a real pgvector service plus
`themis-init-db`), `boundaries` (a 4-leg matrix installing each member alone, so a cross-member
import fails loudly), and `wheels` (proves `schema.sql` ships as package data — it is resolved by
name at runtime, so a missing declaration would fail only inside a container). `mcp` and
`embeddings` are still never installed in CI.
Deployment target is a **UZH Kubernetes cluster** pulling from a **private Harbor registry**,
with a **Postgres + pgvector** server; see [`docs/deployment.md`](docs/deployment.md). Images
are built by hand until Harbor access exists, and harvesting runs as a cluster job — never in
CI, and **never committing data back to the repo**.

Keep this table current as modules land; put the detail in the member README, not here.

## Architecture (agreed decisions — respect them)

**Modular monolith in a single monorepo.** Not microservices: batch ingestion pipelines writing to
shared storage don't need HTTP boundaries, and hard module seams inside one repo give decoupling
without multi-repo friction for a four-person, one-semester team.
**Per-component containerization**: one image per deployable role (harvester, matcher, gateway,
posting scraper), each built from `projects/<member>/Dockerfile` by
`uv sync --package themis-<member>` — so an image installs that member's closure and nothing else.
**`src/` was split into this workspace on 2026-08-26** (`d79f9d2`…`f5c1460`); the scraper port is
what forced the question, and `src/thesis_matchmaker/` no longer exists. See the Images section of
[`docs/deployment.md`](docs/deployment.md).

Target layout (~6–8 packages). Names in the code drifted from this list; the mapping and what is
still missing:

- `common` — shared schemas / domain types (the contract between modules)
  → **shipped as `themis_shared.contracts`**, inside the `themis-shared` distribution
- `ingestion` — **owns all writes**; sub-packages for ZORA and the scraper, plus a store layer;
  includes a scheduled ingest runner
  → **shipped as two separate distributions, `themis-zora` and `themis-scraper`**, both under
  `projects/` (the cluster's CronJobs own scheduling). An `ingestion/` parent is now only a
  workspace-*grouping* question — a `libs`/`projects`-style directory — because the import rename
  it used to cost was already paid by the split. Still open, on those reduced terms.
- `indexing` — builds the searchable index / embeddings from ingested data → shipped
- `retrieval` — semantic similarity search over the index; read-only → shipped
- `ranking` — multi-signal scoring over retrieved candidates; read-only
  → **not built.** Ranking is currently one line inside `themis_matcher.retrieval`'s
  `VectorRetriever._group_by_person` (`score = max(hit.score)`). Keep the intent; the slot is
  between retrieve and synthesise
- `application service` — plain functions orchestrating retrieval → ranking → LLM synthesis;
  exposes the core use cases
  → **shipped as `themis_matcher.pipeline`**, plus `themis_gateway.service` — note these are
  now two different distributions, which is what makes the HTTP swap below a contained change
- Two thin adapter apps: **REST API** and **MCP adapter** — front doors over the
  application-service functions only
  → **MCP shipped as `themis-gateway`; REST not built.** The gateway no longer calls the matcher
  in-process: since 2026-08-26 `themis_gateway/service.py` is an HTTP client, and the matcher serves
  [`themis_matcher.api`](projects/matcher/src/themis_matcher/api/) behind `themis-matcher serve`.
  A REST front door for *students* is still unbuilt; this is the internal seam, not that

### Invariants

1. **Ingestion owns all writes.** Serving (retrieval / ranking / app-service / adapters) is
   strictly read-only. No write paths outside ingestion. One qualifier since 2026-08-26: the
   matcher's API *process* both serves and indexes, because one process means one copy of a
   2.27 GB model against a 4 GiB namespace quota. The **modules** are unchanged — `retrieval/`
   still never writes, `indexing/` still owns the `document` table — and the ingestion members
   still own every source table.
2. **Core exposes plain application-service functions.** The gateway (MCP, and REST when it
   exists) calls them; it holds no business logic, and no core member imports `themis_gateway`.
3. **Swappable seams behind interfaces**: the embedding model and LLM provider are
   **not finalized** — keep each replaceable without touching the rest. Do not hardcode a choice;
   if code later picks one, record what the code actually uses here and note it may change. The
   vector store is settled (pgvector) but stays behind `VectorStore` all the same.
4. **Module boundaries map to team ownership** to minimize merge conflicts. Respect the seams.

## Tech stack

- **Python** unless a package explicitly states otherwise (README targets 3.11). State key
  library/version assumptions when they matter.
- Embedding model and LLM provider: undecided (see invariant 3). **Vector store: decided** —
  Postgres + pgvector, a deployment constraint rather than a preference; see Seam status above.
- Project management: **GitHub Projects v2** (org-level), Issues as the atomic ticket unit,
  Iteration field for sprints, custom fields (Module, Priority, Assignee, Status).

## Git workflow (from CONTRIBUTING.md)

- Never commit to `main`; branch per task, naming it `<kind>/#<issue>-<slug>` where `<kind>` is one
  of `feature`, `bugfix`, `docs`, `refactor`, or `experimental` (the last for work that may never
  merge). Work not covered by an issue uses `NOREF` in place of the number —
  `docs/#NOREF-fix-broken-links`.
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