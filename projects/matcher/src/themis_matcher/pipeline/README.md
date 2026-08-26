# pipeline

Wires the read-side packages together into the use cases the system actually
offers. This is the plumbing behind the *Retrieval + Generation* lane of
[`docs/architecture.png`](../../../../../docs/architecture.png) — it owns the order of
operations, and nothing else.

This is the **application-service layer** of invariant 2: adapters (CLI, MCP, a
future REST API) call these functions, and business logic lives in the packages
below rather than here. Deliberately thin — under 50 lines.

## Role in the pipeline

```
raw query string
      │
      ▼
Pipeline.run(query, top_k)
      ├── extractor.extract(query)        → ParsedQuery              [parsing/]
      └── retriever.retrieve(pq, top_k)   → list[SupervisorMatch]    [retrieval/]
      │
      ▼
Pipeline.recommend(query, top_k)
      └── synthesizer.synthesize(pq, matches) → str                  [synthesis/]
```

`run()` is the structured use case; `recommend()` is `run()` plus synthesis. Both
are what the adapters call.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `Pipeline` | `orchestrator.py` | Holds a `Retriever`, a `QueryExtractor`, and a `Synthesizer`. All three are constructor arguments with defaults, so tests inject fakes without patching. |
| `Pipeline.run(query, top_k)` | `orchestrator.py` | parse → retrieve. Returns `list[SupervisorMatch]`. |
| `Pipeline.recommend(query, top_k)` | `orchestrator.py` | `run()` → synthesise. Returns prose. |
| `parse_query(raw)` | `orchestrator.py` | Convenience wrapper that always uses the rule-based extractor, so it never touches the network. |

## Data flow

**Reads:** nothing directly — everything goes through the injected components.
**Writes:** nothing (invariant 1).

### Construction defaults are a trap worth knowing

`Pipeline()` with no arguments builds:

- `retriever` → **`FakeRetriever`** — three hard-coded matches, query ignored
- `extractor` → `build_extractor()` — LLM or rule-based, from settings
- `synthesizer` → `build_synthesizer()` — LLM or template, from settings

So a bare `Pipeline()` returns plausible-looking fake results. Every real caller
must check that the index exists and pass a real retriever; `cli.py` and
`themis_gateway/service.py` both do this by testing for
the `index_manifest` row. That check is duplicated in two places rather
than living here.

## Configuration

None of its own. It reads settings only indirectly, through the `build_*`
factories it calls when a component is not injected.

## Swappable seams

This package defines no Protocol — it *consumes* the three that `parsing/`,
`retrieval/`, and `synthesis/` define. That is the whole design: the orchestrator
knows the order of steps and nothing about how any step is implemented, which is
what lets the embedding model, vector store, and LLM provider change without
touching this file (invariant 3).

## Status

**Implemented and tested**, but thin. `projects/matcher/tests/test_pipeline.py` (3 tests), using
the fake retriever.

## Known gaps

- **There is no rank step.** `orchestrator.py`'s module docstring describes
  "parse → retrieve → rank → synthesise", but no ranking happens here and there is
  no `ranking` package. What ranking exists is `score = max(hit.score)` inside
  `VectorRetriever._group_by_person` — see
  [`../retrieval/README.md`](../retrieval/README.md). When multi-signal ranking is
  built, this is where it slots in, between retrieve and synthesise.
- **The index-exists check is duplicated** in `cli.py` and
  `themis_gateway/service.py`. It arguably belongs here, so that no future adapter can
  forget it and silently serve `FakeRetriever` output.
- **`cli.py` does not use `recommend()`** — it calls `run()` and then the
  synthesizer separately, which is the same two calls in the same order.
  `themis_gateway/service.py` does use `recommend()`. Harmless duplication, but it means
  the CLI path and the MCP path are not literally the same code.
- No timing, tracing, or structured logging. Anything you want to measure for the
  evaluation section (latency per stage, retrieval hit counts) has to be added
  here first.
