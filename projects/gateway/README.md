# adapters

The front doors. Transport-specific wrappers that expose the application-service
functions to the outside world without containing any logic of their own. In
[`docs/architecture.png`](../../../docs/architecture.png) these are the two orange
boxes on the left of the *Retrieval + Generation* lane — *REST API* and
*MCP Adapter (AI Buddy)*.

**Invariant 2 lives here.** Adapters call the core; the core never imports an
adapter. Both MCP tools are one-line pass-throughs to `service.py` — if you find
yourself writing an `if` in this package, the logic belongs in `pipeline/` or
below.

## Role in the pipeline

```
askUZH agent ──MCP over HTTP──▶ mcp_server.py
                                     │  (no logic — pass-through)
                                     ▼
                                service.py
                                     │
                                     ▼
                              Pipeline.run / .recommend   [pipeline/]
```

`service.py` deliberately does not import the MCP SDK. That separation is what
makes the use cases testable without a transport, and what will let a REST API be
added later as a second thin wrapper over the same two functions.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `find_researchers(query, top_k=5, pipeline=None)` | `service.py` | Returns `list[dict]` — ranked people as JSON-ready dicts (`SupervisorMatch.model_dump(mode="json")`). The structured use case. |
| `recommend_supervisors(interests, top_k=5, pipeline=None)` | `service.py` | Returns `str` — a grounded prose recommendation. |
| `_default_pipeline()` | `service.py` | Builds a `Pipeline` with the real retriever if the index exists, otherwise a bare `Pipeline` (fake retriever). |
| `find_researchers` (MCP tool) | `mcp_server.py` | The tool askUZH calls. Same signature, minus the injectable pipeline. |
| `recommend_supervisors` (MCP tool) | `mcp_server.py` | Prose variant, same shape. |
| `main()` | `mcp_server.py` | Server entry point; console script `themis-gateway-mcp`. |

The `pipeline` parameter on both service functions exists purely for injection in
tests — production callers omit it.

## Data flow

**Reads:** nothing directly; everything flows through `Pipeline`.
**Writes:** nothing (invariant 1).

### Why two tools rather than one

`find_researchers` returns structured data — people, departments, scores, open
positions, and the evidence behind each suggestion. That is what askUZH consumes:
its own agent writes the final answer to the student, so handing it prose would
mean two models writing over each other.

`recommend_supervisors` returns prose for callers with no LLM of their own — the
CLI, a demo, a REST client. Same retrieval, different last step.

## Configuration

| Setting | Env var | Default | Effect |
|---|---|---|---|
| `mcp_host` | `MCP_HOST` | `127.0.0.1` | Bind address for the HTTP transport. |
| `mcp_port` | `MCP_PORT` | `8000` | Port; the server is served at `http://<host>:<port>/mcp`. |

Everything else is inherited from whatever `Pipeline` builds — see
[`../indexing/README.md`](../indexing/README.md) and
[`../synthesis/README.md`](../synthesis/README.md).

## Running it

```
uv sync --extra mcp                       # add --extra dev too if you want the test tooling
uv run themis-gateway-mcp              # streamable HTTP on 127.0.0.1:8000/mcp
uv run themis-gateway-mcp --stdio      # stdio, for the MCP inspector
```

The default is streamable HTTP because askUZH points its agent at a URL. `--stdio`
exists for local testing with the MCP inspector.

## Swappable seams

This package has none of its own — it is the outermost layer. The seam that
matters is the one *below* it: both adapters depend only on `service.py`, so a
REST adapter added tomorrow shares the exact same two use cases with no duplicated
logic.

## Status

**MCP adapter implemented** (merged in PR #16) and exposed as a console script.
`tests/test_mcp_service.py` (2 tests) covers `service.py` with an injected
pipeline.

**REST API: design-only.** The diagram shows it; no code exists yet. When it is
built it belongs here, as a second wrapper over the same `service.py` functions.

## Known gaps

- **`mcp_server.py` is untested.** Only the transport-free `service.py` has tests;
  the `MCPServer` registration, the two tool signatures, and `main()`'s transport
  switch are not exercised. CI also never installs the `mcp` extra, so nothing in
  CI would notice if this module stopped importing — and that is not theoretical:
  it *did* stop importing when the SDK reached 2.0 and removed `FastMCP`, and the
  failure was found by building an image rather than by a test.
- **`_default_pipeline` duplicates the index-exists check** found in `cli.py`. If
  the check is wrong or forgotten, the adapter silently serves `FakeRetriever`
  output to askUZH — plausible-looking fake supervisors. Worth centralising in
  `pipeline/`.
- No authentication, rate limiting, or request logging. Fine for a
  bound-to-localhost development server; all three need answering before anything
  is exposed to askUZH in production.
- The REST API in the diagram does not exist.
