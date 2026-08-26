# themis-matcher

The matching engine: everything between a student's sentence and a ranked list of
supervisors. Five sub-packages, each with its own README, each following the same
shape — `base.py` is a `Protocol`, sibling modules are implementations, and
`__init__.py` exposes a `build_*(settings)` factory that picks one from
`themis_shared.config.Settings`.

| Sub-package | What it does |
|---|---|
| [`indexing/`](src/themis_matcher/indexing/README.md) | JSONL or Postgres → `Document` → content-hash diff → pgvector |
| [`retrieval/`](src/themis_matcher/retrieval/README.md) | Filtered vector queries, grouping per person, and the only ranking there is |
| [`parsing/`](src/themis_matcher/parsing/README.md) | Free text → `ParsedQuery`; rule-based by default, LLM optional |
| [`synthesis/`](src/themis_matcher/synthesis/README.md) | Grounded prose; weak matches short-circuit before any LLM call |
| [`pipeline/`](src/themis_matcher/pipeline/README.md) | Application-service functions the gateway calls |

Every seam ships a real offline implementation — `HashEmbedder`,
`InMemoryVectorStore`, `FakeRetriever`, `RuleBasedExtractor`,
`TemplateSynthesizer` — not mocks. That is what lets CI run the whole pipeline
end to end with no model download, no database and no network.

## Install and run

```bash
uv sync --package themis-matcher                  # offline implementations only
uv sync --package themis-matcher --extra embeddings   # real BGE-M3 (pulls torch)

uv run themis-matcher index --source db
uv run themis-matcher match "NLP thesis on retrieval-augmented generation"
uv run themis-matcher repl
```

`themis-matcher init-db` also works, delegating to the command
[`themis-shared`](../../libs/shared/README.md) owns.

## Dependencies

`httpx` is the matcher's alone — `llm.py` is the workspace's only httpx client.
`sentence-transformers` and `torch` sit behind the `embeddings` extra, and this is
the one distribution where `torch` is a direct dependency, which is what makes the
workspace root's CPU-wheel override apply. See the comments in `pyproject.toml`.

## Not here

Ranking is one line inside `retrieval/vector.py` — `score = max(hit.score)` in
`_group_by_person`, plus a two-level sort for `uzh_first`. The separate `ranking`
package the architecture calls for does not exist yet. The slot is between
retrieve and synthesise.
