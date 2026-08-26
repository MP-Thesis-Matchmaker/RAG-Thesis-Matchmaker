"""Tests for the app-service functions behind the MCP adapter (offline).

No MCP SDK, no matcher, no network: `httpx.MockTransport` stands in for the
matcher's HTTP API. These used to inject a `Pipeline` and monkeypatch
`service.read_manifest` / `service.build_retriever`, which pinned the in-process
wiring by name -- and that wiring is exactly what this distribution no longer
has. This file must not import `themis_matcher`; CI's `boundaries` job installs
the gateway alone, and it would not be importable there.
"""

from __future__ import annotations

import json

import httpx
import pytest

from themis_gateway import service

BASE_URL = "http://matcher.test:8100"

_MATCH_BODY = {
    "matches": [
        {
            "supervisor": "Prof. A. Müller",
            "department": "Informatics",
            "score": 0.82,
            "has_uzh_affiliation": True,
            "matched_topics": ["rag"],
            "publication_count": 3,
            "posting_count": 1,
            "evidence": [
                {
                    "source_type": "publication",
                    "source_id": "zora:1",
                    "title": "Retrieval-augmented generation",
                    "url": "https://zora.uzh.ch/1",
                    "year": 2024,
                }
            ],
        }
    ]
}


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The matcher's address is configuration, not a constant."""
    monkeypatch.setenv("MATCHER_BASE_URL", BASE_URL)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _responder(status_code: int, body: dict, seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status_code, json=body)

    return handler


def test_find_researchers_returns_the_matches_the_matcher_sent() -> None:
    results = service.find_researchers(
        "nlp thesis on rag", top_k=2, client=_client(_responder(200, _MATCH_BODY))
    )

    assert isinstance(results, list)
    assert results[0]["supervisor"] == "Prof. A. Müller"
    assert isinstance(results[0]["evidence"], list)


def test_find_researchers_asks_the_right_endpoint_with_the_right_body() -> None:
    """The request shape is half the contract; a silently wrong top_k is invisible."""
    seen: list[httpx.Request] = []

    service.find_researchers(
        "nlp thesis on rag", top_k=7, client=_client(_responder(200, _MATCH_BODY, seen))
    )

    assert len(seen) == 1
    assert str(seen[0].url) == f"{BASE_URL}/v1/match"
    assert json.loads(seen[0].content) == {"query": "nlp thesis on rag", "top_k": 7}


def test_recommend_supervisors_returns_the_prose() -> None:
    text = service.recommend_supervisors(
        "nlp thesis on rag", top_k=2, client=_client(_responder(200, {"answer": "Try Müller."}))
    )

    assert text == "Try Müller."


def test_recommend_supervisors_sends_interests_as_the_query() -> None:
    """The MCP tool's parameter is `interests`; the wire field is `query`."""
    seen: list[httpx.Request] = []

    service.recommend_supervisors(
        "medieval history", client=_client(_responder(200, {"answer": "x"}, seen))
    )

    assert str(seen[0].url) == f"{BASE_URL}/v1/recommend"
    assert json.loads(seen[0].content)["query"] == "medieval history"


def test_no_index_raises_instead_of_serving_fake_matches() -> None:
    """The MCP must never answer askUZH with invented supervisors.

    The condition is now detected by the matcher, but the contract with the
    caller is unchanged: an unbuilt index is an error, not an empty list and
    certainly not canned people.
    """
    refusal = _responder(409, {"code": "index_not_built", "message": "No index has been built."})

    for call in (
        lambda: service.find_researchers("nlp thesis on rag", client=_client(refusal)),
        lambda: service.recommend_supervisors("nlp thesis on rag", client=_client(refusal)),
    ):
        with pytest.raises(service.IndexNotBuiltError):
            call()


def test_the_error_code_is_what_is_branched_on_not_the_message() -> None:
    """Rewording the matcher's message must not turn a known refusal into an unknown one."""
    refusal = _responder(409, {"code": "index_not_built", "message": "totally different words"})

    with pytest.raises(service.IndexNotBuiltError):
        service.find_researchers("nlp thesis on rag", client=_client(refusal))


def test_an_unrecognised_refusal_is_not_mistaken_for_a_missing_index() -> None:
    refusal = _responder(409, {"code": "index_run_in_progress", "message": "busy", "run_id": 4})

    with pytest.raises(service.MatcherUnavailableError):
        service.find_researchers("nlp thesis on rag", client=_client(refusal))


def test_an_unreachable_matcher_is_reported_as_such() -> None:
    """A connection failure must not surface to askUZH as a bare httpx error."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(service.MatcherUnavailableError, match="could not reach the matcher"):
        service.find_researchers("nlp thesis on rag", client=_client(handler))


def test_an_unconfigured_matcher_url_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATCHER_BASE_URL", raising=False)

    with pytest.raises(service.MatcherUnavailableError, match="MATCHER_BASE_URL"):
        service.find_researchers("nlp thesis on rag", client=_client(_responder(200, _MATCH_BODY)))
