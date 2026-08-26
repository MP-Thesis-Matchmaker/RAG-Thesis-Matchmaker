# Sample data

Fifty records — 30 `ZoraPublication` and 20 `ThesisPosting` — that stand in for the output of the
ingestion components. They are what lets `pytest` and a bare `themis-matcher index` run with no
database and no network. The indexer reads them through `MATCHER_SOURCES_PATH`, which defaults to this
directory.

**Both files are real data now.** `theses.jsonl` was 20 invented fixtures until 2026-08-26
(`Prof. Anna Meierhans`, `example.org`); it is now real scraped UZH postings, like
`publications.jsonl` has been since the ZORA migration. Treat both as real, including anywhere
downstream that assumed otherwise.

Regenerate with [`projects/matcher/scripts/export_samples.py`](../../projects/matcher/scripts/export_samples.py),
which is also where the selection rules live. `projects/matcher/tests/test_sample_data.py` is the
alarm that says when to re-run it.

## Email addresses are removed, and that is the one edit

Supervisor `email` is always `null` here, and addresses written into a description are replaced
with `[email removed]` — three of the twenty. The corpus itself carries 336 distinct real
addresses; `contracts/sources.py` records the position that an address "travels no further than
the record carrying it", and a git repository is travelling further. This is the only respect in
which these records are not what the scraper stored, which is why the redaction is marked rather
than blanked. `projects/matcher/tests/test_sample_data.py` enforces it, because a leaked address
cannot be withdrawn by a later commit.

## What the fifty cover

Most records sit on four subjects that exist on both sides — machine learning, climate,
economics, medical imaging — so a query can match a person rather than returning noise. The rest
are chosen to exercise the paths a happy-path corpus hides:

| Publications (30) | | Postings (20) | |
|---|---|---|---|
| no abstract | 4 | `assigned` / `private` | 2 / 1 |
| no UZH author | 6 | no status at all | 1 |
| no keywords | 1 | names no supervisor | 6 |
| no authority at all | 4 | two degree levels | 6 |
| languages | eng 25, deu 3, ita 1, lav 1 | no description | 5 |
| departments | 22 | faculties | 3 of the corpus's 4 |

Two of those rows are load-bearing rather than decorative. Without a publication that has **no UZH
author**, `MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR` cannot be exercised offline — every record passes the
filter whichever way it is set. The **unavailable postings** are the same story for
`MATCHER_RETRIEVAL_REQUIRE_AVAILABLE_POSTING`. Neither was possible with the previous set.

The postings also fix what this file used to confess about the fixtures: they were uniformly one
supervisor and one degree level, where a quarter of real topics name nobody and half take two.
Here 6 of 20 name nobody and 6 take two.

## Known limitation: publications and postings effectively never meet

Retrieval groups by person on an exact name match, and the two sources spell people differently —
`"Davide Scaramuzza"` on a posting against `"Scaramuzza, D"` on a paper. Measured over the whole
corpus: 403 distinct supervisor names, **zero** of them matching any of the 2,942 `uzh_authors`.
Three match a plain `authors` entry, and only through the unaffiliated-author fallback — so a merge
happens exactly where the UZH signal is missing, in 3 of 403 cases. In practice, then, every result
is either publication-backed or posting-backed, and a supervisor with an open position is not
evidenced by their own papers.

No choice of sample records can hide this and none should try. The fix is name normalisation or an
identity join through `person`, which belongs with the unbuilt `ranking` package — see
[`retrieval/README.md`](../../projects/matcher/src/themis_matcher/retrieval/README.md).
