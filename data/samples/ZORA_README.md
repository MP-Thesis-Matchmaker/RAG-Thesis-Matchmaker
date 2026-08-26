# Sample Publications

This file contains a small sample of ZORA publications for development and testing.

## Provenance

- **Source**: Full UZH-wide ZORA harvest (all faculties)
- **Content**: 30 most recent publications by accession date
- **Generated**: 2026-07-16
- **Schema**: `ZoraPublication` in
  [`libs/shared/src/themis_shared/contracts/sources.py`](../../libs/shared/src/themis_shared/contracts/sources.py)

## Purpose

Use this file for:
- Local development without harvesting or indexing the full corpus
- Unit/integration testing of downstream components (indexing, retrieval)
- Quick validation that the publication schema hasn't changed

## Full Dataset

The full corpus lives in the `publication` table — **214,756 publications**, harvested from the
DSpace REST API. Nothing writes `data/publications.jsonl` any more and it is not tracked; that
file was the pre-Postgres artefact, and the JSON-schema file this document used to cite has not
existed for some time. To fill the table yourself, see
[`docs/zora-harvester.md`](../../docs/zora-harvester.md).
