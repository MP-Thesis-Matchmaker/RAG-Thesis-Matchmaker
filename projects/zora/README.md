# zora

Harvests publication metadata from ZORA, the Zurich Open Repository and Archive,
into the `publication` table that the rest of the system indexes, plus two
entity mirrors: `person` (DSpace-CRIS researcher profiles) and `org_unit` (the
UZH community tree — faculties and institutes). This is the *Data Extraction*
lane of [`docs/architecture.png`](../../../docs/architecture.png), upper half.

**This package owns all writes to source data (invariant 1).** `store.py` is the
only writer of `publication`, `harvest_state`, `person` and `org_unit`; nothing
in `indexing/`, `retrieval/`, `pipeline/`, or `adapters/` may write them. The
read side of the publication table is
`indexing/sources.py::PostgresSourceReader`. If you need new data, it enters the
system through this package.

ZORA runs DSpace-CRIS and is accessed through its REST API — **not** OAI-PMH. See
`CLAUDE.md` for the API facts confirmed with the ZORA maintainers, and
[`docs/zora-harvester.md`](../../../docs/zora-harvester.md) for operator-facing
run instructions.

## Role in the pipeline

One `harvest` run does three things in order — the two entity mirrors, then the
publications:

```
ZORA DSpace REST API
   │
   ├─1─▶ entities.harvest_persons    (iter_persons  → normalize_person  → mapping.to_person)
   ├─2─▶ entities.harvest_org_units  (iter_org_tree → normalize_org_unit → mapping.to_org_unit)
   │        each: data/raw/<ts>_{persons,orgunits}.jsonl, then a snapshot replace
   │        (upsert + prune in ONE TRANSACTION; empty snapshot refused)
   │        a failure here stops the run before the expensive half
   │
   └─3─▶ zora_client.iter_items ──▶ normalize.normalize_item ──▶ mapping.to_publication
            │  Solr query, paginated, sorted by dc.date.accessioned
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
                              (`themis-matcher index --source db`)
```

The mirrors come first because they are what a publication's author authorities
(`cris` ids → `person.uuid`) and owning collection (`owning_collection_uuid` →
`org_unit.collection_uuid`) resolve *against*.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| *(constants only)* | `config.py` | Every DSpace field name, the API endpoint, the raw-dump directory, and the safety threshold. One place to change when ZORA's schema moves. |
| `write_harvest(rows, ...)` | `store.py` | Upsert + prune + retention check, in one transaction. Returns counts and whether it aborted. |
| `publication_count()` | `store.py` | Row count, for the retention check and for operators. |
| `load_state()` / `save_state(...)` | `store.py` | The `harvest_state` row. `load_state` returns a `HarvestState`; a database that has never been harvested yields the defaults rather than an error. |
| `get_client()` | `zora_client.py` | Builds an authenticated `DSpaceClient` with retries (3×, backoff 2, on 500/502/503/504) and timeouts (10 s connect, 60 s read). Raises `RuntimeError` if the token is missing or auth fails. |
| `iter_items(client, scope, since)` | `zora_client.py` | Generator over DSpace items; builds the Solr query and handles pagination. |
| `normalize_item(dso)` | `normalize.py` | Raw `SimpleDSpaceObject` → flat internal dict, unwrapping DSpace's `{"value": …}` metadata entries. |
| `to_publication(record)` | `mapping.py` | Internal flat dict → validated `contracts.ZoraPublication` row. The renames (`handle`→`id`, `type`→`publication_type`, `uri`→`url`) live here. |
| `run(mode, since_override, limit, from_dump, persons, org_units, publications)` | `harvest.py` | The whole harvest: persons → org units → publications, each opt-out-able. Returns an exit code. |
| `main()` | `harvest.py` | argparse entry point. The only runnable module in the package. |
| `iter_persons(client)` | `zora_client.py` | Generator over the ~2,017 DSpace-CRIS Person entities (`dspace.entity.type:Person`). |
| `iter_org_tree(client, root_uuid)` | `zora_client.py` | BFS over the community tree from the UZH root; yields `(community, parent_uuid, depth, faculty_uuid, collections)` per node. Raises on any failed page — a half-walked tree must not become a snapshot. |
| `normalize_person(dso)` / `normalize_org_unit(...)` | `normalize.py` | Raw API objects → flat person / org-unit dicts. |
| `to_person(record)` / `to_org_unit(record)` | `mapping.py` | Flat dicts → validated `contracts.ZoraPerson` / `ZoraOrgUnit` rows. |
| `harvest_persons(client, limit)` / `harvest_org_units(client, limit)` | `entities.py` | One entity mirror each: fetch → normalize → dump → validate → snapshot write. Called by `harvest.run`; **not runnable on their own**. |
| `reconcile_uzh_authors()` | `store.py` | Recomputes `uzh_authors` for every publication from `authors` + `author_authority_map`. Returns rows changed; idempotent. How an eligibility-rule change reaches the existing corpus without a re-harvest. |
| `write_persons(rows)` / `write_org_units(rows)` | `store.py` | Snapshot-replace of the mirror tables (upsert + prune in one transaction). Refuse an empty snapshot over a non-empty table. |
| `write_raw_dump(records, kind)` / `read_raw_dump(path)` | `raw_dump.py` | The per-step JSONL cache under `data/raw/`. Its own module because both `harvest.py` and `entities.py` write dumps. |
| `dump_kind(path)` | `raw_dump.py` | Which step a dump feeds, read off its filename. Raises rather than guessing when the name says nothing. |

**The models are not here.** `contracts.ZoraPublication`, `ZoraPerson`,
`ZoraOrgUnit` and `AuthorAuthority` live in
[`../contracts/`](../contracts/README.md); this package maps onto them. Until
2026-08-24 it kept its own parallel copies in `output_schema.py`, and they drifted
— see that README for what broke.

## Data flow

**Reads:** the ZORA REST API; the API token file; the `harvest_state` row.

**Writes:** the `publication` table and the `harvest_state` row (both via
`store.py`, the only writer), plus
`data/raw/<YYYYMMDDTHHMMSSZ>_<mode>.jsonl` — the raw-response cache, the one
thing still on disk.

The previous corpus does **not** need reading back: the upsert is the merge, and
the retention check counts rows inside the same transaction.

`harvest_state` is a single row, `CHECK (id = 1)`:

| Column | Purpose |
|---|---|
| `last_accessioned` | The watermark — highest `dc.date.accessioned` seen. |
| `last_total_publications` | Row count after the last successful run; the retention check's baseline. |
| `last_run_at` | Any successful run. |
| `last_incremental_run_at` / `last_full_run_at` | Per-mode stamps. Both are stamped by a full run, because a full harvest supersedes an incremental one. Nothing reads them now that the CronJobs own the cadence, but they are not dead: a CronJob's own history records that a pod *fired*, whereas these record that a harvest *committed*, and the retention rail can roll a run back while the pod still exits 0. `schema.sql` used to call them redundant; that comment was wrong and stayed wrong while a fix cost a full reset, and was corrected once `schema.py` began fingerprinting the DDL rather than the file. |

### Incremental harvesting

DSpace gives us no OAI-PMH-style datestamp windows and no deleted-record
tombstones, so the delta logic is ours:

- **Watermark** — `state["last_accessioned"]`, the `dc.date.accessioned` of the
  last item seen. Because results are always sorted `dc.date.accessioned,asc`,
  that is the highest value seen.
- **Query** — full: `dspace.entity.type:Publication`. Incremental: the same plus
  `AND dc.date.accessioned_dt:[<since> TO *]`. The range is inclusive on both
  ends; the boundary item is dropped by id-dedupe rather than by the query.
- **`full` mode is authoritative.** After the upsert it runs
  `DELETE FROM publication WHERE id <> ALL(<harvested ids>)`, so it is the only
  mode that reflects upstream deletions and corrections.
- **`incremental` mode never deletes.** It upserts what it fetched and nothing
  else. Since the incremental query only returns newly-accessioned items, an
  upstream edit to an existing record still stays invisible until the next full
  harvest — `dc.date.accessioned` does not change on edit.
- **Safety rail** — upsert, prune and count all happen in **one transaction**. If
  the resulting total is under 50 % of `last_total_publications`
  (`MIN_RETENTION_RATIO`), the transaction is rolled back, so a partial API
  failure leaves the previous corpus completely intact rather than a
  half-written one.

### CLI

```
python -m themis_zora.harvest --mode incremental
python -m themis_zora.harvest --mode full --since 2024-07-01
python -m themis_zora.harvest --mode full --limit 50     # smoke test
python -m themis_zora.harvest --no-persons --no-org-units
python -m themis_zora.harvest --no-publications         # mirrors only
python -m themis_zora.harvest --mode full --from-dump data/raw/<ts>_full.jsonl
```

| Flag | Default | Behaviour |
|---|---|---|
| `--mode {incremental,full}` | `incremental` | See above. **Publication step only** — the mirrors are always full snapshots. |
| `--since ISO_DATE` | none | **Full mode only.** Ignored with a warning in incremental mode, which takes its `since` from `harvest_state`. Also ignored with `--from-dump`, where the filter was already applied at fetch time, and with `--no-publications`. |
| `--limit N` | none | Stop after N items **per step**. For smoke tests. |
| `--from-dump PATH` | none | Replay a `data/raw/` dump instead of calling ZORA. **Repeatable**, once per kind; which step it feeds comes from the filename. |
| `--dump-kind KIND` | none | The kind of a `--from-dump` file whose name does not say (renamed, hand-copied). One dump only. |
| `--no-persons` | off | Skip the `person` mirror. |
| `--no-org-units` | off | Skip the `org_unit` mirror. |
| `--no-publications` | off | Skip the publication harvest; refresh only the mirrors. Nothing writes `harvest_state` in that case. |

Disabling all three is a usage error rather than a no-op run.

**`--from-dump` is the point of the raw cache.** A full harvest is ~215K records
and roughly two hours of requests, and those records land in `data/raw/` *before*
anything is written to Postgres. So a run that fetches successfully but fails on
the write does not have to fetch again: the dump already holds normalized
records, and replaying it re-runs only the validate/upsert half of the pipeline.
No API token is needed (no client is built), and no second dump is written, since
the source file already *is* the cache. Everything downstream is unchanged — same
`mapping.to_publication` validation, same single transaction, same retention rail,
same watermark.

Every step writes a dump, so every step can replay one. `write_raw_dump` puts the
kind in the filename and `dump_kind` reads it back out; `--from-dump` is repeatable,
once per kind. One rule covers the lot:

> **If any dump is given, no API request is made. A step with a dump replays from
> it; a step without one is skipped.**

A lone `<ts>_full.jsonl` therefore behaves as it always did — publications only,
neither mirror — which is the old "implies `--no-persons --no-org-units`" rule as a
special case rather than a separate one. Enforced inside `run()`, not just in
argparse, so it holds for programmatic callers; a replaying step is handed `None`
where the client goes, so "no API request" is structural rather than a promise.

Contradictions are usage errors, not precedence questions: a dump whose step is
disabled by its own `--no-*` flag, two dumps of one kind, or `--dump-kind` with
anything other than exactly one dump. A name carrying no kind is likewise refused
up front — guessing would only defer the failure to the validator, with a worse
message — and `--dump-kind` is the way to replay a renamed or hand-copied file.

**A dump's ceiling is what the normalizer extracted when it was written.** It cannot
supply fields a later `normalize.py` change added, which is why the 2026-08-21
publication dump cannot stand in for a re-harvest: it predates
`owning_collection_uuid` and the typed `author_authority_map`, and fails validation
against the current contract.

Note this is reachable only via `python -m`; unlike `themis-matcher` and
`themis-gateway-mcp`, the harvester has no console-script entry point.

### The schema preflight

`run()` calls `schema.require_current(database_url)` before it builds a client, so a
database whose schema predates the code fails in one round-trip instead of at
whichever relation the first query happened to touch. That is not hypothetical: a
`--no-publications` run once fetched 2,018 person records before finding out
`person` did not exist, because the mirrors had been added to `schema.sql` and
`init-db --reset` had not been run yet.

The check applies to **every** path, a `--from-dump` replay included — a replay skips
the API, not Postgres. The message names both fingerprints and the command that
fixes it. `psycopg` errors reach `main`'s one-line handler too (`db.DB_ERRORS`), so a
dead or out-of-date database is reported rather than traced.

### Entity mirrors: `person` and `org_unit`

They are **steps of a harvest, not a job of their own**: every run refreshes them
first, and there is no separate entrypoint and no separate schedule. `entities.py`
holds the two steps and is deliberately not runnable — no argparse, no `main`.
Opt out per run with `--no-persons` / `--no-org-units`.

Both are always full snapshots: fetch everything, upsert, prune what disappeared.
Watermark, incremental mode and the retention ratio are all meaningless at ~2,000
and ~500 rows; the one safety rail is in the store — an empty snapshot never
overwrites a non-empty table. Raw dumps land in `data/raw/<ts>_persons.jsonl` /
`<ts>_orgunits.jsonl` like publication runs.

An entity step that fails ends the run **before** the publication step: a full
harvest costs hours, and if the API is refusing requests or the tree walk broke,
finding out now is cheaper than finding out then.

**`person`** mirrors the ~2,017 `dspace.entity.type:Person` items (probed
2026-08-24): uuid, names, bare ORCID, handle. Upstream carries **no affiliation,
department or email** on these items, and CRIS coverage is sparse — most UZH
authors have no Person record, so *absent from `person` does not mean not UZH*.

**`org_unit`** mirrors the community tree under the UZH root
(`config.UZH_ROOT_COMMUNITY_UUID`): root → 13 faculties → institutes/clinics,
with `parent_uuid` / `faculty_uuid` / `depth` precomputed by the walk, plus
`dc.zora.subjectid` (UZH's own numeric org id) and the attached
"Publications of X" collection. ZORA's OrgUnit *entity type* exists but has 0
items — the communities are the org structure.

**Join paths** (query-time, no FKs):

| From | To | On |
|---|---|---|
| `publication` | `person` | `author_authority_map[name].id = person.uuid` where `.type = 'cris'` |
| `publication` | `person` | `author_authority_map[name].id = person.orcid` where `.type = 'orcid'` (the ~55 "seen both ways" names) |
| `publication` | `org_unit` | `publication.owning_collection_uuid = org_unit.collection_uuid` |

### Scheduling

This package does not decide when to run. There was an in-process poll loop
(`scheduler.py`, deleted); the cluster's CronJobs replace it, and
[`k8s/`](../../../k8s/README.md) holds the two schedules. See
[`docs/deployment.md`](../../../docs/deployment.md).

What the CronJobs replaced is narrower than it looks. They took over the *when* —
the "is a run due yet?" decision. They did not touch the *what*: `--mode
incremental` still resumes from `harvest_state.last_accessioned`, exactly as it
did under the poll loop. The one rule that had to survive the move is "full wins
when both are due", and it is now declarative: the two CronJobs get disjoint day
sets (`0 1 * * 1` and `0 1 * * 0,2-6`), so they cannot both fire and the tie-break
has nowhere left to live.

Still worth knowing: a fresh deployment with no `harvest_state` row writes that
row by INSERT rather than UPDATE, which is the one place where "a full run also
stamps the incremental column" is easy to get wrong.

## Configuration

| Setting | Env var | Default | Effect |
|---|---|---|---|
| Database | `DATABASE_URL` | see `config.py` in the package root | Postgres holding `publication` and `harvest_state`. Create the schema with `themis-init-db`. |
| Data directory | `ZORA_DATA_DIR` | `data` | Root for `raw/` — the per-run response cache, the only thing still written to disk. |
| API endpoint | `DSPACE_API_ENDPOINT` | `https://www.zora.uzh.ch/server/api` | Defined in `zora_client.py`, not `config.py`. |
| API token (file) | `ZORA_UZH_API_KEY_FILE` | — | Path to a file holding the token. Wins over the inline variable below; how the token arrives in the cluster. |
| API token (inline) | `ZORA_UZH_API_KEY` | — | The token itself, for local runs. Both are resolved by `config.resolve_api_token` and assigned to the DSpace client. **Never commit the token.** |

## Swappable seams

This package does not follow the `base.py` + `build_*` factory idiom that
`parsing/`, `indexing/`, `retrieval/`, and `synthesis/` use — it is a concrete
harvester for one concrete API, and the swap point is at the *table* boundary
instead: anything that can populate `publication` can replace it
without the indexer noticing. The DSpace field names are all isolated in
`config.py` for the same reason.

## Operations

- **Docker** — `projects/zora/Dockerfile` builds a `python:3.12-slim` image with
  entrypoint `python -m themis_zora.harvest`. `data/` is expected to be
  bind-mounted; the container is run with `--user "$(id -u):$(id -g)"` so host
  file ownership stays sane.
- **Scheduling** — harvesting is a cluster concern, not a CI concern. The image is
  invoked as a one-shot job with `--mode incremental` / `--mode full`; see
  [`docs/deployment.md`](../../../docs/deployment.md). Harvest output **never**
  goes back into git.
- **`scripts/zora_inspect_fields.py`** — one-off live-API diagnostic. Prints every
  metadata field present on real items, checks the field names assumed in
  `config.py`, and shows per-author `authority` keys. Run with
  `export ZORA_UZH_API_KEY=... && python projects/zora/scripts/zora_inspect_fields.py 5`.
  Use it before changing any `FIELD_*` constant.
- **`scripts/zora_authority_audit.py`** — measures the candidate `uzh_authors`
  eligibility rules (first Known gap below) against the harvested corpus, so the
  rule is chosen from data rather than from estimates. Postgres only: no API, no
  token, and it writes nothing but a session-scoped TEMP table. Needs the current
  schema *and* a completed `--mode full` run, since it reads
  `author_authority_map`, `owning_collection_uuid` and the `person` mirror.
  Run with `python projects/zora/scripts/zora_authority_audit.py`.

## Field mapping

The notable, non-obvious mappings (`normalize.py`):

| Output field | Source | Note |
|---|---|---|
| `id` | `dso.handle` | Items without a handle are skipped. |
| `authors` | `uzh.contributor.author` | A **UZH custom field**, not `dc.contributor.author`. |
| `uzh_authors` | the same entries, filtered to those with a non-empty `authority` | Any authority — which admits ORCID-only co-authors of unknown affiliation; see Known gaps. The typed map below is what separates the kinds. |
| `author_authority_map` | `{name: {"type": "cris"\|"orcid", "id": ...}}` | The type comes from DSpace's `"will be referenced::ORCID::"` marker at fetch time: `cris` = a Person item UUID (resolves in `person`), `orcid` = a canonical ORCID with no local Person record. Authors with no authority at all map to `None`. Never classified by id shape — malformed values are common enough that shape would misfile them. orcid ids go through `_normalize_orcid`; cris ids never do (see below). |
| `department` | `_embedded.owningCollection.name`, minus the `"Publications of "` prefix | Not a metadata field — it comes from the collection the item lives in, falling back to the first mapped collection. |
| `owning_collection_uuid` | `_embedded.owningCollection.uuid` (same fallback) | Always the *same* collection the department name came from. Joins to `org_unit.collection_uuid`. |
| `keywords` | `dc.subject.ddc` + `uzh.scopus.subjects` + `dc.subject`, order-preserving dedupe | |
| `publication_type` | `dc.type` | Renamed from the internal key `type` in `to_output`. |
| `language` | `dc.language.iso` | e.g. `eng`, `deu`. |
| `accessioned` | `dc.date.accessioned` | The incremental watermark, and part of `ZoraPublication` — so the validated model is exactly what gets written, with no post-validation splice. |

### ORCID normalization

Every ORCID the harvester emits — `author_authority_map` orcid ids, `person.orcid`,
`author_orcid` — goes through one function, `normalize._normalize_orcid`. ZORA emits
four corruptions of the field, all rare and all present in the 2026-08-25 corpus (20
entries of 157,800):

| raw | cause | result |
|---|---|---|
| `https://orcid.org/0009-0005-4380-7204` | full URL | prefix stripped |
| `0000-0001-5644-045x` | lowercase check digit | uppercased |
| `0000-0002-3148-0954.` | trailing punctuation | truncated to the ORCID |
| `0000-0002-8070-773` | check digit stripped | `…-773X`, **if** the checksum says so |

The last one is a repair rather than a cleanup, and it is guarded. An ORCID's 16th
character is a check digit over the first 15 (ISO 7064 MOD 11-2), so it is
computable — but a 3-character final group has two possible causes, a stripped `X`
or a dropped leading zero, and for `0000-0002-8070-773` **both** readings are
checksum-valid (`…-773X` and `…-0773`). The computed digit is therefore appended
only when it is `X`, the one case the stripped-X explanation covers; when it
computes to a digit the value is left visibly broken instead of being completed into
a wrong ORCID attributed to a named researcher. All three real cases (`773`, `166`,
`993`) compute to `X` and match manual ORCID lookups.

**cris ids never go through it.** They are lowercase-hex UUIDs joining to
`person.uuid`, and the pipeline uppercases; running it there would break every join.

Classification uses two signals, never an id's shape. The
`will be referenced::ORCID::` marker is the primary one. An explicit `orcid.org`
URL is the second, and it is not a shape inference — the value declares what it is,
and a CRIS UUID can never take that form. That branch is defensive: every URL seen
so far arrived with the marker as well (0 rows in the 2026-08-25 corpus depend on
it), but the failure it prevents is silent — an unmarked URL typed `cris` is a
phantom UZH researcher that resolves to nobody in `person` while still counting
toward eligibility. Shape itself stays untrusted because upstream ORCIDs are often
malformed, so a strict pattern test would misfile exactly the broken ones.

Rows harvested before this landed were repaired by
`scripts/backfill_orcid_authorities.py`, which calls the same function rather than
re-implementing the rule in SQL.

## Status

**Implemented and running in production.** Nothing is written back into the repository: the
pre-Postgres `data/publications.jsonl` and `data/state.json` are untracked, and
the only file a run still leaves behind is its raw-response dump in `data/raw/`.

Test coverage: `tests/zora/test_normalize.py` and `tests/zora/test_mapping.py`
are thorough on the pure functions; `store.py` is covered from
`tests/test_zora_store.py` (Postgres-gated); `tests/zora/test_harvest_run.py`
covers the orchestration — step order, each opt-out, an aborted mirror stopping
the run, and dump routing across every combination — with ZORA and the store faked;
`tests/zora/test_raw_dump.py` round-trips `write_raw_dump` through `dump_kind` for
each kind; `tests/zora/test_entities.py` covers the two mirror steps, fetch and
replay alike, and
`tests/zora/test_org_tree.py` the community walk, including pagination and the
fail-on-a-bad-page rule. `zora_client.get_client` itself (auth, retries,
timeouts) is still only exercised through `tests/zora/test_config_auth.py`'s token
resolution.

`state.py` used to be on that list and is no longer, which is worth stating
precisely: it was deleted, not tested. It forwarded to `store.py` without adding
behaviour, so the untested surface went away without anything new being verified.

## Known gaps

- **`uzh_authors` now means "registered UZH researcher"** — resolved 2026-08-25.
  `_get_uzh_authors` used to admit any non-empty `authority`, conflating the two
  kinds DSpace-CRIS stores there: a bare UUID is a CRIS Person record (an actual UZH
  researcher), while `will be referenced::ORCID::…` means DSpace did not link *this
  item* to a local Person. An ORCID is a global identifier; every researcher on earth
  can have one. The defect was visible in output — a `match` on medical imaging
  returned Oxford, Birmingham and Belfast co-authors of a single UZH paper as
  candidate supervisors.

  Measured over the whole 214,756-record corpus with
  `scripts/zora_authority_audit.py`:

  | rule | publications | eligible names |
  |---|---:|---:|
  | any authority (the bug) | 91,729 | 58,218 |
  | **cris-typed only (adopted)** | **53,544** | **2,943** |
  | cris, or ORCID resolving in `person` | 53,544 | 2,943 |
  | cris/ORCID, or sole-author in an org collection | 108,258 | 22,964 |
  | the above minus dissertations | 94,721 | 11,085 |

  Two candidates died on the numbers. **Resolving ORCIDs against the mirror adds
  nothing**: 0 of 51,226 distinct ORCID authorities match any of the 1,990 populated
  `person.orcid` values — which is exactly what the marker asserts, so the two
  populations are disjoint by construction rather than by accident. (A digits-only
  comparison finds 2, a formatting artefact fixed by `_normalize_orcid`, which
  canonicalises at harvest; the 20 malformed values were backfilled the same day.)
  And the **"sole-author in an org-unit collection" clause does not discriminate**:
  all 214,756 publications join an org unit and none lacks an
  `owning_collection_uuid`, so the clause selects everything and the rule collapses
  to "sole-authored" — which admits *more* records than the bug it was meant to
  replace, including an entire `UZH Dissertations` collection (1,809 publications,
  1,809 names, all dissertations) and non-persons such as `RAPID Consensus ISLS 2023
  Zurich Collabo`.

  Adopting the strict rule was safe only because excluding is no longer the cost it
  was. Since `8590d7c` the whole corpus is indexed, `RETRIEVAL_REQUIRE_UZH_AUTHOR`
  defaults off, and `vector.py::_persons` falls back to `authors` — so a narrower
  `uzh_authors` **demotes rather than excludes**. The ~38,190 records that lost their
  ORCID-only entries stay searchable and stay creditable, ranked below CRIS-backed
  candidates by `uzh_first`.

  Applied in two places, because they fix different populations:
  `normalize._get_uzh_authors` decides eligibility for records a harvest is writing,
  and `store.reconcile_uzh_authors()` recomputes the column for records already
  stored — `authors` plus `author_authority_map` are a complete input for the rule,
  so a rule change reaches 215k rows in seconds instead of costing a re-harvest.
  `harvest.run` calls it at the end of every run, `--no-publications` included.

  The rule is a shade wider than "cris-typed", and deliberately so.
  `reconcile_uzh_authors` also admits an **ORCID-typed authority whose ORCID matches
  a harvested `person` row** — the marker says DSpace did not link *that item* to a
  Person, which is not the same as no Person existing. `normalize` cannot make that
  call (one item at a time, no database); the reconciliation can, and running last
  in every harvest is what keeps the column at the wider rule. Measured 2026-08-25:
  **2 publications, 0 additional names** — both researchers already qualify through
  CRIS-typed entries elsewhere, so it adds evidence to existing candidates rather
  than new ones. A floor rather than a ceiling: it grows on its own as ZORA links
  more ORCIDs, with no code change and no re-harvest.

  Corpus after the change: **53,545 of 214,756 publications (24.9%), 2,942 distinct
  names**, against 91,734 and 58,218 under the old rule.

  Residual, all small and all measured:
  - **1,807 of 1,969 distinct cris-typed ids (91.8%) resolve in `person`.** The other
    ~162 name something the Person entity query does not expose, and still count as
    eligible.
  - **The unmarked bare ORCID is fixed** (`0000-0002-7695-501X` on
    `20.500.14742/59205` — Amsler, Claude). Upstream omitted the marker, so the old
    unconditional `cris` fall-through made it a phantom researcher joining to nothing
    in `person`. `_typed_authority` now lets shape decide *unmarked* values only: it
    tests a `_normalize_orcid` throwaway and stores the original, so a lowercase-hex
    Person UUID can never be uppercased into a broken join. The marker still wins
    wherever it exists, so a malformed marked payload is never demoted to `cris`.
    Stored rows were repaired by `scripts/backfill_orcid_authorities.py`, which is
    also why **0 malformed ORCID values** now remain (was 20).
  - **2,942 eligible names resolve to only 1,969 distinct ids**, so ~49% of candidate
    names are spelling variants of someone already counted. `_group_by_person` keys
    on the display string, so `person` remains a table nothing reads. That is the
    person-keying work, not this gap.
- **A dump can never repopulate a field the normalizer did not extract when it was
  written.** The 2026-08-24 schema change (`person`, `org_unit`,
  `owning_collection_uuid`, the typed `author_authority_map`) was applied on
  2026-08-25 via `init-db --reset` plus a fresh `--mode full` API harvest — and the
  API was the only option, because older `data/raw/` dumps hold post-normalization
  records that predate those fields. `--from-dump` replays normalization output, not
  raw responses, so every normalizer change invalidates every earlier dump. Worth
  deciding before the UZH-server harvest, where re-fetching stops being cheap:
  caching raw DSpace JSON instead would cost roughly 3–5x the disk and make dumps
  survive changes like this one. The next schema change against a database with no
  replayable dump behind it is where numbered migrations stop being optional (see
  `schema.py`).
- **`zora_client.get_client` is untested.** Auth, the retry policy and the
  timeouts are exercised only by running the thing. `iter_org_tree` and the
  harvest orchestration around it are covered now (see Status).
- **Incremental mode never updates an existing record.** Merge is new-ids-only; a
  title correction or a newly added abstract upstream is invisible until the next
  full harvest. `dc.date.accessioned` does not change on edit, so this is a real
  limitation of the approach, not just an implementation shortcut. Note the entity
  mirrors do not share it: they are full snapshots on every run, so an upstream
  edit to a Person or a community shows up the next time a harvest runs.
- **The `--since` range query is untested against the live API** — the code says
  so itself in a warning log.
- **The harvested table is not what gets indexed by default.** `SOURCES_PATH`
  defaults to `data/samples`, so `themis-matcher index` indexes the 50 sample
  documents unless you pass `--source db`. Easy to miss.
- **`author_orcid` is normalised but never emitted** — `mapping.to_publication`
  drops it. It is an item-level single value, whereas the per-author identifiers
  in `author_authority_map` are what a person-level join needs.
- `config.py` refers to the inspect script by its old name `scripts.inspect_fields`.
- `projects/zora/Dockerfile.dockerignore` ends with a stray `pytest tests/zora/ -v`
  line — harmless as an ignore pattern, clearly accidental.
