# adapters

The front doors. Transport-specific wrappers that expose the application-service
functions to the outside world without containing any logic of their own. In
[`docs/architecture.png`](../../docs/architecture.png) these are the two orange
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
                                     │  httpx
                                     ▼
                    themis-matcher   POST /v1/match
                                     POST /v1/recommend
```

`service.py` deliberately does not import the MCP SDK. That separation is what
makes the use cases testable without a transport, and what will let a REST API be
added later as a second thin wrapper over the same two functions.

**Until 2026-08-26 that last hop was a Python import.** `service.py` built a real
`Pipeline`, which meant this always-on process loaded `BAAI/bge-m3` to embed the
incoming query, opened a psycopg pool, and held the LLM client — around 3.6 GiB
resident, in a namespace whose entire quota is 4 GiB. It is now an HTTP client, and
this distribution depends on `themis-shared` alone like every other member. CI's
`boundaries` job installs it by itself, so the boundary is enforced rather than
described.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `find_researchers(query, top_k=5, client=None)` | `service.py` | Returns `list[dict]` — ranked people as JSON-ready dicts, straight from `/v1/match`. The structured use case. |
| `recommend_supervisors(interests, top_k=5, client=None)` | `service.py` | Returns `str` — a grounded prose recommendation, from `/v1/recommend`. |
| `IndexNotBuiltError` | `service.py` | Raised on the matcher's `index_not_built` code. Matched on the code, never the message. |
| `MatcherUnavailableError` | `service.py` | The matcher is unreachable, unconfigured, or refused for a reason we do not recognise. |
| `find_researchers` (MCP tool) | `mcp_server.py` | The tool askUZH calls. Same signature, minus the injectable client. |
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
| `mcp_host` | `GATEWAY_MCP_HOST` | `127.0.0.1` | Bind address for the HTTP transport. |
| `mcp_port` | `GATEWAY_MCP_PORT` | `8000` | Port; the server is served at `http://<host>:<port>/mcp`. |
| `matcher_base_url` | `MATCHER_BASE_URL` | unset | Where the matcher's API is. **Unset means no matcher**: both functions then raise `MatcherUnavailableError` rather than guessing at localhost. |

That is the whole list, and the short list is the point: this process holds no
model, no database connection and no LLM client, so it has nothing else to
configure. Everything that used to be inherited from `Pipeline` is now the
matcher's — see [`projects/matcher/README.md`](../matcher/README.md).

## Running it

```
uv sync --package themis-gateway --extra mcp   # the root is virtual; --package is required
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
`projects/gateway/tests/test_mcp_service.py` (9 tests) covers `service.py` against an
`httpx.MockTransport`, so it needs neither a matcher nor a network.

**REST API: design-only.** The diagram shows it; no code exists yet. When it is
built it belongs here, as a second wrapper over the same `service.py` functions.
Note that the matcher's own HTTP API is *not* that REST API: it is an internal
seam between two of our processes, in-cluster only, and a student-facing front
door is still unwritten.

## Known gaps

- **`mcp_server.py` is untested.** Only the transport-free `service.py` has tests;
  the `MCPServer` registration, the two tool signatures, and `main()`'s transport
  switch are not exercised. CI also never installs the `mcp` extra, so nothing in
  CI would notice if this module stopped importing — and that is not theoretical:
  it *did* stop importing when the SDK reached 2.0 and removed `FastMCP`, and the
  failure was found by building an image rather than by a test.
- **No connection reuse.** Each call opens a fresh `httpx.post`. One TCP and TLS
  handshake per MCP tool call is measurable next to a fast query, and a long-lived
  `httpx.Client` on the server object would remove it. Left out deliberately: it is
  a performance change wanting a measurement, not a correctness one.
- No authentication, rate limiting, or request logging. Fine for a
  bound-to-localhost development server; all three need answering before anything
  is exposed to askUZH in production. Note the matcher behind it is unauthenticated
  too and must stay in-cluster.
- The REST API in the diagram does not exist.
