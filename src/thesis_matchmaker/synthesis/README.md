# synthesis

Turns a ranked list of `SupervisorMatch` objects into prose a student can read.
This is the *LLM Synthesis* box at the end of the *Retrieval + Generation* lane of
[`docs/architecture.png`](../../../docs/architecture.png).

Read-only. Its single job is presentation — it must never introduce a fact that is
not already in the matches it was handed.

## Role in the pipeline

```
list[SupervisorMatch] ──▶ Synthesizer.synthesize(query, matches) ──▶ str
                                │
                                ├── LLMSynthesizer   (when LLM_BASE_URL is set)
                                │     ├─ no matches           → template fallback
                                │     ├─ none ≥ min_score     → _no_strong_match, NO LLM CALL
                                │     ├─ LLMError             → template fallback
                                │     └─ otherwise            → grounded LLM answer
                                └── TemplateSynthesizer  (deterministic, offline)
```

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `Synthesizer` | `base.py` | Protocol: `synthesize(query: ParsedQuery, matches: list[SupervisorMatch]) -> str`. |
| `TemplateSynthesizer` | `template.py` | Deterministic string assembly. Grounded by construction — it can only restate the fields it was given. |
| `LLMSynthesizer` | `llm.py` | Wraps an `LLMClient`, a fallback `Synthesizer`, and a `min_score` threshold. |
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
2. **No match reaches `min_score`** → return `_no_strong_match`, a deterministic
   sentence that says plainly there is no strong match, names the closest
   candidate, and frames it as a long shot. **No LLM call is made.** The model is
   never given the chance to talk up a bad match.
3. **Otherwise** → format the strong candidates and call the LLM with an
   anti-hallucination system prompt. An `LLMError` falls back to the template.

For graded academic work, the property that matters is that every failure path
lands on deterministic, grounded output rather than on generated text.

## Configuration

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `llm_base_url` | `LLM_BASE_URL` | unset | **The switch.** Unset → `TemplateSynthesizer`. Set → `LLMSynthesizer`. |
| `llm_model` | `LLM_MODEL` | `llama3.1` | Model name sent to the endpoint. |
| `llm_api_key` | `LLM_API_KEY` | unset | Bearer token, when the endpoint needs one. |
| `synthesis_min_score` | `SYNTHESIS_MIN_SCORE` | `0.0` | Threshold below which a match counts as weak. |

`SYNTHESIS_MIN_SCORE` is compared against `SupervisorMatch.score`, which is a
cosine similarity in `[-1, 1]` — not a percentage. Pick a value from observed
scores on real queries; see [`../indexing/README.md`](../indexing/README.md).

## Swappable seams

Follows the repository-wide idiom: `base.py` Protocol, implementations beside it,
`build_synthesizer(settings)` in `__init__.py`. `TemplateSynthesizer` plays two
roles at once — the offline implementation *and* the fallback injected into
`LLMSynthesizer` — which is why it must stay dependency-free.

## Status

**Implemented and tested.** `tests/test_synthesis.py` (6 tests), including an
assertion that a below-threshold match produces an answer **without** calling the
LLM.

## Known gaps

- **`synthesis_min_score` is honoured only by `LLMSynthesizer`.** On the default
  offline path, `TemplateSynthesizer` prints weak matches exactly like strong
  ones. Since the offline path is the default, the guard is inert in the
  configuration most people run.
- **The default threshold is `0.0`**, and scores can be negative, so
  out-of-the-box the weak-match guard only catches genuinely anti-correlated
  matches. It needs a calibrated value to do real work.
- **`llm.py` has no dedicated test file.** The prompt construction, the candidate
  formatting, and the `LLMError` fallback are only exercised indirectly.
- The system prompt is a single hard-coded English string. Nothing evaluates
  whether the model actually respects it — that belongs in the evaluation harness,
  not here.
