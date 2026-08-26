"""LLMClient's optional request fields, and the visibility of its fallbacks.

The reasoning-effort field exists because a reasoning model spends its time on
hidden tokens before the first word of the answer: measured against qwen3:8b on
Ollama, one synthesis call took 31 s with reasoning on and 6 s with it off. The
client's timeout is 30 s, so "on" meant the LLM path silently degraded to the
offline template -- which is also why the fallbacks now log.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from themis_shared.config import Settings
from themis_shared.contracts import Evidence, SupervisorMatch
from themis_matcher.llm import LLMClient, LLMError

_URL = "http://localhost:11434/v1"


def _reply(text: str = "ok") -> dict:
    return {"choices": [{"message": {"content": text}}]}


class _Recorder:
    """Stands in for httpx.post, recording payloads and replaying statuses."""

    def __init__(self, statuses: list[int], error_body: dict | None = None) -> None:
        self.statuses = list(statuses)
        self.payloads: list[dict] = []
        # Default is a body that names nothing, which is how an endpoint with no
        # structured errors refuses a field.
        self.error_body = error_body if error_body is not None else {"error": "unknown field"}

    def __call__(self, url, *, json, headers, timeout):  # noqa: A002 - httpx's kwarg name
        self.payloads.append(json)
        status = self.statuses.pop(0) if self.statuses else 200
        body = _reply() if status == 200 else self.error_body
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


def _openai_temperature_error() -> dict:
    """The real 400 body from gpt-5-mini, verbatim apart from the whitespace."""
    return {
        "error": {
            "message": (
                "Unsupported value: 'temperature' does not support 0 with this model. "
                "Only the default (1) value is supported."
            ),
            "type": "invalid_request_error",
            "param": "temperature",
            "code": "unsupported_value",
        }
    }


@pytest.fixture
def recorder(monkeypatch):
    def _make(statuses: list[int], error_body: dict | None = None) -> _Recorder:
        rec = _Recorder(statuses, error_body)
        monkeypatch.setattr(httpx, "post", rec)
        return rec

    return _make


def test_reasoning_effort_is_omitted_when_unset(recorder):
    rec = recorder([200])
    LLMClient(_URL, "llama3.1").chat("sys", "user")
    assert "reasoning_effort" not in rec.payloads[0]


def test_reasoning_effort_is_sent_when_set(recorder):
    rec = recorder([200])
    LLMClient(_URL, "qwen3:8b", reasoning_effort="none").chat("sys", "user")
    assert rec.payloads[0]["reasoning_effort"] == "none"


def test_rejected_reasoning_effort_is_retried_without_it(recorder):
    """An endpoint that does not know the field answers 400. One plain retry
    beats failing, because the field is an optimisation, not the request."""
    rec = recorder([400, 200])
    assert LLMClient(_URL, "m", reasoning_effort="none").chat("sys", "user") == "ok"
    assert len(rec.payloads) == 2
    assert "reasoning_effort" not in rec.payloads[1]


def test_rejected_json_mode_is_still_retried_without_it(recorder):
    rec = recorder([400, 200])
    assert LLMClient(_URL, "m").chat("sys", "user", json_mode=True) == "ok"
    assert "response_format" not in rec.payloads[1]


def test_retry_drops_all_optional_fields_at_once(recorder):
    rec = recorder([400, 200])
    LLMClient(_URL, "m", reasoning_effort="none").chat("sys", "user", json_mode=True)
    assert "reasoning_effort" not in rec.payloads[1]
    assert "response_format" not in rec.payloads[1]
    assert "temperature" not in rec.payloads[1]


def test_rejected_temperature_is_retried_without_it(recorder):
    """OpenAI's gpt-5/o-series answer 400 to any temperature but the default.

    The live incident: gpt-5-mini rejected temperature=0, and because the old
    retry only fired for json_mode/reasoning_effort, both the parser and the
    synthesiser silently degraded to their offline paths.
    """
    rec = recorder([400, 200])
    assert LLMClient(_URL, "gpt-5-mini").chat("sys", "user") == "ok"
    assert rec.payloads[0]["temperature"] == 0
    assert "temperature" not in rec.payloads[1]


def test_a_stripped_request_rejected_is_an_error_not_a_retry_loop(recorder):
    """Once every optional field is gone, a 400 is the endpoint's real answer."""
    rec = recorder([400, 400])
    with pytest.raises(LLMError):
        LLMClient(_URL, "m").chat("sys", "user")
    assert len(rec.payloads) == 2
    assert "temperature" not in rec.payloads[1]


def test_retry_happens_at_most_once(recorder):
    rec = recorder([400, 400])
    with pytest.raises(LLMError):
        LLMClient(_URL, "m", reasoning_effort="none").chat("sys", "user")
    assert len(rec.payloads) == 2


def test_settings_reach_the_synthesiser():
    from themis_matcher.synthesis import build_synthesizer

    synth = build_synthesizer(
        Settings(llm_base_url=_URL, llm_model="qwen3:8b", llm_reasoning_effort="none")
    )
    assert synth._client._reasoning_effort == "none"


def test_settings_reach_the_parser():
    from themis_matcher.parsing import build_extractor

    extractor = build_extractor(
        Settings(llm_base_url=_URL, llm_model="qwen3:8b", llm_reasoning_effort="none")
    )
    assert extractor._client._reasoning_effort == "none"


def test_synthesis_fallback_is_logged(caplog, recorder):
    """A degraded answer that logs nothing is indistinguishable from the
    offline path, which is how a timing-out endpoint hides."""
    from themis_matcher.synthesis.llm import LLMSynthesizer

    recorder([500])
    match = SupervisorMatch(
        supervisor="Prof. X",
        department="Informatics",
        score=0.9,
        matched_topics=["nlp"],
        publication_count=1,
        posting_count=0,
        evidence=[Evidence(source_type="publication", source_id="pub:1", title="A Paper")],
    )
    with caplog.at_level(logging.WARNING):
        text = LLMSynthesizer(LLMClient(_URL, "m")).synthesize("nlp", [match])
    assert "Prof. X" in text  # the template answered
    assert any("falling back to the template" in r.getMessage() for r in caplog.records)


def test_parser_fallback_is_logged(caplog, recorder):
    from themis_matcher.parsing.openai_compat import OpenAICompatExtractor

    recorder([500])
    with caplog.at_level(logging.WARNING):
        parsed = OpenAICompatExtractor(_URL, "m").extract("information retrieval")
    assert parsed.topics  # the rule-based parser answered
    assert any("falling back to the rule-based" in r.getMessage() for r in caplog.records)


def test_only_the_named_field_is_dropped(recorder):
    """gpt-5-mini refuses temperature and nothing else.

    Dropping response_format along with it would silently downgrade the query
    parser to unstructured output on every call -- a real cost paid for an
    unrelated rejection.
    """
    rec = recorder([400, 200], _openai_temperature_error())
    client = LLMClient(_URL, "gpt-5-mini")
    assert client.chat("sys", "user", json_mode=True) == "ok"
    assert "temperature" not in rec.payloads[1]
    assert rec.payloads[1]["response_format"] == {"type": "json_object"}


def test_a_rejected_field_is_omitted_from_later_calls(recorder):
    """The doomed request is paid once per client, not once per call.

    Against gpt-5-mini every REPL query used to cost two round trips: one 400,
    one 200. The client now remembers.
    """
    rec = recorder([400, 200], _openai_temperature_error())
    client = LLMClient(_URL, "gpt-5-mini")
    client.chat("sys", "first")
    assert len(rec.payloads) == 2  # the rejection plus its retry
    client.chat("sys", "second")
    assert len(rec.payloads) == 3  # one request, no rejection to pay for
    assert "temperature" not in rec.payloads[2]


def test_a_prose_only_rejection_is_still_pinned_to_the_field(recorder):
    """Endpoints without OpenAI's `param` still name the field in the message."""
    rec = recorder(
        [400, 200],
        {"error": {"message": "reasoning_effort is not supported by this deployment"}},
    )
    LLMClient(_URL, "m", reasoning_effort="none").chat("sys", "user", json_mode=True)
    assert "reasoning_effort" not in rec.payloads[1]
    assert rec.payloads[1]["response_format"] == {"type": "json_object"}
    assert rec.payloads[1]["temperature"] == 0


def test_an_unhelpful_rejection_still_drops_everything(recorder):
    """With no field named anywhere, blame all of them -- the old behaviour."""
    rec = recorder([400, 200], {"detail": "Bad Request"})
    LLMClient(_URL, "m", reasoning_effort="none").chat("sys", "user", json_mode=True)
    assert rec.payloads[1].keys() == {"model", "messages"}


def test_a_second_client_inherits_what_the_first_learned(recorder):
    """The parser and the synthesiser are separate clients on one endpoint.

    Observed in a live REPL session: two 400s on the first query, one per
    client, each logging "endpoint rejected temperature". The endpoint's
    capabilities do not depend on which object asks, so the second client must
    not have to find out for itself.
    """
    rec = recorder([400, 200], _openai_temperature_error())
    parser = LLMClient(_URL, "gpt-5-mini")
    parser.chat("sys", "parse this", json_mode=True)
    assert rec.payloads[0]["temperature"] == 0  # the one doomed request

    synthesiser = LLMClient(_URL, "gpt-5-mini")
    synthesiser.chat("sys", "write prose")
    assert len(rec.payloads) == 3  # no second rejection to pay for
    assert "temperature" not in rec.payloads[2]


def test_a_different_endpoint_learns_for_itself(recorder):
    """The cache is keyed by endpoint and model, not shared globally.

    Ollama accepting temperature must not be inferred from OpenAI refusing it.
    """
    rec = recorder([400, 200], _openai_temperature_error())
    LLMClient(_URL, "gpt-5-mini").chat("sys", "user")
    LLMClient("http://elsewhere/v1", "llama3.1").chat("sys", "user")
    assert rec.payloads[2]["temperature"] == 0
