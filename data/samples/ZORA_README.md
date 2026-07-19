# Sample Publications

This file contains a small sample of ZORA publications for development and testing.

## Provenance

- **Source**: Full UZH-wide ZORA harvest (all faculties)
- **Content**: 30 most recent publications by accession date
- **Generated**: 2026-07-16
- **Schema**: Same as `data/publications.jsonl` — see `src/thesis_matchmaker/zora/schema/zora_publication.schema.json`

## Purpose

Use this file for:
- Local development without downloading the full dataset (~47MB)
- Unit/integration testing of downstream components (indexing, retrieval)
- Quick validation that the publication schema hasn't changed

## Full Dataset

The full `publications.jsonl` (~22K records, ~47MB) is committed at `data/publications.jsonl`.
For the complete UZH-wide dataset (~238K records, ~346MB), see the shared OneDrive upload.
