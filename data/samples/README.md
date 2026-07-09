# Sample data

**Entirely synthetic.** All names, publications, postings, DOIs, and URLs in these files are
invented fixtures for development and testing. They do not refer to real UZH researchers or real
ZORA records and must never be presented as real data or used in any evaluation results.

- `publications.jsonl` — 20 `ZoraRecord` rows (see `thesis_matchmaker.contracts.sources`).
- `theses.jsonl` — 20 `ThesisPosting` rows.

These files stand in for the output of the ingestion component (Persons 1 & 2) until the real
harvest and scraper land. When the ZoraPipeline export is available, replace the synthetic
`publications.jsonl` with the latest real export from that pipeline, or point `SOURCES_PATH` at
the directory containing the ZoraPipeline data before running the indexer.