# parsing

Turns a student's free-text description of their interests into a `ParsedQuery`:
topics, keywords, degree level, department. This is the *Query Parser* box at the
start of the *Retrieval + Generation* lane of
[`docs/architecture.png`](../../../docs/architecture.png).

Read-only. No I/O except the optional LLM call.

## Role in the pipeline

```
"I want a master's thesis in NLP on retrieval-augmented generation"
        │
        ▼
  QueryExtractor.extract
        │
        ├── OpenAICompatExtractor  (when LLM_BASE_URL is set)
        │        └── on LLMError or ValidationError ──▶ falls back to ↓
        └── RuleBasedExtractor     (always available, offline)
        │
        ▼
  ParsedQuery(topics=["nlp", "retrieval-augmented generation"],
              degree_level="master", department=None, keywords=[],
              raw_query="…")
        │
        ▼  retrieval/ embeds topics + keywords, filters on degree_level + department
```

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `QueryExtractor` | `base.py` | Protocol: `extract(raw_query: str) -> ParsedQuery`. |
| `RuleBasedExtractor` | `rule_based.py` | Offline extractor. Lowercases, strips ~20 filler phrases, splits on `and` / `,` / `;` / `/`, keeps fragments longer than two characters, detects degree level from a keyword table. |
| `OpenAICompatExtractor` | `openai_compat.py` | Calls an OpenAI-compatible chat endpoint in JSON mode, validates the response, and falls back to the rule-based extractor on failure. |
| `build_extractor(settings)` | `__init__.py` | Factory: LLM extractor if `llm_base_url` is set, rule-based otherwise. |

## Data flow

**Reads:** nothing on disk. `OpenAICompatExtractor` makes one HTTP request to the
configured endpoint. **Writes:** nothing.

### The fallback chain is the point

Query parsing is the one step where an LLM failure must not become a user-facing
error. `OpenAICompatExtractor` catches both `LLMError` (endpoint down, timeout,
bad status) and `ValidationError` (the model returned JSON that does not fit
`ParsedQuery`) and delegates to `RuleBasedExtractor`. A degraded query is far
better than no answer, and the rest of the pipeline cannot tell the difference —
both paths return the same contract.

## Configuration

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `llm_base_url` | `LLM_BASE_URL` | unset | **The switch.** Unset → rule-based. Set → LLM extractor with rule-based fallback. |
| `llm_model` | `LLM_MODEL` | `llama3.1` | Model name sent to the endpoint. |
| `llm_reasoning_effort` | `LLM_REASONING_EFFORT` | unset | Only for reasoning models. `none` disables hidden reasoning; sent only when set, and dropped on a 400/422 from an endpoint that does not know the field. Measured on `qwen3:8b`: ~31 s per synthesis call with reasoning on, ~6 s off — enough to cross the client's 30 s timeout and degrade to the fallback. |
| `llm_api_key` | `LLM_API_KEY` | unset | Bearer token, when the endpoint needs one. |

Any OpenAI-compatible endpoint works — LibreChat in production, a local Ollama
during development.

## Swappable seams

Follows the repository-wide idiom: `base.py` Protocol, implementations beside it,
`build_extractor(settings)` in `__init__.py`. This is the **LLM provider** seam
named in invariant 3; nothing outside `parsing/`, `synthesis/`, and `llm.py`
should know which provider is in use.

## Status

**Implemented and tested.** `tests/test_parsing.py` (5 tests) covers the
rule-based extractor and the factory's branch on `LLM_BASE_URL`.

`RuleBasedExtractor` describes itself in its own docstring as a stand-in, and that
is accurate — it is a deliberate offline baseline, not a serious NLP component.

## Known gaps

- **`RuleBasedExtractor` never sets `department` or `keywords`.** Both fields stay
  empty on the offline path, which means the department filter in `retrieval/` is
  dormant unless an LLM is configured. Since the offline path is the default, that
  filter is dormant most of the time.
- **The LLM path has no HTTP-level test.** Only the factory branch is tested; the
  request shape, the JSON-mode handling, and the fallback trigger are not
  exercised against a stub server.
- Splitting on `and` / `,` / `;` / `/` mangles topics that legitimately contain
  those characters ("search and rescue", "A/B testing").
- The filler-phrase list is hand-maintained and English-only, while UZH thesis
  topics and postings are frequently German.
