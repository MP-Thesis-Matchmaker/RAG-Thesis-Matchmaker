# Posting scraper — operator guide

Runbook for `python -m thesis_matchmaker.scraper.main`. What the package *is* and why it
is shaped that way lives in
[`src/thesis_matchmaker/scraper/README.md`](../src/thesis_matchmaker/scraper/README.md);
this page is about running it.

Companion to [`zora-harvester.md`](zora-harvester.md). The two producers differ in one way
that matters operationally: the harvester talks to one API that is happy to be queried,
this one talks to 103 pages belonging to people who did not ask to be scraped. Most of
what looks like friction below is that difference.

## Before the first run

```bash
uv sync --extra scraping --extra render  # render: 3 sources are JS-only (see below)
export SCRAPER_CONTACT='thesis-matchmaker@example.uzh.ch'
export DATABASE_URL='postgresql://matchmaker:matchmaker@localhost:5432/matchmaker'
thesis-matchmaker init-db                # the posting/* tables have to exist
```

`SCRAPER_CONTACT` is **required and has no default**. It is advertised in the `User-Agent`
of every request so a site owner can reach a human, and `Settings.user_agent` raises
rather than sending a blank one. Use a project address; the value ends up in other
people's server logs.

The `render` extra additionally wants `uv run python -m playwright install chromium`
(~100 MB, cached outside the repo). Three sources need it — `ibw--1` and `ivw--4` serve
their listings JS-only, `iff--3`'s profile pages render client-side — so a full run
without it permanently flags those three. `fetch.py` still imports playwright lazily and
degrades to the static fetch when it is absent, so every *other* source works either way.
The container image bakes the browser in (`projects/scraper/Dockerfile`).

## The two stages, and why they are separate

```bash
python -m thesis_matchmaker.scraper.main fetch --resume   # talks to uzh.ch
python -m thesis_matchmaker.scraper.main run --resume     # reads only the cache
```

`fetch` is the only stage that makes network requests. It is sequential, waits
`SCRAPER_POLITE_DELAY_SECONDS` between pages, and writes each response to
`data/scraper/cache/<source_id>/`. `run` extracts from that cache and writes Postgres.

Keeping them apart is what makes the extractor safe to iterate on: you can re-run `run` as
often as a spec needs without touching a single UZH server. Treat `fetch` as something you
do deliberately and `run` as something you do freely.

Both take `--resume`, which skips sources already completed in
`data/scraper/var/state.json`, and `--only <source_id> [<source_id> ...]` to work on a
subset. `--only` takes its ids as one space-separated list, not as a repeated flag:
`--only a b` runs both, whereas `--only a --only b` silently runs only `b`.

## Day to day

```bash
python -m thesis_matchmaker.scraper.main status             # per-source lifecycle table
python -m thesis_matchmaker.scraper.main check <source_id>  # one source, verbose
```

`run` exits **non-zero when any source is flagged** — that is the alarm, not a failure of
the run itself. The states, from `validate.py`:

| Status | Means | What to do |
|---|---|---|
| `OK` | extracted and stored | nothing |
| `PAGE_CHANGED` | content hash moved since the frozen snapshot | look at the diff; the spec may still be right |
| `NEEDS_REVIEW` | extracted, but the result looks implausible | usually a title; see `title_check.py` |
| `SCHEMA_INVALID` | records did not satisfy the record shape | the spec is wrong — unless the reason opens with "LLM unavailable": on specs with `pdf_enrich`/`pdf_summary` a dead LLM leaves the enriched field empty, and that is an outage, not drift (it misdiagnosed `ifi--17` once) |
| `EXTRACT_FAILED` | the spec matched nothing | the page was restructured |
| `LLM_FALLBACK` | a template matched nothing and the model filled in | **always** review; the one non-deterministic path |
| `FETCH_FAILED` | network, timeout, or a block | retry later; check whether the UA is being rejected |

A flagged source keeps its previous rows. `store.write_dataset` replaces a source's rows
only from a run that produced records for it, so a failed extraction cannot silently empty
a source — and a run covering one source cannot touch the other 102.

## Adding a source

```bash
python -m thesis_matchmaker.scraper.main onboard --next
# or: onboard <source_id> --page-type topics --hint "the table under 'Open theses'"
```

Interactive by design. The model drafts a `spec.yaml`, you approve or reject what it
extracted, and approval freezes a `snapshot.html` plus an `expected.json` under
`data/scraper/specs/<source_id>/`. That frozen triple is what the offline suite replays —
so onboarding a source is also what gives it a regression test.

Afterwards regenerate the committed baseline and commit both:

```bash
uv run python tests/scraper/regen_golden.py
uv run pytest tests/scraper
```

Do not hand-edit `golden_specs.json`. If the diff surprises you, that is the test doing its
job.

## When a page changes

`PAGE_CHANGED` means the content hash moved, not that extraction broke:

1. `check <source_id>`, and compare against `data/scraper/specs/<source_id>/snapshot.html`.
2. If the spec still extracts correctly, re-freeze the snapshot with `onboard --refetch`.
3. If it does not, fix `spec.yaml` by hand or `onboard --redraft`, then regenerate the
   golden baseline.

Resist treating the LLM fallback as the fix. It exists so one changed page does not stop a
run, and every record it produces is flagged precisely so it cannot quietly become the
normal path.

## In the cluster

`projects/scraper/Dockerfile`; `ENTRYPOINT` is the module and `CMD` is `run --resume`, so a
CronJob overriding `args` chooses the stage. Locally:

```bash
docker compose run --rm scraper fetch --resume
docker compose run --rm scraper run --resume
```

Three things to get right in a manifest:

- **`data/scraper/var/` needs a PVC, and it is the one that bites first.**
  `var/state.json` is the *only* record of which sources are onboarded, and it is
  gitignored — so a pod with an empty volume sees 0 verified sources however many specs
  are baked into the image, and there is nothing for `run` to do. `run` exits **non-zero**
  in that state rather than 0, so the CronJob fails visibly instead of reporting Success
  while `posting` stays empty; that is a guard, not a fix. Onboarding has to happen
  somewhere whose output survives the pod.
- **`data/scraper/cache/` wants a PVC.** With an `emptyDir`, every scheduled run re-fetches
  all 103 pages — which throws away the property the cache exists for and puts avoidable
  load on other people's servers. Same open question [`deployment.md`](deployment.md)
  raises about the ZORA raw cache; the answer matters more here.
- **`SCRAPER_CONTACT` and `SCRAPER_LLM_API_KEY` come from Vault**, like every other
  secret. The contact address is not secret, but it should not be baked into an image
  either.

## Data and ethics

Supervisor names, emails and profile text are **personal data** the departments chose to
publish. They are stored in `posting` and `researcher_profile`, never embedded into
vectors, and `office`/`phone` are dropped at normalisation even where a page carries them.

Two honest limits, both also in the package's Known gaps:

- **`robots.txt` is not parsed.** Politeness here is a sequential fetch, a real delay and
  an honest User-Agent. That is not the same as checking a policy file, and the gap should
  be closed before running at any wider scope than the 103 curated sources.
- **The registry is a deliberate whitelist.** Nothing crawls; nothing follows links off a
  page except the profile links a spec explicitly names. Keeping it that way is what makes
  the politeness argument hold.
