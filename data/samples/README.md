# Sample data

- `publications.jsonl` — 30 rows of **real** ZORA publications (see `data/samples/ZORA_README.md`
  for provenance). No longer synthetic as of the ZORA harvester migration — treat this as real data,
  including for anything downstream that assumes otherwise.
- `theses.jsonl` — 20 `ThesisPosting` rows, **still entirely synthetic**. Invented fixtures for
  development and testing; do not refer to real UZH postings and must never be presented as real
  data or used in any evaluation results. Real postings live in the `posting` table, which
  `scraper/` writes. Note these fixtures are *unrepresentative* in two ways beyond being
  invented: every one names exactly one supervisor and exactly one degree level, where a quarter
  of real topics name nobody and half are open to two levels.

These files stand in for the output of the ingestion components. Both producers now exist —
`zora/` writes `publication`, `scraper/` writes `posting` — so the purpose of these files is
narrower than it was: they are what lets `pytest` and a bare `thesis-matchmaker index` run with
no database and no network. The indexer reads them via the `SOURCES_PATH` setting, which defaults
to this directory.