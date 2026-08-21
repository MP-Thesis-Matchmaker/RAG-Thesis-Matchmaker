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

from thesis_matchmaker.config import Settings
from thesis_matchmaker.contracts import Evidence, SupervisorMatch
from thesis_matchmaker.llm import LLMClient, LLMError

_URL = "http://localhost:11434/v1"


def _reply(text: str = "ok") -> dict:
    return {"choices": [{"message": {"content": text}}]}


class _Recorder:
    """Stands in for httpx.post, recording payloads and replaying statuses."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.payloads: list[dict] = []

    def __call__(self, url, *, json, headers, timeout):  # noqa: A002 - httpx's kwarg name
        self.payloads.append(json)
        status = self.statuses.pop(0) if self.statuses else 200
        body = _reply() if status == 200 else {"error": "unknown field"}
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def recorder(monkeypatch):
    def _make(statuses: list[int]) -> _Recorder:
        rec = _Recorder(statuses)
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


def test_retry_drops_both_optional_fields_at_once(recorder):
    rec = recorder([400, 200])
    LLMClient(_URL, "m", reasoning_effort="none").chat("sys", "user", json_mode=True)
    assert "reasoning_effort" not in rec.payloads[1]
    assert "response_format" not in rec.payloads[1]


def test_a_plain_request_rejected_is_an_error_not_a_retry_loop(recorder):
    """With no optional field to blame, a 400 is the endpoint's real answer."""
    rec = recorder([400])
    with pytest.raises(LLMError):
        LLMClient(_URL, "m").chat("sys", "user")
    assert len(rec.payloads) == 1


def test_retry_happens_at_most_once(recorder):
    rec = recorder([400, 400])
    with pytest.raises(LLMError):
        LLMClient(_URL, "m", reasoning_effort="none").chat("sys", "user")
    assert len(rec.payloads) == 2


def test_settings_reach_the_synthesiser():
    from thesis_matchmaker.synthesis import build_synthesizer

    synth = build_synthesizer(
        Settings(llm_base_url=_URL, llm_model="qwen3:8b", llm_reasoning_effort="none")
    )
    assert synth._client._reasoning_effort == "none"


def test_settings_reach_the_parser():
    from thesis_matchmaker.parsing import build_extractor

    extractor = build_extractor(
        Settings(llm_base_url=_URL, llm_model="qwen3:8b", llm_reasoning_effort="none")
    )
    assert extractor._client._reasoning_effort == "none"


def test_synthesis_fallback_is_logged(caplog, recorder):
    """A degraded answer that logs nothing is indistinguishable from the
    offline path, which is how a timing-out endpoint hides."""
    from thesis_matchmaker.synthesis.llm import LLMSynthesizer

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
    from thesis_matchmaker.parsing.openai_compat import OpenAICompatExtractor

    recorder([500])
    with caplog.at_level(logging.WARNING):
        parsed = OpenAICompatExtractor(_URL, "m").extract("information retrieval")
    assert parsed.topics  # the rule-based parser answered
    assert any("falling back to the rule-based" in r.getMessage() for r in caplog.records)
