# Example run

This page shows the recommendation pipeline end to end: a student query goes in, the retriever
returns candidate supervisors with supporting evidence, and the synthesis step turns that into a
written recommendation.

Everything below is real output, recorded on 2026-08-21. It is not illustrative filler, and where a
run exposed a defect the defect is recorded rather than tidied away — see
[What these runs revealed](#what-these-runs-revealed).

## Reproducing it

Both examples run over the checked-in samples in `data/samples` — 50 documents (30 real ZORA
publications, 20 synthetic thesis postings, kept as the offline fixture set):

```bash
docker compose up -d postgres
themis-init-db
EMBEDDING_MODEL=hash-fake themis-matcher index --source data/samples --rebuild
```

**Example 1 needs no model and no API key** — with `LLM_BASE_URL` unset, both parsing and synthesis
use their offline implementations. **Example 2 and 3 add a local model**, which
`docker-compose.yml` provides:

```bash
docker compose up -d ollama
docker compose exec ollama ollama pull qwen3:8b

export LLM_BASE_URL=http://localhost:11434/v1
export LLM_MODEL=qwen3:8b
export LLM_REASONING_EFFORT=none    # not optional for qwen3 — see the note below
```

Two caveats that shape everything on this page:

- **`EMBEDDING_MODEL=hash-fake` ranks pseudo-randomly, not semantically.** It is the deterministic
  offline embedder, so candidate *ordering* here is noise. That is deliberate: it keeps the page
  reproducible with no 4 GB model download, and it makes the pipeline's grounding behaviour easy to
  see, because a semantically clean candidate list would hide whether the answer is grounded or
  merely plausible. With real `BAAI/bge-m3` embeddings the ordering is meaningful; the *shape* of
  the output is identical.
- **`LLM_REASONING_EFFORT=none` is required for `qwen3:8b`.** Left on, hidden reasoning takes the
  synthesis call to ~31 s against `LLMClient`'s 30 s timeout, so it fails and the answer silently
  degrades to Example 1's template. Off, the same call takes ~6 s. Measured while writing this page.

## Example 1 — offline, no model, no key

**Query:** `retrieval-augmented generation and misinformation detection`

```
$ EMBEDDING_MODEL=hash-fake themis-matcher match \
    "retrieval-augmented generation and misinformation detection" --top-k 5

query: retrieval-augmented generation and misinformation detection

Based on your interest in "retrieval-augmented generation and misinformation detection", here are the top matches:

1. Prof. Anna Meierhans (Department of Computational Linguistics)
   Works on retrieval-augmented generation, misinformation detection; 0 related publications; has an open thesis position.
   - MSc thesis: Grounding LLM Answers with Retrieval over Course Materials (https://example.org/theses/sample-001)

2. Prof. Daniel Keller (Department of Informatics)
   Works on retrieval-augmented generation, misinformation detection; 0 related publications; has an open thesis position.
   - MSc thesis: Early Detection of Coordinated Misinformation Campaigns (https://example.org/theses/sample-004)

3. Prof. Isabelle Roth (Department of Geography)
   Works on retrieval-augmented generation, misinformation detection; 0 related publications; has an open thesis position.
   - MSc thesis: Glacier Change Detection from Satellite Time Series (https://example.org/theses/sample-017)

4. Mahl, Daniela (Department of Communication and Media Research)
   Works on retrieval-augmented generation, misinformation detection; 1 related publications; no open position listed.
   - “We Follow the Disinformation”: Conceptualizing and Analyzing Fact-Checking Cultures Across Countries (https://www.zora.uzh.ch/handle/20.500.14742/221780)

5. Zeng, Jing (Department of Communication and Media Research)
   Works on retrieval-augmented generation, misinformation detection; 1 related publications; no open position listed.
   - “We Follow the Disinformation”: Conceptualizing and Analyzing Fact-Checking Cultures Across Countries (https://www.zora.uzh.ch/handle/20.500.14742/221780)

matches (retrieval detail):
1. Prof. Anna Meierhans  (score 0.38)
   Department of Computational Linguistics
   topics: retrieval-augmented generation, misinformation detection  |  0 papers  |  open position
     - MSc thesis: Grounding LLM Answers with Retrieval over Course Materials
2. Prof. Daniel Keller  (score 0.25)
   Department of Informatics
   topics: retrieval-augmented generation, misinformation detection  |  0 papers  |  open position
     - MSc thesis: Early Detection of Coordinated Misinformation Campaigns
3. Prof. Isabelle Roth  (score 0.21)
   Department of Geography
   topics: retrieval-augmented generation, misinformation detection  |  0 papers  |  open position
     - MSc thesis: Glacier Change Detection from Satellite Time Series
4. Mahl, Daniela  (score 0.07)
   Department of Communication and Media Research
   topics: retrieval-augmented generation, misinformation detection  |  1 papers  |  no open position
     - “We Follow the Disinformation”: Conceptualizing and Analyzing Fact-Checking Cultures Across Countries
5. Zeng, Jing  (score 0.07)
   Department of Communication and Media Research
   topics: retrieval-augmented generation, misinformation detection  |  1 papers  |  no open position
     - “We Follow the Disinformation”: Conceptualizing and Analyzing Fact-Checking Cultures Across Countries
```

**Ranks 4 and 5 are the interesting part.** `Mahl, Daniela` and `Zeng, Jing` are two entries citing
*the same* publication. That is the multi-UZH-author fan-out in
`VectorRetriever._group_by_person`: a publication with several UZH-affiliated authors credits each of
them separately, because any of them could supervise. Co-authors with no CRIS authority — external
collaborators — are filtered out before this point and never appear as candidates. This is the
behaviour the Chroma-to-pgvector migration had to preserve, and it is preserved over real ZORA rows.

Note also that the top three are all thesis postings with `0 related publications`, and the real ZORA
records only appear from rank 4. That is the `hash-fake` ordering, not a property of the pipeline.

## Example 2 — same index, same query, with the LLM

Only the synthesis step changes. The retrieval detail block is byte-identical to Example 1, so it is
omitted here.

```
$ themis-matcher match \
    "retrieval-augmented generation and misinformation detection" --top-k 5

query: retrieval-augmented generation and misinformation detection

Given your interests in retrieval-augmented generation and misinformation detection, **Prof. Anna
Meierhans** and **Prof. Daniel Keller** are the most relevant supervisors. Both have explicitly
listed these topics in their research areas, and their MSc theses directly relate to your interests:
Prof. Meierhans worked on grounding LLM answers with retrieval over course materials, while Prof.
Keller focused on early detection of coordinated misinformation campaigns. Both are currently open to
new students, making them strong candidates.

**Prof. Isabelle Roth** also has these topics listed, but her MSc thesis on glacier change detection
is unrelated, so the fit is only partial.

**Mahl, Daniela**, and **Zeng, Jing** have relevant topics, but they are not currently accepting new
students, and their work focuses more on fact-checking cultures rather than the technical aspects of
retrieval-augmented generation or misinformation detection.
```

The model named only supervisors from the candidate list, cited their work by real title, and — the
part the prompt explicitly asks for — **called out Isabelle Roth as only a partial fit** rather than
inventing a justification for a candidate that `hash-fake` had surfaced by chance. That is the
grounding discipline working under adverse ranking.

It also overstates two things. See [below](#what-these-runs-revealed).

## Example 3 — when nothing fits

Same setup, a query the samples cannot serve.

```
$ themis-matcher match \
    "I want a master's thesis on multilingual embeddings and machine translation" --top-k 3

query: I want a master's thesis on multilingual embeddings and machine translation

There is no strong match for your interest in multilingual embeddings and machine translation, as
none of the candidates have a clear track record or open position in these areas. However, Prof. Anna
Meierhans from the Department of Computational Linguistics is the closest fit, as her listed work
focuses on multilingual embeddings, specifically in the context of Swiss German, which aligns with
your thesis topic. She is also currently open to new students, making her the most viable option
despite her limited publication record.

matches (retrieval detail):
1. Prof. Anna Meierhans  (score 0.17)
   Department of Computational Linguistics
   topics: multilingual embeddings, machine translation  |  0 papers  |  open position
     - MSc thesis: Evaluating Multilingual Embedding Models on Swiss German
2. Dergaa, Ismail  (score 0.05)
   Institute of General Practice
   topics: multilingual embeddings, machine translation  |  1 papers  |  no open position
     - Impact of wet and dry cupping therapy on endurance, perceived wellness, and exertion in recreational male runners
3. Ghouili, Hatem  (score 0.05)
   Institute of General Practice
   topics: multilingual embeddings, machine translation  |  1 papers  |  no open position
     - Impact of wet and dry cupping therapy on endurance, perceived wellness, and exertion in recreational male runners
```

Handed two cupping-therapy papers and one genuinely relevant posting, the answer **opened by saying
there is no strong match** and presented the one plausible candidate as the closest option. The
fan-out shows again at ranks 2 and 3 — one publication, two UZH co-authors.

## What these runs revealed

Recorded as observed. None of these are hypothetical.

**1. `matched_topics` is circular, and the LLM reads it as evidence.** Every candidate in Example 2
shows the same `topics:` line, because those are the *query's* topics echoed back, not anything the
supervisor declared. The model took them at face value — "Both have explicitly listed these topics in
their research areas" — which is not true of anyone in the data. The field is fine for the retrieval
detail block but misleading inside the LLM's candidate list.

**2. `has_open_position` gets read as "accepting students".** The flag means "this person has a
thesis posting in our scraped data". The model rendered it as "currently open to new students"
(Example 2, Example 3) and "not currently accepting new students" (Example 2), which the data does
not support. In this sample run that flag only ever comes from the synthetic
sample postings. *Since fixed*: the field is now `posting_count`, an int, and the CLI, the
template synthesiser and the LLM candidate list emit a posting clause only when it is
non-zero, so absent data reaches the reader as absent rather than as a negative. The
synthesis prompt also now forbids any claim about a supervisor's availability. Note the
deeper reason the old `False` was indefensible: the posting query has no distance
threshold, so an empty posting list means "none of theirs ranked in the top-k", not
"there are none".

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
sites now log a warning, and `LLM_REASONING_EFFORT` exists so a reasoning model can be told not to
spend its budget thinking.

**Grounding held throughout.** Across all three examples the synthesis named only supervisors from
the retrieved list and cited only real titles. The overreaches in items 1 and 2 are about how it
*characterises* the fields it was given, not invented people or invented work — which is the failure
mode the prompt in `synthesis/llm.py` is built to prevent, and the one worth measuring in the
evaluation.
