# scraper

Collects open thesis topics, supervisor profiles and application procedures from UZH
departmental websites. This is the second producer in the *Data Extraction* lane of
[`docs/architecture.png`](../../docs/architecture.png), beside
[`zora/`](../zora/README.md): where that one asks a REST API for publications, this one
reads 103 human-chosen web pages and turns them into records.

Ported from the `Webscraping-Prototype` repository (commits `8b19feb..6f40922`), which
remains the authorship record. **Where this package and the rest of the repository
disagreed about the shape of a posting, the package won** — `contracts.ThesisPosting`
was written before any scraper existed and had guessed wrong in four places.

**Owns all writes to its three tables (invariant 1).** Serving code reads them through
`themis_matcher.indexing.sources.PostgresSourceReader`, never from here.

## Role in the pipeline

```
registry/scraping_sources.json   (human-authored: 103 sources across 37 units)
        │
        ▼
   fetch  ──▶ cache/<source_id>/page.html      polite, sequential, honest UA
        │        (+ history/, for change detection)
        ▼
   route by page_type ── topics (29) │ people (21) │ process (50) │ none (3)
        │
        ├─ spec_engine   deterministic extraction driven by specs/<id>/spec.yaml
        │                └─ llm_extract  ONLY for process prose, or as a flagged fallback
        ▼
   title_check ──▶ validate ──▶ dataset (nested JSON) ──▶ store (Postgres)
                       │                                      │
                       └─ report: flagged sources, non-zero exit
                                                              ▼
                                              posting / researcher_profile /
                                              application_process
```

The philosophy is the prototype's and worth keeping verbatim: **humans decide *where*
and *what*; deterministic templates make extraction *repeatable*; cached HTML
*decouples* fetching from scraping; alarms *report drift*.** A routine re-run touches no
LLM at all.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `main()` | `main.py` | argparse CLI, interrupt/resume loop, interactive `onboard` flow. Orchestration only. |
| `Settings`, `get_settings()` | `config.py` | Every configurable default, `SCRAPER_`-prefixed. Derived paths are properties off `data_root`. |
| `Source`, registry/state loaders | `registry.py` | The immutable source list, and the mutable `var/state.json` lifecycle. |
| `FetchResult` and the fetch stage | `fetch.py` | `requests` first, Playwright chromium only if installed and needed, PDFs as bytes. |
| cache read/write, content hashes | `cache.py` | `cache/<id>/{page.html,meta.json,history/}`; change detection. |
| spec-driven extraction, `SpecError` | `spec_engine.py` | LLM-free: container, fields, transforms, follow. |
| spec drafting, `DraftError` | `spec_generator.py` | LLM drafts a spec at onboarding. Never trusted blind — a human approves it. |
| `complete()`, `is_available()` | `llm.py` | The package's only LLM boundary. |
| process extraction | `llm_extract.py` | Process page → one record. Only `process_description` is LLM-written. |
| scoring, repair, `Verdict` | `title_check.py` | Title plausibility and repair. |
| `classify()`, `Result` | `validate.py` | OK / PAGE_CHANGED / NEEDS_REVIEW / FETCH_FAILED / EXTRACT_FAILED / SCHEMA_INVALID / LLM_FALLBACK. |
| `load()`, `save()`, `upsert_source()` | `dataset.py` | The nested target data model, and its JSON on disk. |
| `to_posting()`, `iter_records()` | `normalize.py` | Records → `ThesisPosting` / `ResearcherProfile` / `ApplicationProcess`. |
| `write_dataset()`, `posting_count()` | `store.py` | **The only writer** of the three tables. |
| `finalize()`, `write()`, `notify()` | `report.py` | Run report; non-zero exit when anything is flagged. |

## Data flow

**Reads:** `data/scraper/registry/scraping_sources.json`, `data/scraper/specs/<id>/spec.yaml`,
`data/scraper/cache/`, and the live web during `fetch`.
**Writes:** `data/scraper/{cache,var,output}/` on disk, and three Postgres tables.

| Table | Rows it holds | Read by |
|---|---|---|
| `posting` | one open thesis topic | `indexing`, via `PostgresSourceReader` |
| `researcher_profile` | a researcher as their own page describes them | **nothing yet** |
| `application_process` | how to apply, per unit and degree level | **nothing yet** |

The last two are persisted and unread on purpose. See **Known gaps**.

### Why `degree_levels` is a list

Pages write the level as prose. Measured across all 247 topics in the frozen corpus:

| Value on the page | Topics |
|---|---|
| `Bachelor, Master` | **121** |
| `Master` | 102 |
| *(nothing)* | 19 |
| `Master Thesis (30 ECTS)` | 3 |
| `Bachelor Thesis (18 ECTS)` | 1 |
| `Bachelor` | 1 |

Half the corpus offers one topic at two levels. A scalar `degree_level` — which is what
the contract had — forces each of those 121 to pick one and go invisible to the other
level's queries.

That list then cannot be filtered directly, in either store: Postgres uses
`metadata @> …` and jsonb containment does not match a scalar against a nested array,
while `InMemoryVectorStore` compares with `==`. They fail *identically*, so
`projects/matcher/tests/test_store_contract.py` — parametrised over both — could not have caught it. So
`posting_to_document` also emits `degree_bachelor` / `degree_master` / `degree_phd`
booleans, following the `has_uzh_author` precedent
[`../indexing/README.md`](../../projects/matcher/src/themis_matcher/indexing/README.md) sets for exactly this problem. In SQL
the `posting.degree_levels text[]` column is queried with `&&` instead.

### Why `status` had to exist

Departmental pages mark a topic as taken rather than removing it. Of 247 topics, 221 are
open, 15 assigned (`taken` folds into `assigned` — same claim), 2 private, 1 pending,
and 8 say nothing. Without the field an assigned topic is indistinguishable from an
available one, and the system would recommend work nobody can do.
`PostgresSourceReader` therefore excludes `assigned` and `private` from indexing, and
**keeps NULL**: "the page did not say" is not the same claim as "taken".

### The LLM's three jobs, and only three

Process-page summarisation, spec drafting at onboarding, and a run-time fallback when a
template matches nothing (always flagged). Everything else is deterministic, which is
what makes "same cached page + same template ⇒ identical records" testable at all —
`projects/scraper/tests/test_specs.py` asserts exactly that against a committed baseline.

## Configuration

`ScraperSettings` in [`config.py`](src/themis_scraper/config.py), a `SCRAPER_`-prefixed
subclass of the shared [`Settings`](../../libs/shared/src/themis_shared/config.py).
The prefix is what keeps `llm_model` and friends from colliding with the matcher's
`MATCHER_LLM_*`, which mean a different model for a different job.

The last two rows are inherited and deliberately **not** prefixed — a
`validation_alias` on the shared class pins them, so `env_prefix` cannot rename
them out from under docker-compose. They used to live on a *separate* Settings
object that `main.py` imported under an alias; one object now, which is what
removed the hazard that comment warned about.

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `contact` | `SCRAPER_CONTACT` | **none** | Advertised in the User-Agent. **Required** — `user_agent` raises without it. |
| `data_root` | `SCRAPER_DATA_ROOT` | `data/scraper` | Relocates registry/, specs/, cache/, var/, output/ as a group. |
| `polite_delay_seconds` | `SCRAPER_POLITE_DELAY_SECONDS` | `2.0` | Between requests. Lower only with a reason. |
| `http_timeout_seconds` | `SCRAPER_HTTP_TIMEOUT_SECONDS` | `30` | Per request; also the Playwright `goto` timeout. |
| `cache_history_keep` | `SCRAPER_CACHE_HISTORY_KEEP` | `3` | Previous page versions kept for drift detection. |
| `llm_provider` / `llm_model` | `SCRAPER_LLM_*` | `openai` / `gpt-5-mini` | Its own LLM, not the matchmaker's. |
| `llm_api_key` | `SCRAPER_LLM_API_KEY`, or `OPENAI_API_KEY` | none | Absent ⇒ `is_available()` is false and every caller keeps deterministic output. |
| `render_idle_ms` / `render_settle_ms` | `SCRAPER_RENDER_*` | `6000` / `700` | Only meaningful with the `render` extra. |
| `database_url` | `DATABASE_URL` | local Postgres | *Inherited.* Where `store.py` writes `posting`, `researcher_profile` and `application_process`. |
| `matcher_base_url` | `MATCHER_BASE_URL` | unset | *Inherited.* Where the post-run index trigger goes. Unset means the trigger is skipped rather than the run failing. |

Deliberately **not** configurable: the title thresholds in `title_check.py`, and the
field lists, regexes and prompts. They are calibrated against
`projects/scraper/tests/golden_specs.json`; an env var moving them would break the determinism
invariant and the test that guards it.

## Swappable seams

Like [`themis-zora`](../zora/README.md), this member does **not** follow the `base.py`
Protocol + `build_*(settings)` idiom `parsing/`, `indexing/`, `retrieval/` and
`synthesis/` use. It is a concrete scraper for concrete websites, and the swap point is
the *table* boundary: anything that fills `posting` correctly is a substitute.

The one real seam is `llm.py`. `SCRAPER_LLM_PROVIDER=foo` loads `llm_foo.py`, which must
expose a `Provider` class, so a local model or a gateway drops in without touching a
caller.

## Operations

Two invocations, kept separate on purpose: `fetch` is the stage that talks to uzh.ch.

```bash
# Stage 1: fetch (polite, sequential, resumable). Needs SCRAPER_CONTACT set.
themis-scraper fetch --resume
# Stage 2: extract, validate, write to Postgres. Reads only the cache.
themis-scraper run --resume

themis-scraper status             # per-source lifecycle
themis-scraper check <source_id>  # one source, verbose
themis-scraper onboard --next     # interactive: add a source
```

Exposed as the console script `themis-scraper`, or `python -m themis_scraper`. This README used
to argue against a script on the grounds that an operator tool does not belong behind the same
command as the front doors — an argument the workspace split retired, because each member now
owns its own command rather than sharing one.

In the cluster: `projects/scraper/Dockerfile`, whose `ENTRYPOINT` is already the module and
whose `CMD` is the `run --resume` half. Locally,
`docker compose run --rm scraper fetch --resume`.

## Field mapping

`concrete_topics` record → `ThesisPosting`, with `faculty` and `department` injected from
the record's *position* in the nested dataset rather than read off it:

| Contract field | Source | Note |
|---|---|---|
| `id` | `topic_id` | sha1 over source url + record seed; stable across runs. |
| `title` | `title` | Also the first line of the embedded text — see below. |
| `description` | `topic_description` | |
| `supervisors` | `supervisors[]`, else flat `supervisor_name`/`supervisor_email` | 205 topics use the list, 34 the flat pair, **0 both**. |
| `degree_levels` | `degree_level` (prose) | Word-matched, so `"Bachelor, Master"` yields two. |
| `status` | `status` | `taken` → `assigned`. |
| `keywords` | `research_area` | The only topical label these pages carry. |
| `url` | `source_link` | |
| `listed_on` | `date_of_listing` | ISO only; any other format yields NULL rather than a guess. |
| `faculty` / `department` | tree position | `faculties[…].faculty`, `units[…].unit`. |

Inside `supervisors[]` only `name` is dependable. Of 264 entries: 96 a bare name, 48 with
an email, 120 with a profile link under one of three different keys (`profile_url`,
`contact_url`, `_url`), 64 of those also naming a chair.

**`posting_to_document`'s part order is load-bearing.** `retrieval/vector.py` recovers a
posting's displayed title as `text.splitlines()[0]`, not from metadata, so moving `title`
off the front silently retitles every posting in every result.

## Status

**Ported and tested.** 148 tests in `projects/scraper/tests/` (7 files), of which 9 are the
Postgres-gated store tests. The rest replay 103 frozen page snapshots and need no
network and no database — the same property the rest of the repository's offline path
has, arrived at independently in the prototype. CI runs them in a dedicated `scraper`
job, because the `offline` job installs no extras and would otherwise skip them
silently.

`projects/scraper/tests/test_specs.py` replays every topics/people spec against its snapshot and
compares against `golden_specs.json`, so extraction drift fails a build rather than a
run.

Last full prototype run: 7 faculties, 37 units, 707 concrete topics, 565 people, 57
process entries, zero quarantined.

## Known gaps

- **`researcher_profile` and `application_process` are written and never read —
  high-priority follow-up.** Hundreds of profiles and dozens of procedures stored with no
  consumer. The intended use is concrete (2026-08-22): when the querying student's
  department is known, attach that unit's application process to the MCP response
  alongside papers and postings — the same argument holds for the people records. The
  profiles are also the more interesting signal: a researcher stating their interests in
  their own words is independent of what ZORA infers from authorship, and the natural
  second input to the missing `ranking` package.
- **63 of 247 topics name no supervisor, and they disappear.** `_persons()` fans a
  posting out to everyone named on it, so a posting naming nobody credits nobody and
  never reaches a result. That is a quarter of the corpus. `has_supervisor` is emitted as
  a filterable companion so a future ranking pass can surface them some other way —
  right now nothing does.
- **A few topics carry 11–15 supervisors**, because the page lists a whole institute
  against every topic on it. Fan-out credits all of them equally, which will distort any
  per-person score built on posting counts.
- **Process-page extraction and PDF enrichment have no tests.**
  `projects/scraper/tests/replay_util.py` says so outright: they need the network and are out of
  scope for the offline replay. That is 50 of 103 sources whose extraction path is
  exercised by nothing.
- **`requests`, not the `httpx` the rest of the repository uses.** Entangled with the
  politeness delay and the Playwright fallback in `fetch.py`. Converting it is mechanical
  but touches the one file nearly every test runs through.
- **A second LLM client.** `llm.py` here and [`../llm.py`](../../projects/matcher/src/themis_matcher/llm.py) both speak
  OpenAI-compatible endpoints. This one has retries with backoff and pluggable providers;
  that one has neither, and a 30 s timeout with no `Settings` knob. They should converge,
  and the honest direction is this one absorbing that one.
- **`main.py` is 1,894 lines.** Orchestration only, but still the largest single file in
  the repository by a wide margin.
- **Onboarding state is untracked, so the committed specs are inert without it.**
  `var/state.json` is the only record of which sources are verified and which page_type
  each one is, and it is gitignored. 66 of the 103 sources ship a frozen
  `spec.yaml` + `snapshot.html` + `expected.json` in the repository, but a fresh checkout
  or a pod with an empty volume marks all 103 unverified and `run` has nothing to do.
  `run` now exits non-zero in that state instead of 0 — it used to look like a healthy
  no-op, which in a CronJob means Success forever while `posting` stays empty — but that
  is a guard, not an answer. The real question is whether "verified" belongs in mutable
  operator state at all when the artefact it certifies is committed: `page_type` in
  particular is declared in the tracked `spec.yaml` *and* duplicated into state, and it is
  the state copy that `run` reads, defaulting to `"process"` when absent. A topics page
  read with the process extractor fails as a plausible-looking `extract_failed`.
  Deriving verification from the committed triple would make the repository
  self-sufficient; decide it with the PVC question below.
- **The page cache is not persisted in the cluster.** The same open question
  [`../../docs/deployment.md`](../../docs/deployment.md) raises about the ZORA raw
  cache: an `emptyDir` throws away the property the cache exists for.
- **Personal data and politeness.** Supervisor and profile emails are personal data the
  departments chose to publish; they are stored, never embedded, and `office`/`phone` are
  dropped at normalisation even though 19 profiles carry them. `robots.txt` is **not**
  parsed — politeness here is a sequential fetch, a real delay and an honest User-Agent,
  which is not the same thing as checking a policy file. Worth closing before any run
  broader than the 103 curated sources.
