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
five `src/` trees, 560 tests in 43 files (494 pass / 66 skip without `DATABASE_URL`). The
workspace root has **no `[project]` table** — it is virtual, which is why a bare `uv sync`
installs nothing and errors; always `--all-packages` or `--package themis-<x>`.
**Per-member detail lives in the member's `README.md`; read those instead of expanding this
section.** Architecture diagram: [`docs/architecture.png`](docs/architecture.png)
(target state — the REST API and multi-signal ranking in it are not built yet).

| Member | Import root | Status | Concern |
|---|---|---|---|
| [`libs/shared/`](libs/shared/README.md) | `themis_shared` | implemented | The floor everything stands on: [`contracts/`](libs/shared/src/themis_shared/contracts/README.md) (**every** data model), `config` (**two fields** — see below), `db`, `schema` + `schema.sql`, `initdb`. Imports no other member |
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

**The person key was the first thing that package had to fix, and it is now fixed —
partially, and the partiality is the point (2026-09-03).** `_group_by_person` used to group on
an exact name string, and the two sources spell people differently — `"Davide Scaramuzza"` on a
posting against `"Scaramuzza, Davide"` on a paper — so **0 of 403 supervisor names matched any
of the 2,942 `uzh_authors`**. `themis_matcher.retrieval.identity` now canonicalises to a
first-given-token + family key, resolving free text against the comma-structured ZORA side:
**103 of 403 (25.6%)**, 0 conflations detectable.

Three things about it that are not obvious:

- **The `person` table is the wrong join target**, which is the opposite of the intuition.
  62% of supervisors have no `person` row with even a matching family name — they are PhD
  students, postdocs and externals with no ZORA record — and `person` resolves *fewer*
  supervisors than `uzh_authors` does (81 vs 94), because only 1,706 of 2,942 author strings
  are exactly a `display_name`. `person` carries identity (CRIS UUID, ORCID), not coverage.
- **103 is a ceiling, not a yield.** `retrieve` fetches `top_k` postings and `top_k`
  publications separately, so a merge needs one person in both slices. Measured: **0 of 25
  returned matches at the default `top_k=5`**, 1 of 100 at 20, 7 of 250 at 50. Do not report
  the corpus figure as a coverage figure.
- **The rule is deliberately strict** because a wrong merge is fabricated evidence shown to a
  student. Family-name-only and initial matches are refused; 46 supervisor names share a family
  name with a *different* ZORA author (`Daniel Müller` against `Müller, Mathias`).

Full measurement: [`docs/person-key-resolution.md`](docs/person-key-resolution.md). Also
detail:
[`retrieval/README.md`](projects/matcher/src/themis_matcher/retrieval/README.md).

Two of the scraper's three record kinds are stored and unread: `researcher_profile` (569 rows) and
`application_process` (45) have tables but no consumer. Only `posting` reaches the index.

**Matcher-wide idiom — respect it.** `themis_matcher`'s `parsing/`, `indexing/`, `retrieval/`,
`synthesis/` each have `base.py` = `Protocol`, sibling modules = implementations, `__init__.py` = a
`build_*(settings)` factory selecting one from `themis_matcher.config.MatcherSettings`. That is invariant 3
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

Entry points — **one console script per member, named after the member, with the role as a
subcommand** (2026-08-27): `themis-matcher` (`init-db`, `index --source --rebuild`,
`match --top-k`, `repl`, `serve --host --port`), `themis-zora` (`harvest`),
`themis-gateway` (`mcp --stdio`), `themis-scraper` (`fetch`, `onboard`, `run`, `status`,
`check`), plus `themis-init-db` from shared. Each also answers to `python -m themis_<member>`.
`themis-zora-harvest` and `themis-gateway-mcp` were the old spellings; they encoded the
subcommand in the script name, so neither member had a top-level command and a second role
would have meant a second script. Bare `themis-<member>` prints what that instance is pointed
at rather than erroring — the fastest way to read a misconfigured pod. `themis-matcher init-db`
delegates to `themis_shared.initdb`, so the two spellings cannot drift.

**Gotcha:** `MATCHER_SOURCES_PATH` defaults to `data/samples`, so a bare `themis-matcher index`
indexes the 50 checked-in sample documents (30 publications + 20 postings). The harvested
corpus lives in the `publication` table — **214,756 publications** as of 2026-08-25, of which
**53,545 (24.9%)** carry a UZH author, naming **2,942** distinct researchers. That figure was
**91,734 (42.7%)** and 58,218 names until 2026-08-25, when
`uzh_authors` stopped admitting ORCID-only authorities — 38,190 records whose authors DSpace never
linked to a local Person. Those publications are still indexed and still retrievable; they are
ranked below CRIS-backed candidates rather than excluded. See
[`zora/README.md`](projects/zora/README.md). `--source db` indexes **all** of them,
plus **all 695 postings**. It briefly indexed only the UZH-authored ones (2026-08-21 to
08-25); that filter is gone because it made `MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR` unflippable —
turning it off would have returned nothing extra until someone re-embedded the corpus. Eligibility
is now a retrieval-time setting, with `MATCHER_RETRIEVAL_RANKING_STRATEGY=uzh_first` demoting
unaffiliated researchers rather than excluding them. The posting side made the same move on
2026-08-26: it used to index only the 678 available topics, and now indexes the 15 `assigned` and
2 `private` ones too, flagged `is_available: false` and excluded by
`MATCHER_RETRIEVAL_REQUIRE_AVAILABLE_POSTING` (on by default) instead of by a `WHERE` clause.
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

**Configuration is owned by the member that reads it (2026-08-27).** `themis_shared.config.Settings`
is **two fields** — `database_url` and `matcher_base_url`, the only two more than one member reads.
Everything else lives in a prefixed subclass: `MatcherSettings` (`MATCHER_`, 17 fields),
`GatewaySettings` (`GATEWAY_`), `ZoraSettings` (`ZORA_`), `ScraperSettings` (`SCRAPER_`). Adding a
field to the shared class means every member inherits it; `test_smoke.py` fails first, deliberately.

Three things about this that are not obvious and cost time to rediscover:

- **`env_prefix` on a subclass re-prefixes *inherited* fields too.** The shared floor's two fields
  carry an explicit `validation_alias`, which is inherited as part of the `FieldInfo` and beats any
  subclass prefix — that is the only reason `DATABASE_URL` is not `MATCHER_DATABASE_URL` inside the
  matcher. `populate_by_name=True` rides along, because an explicit alias otherwise makes the alias
  the *only* accepted constructor keyword and `extra="ignore"` silently drops `Settings(database_url=…)`.
- **A wrong or stale variable name is silent**, for the same `extra="ignore"` reason: not rejected,
  just not read, and the default applies. `get_settings()` logs a warning for the pre-rename
  spellings; that helper is a migration aid with an expiry date, not a compatibility layer.
- **Values that must not be environment-settable are `ClassVar`** — `ZoraSettings.ZORA_DSPACE_API_URL`
  and its four siblings. `ClassVar` is the only mechanism that works: pydantic registers no field, so
  there is no name to override. `Field(frozen=True)` blocks assignment but **still loads from the
  environment**, and a bare `Final` is deprecated for this since pydantic 2.11.

`.env.example` is the full inventory, grouped by owning member.

Tooling: `uv` everywhere — a **single root `uv.lock` covering all five members**, tracked, and what
actually gets installed by CI (`uv sync --locked --all-packages`, or `--package themis-<x>`) and by
the container images alike; pip is used nowhere. One `.venv`, at the root: `--package X` *replaces*
its contents rather than making a second environment. `pytest` (560 tests / 43 files; 66 need
Postgres and skip without `DATABASE_URL`) and `ruff` (line length 100, py311) are configured
**only in the root `pyproject.toml`** — which fixes pytest's rootdir at the repo root, so always
invoke it from there. `ruff` lives in the root `dev` group; `pytest` is repeated in every member's
`dev` group so `--package X` still yields a runnable environment.

**Run `scripts/check.sh --ci` before handing work over, and never read a green local `pytest` as a
green CI.** CI installs *less* than a development machine: `offline` and `pgvector` sync
`--all-packages` with no extras, while the local `.venv` carries `scraping`, `embeddings` and
`mcp`. Anything gated on an extra therefore passes here and fails there — a `conftest.py` calling
`pytest.importorskip` at module level took down both of those jobs in exactly that way, while the
one job installing the extra stayed green. `--ci` rehearses all five jobs in scratch environments
via `UV_PROJECT_ENVIRONMENT`, leaving `.venv` and its 2.27 GB of torch alone; the same script with
no argument is the fast lint/format/test pass.

**One workflow file, four jobs** — `ci.yml`: `offline` (all members, no network or database),
`pgvector` (a real pgvector service plus `themis-init-db`), `boundaries` (a **5-leg** matrix
installing each member alone, so a cross-member import fails loudly), and `wheels` (proves
`schema.sql` ships as package data — it is resolved by name at runtime, so a missing declaration
would fail only inside a container). `mcp`, `embeddings` and `render` are never installed in CI.

A standalone `scraper` job existed until 2026-08-27, when the scraper's `scraping` extra became
ordinary dependencies and the boundaries matrix grew a fifth leg that subsumes it. **An extra is a
configuration nobody tests**: `uv sync --package themis-scraper` produced a package whose only
console script died on `--help`, and no job installed it that way. Each leg's import target is
spelled out rather than derived, because `themis_scraper/__init__.py` imports nothing and the bare
package import would have passed anyway.

**Two CI systems, two remotes, no overlap (2026-08-27).** `origin` is GitHub and runs `ci.yml`,
which never builds an image. `gitlab` (`git@gitlab.uzh.ch:askuzh/themis.git`) runs
`.gitlab-ci.yml`, which builds all four images and never runs a test — green on one says nothing
about the other. It reads the tag from `projects/<role>/pyproject.toml`, so bumping `version`
there is the whole release procedure; every branch builds, only the default branch pushes
`themis-<role>:<version>-test` and `:latest-test` to
`registry.cs.zi.uzh.ch/uzh-dsi-askuzh-masterthesis-supervisor`. It builds with **kaniko** (since
2026-08-28), and that is **carried debt, not a preference** — kaniko was archived upstream in 2025.
gitlab.uzh.ch's shared runners are unprivileged, and both normal builders need a change to the
runner's `config.toml` that no `.gitlab-ci.yml` can make: `docker:29-dind` needs `privileged = true`
(without it the daemon never starts and the job dies on a missing `/certs/client/ca.pem`, which
reads as a TLS fault and is not one), and **buildah fails too** — under Docker's default capability
set it tries to gain `CAP_SYS_ADMIN` through a user namespace, and the default seccomp profile
blocks `CLONE_NEWUSER`; neither `STORAGE_DRIVER=vfs` nor `BUILDAH_ISOLATION=chroot` avoids it. All
three were tested; the reproductions are in the file. **The exit condition is a privileged runner**,
not a newer kaniko.

Two consequences. There is **no cross-job layer cache** — kaniko can only cache by pushing every
intermediate layer into Harbor, against a 10 GB quota with no GC — so every build is cold and the
matcher re-downloads torch each time. And the **root `.dockerignore` is the one CI applies**,
because `projects/<role>/Dockerfile.dockerignore` is a BuildKit convention kaniko ignores; those
four still govern a local `docker build`, so the two paths filter the context differently. For the
same reason no Dockerfile here may use BuildKit-only syntax (`RUN --mount`, heredocs, `# syntax=`):
it would build locally and break CI.

Deployment target is a **UZH Kubernetes cluster** pulling from that registry, with a
**Postgres + pgvector** server; see [`docs/deployment.md`](docs/deployment.md). Harvesting runs
as a cluster job — never in CI, and **never committing data back to the repo**.

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