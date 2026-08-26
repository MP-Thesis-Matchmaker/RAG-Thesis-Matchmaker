"""The post-harvest index trigger.

Its whole contract is "tell the matcher, and never make a harvest look failed",
so most of these are about what it refuses to do.
"""

from __future__ import annotations

import httpx
import pytest

from themis_shared.config import Settings
from themis_zora import index_trigger

BASE_URL = "http://matcher.test:8100"


def _settings(url: str | None = BASE_URL) -> Settings:
    return Settings(matcher_base_url=url)


def _post(monkeypatch: pytest.MonkeyPatch, response, seen: list[str] | None = None):
    def fake_post(url: str, **kwargs):
        if seen is not None:
            seen.append(url)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(index_trigger.httpx, "post", fake_post)


def test_accepted_trigger_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    _post(monkeypatch, httpx.Response(202, json={"run_id": 7, "kind": "publication"}), seen)

    assert index_trigger.trigger_index(_settings()) is True
    assert seen == [f"{BASE_URL}/v1/index/publications"]


def test_nothing_is_posted_when_no_matcher_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local harvest with no matcher must be silent, not noisy or slow."""
    seen: list[str] = []
    _post(monkeypatch, httpx.Response(202, json={"run_id": 1}), seen)

    assert index_trigger.trigger_index(_settings(None)) is False
    assert seen == []


def test_a_trailing_slash_does_not_double_up(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    _post(monkeypatch, httpx.Response(202, json={"run_id": 1}), seen)

    index_trigger.trigger_index(_settings(f"{BASE_URL}/"))

    assert seen == [f"{BASE_URL}/v1/index/publications"]


def test_an_unreachable_matcher_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one thing this must never do is fail a harvest that committed."""
    _post(monkeypatch, httpx.ConnectError("connection refused"))

    assert index_trigger.trigger_index(_settings()) is False


def test_a_busy_matcher_is_reported_as_not_triggered(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """409 is not success. The run holding the slot cannot see rows committed after it started."""
    _post(monkeypatch, httpx.Response(409, json={"code": "index_run_in_progress", "run_id": 3}))

    with caplog.at_level("WARNING"):
        assert index_trigger.trigger_index(_settings()) is False

    assert "next trigger" in caplog.text


def test_an_unexpected_refusal_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    _post(monkeypatch, httpx.Response(500, text="boom"))

    assert index_trigger.trigger_index(_settings()) is False
