"""App-service functions behind the interface adapters.

Plain functions the MCP server (and later a REST adapter) wrap. They hold no
transport concern towards their callers and no business logic of their own: they
ask the matcher and pass the answer on. Kept free of the MCP SDK so they can be
tested without it, and the HTTP client is injectable so they can be tested
without a matcher.

**These used to import `themis_matcher` and drive a `Pipeline` in-process.** That
made this an ordinary function call, and it also made every gateway process load
`BAAI/bge-m3` (2.27 GB) to embed the incoming query, open a psycopg pool, and
hold the LLM client -- roughly 3.6 GiB of resident memory in a namespace whose
whole quota is 4 GiB. The matcher now owns all three, and `themis-gateway`
depends on `themis-shared` alone, like every other member. CI's `boundaries` job
installs this distribution by itself, so an accidental `from themis_matcher
import ...` here fails loudly rather than working on the laptop of whoever wrote
it.
"""

from __future__ import annotations

import httpx

from themis_shared.config import get_settings
from themis_shared.contracts import MatchRequest

# Long enough for a cold query -- the matcher may still be loading the embedding
# model on its first request -- and short enough that askUZH is not left hanging
# on a matcher that is never going to answer.
DEFAULT_TIMEOUT_S = 60.0


class MatcherUnavailableError(RuntimeError):
    """The matcher could not be reached, or answered in a way we cannot use."""


class IndexNotBuiltError(RuntimeError):
    """Raised when a tool is called before anything has been indexed.

    Kept as this package's own type even though the condition is now detected by
    the matcher: the MCP tools' contract with their caller did not change just
    because the transport underneath did. It is raised on the matcher's
    `index_not_built` code, never on the wording of its message.
    """


def _base_url() -> str:
    settings = get_settings()
    if not settings.matcher_base_url:
        raise MatcherUnavailableError(
            "MATCHER_BASE_URL is not set, so there is no matcher to ask. "
            "Point it at the matcher's HTTP API (default port 8100)."
        )
    return settings.matcher_base_url.rstrip("/")


def _post(path: str, body: MatchRequest, client: httpx.Client | None = None) -> dict:
    """One call to the matcher, with its refusals mapped onto our own types.

    A refusal carries a machine-readable `code`; that is what is branched on. The
    message is for humans and may be reworded at any time.
    """
    url = f"{_base_url()}{path}"
    payload = body.model_dump(mode="json")
    try:
        if client is not None:
            response = client.post(url, json=payload)
        else:
            response = httpx.post(url, json=payload, timeout=DEFAULT_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise MatcherUnavailableError(f"could not reach the matcher at {url}: {exc}") from exc

    if response.status_code >= 400:
        _raise_for(response)
    return response.json()


def _raise_for(response: httpx.Response) -> None:
    try:
        error = response.json()
        code = error.get("code", "")
        message = error.get("message") or response.text
    except ValueError:
        code, message = "", response.text
    if code == "index_not_built":
        raise IndexNotBuiltError(message)
    raise MatcherUnavailableError(
        f"the matcher refused the request ({response.status_code} {code or 'unknown'}): {message}"
    )


def find_researchers(
    query: str, top_k: int = 5, client: httpx.Client | None = None
) -> list[dict]:
    """Ranked researchers and supervisors matching a topic, as structured data."""
    body = _post("/v1/match", MatchRequest(query=query, top_k=top_k), client)
    return body["matches"]


def recommend_supervisors(
    interests: str, top_k: int = 5, client: httpx.Client | None = None
) -> str:
    """A written, grounded recommendation of supervisors for a student."""
    body = _post("/v1/recommend", MatchRequest(query=interests, top_k=top_k), client)
    return body["answer"]
