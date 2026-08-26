"""The post-scrape index trigger.

The posting-side twin of zora's, with the same contract: tell the matcher, and
never make a scrape that wrote rows look failed.
"""

from __future__ import annotations

import json

import pytest
import requests

from themis_scraper import index_trigger
from themis_shared.config import Settings

BASE_URL = "http://matcher.test:8100"


def _settings(url: str | None = BASE_URL) -> Settings:
    return Settings(matcher_base_url=url)


def _response(status_code: int, payload: dict | None = None, text: str = "") -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(payload).encode() if payload is not None else text.encode()
    return response


def _post(monkeypatch: pytest.MonkeyPatch, result, seen: list[str] | None = None) -> None:
    def fake_post(url: str, **kwargs):
        if seen is not None:
            seen.append(url)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(requests, "post", fake_post)


def test_accepted_trigger_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    _post(monkeypatch, _response(202, {"run_id": 4, "kind": "thesis_posting"}), seen)

    assert index_trigger.trigger_index(_settings()) is True
    assert seen == [f"{BASE_URL}/v1/index/postings"]


def test_nothing_is_posted_when_no_matcher_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    _post(monkeypatch, _response(202, {"run_id": 1}), seen)

    assert index_trigger.trigger_index(_settings(None)) is False
    assert seen == []


def test_an_unreachable_matcher_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scrape that wrote 695 postings did its job; the matcher being down is not its fault."""
    _post(monkeypatch, requests.ConnectionError("connection refused"))

    assert index_trigger.trigger_index(_settings()) is False


def test_a_busy_matcher_is_reported_as_not_triggered(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _post(monkeypatch, _response(409, {"code": "index_run_in_progress", "run_id": 3}))

    with caplog.at_level("WARNING"):
        assert index_trigger.trigger_index(_settings()) is False

    assert "next trigger" in caplog.text


def test_an_unexpected_refusal_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    _post(monkeypatch, _response(500, text="boom"))

    assert index_trigger.trigger_index(_settings()) is False
