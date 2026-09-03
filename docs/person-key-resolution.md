# Person-key resolution: what joins the two sources, and what does not

Measured 2026-09-03 against the live index (214,756 publications, 695 postings,
215,451 embedded documents). Reproduce with
[`scripts/person_key_coverage.py`](../scripts/person_key_coverage.py).

> **This is not an evaluation.** The five probe queries carry no relevance
> judgements and no gold set. They sample the corpus across faculties so that a
> coverage figure is not drawn from one corner of it. Nothing here may be
> reported as retrieval accuracy, precision@k, MRR or nDCG. What *is* measured
> here is a join rate, which is a property of the data and the key, not of the
> ranking.

## The question

`VectorRetriever._group_by_person` grouped retrieved documents into
`SupervisorMatch` objects keyed on the **raw name string**. ZORA writes
`"Scaramuzza, Davide"`; a department page writes `"Davide Scaramuzza"`. One
researcher, two matches, and `publication_count` / `posting_count` never both
non-zero — so the multi-signal score the `ranking` package is meant to compute
would have been scoring a join that never happens, while looking correct in
review.

## The starting hypothesis was wrong

The intuition under test: *there should be exactly one row in the `person` table
for each supervisor named on a posting, so `person` is the natural join target.*

Neither half holds.

| Supervisor name vs `person` | Count | % |
|---|---:|---:|
| **No person with even a matching family name** | **251** | 62% |
| Unique full-name match after folding and flipping | 90 | 22% |
| Family matches, given name does *not* (different people) | 46 | 11% |
| Initial-only match | 12 | 3% |
| Ambiguous / unparseable | 4 | 1% |

Of those 251, only **6** appear anywhere among the 2,942 `uzh_authors` strings
either. The shortfall is not a spelling problem: those people are genuinely
absent from ZORA's registered-author world. `person` mirrors CRIS Person
entities (2,018 rows), and thesis supervisors are frequently PhD students,
postdocs, or externals — 103 of 654 supervisor email addresses are not UZH at
all (`vogelwarte.ch`, `eawag.ch`, `agroscope.admin.ch`, `wsl.ch`).

**And `person` is the worst of the three candidate join targets, not the best:**

| Join target | Distinct keys | Supervisors uniquely resolved |
|---|---:|---:|
| `person.display_name` | 2,003 | 81 / 403 |
| `publication.uzh_authors` | 2,812 | **94** / 403 |
| `publication.authors` (all) | 331,301 | 267 / 403 |

Only 1,706 of 2,942 `uzh_authors` strings are exactly a `display_name`, so
routing a join through `person` adds a lossy hop rather than removing one. What
`person` uniquely offers is *identity* — a CRIS UUID and an ORCID on 1,990 of
2,018 rows — not coverage. That makes it something to attach to a resolved
person later, and the wrong thing to resolve through.

The 267 from all-authors is the most dangerous number in the table. 331,301 keys
means a common name almost certainly collides with a stranger, and the 46
family-collision cases already show the shape:

```
Daniel Müller    ->  Müller, Mathias / Müller, Sabrina / Müller, Thomas
Qianyu Liu       ->  Liu, Tingting
Michael Kessler  ->  Kessler, Stefan
Gian Ege         ->  Ege, Moritz
```

Crediting a supervisor with a stranger's papers and showing it to a student as
evidence is fabricated evidence. It fails silently and it looks plausible.

## Email was investigated and rejected

Email is the highest-precision identifier available on the posting side — 654 of
762 supervisor entries carry one — and `person` has no email column, so it can
never join to ZORA. The question was whether it could act as a **veto**: same
name, different address ⇒ do not merge.

It cannot. The rule fires on 6 cases and is **wrong in 5**:

```
chao feng          cfeng@ifi.uzh.ch        |  feng@ifi.uzh.ch
giacomo spinelli   …@psychologie.uzh.ch    |  …@uzh.ch
charles driver     …@psychologie.uz.ch     |  …@psychologie.uzh.ch   (typo at source)
michelle roth      …@psychologie.uzh.ch    |  …@uzh.ch
florian altermatt  …@ieu.uzh.ch            |  …@uzh.ch
```

Five are one person holding an institute address and a central alias. The domain
veto fails the same way: two supervisors resolve to a UZH author on a non-UZH
address (`liudmila.zavolokina@unil.ch`, `stefanie.lutz@agroscope.admin.ch`) and
read as researchers who published here and moved.

The general lesson, worth carrying: **an email is a contact route, not an
identifier.** People have several. Route-shaped fields are safe to merge on and
unsafe to split on, because absence of agreement carries no information.

Net value of email after that: about 4 posting-side spelling merges, of which
title-stripping plus first-given-token-only comparison already catches all but
~2. Two corrections out of 403 did not justify adding a field to the index.

## The rule that shipped

Strict, by decision — precision over recall, because the failure mode is
fabricated evidence.

- names fold through NFKD with combining marks dropped, titles stripped
- the key is **first given token + family**, so `Alexandra M. Freund` and
  `Alexandra Freund` are one person
- the **full** first given token must agree; an initial is never enough
- a family-name match alone is never enough
- publications supply the anchors (ZORA's comma says where the name splits);
  a posting's free text is resolved *against* them, never the reverse
- where free text could split two ways (`Alessandro De Luca`), both readings are
  offered and the structured side decides — so there is no particle list
- a name matching more than one anchor is **not merged at all**

## Results

### F1 — The corpus ceiling is 103 of 403 supervisors (25.6%)

| | |
|---|---:|
| Distinct supervisor names | 403 |
| Anchor keys from `uzh_authors` | 2,411 |
| **Resolved** | **103 (25.6%)** |
| Refused as ambiguous | 0 |
| Unresolved | 300 |

### F2 — Conflation is measurably zero on this corpus

Of 2,411 anchor keys, 4 collapse authors whose given names differ:

```
pascal|meier      ['felix', 'flurin']        <- a genuine conflation
malte|claussen    ['christian', 'cristian']  <- probably one person, typo
elena|cabello     ['maria', 'maría']         <- one person, accent
jose|mateos       ['maria', 'maría']         <- one person, accent
```

**No supervisor name reaches any of the four.** So the strict rule produced no
false merge that could be detected, and one genuinely risky key exists that
nothing currently touches.

### F3 — 0 refusals, which means the ambiguity guard is untested by real data

`resolve` returns None when several candidate splits match. Across 403 names
that never happened. The guard is exercised by a unit test and by nothing else;
it is insurance, not a working part.

### F4 — The join effectively never fires at the default width

This is the finding that matters most, and the one most easily misreported.

`retrieve` fetches `top_k` postings **and** `top_k` publications, so a merge
needs the same person in both slices at once:

| `top_k` | Cross-source matches | Of returned |
|---:|---:|---:|
| **5 (default)** | **0** | 0 of 25 (0.0%) |
| 20 | 1 | 1 of 100 (1.0%) |
| 50 | 7 | 7 of 250 (2.8%) |

Who merges, at `top_k=50`: Rico Sennrich, Simon Clematide, Volker Dellwo
(computational linguistics), Gerald Schwank, Klaus Oberauer, Liudmila
Zavolokina.

**103 is a ceiling on who could ever merge; it is not a yield.** At the shipped
default the answer is zero. The change remains a precondition for the `ranking`
package and still collapses duplicate spellings within a single source, but no
coverage claim follows from the corpus figure.

### F5 — 62% cannot be fixed by any key

251 of 403 supervisors have no ZORA record at all. No normalisation reaches
them. Raising coverage past roughly a quarter requires a *different source of
identity* — a UZH directory, or the scraped `researcher_profile` records that
are already stored and unread (569 rows) — not a better string rule.

### F6 — A title-stripping bug was corrupting names, and had already reached the database

Found while building this. The scraper's title regex kept an optional period
*inside* each alternative, so `sc\.?` matched the first two letters of
`Scaramuzza` and the leading-title regex stripped them. Every branch with an
optional period did it: `med` to `Medina`, `nat` to `Nathalie`, `em` to `Emma`,
`pol` to `Polanski`, `phil` to `Philippe`.

The golden baselines were storing the damage:

```
ip Gerard          -> Philip Gerard
Michael haepman    -> Michael Schaepman
Michael W.I. hmidt -> Michael W.I. Schmidt
Meredith human     -> Meredith Schuman
Karin hwiter       -> Karin Schwiter
ippe Jetzer        -> Philippe Jetzer
ala, Gavino        -> Scala, Gavino
```

Fixed by requiring a word boundary after each title. **The stored data is not
repaired:** the live `posting` table holds 10 mangled supervisor names —
`anuele Giacomuzzo`, `halie von Rooy`, `ippe Jetzer`, `lip Ströbel`,
`lip B. Ströbel`, `utr. Brigitte Tag` among them — and only a re-scrape fixes
those rows. Until then those supervisors cannot resolve, so F1's 103 is a slight
*under*-count.

## Threats to validity

- **Five probe queries.** F4's rates rest on 25/100/250 returned matches. The
  direction (near-zero at default width) is robust; the exact percentages are
  not.
- **The stored corpus is partly corrupted** by F6, which depresses F1.
- **F2 measures conflation only where it is detectable** — two ZORA strings
  under one key with differing given names. Two distinct people who share a
  first name *and* a family name are invisible to it and to the rule, and no
  string method can separate them.
- **`person` was measured, then not used.** The comparison table stands, but the
  shipped rule resolves against `uzh_authors`, so `person`'s 81 is a
  road-not-taken figure rather than a property of the system.
- **The probes are not a gold set** and no relevance judgement backs them.

## Open questions

- **Should the retriever over-fetch for grouping?** F4 says the join is invisible
  at `top_k=5`. Fetching wider and truncating after grouping would surface it, at
  a cost in latency and in `posting_count` inflation. Not attempted here.
- **The 569 unread `researcher_profile` rows** are the obvious candidate for
  reaching part of F5's 62%. They have a table and no consumer.
- **Re-measure `MATCHER_SYNTHESIS_MIN_SCORE_PUBLICATION` / `_POSTING`**
  (0.57 / 0.48, [`score-calibration.md`](score-calibration.md)). Merging changes
  which source supplies a merged person's `score`, so `score_source` flips for
  exactly the population this change creates — currently a very small one.
- **Reversed-order names** (`SHIMIZU Kentaro` against `Kentaro Shimizu`) are not
  caught. `flip_trailing_given` exists in `themis_shared.names` and could be
  offered as a third candidate; it was left out because it doubles the guess
  space for two known cases.

## Reproduction

```bash
uv run --package themis-matcher --extra embeddings python scripts/person_key_coverage.py
uv run --package themis-matcher --extra embeddings \
    python scripts/person_key_coverage.py --top-k 5 20 50 100
```

Read-only: `SELECT` only, no writes. Needs `DATABASE_URL` on a built index and
the real `BAAI/bge-m3` model; the script refuses to run against `hash-fake` or a
model that disagrees with the index manifest.
