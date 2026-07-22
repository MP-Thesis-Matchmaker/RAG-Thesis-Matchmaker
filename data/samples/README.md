# Sample data

- `publications.jsonl` — 30 rows of **real** ZORA publications (see `data/samples/ZORA_README.md`
  for provenance). No longer synthetic as of the ZORA harvester migration — treat this as real data,
  including for anything downstream that assumes otherwise.
- `theses.jsonl` — 20 `ThesisPosting` rows, **still entirely synthetic**. Invented fixtures for
  development and testing; do not refer to real UZH postings and must never be presented as real
  data or used in any evaluation results.

These files stand in for the output of the ingestion components until the real scraper (Person 2)
lands too. The indexer reads them via the `SOURCES_PATH` setting, which defaults to this directory.