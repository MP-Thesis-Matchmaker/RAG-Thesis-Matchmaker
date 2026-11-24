"""The CLI's handling of the tool flags, without the `mcp` extra installed."""

from __future__ import annotations

import sys

import pytest

from themis_gateway.cli import main

_FIND = "GATEWAY_MCP_ENABLE_FIND_RESEARCHERS"
_RECOMMEND = "GATEWAY_MCP_ENABLE_RECOMMEND_SUPERVISORS"


@pytest.fixture(autouse=True)
def _no_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Run from an empty directory so a developer's `.env` cannot leak in."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_FIND, raising=False)
    monkeypatch.delenv(_RECOMMEND, raising=False)


def test_mcp_refuses_to_start_with_both_tools_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_FIND, "false")
    monkeypatch.setenv(_RECOMMEND, "false")
    monkeypatch.delitem(sys.modules, "themis_gateway.mcp_server", raising=False)

    with pytest.raises(SystemExit) as raised:
        main(["mcp"])

    message = str(raised.value)
    assert _FIND in message and _RECOMMEND in message
    # Refused before the lazy import, so the message does not depend on the extra.
    assert "themis_gateway.mcp_server" not in sys.modules


def test_status_lists_the_enabled_tools(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(_RECOMMEND, "false")
    main([])
    out = capsys.readouterr().out
    assert "tools:    find_researchers\n" in out
    assert "recommend_supervisors" not in out


def test_status_still_works_with_both_tools_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(_FIND, "false")
    monkeypatch.setenv(_RECOMMEND, "false")
    main([])
    assert "tools:    none" in capsys.readouterr().out
