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
                       └─ report: flagged sources; exit 1 above 30% not scraped
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
| `Source`, registry/state loaders | `registry.py` | The immutable source list, and the `var/state.json` lifecycle, with verification and `page_type` taken from the committed contracts. |
| `FetchResult` and the fetch stage | `fetch.py` | `requests` first, Playwright chromium only if installed and needed, PDFs as bytes. |
| cache read/write, content hashes | `cache.py` | `cache/<id>/{page.html,meta.json,history/}`; change detection. |
| spec-driven extraction, `SpecError` | `spec_engine.py` | LLM-free: container, fields, transforms, follow. Name transforms (`strip_titles`, `name_lastfirst`, `name_lastfirst_space`) delegate to [`themis_shared.names`](../../libs/shared/README.md) — the matcher needs the same title vocabulary, and one vocabulary in two places drifts. The `_TRANSFORMS` keys are unchanged, because the frozen specs reference them by name. |
| spec drafting, `DraftError` | `spec_generator.py` | LLM drafts a spec at onboarding. Never trusted blind — a human approves it. |
| `complete()`, `is_available()` | `llm.py` | The package's only LLM boundary. |
| process extraction | `llm_extract.py` | Process page → one record. Only `process_description` is LLM-written. |
| scoring, repair, `Verdict` | `title_check.py` | Title plausibility and repair. |
| `classify()`, `Result` | `validate.py` | OK / PAGE_CHANGED / NEEDS_REVIEW / FETCH_FAILED / EXTRACT_FAILED / SCHEMA_INVALID / LLM_FALLBACK. |
| `load()`, `save()`, `upsert_source()` | `dataset.py` | The nested target data model, and its JSON on disk. |
| `to_posting()`, `iter_records()` | `normalize.py` | Records → `ThesisPosting` / `ResearcherProfile` / `ApplicationProcess`. |
| `write_dataset()`, `posting_count()` | `store.py` | **The only writer** of the three tables. |
| `finalize()`, `write()`, `notify()` | `report.py` | Run report and notification of flagged sources. The exit code is set in `main.cmd_run`, not here: see "Exit codes". |

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

### Exit codes

A CronJob decides success from the exit code alone, so it answers "is the scraper
broken?", not "did every page work?". Over ~100 pages some are always dead, and failing
on the first one made every run red.

- **`run` exits 1** if a failure raises out of it (the Postgres write, the dataset file),
  if no source is verified at all, or if **more than 30%** of the verified sources were
  not scraped this cycle (`validate.MAX_UNSCRAPED_RATIO`). "Not scraped" means the run
  could not store the source (`fetch_failed`, `extract_failed`, `schema_invalid`) *or*
  this cycle's fetch failed. The second matters because `run` extracts from any cached
  page, so a dead page still comes out `ok` on last cycle's content.
- Flagged sources that were stored (`page_changed`, `needs_review`, `llm_fallback`) are
  in the run report and the notification and do not affect the exit code.
- **An LLM failure is not the page's failure.** When the LLM is unconfigured or errors
  where a source needed it (a process page's summary, a PDF-enriched description), the
  source fails this run and counts toward the 30%, but is **not quarantined**: a quarantine
  on the PVC would outlive the missing key. Without `SCRAPER_LLM_API_KEY` the 50 process
  pages alone are 49%, so such a run exits 1, and the next run with a key recovers.
- **`fetch` exits 1** above the same 30%, counted against every selected source, so a
  `--resume` retry of 3 dead pages out of 103 is 3%, not 100%.
- The ratio is taken before `--resume` filtering in both commands, so a retry pod judges
  the whole cycle. `check` still exits 1 on any flag: it is the single-source dev tool.

The cluster runs `fetch --resume; run --resume`, with `;`, not `&&`: `run` must not be
skipped because a page died, and the Job's status is `run`'s.

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
- **Verification comes from the committed contracts (resolved 2026-09-30).** It used to
  live only in the gitignored `var/state.json`, so a pod with a fresh volume saw all 103
  sources unverified and `run` did nothing. That happened in the cluster, where nobody
  can copy a state file into the PVC. Now `registry.load_state` treats a committed
  `specs/<id>/expected.json` as the record of an approved onboarding: every source has
  one, and `onboard` re-freezes it on each re-onboarding, so it is as current as the
  onboarding itself. `page_type` comes from it too; the state copy `run` used to read,
  defaulting to `"process"`, was a second truth. The state keeps what only runs know:
  progress, fetch results, quarantine. A quarantine records which contract it was taken
  against (`quarantined_contract`), so a re-onboarding shipped in a new image lifts it
  without anyone touching the cluster's state file.
- **The page cache is not persisted in the cluster.** The same open question
  [`../../docs/deployment.md`](../../docs/deployment.md) raises about the ZORA raw
  cache: an `emptyDir` throws away the property the cache exists for.
- **Personal data and politeness.** Supervisor and profile emails are personal data the
  departments chose to publish; they are stored, never embedded, and `office`/`phone` are
  dropped at normalisation even though 19 profiles carry them. `robots.txt` is **not**
  parsed — politeness here is a sequential fetch, a real delay and an honest User-Agent,
  which is not the same thing as checking a policy file. Worth closing before any run
  broader than the 103 curated sources.
