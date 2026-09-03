# synthesis

Turns a ranked list of `SupervisorMatch` objects into prose a student can read.
This is the *LLM Synthesis* box at the end of the *Retrieval + Generation* lane of
[`docs/architecture.png`](../../../../../docs/architecture.png).

Read-only. Its single job is presentation — it must never introduce a fact that is
not already in the matches it was handed.

## Role in the pipeline

```
list[SupervisorMatch] ──▶ Synthesizer.synthesize(query, matches) ──▶ str
                                │
                                ├── LLMSynthesizer   (when MATCHER_LLM_BASE_URL is set)
                                │     ├─ no matches           → template fallback
                                │     ├─ none ≥ its threshold → _no_strong_match, NO LLM CALL
                                │     ├─ LLMError             → template fallback
                                │     └─ otherwise            → grounded LLM answer
                                └── TemplateSynthesizer  (deterministic, offline)
```

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `Synthesizer` | `base.py` | Protocol: `synthesize(query: ParsedQuery, matches: list[SupervisorMatch]) -> str`. |
| `TemplateSynthesizer` | `template.py` | Deterministic string assembly. Grounded by construction — it can only restate the fields it was given. |
| `LLMSynthesizer` | `llm.py` | Wraps an `LLMClient`, a fallback `Synthesizer`, and one weak-match threshold per source type. |
| `build_synthesizer(settings)` | `__init__.py` | Factory: `LLMSynthesizer` if `llm_base_url` is set (with `TemplateSynthesizer` as its fallback), otherwise `TemplateSynthesizer`. |

## Data flow

**Reads:** nothing on disk. `LLMSynthesizer` makes at most one HTTP request.
**Writes:** nothing.

### Weak matches are handled before the LLM, not by it

This is the most important behaviour in the package. Semantic search always
returns *something* — ask about marine biology in a corpus with no marine
biologists and you still get the five least-dissimilar people back. Handing those
to an LLM and asking it to recommend supervisors reliably produces confident,
useless prose.

So `LLMSynthesizer.synthesize` filters first:

1. **No matches at all** → delegate to the template fallback.
2. **No match reaches the threshold for its own source type** → return
   `_no_strong_match`, a deterministic
   sentence that says plainly there is no strong match, names the closest
   candidate, and frames it as a long shot. **No LLM call is made.** The model is
   never given the chance to talk up a bad match.
3. **Otherwise** → format the strong candidates and call the LLM with an
   anti-hallucination system prompt. An `LLMError` falls back to the template.

For graded academic work, the property that matters is that every failure path
lands on deterministic, grounded output rather than on generated text.

## Configuration

The subset of `MatcherSettings` this sub-package reads; the whole list is in
[the package README](../../../README.md#configuration).

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `llm_base_url` | `MATCHER_LLM_BASE_URL` | unset | **The switch.** Unset → `TemplateSynthesizer`. Set → `LLMSynthesizer`. |
| `llm_model` | `MATCHER_LLM_MODEL` | `llama3.1` | Model name sent to the endpoint. |
| `llm_reasoning_effort` | `MATCHER_LLM_REASONING_EFFORT` | unset | Only for reasoning models. `none` disables hidden reasoning; sent only when set, and dropped on a 400/422 from an endpoint that does not know the field. Measured on `qwen3:8b`: ~31 s per synthesis call with reasoning on, ~6 s off — enough to cross the client's 30 s timeout and degrade to the fallback. |
| `llm_api_key` | `MATCHER_LLM_API_KEY` | unset | Bearer token, when the endpoint needs one. |
| `synthesis_min_score_publication` | `MATCHER_SYNTHESIS_MIN_SCORE_PUBLICATION` | `0.57` | Below this, a publication-scored match counts as weak. |
| `synthesis_min_score_posting` | `MATCHER_SYNTHESIS_MIN_SCORE_POSTING` | `0.48` | The same for a posting-scored match. |

Both are compared against `SupervisorMatch.score`, a cosine similarity in `[-1, 1]` —
not a percentage. Why the range is signed rather than rescaled:
[`../indexing/README.md`](../indexing/README.md#what-the-score-is-and-why-it-is-not-0-1).

**Why two.** The threshold used to be one value at `0.0`, which is inert. Measuring it
([`docs/score-calibration.md`](../../../../../docs/score-calibration.md), nine queries
over the full index) showed why a single calibrated value is not available: an arbitrary
query lands closer to *something* among 214,756 publication abstracts than among 695
short postings, purely from sampling density, so out-of-domain queries peak at 0.542
against publications and 0.431 against postings while on-topic queries bottom out at
0.605 and 0.564. One value would have to sit in the 0.022-wide overlap, and every value
there trims postings while leaving publications untouched — which, because the person
key never joins the two sources, removes exactly the supervisors with open positions.
Two values sit mid-band instead, with room either side.

`LLMSynthesizer` picks between them on `SupervisorMatch.score_source`, which the
retriever sets to the source of the person's highest-scoring hit. Reproduce or extend
the measurement with
[`scripts/score_distribution.py --control`](../../../../../scripts/score_distribution.py).

## Swappable seams

Follows the repository-wide idiom: `base.py` Protocol, implementations beside it,
`build_synthesizer(settings)` in `__init__.py`. `TemplateSynthesizer` plays two
roles at once — the offline implementation *and* the fallback injected into
`LLMSynthesizer` — which is why it must stay dependency-free.

## Status

**Implemented and tested.** `projects/matcher/tests/test_synthesis.py` (10 tests), including an
assertion that a below-threshold match produces an answer **without** calling the
LLM, and one that gives two matches the same score under different source types and
checks they get opposite verdicts — which no single-threshold implementation can pass.

## Known gaps

- **The thresholds are honoured only by `LLMSynthesizer`.** On the default offline
  path, `TemplateSynthesizer` prints weak matches exactly like strong ones. Since the
  offline path is the default, the guard is inert in the configuration most people
  run — which is also why turning it on by default changed nothing for a
  default-configured run.
- **The thresholds rest on nine queries.** Five on-topic probes and four out-of-domain
  controls, which is enough to establish that the two sources need separate values and
  not enough to pin either precisely. The bands they sit in can only narrow as queries
  are added — the floor is a max over controls, the ceiling a min over probes — so treat
  0.57 and 0.48 as a first calibration, not a settled constant. Threats to validity are
  listed in [`docs/score-calibration.md`](../../../../../docs/score-calibration.md).
- **They will need re-measuring once the person key is fixed.** A person's `score_source`
  is whichever source their best hit came from, and today no one is credited by both, so
  the two populations are disjoint. Merging them changes which threshold applies to whom.
- **`llm.py` has no dedicated test file.** The prompt construction, the candidate
  formatting, and the `LLMError` fallback are only exercised indirectly.
- The system prompt is a single hard-coded English string. Nothing evaluates
  whether the model actually respects it — that belongs in the evaluation harness,
  not here.
