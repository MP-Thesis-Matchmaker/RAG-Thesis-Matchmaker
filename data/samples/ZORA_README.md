# Sample Publications

`publications.jsonl` — 30 real ZORA publications for development and testing. See
[`README.md`](README.md) for the posting half and for what the fifty records between them cover.

## Provenance

- **Source**: the harvested `publication` table, not the ZORA API. A harvest fills the table; this
  file is a slice of it.
- **Selection**: 24 records across four subjects that also appear in the postings, plus 6 chosen
  for the awkward paths. Newest first — ordered by `year`, not by handle, because ZORA handles
  ascend with age and ordering by id selects the oldest corner of the corpus. All 30 are from 2026.
- **Generated**: 2026-08-26, by
  [`projects/matcher/scripts/export_samples.py`](../../projects/matcher/scripts/export_samples.py).
  Re-running it against an unchanged database reproduces this file byte for byte.
- **Schema**: `ZoraPublication` in
  [`libs/shared/src/themis_shared/contracts/sources.py`](../../libs/shared/src/themis_shared/contracts/sources.py)

Two claims this document used to make, corrected: it said "30 most recent publications by
accession date", but no record in the file carried an `accessioned` field at all, and the ordering
was by handle. It also offered itself as a "quick validation that the publication schema hasn't
changed" — which is exactly what it failed to be, silently, for a month.

## Purpose

- Local development without harvesting or indexing the full corpus
- Unit and integration testing of downstream components (indexing, retrieval)
- The offline path: `SOURCES_PATH` defaults to this directory, so `themis-matcher index` with no
  arguments reads it

That last one is why the file has to keep parsing. `JsonlSourceReader` counts invalid lines and
carries on, so a stale file does not fail — it just shrinks the corpus without saying so. Between
2026-07-19 and 2026-08-26 every record here was unparseable and the offline index held 20
documents instead of 50. `projects/matcher/tests/test_sample_data.py` now fails instead.

## Full Dataset

The full corpus lives in the `publication` table — **214,756 publications**, harvested from the
DSpace REST API. Nothing writes `data/publications.jsonl` any more and it is not tracked; that
file was the pre-Postgres artefact. To fill the table yourself, see
[`docs/zora-harvester.md`](../../docs/zora-harvester.md).
