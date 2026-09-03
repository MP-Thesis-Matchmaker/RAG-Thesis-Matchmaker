# Example run

This page shows the recommendation pipeline end to end: a student query goes in, the retriever
returns candidate supervisors with supporting evidence, and the synthesis step turns that into a
written recommendation.

Everything below is real output, recorded on 2026-08-26 against the regenerated sample corpus. It is
not illustrative filler, and where a run exposed a defect the defect is recorded rather than tidied
away — see [What these runs revealed](#what-these-runs-revealed).

> The environment variables in the commands were renamed on 2026-08-27, when configuration moved to
> the member that reads it (`EMBEDDING_MODEL` → `MATCHER_EMBEDDING_MODEL`, and so on). The output is
> unchanged and was not re-recorded: the settings are the same settings, reached by a different name.

## Reproducing it

All three examples run over the checked-in samples in `data/samples` — 50 documents, 30 real ZORA
publications and 20 real scraped thesis postings. Both halves are real as of 2026-08-26; the
postings used to be invented fixtures. See [`data/samples/README.md`](../data/samples/README.md).

```bash
docker compose up -d postgres
themis-init-db
MATCHER_EMBEDDING_MODEL=hash-fake themis-matcher index --source data/samples --rebuild
```

**Example 1 needs no model and no API key** — with `MATCHER_LLM_BASE_URL` unset, both parsing and synthesis
use their offline implementations. **Examples 2 and 3 add an LLM.** These were recorded against
OpenAI, because that is what the recording machine had configured:

```bash
export MATCHER_LLM_BASE_URL=https://api.openai.com/v1
export MATCHER_LLM_MODEL=gpt-5-mini
export MATCHER_LLM_API_KEY=...
```

Any OpenAI-compatible endpoint works, and a local one avoids the second caveat below.
`docker-compose.yml` ships an `ollama` service for that; set `MATCHER_LLM_REASONING_EFFORT=none` if you
point it at a reasoning model such as `qwen3:8b`, or hidden reasoning will push the synthesis call
past `LLMClient`'s 30 s timeout and the answer will silently degrade to Example 1's template.

Three caveats that shape everything on this page:

- **`MATCHER_EMBEDDING_MODEL=hash-fake` ranks pseudo-randomly, not semantically.** It is the deterministic
  offline embedder, so candidate *ordering* here is noise — which is why a query about RAG returns
  neurosurgery papers below. That is deliberate: it keeps the page reproducible with no 4 GB model
  download, and it makes the pipeline's grounding behaviour easy to see, because a semantically
  clean candidate list would hide whether the answer is grounded or merely plausible. With real
  `BAAI/bge-m3` embeddings the ordering is meaningful; the *shape* of the output is identical.
- **Running Examples 2 and 3 sends real data to your LLM endpoint.** The retrieved supervisor
  names, publication titles and abstracts all go into the synthesis prompt. Against a hosted API
  that is UZH personal data leaving the university. Worth knowing which endpoint is configured
  before running these; the pipeline does not warn.
- **A supervisor is still rarely evidenced by both a paper and a posting**, though no longer
  never. Retrieval used to group people by exact name, and the two sources spell them
  differently — `"Davide Scaramuzza"` on a posting against `"Scaramuzza, Davide"` on a paper —
  so **zero** of 403 supervisor names matched any of the 2,942 `uzh_authors`. Since 2026-09-03
  the key is canonicalised and 103 of 403 resolve. But `retrieve` fetches `top_k` of each source
  separately, so a merge needs one person in both slices: measured over five probes, **0 of 25
  returned matches at `top_k=5`**, rising to 7 of 250 at `top_k=50`. Most candidates below are
  therefore still either publication-backed or posting-backed. See
  [`docs/person-key-resolution.md`](person-key-resolution.md).

## Example 1 — offline, no model, no key

**Query:** `retrieval-augmented generation and misinformation detection`

```
$ MATCHER_EMBEDDING_MODEL=hash-fake themis-matcher match \
    "retrieval-augmented generation and misinformation detection" --top-k 5

query: retrieval-augmented generation and misinformation detection

Based on your interest in "retrieval-augmented generation and misinformation detection", here are the top matches:

1. Indiveri, Giacomo (Clinic for Neurosurgery)
   Works on retrieval-augmented generation, misinformation detection; 2 related publications.
   - Real-time chirp-based seizure detection in human iEEG with neuromorphic hardware (https://www.zora.uzh.ch/handle/20.500.14742/249206)
   - Detecting high-frequency oscillations in real time during epilepsy surgery with neuromorphic hardware validated to predict postoperative seizure outcome (https://www.zora.uzh.ch/handle/20.500.14742/249204)

2. Sarnthein, Johannes (Clinic for Neurosurgery)
   Works on retrieval-augmented generation, misinformation detection; 2 related publications.
   - Real-time chirp-based seizure detection in human iEEG with neuromorphic hardware (https://www.zora.uzh.ch/handle/20.500.14742/249206)
   - Detecting high-frequency oscillations in real time during epilepsy surgery with neuromorphic hardware validated to predict postoperative seizure outcome (https://www.zora.uzh.ch/handle/20.500.14742/249204)

3. Weibel, Robert (Institute of Geography)
   Works on retrieval-augmented generation, misinformation detection; 1 related publications.
   - Places Are More Than Just Stops: Integrating Move Segments in Place Location Detection from Trajectory Data (https://www.zora.uzh.ch/handle/20.500.14742/249130)

4. Langenfeld Sickendieck, Anke (Balgrist University Hospital, Swiss Spinal Cord Injury Center)
   Works on retrieval-augmented generation, misinformation detection; 1 related publications.
   - Correlation of examiner judgement and radiological digital pictures in infants with upper cervical spine dysfunction: a cross-sectional study (https://www.zora.uzh.ch/handle/20.500.14742/248844)

5. Ramantani, Georgia (Clinic for Neurosurgery)
   Works on retrieval-augmented generation, misinformation detection; 1 related publications.
   - Detecting high-frequency oscillations in real time during epilepsy surgery with neuromorphic hardware validated to predict postoperative seizure outcome (https://www.zora.uzh.ch/handle/20.500.14742/249204)

matches (retrieval detail):
1. Indiveri, Giacomo  (score 0.12)
   Clinic for Neurosurgery
   topics: retrieval-augmented generation, misinformation detection  |  2 papers
     - Real-time chirp-based seizure detection in human iEEG with neuromorphic hardware
     - Detecting high-frequency oscillations in real time during epilepsy surgery with neuromorphic hardware validated to predict postoperative seizure outcome
2. Sarnthein, Johannes  (score 0.12)
   Clinic for Neurosurgery
   topics: retrieval-augmented generation, misinformation detection  |  2 papers
     - Real-time chirp-based seizure detection in human iEEG with neuromorphic hardware
     - Detecting high-frequency oscillations in real time during epilepsy surgery with neuromorphic hardware validated to predict postoperative seizure outcome
3. Weibel, Robert  (score 0.12)
   Institute of Geography
   topics: retrieval-augmented generation, misinformation detection  |  1 papers
     - Places Are More Than Just Stops: Integrating Move Segments in Place Location Detection from Trajectory Data
4. Langenfeld Sickendieck, Anke  (score 0.09)
   Balgrist University Hospital, Swiss Spinal Cord Injury Center
   topics: retrieval-augmented generation, misinformation detection  |  1 papers
     - Correlation of examiner judgement and radiological digital pictures in infants with upper cervical spine dysfunction: a cross-sectional study
5. Ramantani, Georgia  (score 0.08)
   Clinic for Neurosurgery
   topics: retrieval-augmented generation, misinformation detection  |  1 papers
     - Detecting high-frequency oscillations in real time during epilepsy surgery with neuromorphic hardware validated to predict postoperative seizure outcome
```

**Ranks 1, 2 and 5 are the interesting part.** `Indiveri, Giacomo`, `Sarnthein, Johannes` and
`Ramantani, Georgia` are three entries citing *the same* publication. That is the multi-UZH-author
fan-out in `VectorRetriever._group_by_person`: a publication with several UZH-affiliated authors
credits each of them separately, because any of them could supervise. Co-authors with no CRIS
authority — external collaborators — are filtered out before this point and never appear as
candidates.

Note that all five candidates are publication-backed, with no posting among them, and that a query
about retrieval-augmented generation returned seizure detection and cervical spine imaging. Both are
the `hash-fake` ordering, not a property of the pipeline.

## Example 2 — same index, same query, with the LLM

Only the synthesis step changes. The retrieval detail block is identical to Example 1's — verified
with `diff`, not assumed — so it is omitted here.

```
$ themis-matcher match \
    "retrieval-augmented generation and misinformation detection" --top-k 5

query: retrieval-augmented generation and misinformation detection

There is no strong match among these candidates for a thesis specifically on retrieval‑augmented generation (RAG) and misinformation detection. The closest, as a long shot, is Robert Weibel (Institute of Geography): his paper "Places Are More Than Just Stops: Integrating Move Segments in Place Location Detection from Trajectory Data" indicates experience with integrating heterogeneous spatio‑temporal data and retrieval-style tasks, which could be relevant if your RAG work involves location or trajectory data. 

Giacomo Indiveri and Johannes Sarnthein (Clinic for Neurosurgery) both have methodological expertise in real‑time detection and neuromorphic systems — see "Real‑time chirp‑based seizure detection in human iEEG with neuromorphic hardware" and "Detecting high‑frequency oscillations in real time during epilepsy surgery with neuromorphic hardware validated to predict postoperative seizure outcome" — so they could be a partial fit if your project emphasizes streaming detection, signal processing, or hardware‑aware implementations. Georgia Ramantani lists the HFO paper as well and would be a similar partial fit. 

Anke Langenfeld Sickendieck’s study "Correlation of examiner judgement and radiological digital pictures in infants with upper cervical spine dysfunction: a cross‑sectional study" might be useful only if your misinformation work focuses on comparing human judgement vs automated outputs; otherwise it is not directly on RAG or misinformation.
```

**The answer led with the absence.** Handed five candidates none of whom work on RAG or
misinformation, it opened by saying so, then offered each as a qualified partial fit with the reason
stated — "could be relevant *if* your RAG work involves location or trajectory data". That is what
the synthesis prompt is built for: no invented people, no invented work, and no pretence that a weak
match is a strong one.

## Example 3 — a conversational query, and a lucky hit

**Query:** `I want a master's thesis on multilingual embeddings and machine translation`

```
$ themis-matcher match \
    "I want a master's thesis on multilingual embeddings and machine translation" --top-k 3

query: I want a master's thesis on multilingual embeddings and machine translation

All three candidates list multilingual embeddings and machine translation as topics, so any could potentially supervise your master's work — I’d highlight Deborah Noemie Jakobi first. Jakobi (Institute of Computational Linguistics) has a publication, "MultiplEYE Data Collection Guidelines", which suggests direct experience with multilingual data and would likely be a strong fit for empirical or data-focused MT/embedding work. Yu Zhang (Institut für Informatik / Department of Informatics) also matches the topics and brings machine‑learning experience evident in "Cryptocurrency Portfolio Strategies with Machine Learning", so they could be a good fit if you want an ML‑driven approach (though that listed work is not MT‑specific). Charles Driver (Psychologisches Institut) lists the topics and has "Machine Learning and Deep Learning Versus Classical Statistics for Psychological Modelling", which indicates strong methodological and evaluation expertise — useful if you care about rigorous statistical analysis, but less obviously MT‑applied from the listed title.

matches (retrieval detail):
1. Charles Driver  (score 0.12)
   Psychologisches Institut
   topics: multilingual embeddings, machine translation  |  0 papers  |  1 open postings
     - Machine Learning and Deep Learning Versus Classical Statistics for Psychological Modelling
2. Yu Zhang  (score 0.11)
   Institut für Informatik (IFI) / Department of Informatics
   topics: multilingual embeddings, machine translation  |  0 papers  |  1 open postings
     - Cryptocurrency Portfolio Strategies with Machine Learning
3. Jakobi, Deborah Noemie  (score 0.10)
   Institute of Computational Linguistics
   topics: multilingual embeddings, machine translation  |  1 papers
     - MultiplEYE Data Collection Guidelines
```

This section used to be titled "when nothing fits", and the reason it is not any more is worth
recording. `Jakobi, Deborah Noemie` at the **Institute of Computational Linguistics** is a genuinely
plausible supervisor for this query — but she surfaced by accident. `hash-fake` cannot rank
semantically, so rank 3 here is luck, not retrieval working. A sample corpus spanning four subjects
makes "nothing fits" hard to demonstrate offline, which is a better problem than the one it replaces.

The mix is also what the previous corpus could not show: two posting-backed candidates and one
publication-backed one in a single result.

## What these runs revealed

Recorded as observed. None of these are hypothetical.

**1. `matched_topics` is circular, and the LLM still reads it as evidence.** Every candidate shows
the same `topics:` line, because those are the *query's* topics echoed back, not anything the
supervisor declared. Example 3's answer opens: "All three candidates list multilingual embeddings
and machine translation as topics, so any could potentially supervise your master's work" — which is
not true of anyone in the data. **Confirmed still live on 2026-08-26**, against a different model and
a different corpus than when it was first recorded, so this is a property of the field rather than of
one model's reading. The field is fine for the retrieval detail block and misleading inside the LLM's
candidate list.

**2. `has_open_position` used to get read as "accepting students".** The flag meant "this person has
a thesis posting in our scraped data". The model rendered it as "currently open to new students",
which the data does not support. *Since fixed*: the field is now `posting_count`, an int, and the
CLI, the template synthesiser and the LLM candidate list emit a posting clause only when it is
non-zero, so absent data reaches the reader as absent rather than as a negative. The synthesis prompt
also now forbids any claim about a supervisor's availability. Visible in Example 3, where two
candidates carry `1 open postings` and the third carries no posting clause at all rather than a
`False`. Note the deeper reason the old `False` was indefensible: the posting query has no distance
threshold, so an empty posting list means "none of theirs ranked in the top-k", not "there are none".

**3. The offline rule-based parser mangles conversational queries.** `RuleBasedExtractor` strips
filler by substring replacement, and its `_FILLER` list covers `"master's thesis"` but not
`"i want a"`, with no whitespace collapse afterwards. The Example 3 query parses offline to this
topic list — `repr` output rather than source, which is why the quotes are single:

```text
['i want a   on multilingual embeddings', 'machine translation']
```

which then reaches the user as `Works on i want a   on multilingual embeddings, ...`. Invisible in
Examples 2 and 3 because the LLM parser handles the phrasing cleanly, and invisible in Example 1
only because that query has no conversational preamble. It is a stand-in parser by design, but this
is a plain bug in it, not a design limitation. *Since fixed*: filler is now removed with a
single case-insensitive, longest-phrase-first alternation, whitespace is collapsed, and a
generic edge-trim pass drops leading and trailing grammatical glue -- which is what stops
the completeness of the filler list from being load-bearing, since cutting any phrase out
of a sentence leaves glue at the seam. Three further bugs surfaced in the same function
while fixing this one: `/` was a topic separator and shredded `AI/ML`, the `len > 2` filter
silently swallowed every acronym topic (`AI`, `ML`, `IR`), and topics were lowercased while
the fallback branch preserved case. The Example 3 query now parses offline to
`['multilingual embeddings', 'machine translation']`.

**4. A slow LLM used to be indistinguishable from no LLM.** Writing this page, three runs produced
Example 1's output while an LLM was configured and reachable: the synthesis call exceeded the 30 s
timeout by about a second and a half, and the `LLMError` was swallowed without a log. Both fallback
sites now log a warning, and `MATCHER_LLM_REASONING_EFFORT` exists so a reasoning model can be told not to
spend its budget thinking.

**Grounding held throughout.** Across all three examples the synthesis named only supervisors from
the retrieved list and cited only real titles. The overreaches in items 1 and 2 are about how it
*characterises* the fields it was given, not invented people or invented work — which is the failure
mode the prompt in `synthesis/llm.py` is built to prevent, and the one worth measuring in the
evaluation.
