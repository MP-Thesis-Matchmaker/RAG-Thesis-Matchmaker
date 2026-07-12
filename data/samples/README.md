# Sample data

**Entirely synthetic.** All names, publications, postings, DOIs, and URLs in these files are
invented fixtures for development and testing. They do not refer to real UZH researchers or real
ZORA records and must never be presented as real data or used in any evaluation results.

- `publications.jsonl` — 20 `ZoraRecord` rows (see `thesis_matchmaker.contracts.sources`).
- `theses.jsonl` — 20 `ThesisPosting` rows.

These files stand in for the output of the ingestion component (Persons 1 & 2) until the real
harvest and scraper land. The indexer reads them via the `SOURCES_PATH` setting, which defaults
to this directory.
