# zora

Harvests publication metadata from ZORA, the Zurich Open Repository and Archive,
into the `publication` table that the rest of the system indexes. This is the
*Data Extraction* lane of
[`docs/architecture.png`](../../../docs/architecture.png), upper half.

**This package owns all writes to source data (invariant 1).** `store.py` is the
only writer of `publication` and `harvest_state`; nothing in `indexing/`,
`retrieval/`, `pipeline/`, or `adapters/` may write them. The read side of the
same table is `indexing/sources.py::PostgresSourceReader`. If you need new data,
it enters the system through this package.

ZORA runs DSpace-CRIS and is accessed through its REST API — **not** OAI-PMH. See
`CLAUDE.md` for the API facts confirmed with the ZORA maintainers, and
[`docs/zora-harvester.md`](../../../docs/zora-harvester.md) for operator-facing
run instructions.

## Role in the pipeline

```
ZORA DSpace REST API
   │  Solr query, paginated, sorted by dc.date.accessioned
   ▼
zora_client.iter_items ──▶ normalize.normalize_item ──▶ output_schema.to_output
   │                        (raw DSpace → flat dict)     (flat dict → ZoraPublication)
   ├──▶ data/raw/<ts>_<mode>.jsonl        (per-run dump, still on disk)
   │
   └──▶ store.write_harvest  ── ONE TRANSACTION ─────────────┐
            upsert publication                              │
            full mode only: delete rows not in this harvest  │
            count, and ROLL BACK if the corpus shrank        │
            implausibly (MIN_RETENTION_RATIO)                │
        store.save_state    → harvest_state (watermark)      │
                                              │              │
                                              ▼              │
                     indexing/ reads the publication table ◀─┘
                     (`thesis-matchmaker index --source db`)
```

## Public API

| Symbol | File | Purpose |
|---|---|---|
| *(constants only)* | `config.py` | Every DSpace field name, the API endpoint, the raw-dump directory, and the safety threshold. One place to change when ZORA's schema moves. |
| `write_harvest(rows, ...)` | `store.py` | Upsert + prune + retention check, in one transaction. Returns counts and whether it aborted. |
| `publication_count()` | `store.py` | Row count, for the retention check and for operators. |
| `load_state()` / `save_state(...)` | `store.py` | The `harvest_state` row. Re-exported by `state.py` with the old signatures. |
| `get_client()` | `zora_client.py` | Builds an authenticated `DSpaceClient` with retries (3×, backoff 2, on 500/502/503/504) and timeouts (10 s connect, 60 s read). Raises `RuntimeError` if the token is missing or auth fails. |
| `iter_items(client, scope, since)` | `zora_client.py` | Generator over DSpace items; builds the Solr query and handles pagination. |
| `normalize_item(dso)` | `normalize.py` | Raw `SimpleDSpaceObject` → flat internal dict, unwrapping DSpace's `{"value": …}` metadata entries. |
| `ZoraPublication` | `output_schema.py` | Pydantic model defining the on-disk contract for `publications.jsonl`. |
| `to_output(record)` | `output_schema.py` | Internal flat dict → output dict. |
| `validate_publications_jsonl(path)` | `output_schema.py` | Per-line validation; returns `(count, errors)`. Also runnable as `python -m thesis_matchmaker.zora.output_schema <path>`. |
| `load_state()` / `save_state(...)` | `state.py` | Read and write the incremental watermark and per-mode run timestamps. |
| `run(mode, since_override, limit)` | `harvest.py` | The whole harvest: fetch → normalize → dedupe → safety check → write → validate → save state. Returns an exit code. |
| `main()` | `harvest.py` | argparse entry point. |
| `run_forever(...)` | `scheduler.py` | Long-lived poll loop that decides when to call `harvest.run()`. |
| `_next_action(state, hour, weekday)` | `scheduler.py` | Deliberately I/O-free decision function, which is why the scheduler is the best-tested part of this package. |

## Data flow

**Reads:** the ZORA REST API; the API token file; `data/state.json`; and — in
incremental mode only — the existing `data/publications.jsonl`.

**Writes:** `data/publications.jsonl`, `data/state.json`,
`data/raw/<YYYYMMDDTHHMMSSZ>_<mode>.jsonl`.

`data/state.json` shape:

```json
{
  "last_accessioned": "2026-07-17T09:04:55Z",
  "last_run_at": "2026-07-17T09:08:19.273497+00:00",
  "last_total_publications": 22541,
  "last_incremental_run_at": null,
  "last_full_run_at": null
}
```

### Incremental harvesting

DSpace gives us no OAI-PMH-style datestamp windows and no deleted-record
tombstones, so the delta logic is ours:

- **Watermark** — `state["last_accessioned"]`, the `dc.date.accessioned` of the
  last item seen. Because results are always sorted `dc.date.accessioned,asc`,
  that is the highest value seen.
- **Query** — full: `dspace.entity.type:Publication`. Incremental: the same plus
  `AND dc.date.accessioned_dt:[<since> TO *]`. The range is inclusive on both
  ends; the boundary item is dropped by id-dedupe rather than by the query.
- **`full` mode replaces** `publications.jsonl` wholesale. It is the only mode
  that reflects upstream deletions and corrections.
- **`incremental` mode merges** by `id`, and **existing records win** — new ids
  are appended, ids already present are skipped, nothing is updated in place.
- **Safety rail** — the run aborts *before writing* if the new total is under
  50 % of `last_total_publications` (`MIN_RETENTION_RATIO`), so a partial API
  failure cannot silently truncate the dataset.

### CLI

```
python -m thesis_matchmaker.zora.harvest --mode incremental
python -m thesis_matchmaker.zora.harvest --mode full --since 2024-07-01
python -m thesis_matchmaker.zora.harvest --mode full --limit 50     # smoke test
```

| Flag | Default | Behaviour |
|---|---|---|
| `--mode {incremental,full}` | `incremental` | See above. |
| `--since ISO_DATE` | none | **Full mode only.** Ignored with a warning in incremental mode, which takes its `since` from `harvest_state`. |
| `--limit N` | none | Stop after N items. For smoke tests. |

Note this is reachable only via `python -m`; unlike `thesis-matchmaker` and
`thesis-matchmaker-mcp`, the harvester has no console-script entry point.

### Scheduler

`scheduler.py` is an in-process poll loop, not cron and not APScheduler. Each
iteration loads state, asks `_next_action`, optionally harvests, then sleeps in
1-second increments so `SIGTERM`/`SIGINT` stays responsive. A shutdown signal lets
an in-flight harvest finish rather than killing it mid-write. Any exception in a
run is logged and swallowed, so one bad night does not kill the process.

Cadence: incremental nightly at `HARVEST_HOUR_UTC`; full weekly on
`FULL_HARVEST_WEEKDAY` at the same hour. When both are due, full wins — which also
means a fresh deployment with no `harvest_state` row does a full harvest first.
That path is worth knowing about: because it is an INSERT rather than an UPDATE,
it is the one place where "a full run also stamps the incremental column" is easy
to get wrong, and getting it wrong makes the scheduler fire an incremental
immediately after a multi-hour full harvest.

## Configuration

| Setting | Env var | Default | Effect |
|---|---|---|---|
| Database | `DATABASE_URL` | see `config.py` in the package root | Postgres holding `publication` and `harvest_state`. Create the schema with `thesis-matchmaker init-db`. |
| Data directory | `ZORA_DATA_DIR` | `data` | Root for `raw/` — the per-run response cache, the only thing still written to disk. |
| Nightly hour | `HARVEST_HOUR_UTC` | `1` | UTC hour at which the scheduler runs an incremental harvest. |
| Weekly day | `FULL_HARVEST_WEEKDAY` | `0` (Monday) | Weekday for the full harvest. |
| API endpoint | `DSPACE_API_ENDPOINT` | `https://www.zora.uzh.ch/server/api` | Defined in `zora_client.py`, not `config.py`. |
| Poll interval | `POLL_INTERVAL_SECONDS` | `3600` | Scheduler sleep between checks. Defined in `scheduler.py`. |
| API token | `PERSONAL_API_TOKEN_FILE` | — | Path to the token file. Read by the vendored DSpace client, not by our code. **Never commit the token.** |

## Swappable seams

This package does not follow the `base.py` + `build_*` factory idiom that
`parsing/`, `indexing/`, `retrieval/`, and `synthesis/` use — it is a concrete
harvester for one concrete API, and the swap point is at the *table* boundary
instead: anything that can populate `publication` can replace it
without the indexer noticing. The DSpace field names are all isolated in
`config.py` for the same reason.

## Operations

- **Docker** — `docker/zora/Dockerfile` builds a `python:3.12-slim` image with
  entrypoint `python -m thesis_matchmaker.zora.harvest`. `data/` is expected to be
  bind-mounted; the container is run with `--user "$(id -u):$(id -g)"` so host
  file ownership stays sane.
- **Scheduling** — harvesting is a cluster concern, not a CI concern. The image is
  invoked as a one-shot job with `--mode incremental` / `--mode full`; see
  [`docs/deployment.md`](../../../docs/deployment.md). Harvest output **never**
  goes back into git.
- **`scripts/zora_inspect_fields.py`** — one-off live-API diagnostic. Prints every
  metadata field present on real items, checks the field names assumed in
  `config.py`, and shows per-author `authority` keys. Run with
  `export PERSONAL_API_TOKEN_FILE=... && python -m scripts.zora_inspect_fields 5`.
  Use it before changing any `FIELD_*` constant.

## Field mapping

The notable, non-obvious mappings (`normalize.py`):

| Output field | Source | Note |
|---|---|---|
| `id` | `dso.handle` | Items without a handle are skipped. |
| `authors` | `uzh.contributor.author` | A **UZH custom field**, not `dc.contributor.author`. |
| `uzh_authors` | the same entries, filtered to those with a non-empty `authority` | A non-null authority means a registered UZH/CRIS researcher. This is what makes supervisor recommendation possible at all — external co-authors are excluded. |
| `author_authority_map` | `{name: cleaned_authority}` | Strips DSpace's `"will be referenced::ORCID::"` placeholder prefix. External co-authors map to `None`. |
| `department` | `_embedded.owningCollection.name`, minus the `"Publications of "` prefix | Not a metadata field — it comes from the collection the item lives in, falling back to the first mapped collection. |
| `keywords` | `dc.subject.ddc` + `uzh.scopus.subjects` + `dc.subject`, order-preserving dedupe | |
| `publication_type` | `dc.type` | Renamed from the internal key `type` in `to_output`. |
| `language` | `dc.language.iso` | e.g. `eng`, `deu`. |
| `accessioned` | `dc.date.accessioned` | Used as the watermark; deliberately **not** part of the output schema. |

## Status

**Implemented and running in production.** `data/publications.jsonl` currently
holds 22,541 harvested records (~45 MB, committed to the repository).

Test coverage is uneven: `tests/zora/test_normalize.py` (20 tests),
`tests/zora/test_scheduler.py` (14), and `tests/zora/test_output_schema.py` (4)
are thorough, but `harvest.py`, `zora_client.py`, and `state.py` have **no
tests** — including `harvest.py`, the largest module in the repository.

## Known gaps

- **`harvest.py`, `zora_client.py`, and `state.py` are untested.** The merge
  semantics, the safety rail, and the watermark update all live in untested code.
- **Incremental mode never updates an existing record.** Merge is new-ids-only; a
  title correction or a newly added abstract upstream is invisible until the next
  full harvest. `dc.date.accessioned` does not change on edit, so this is a real
  limitation of the approach, not just an implementation shortcut.
- **The `--since` range query is untested against the live API** — the code says
  so itself in a warning log.
- **Schema drift with `contracts/`**: `ZoraPublication.title` is `str | None` but
  `ZoraRecord.title` is a required `str`, so a title-less record passes validation
  here and fails at index time. See [`../contracts/README.md`](../contracts/README.md).
- **The harvested file is not what gets indexed by default.** `SOURCES_PATH`
  defaults to `data/samples`, so `thesis-matchmaker index` indexes the 30-row
  sample file unless you pass `--source data`. Easy to miss.
- **`data/raw/` is not gitignored.** A local harvest leaves untracked dumps that
  are easy to commit by accident.
- **`author_orcid` is normalised but never emitted** — `to_output` drops it.
- **`data/state.json` on disk predates the current `save_state`** and lacks
  `last_incremental_run_at` / `last_full_run_at`. They back-fill to `None`, so a
  scheduler started against it treats both cadences as "never ran".
- `schema/zora_publication.schema.json` is a hand-maintained mirror of
  `ZoraPublication`. Nothing checks that the two agree.
- `config.py` refers to the inspect script by its old name `scripts.inspect_fields`.
- `docker/zora/Dockerfile.dockerignore` ends with a stray `pytest tests/zora/ -v`
  line — harmless as an ignore pattern, clearly accidental.
