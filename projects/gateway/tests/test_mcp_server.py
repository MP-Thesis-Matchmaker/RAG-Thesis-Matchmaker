"""Which tools `build_server` registers.

Needs the `mcp` extra, which neither CI nor a default development environment
installs, so this file skips in both. The skip is here and not in a conftest:
a module-level `importorskip` in a conftest skips every test beside it.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

from themis_gateway.config import GatewaySettings  # noqa: E402
from themis_gateway.mcp_server import build_server  # noqa: E402


def _tool_names(**flags: bool) -> set[str]:
    server = build_server(GatewaySettings(_env_file=None, **flags))
    return {tool.name for tool in asyncio.run(server.list_tools())}


def test_both_tools_are_registered_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_MCP_ENABLE_FIND_RESEARCHERS", raising=False)
    monkeypatch.delenv("GATEWAY_MCP_ENABLE_RECOMMEND_SUPERVISORS", raising=False)
    assert _tool_names() == {"find_researchers", "recommend_supervisors"}


def test_a_disabled_tool_is_not_registered() -> None:
    assert _tool_names(mcp_enable_recommend_supervisors=False) == {"find_researchers"}
    assert _tool_names(mcp_enable_find_researchers=False) == {"recommend_supervisors"}


def test_a_registered_tool_keeps_its_description() -> None:
    """The docstring is what the calling model reads; registration must carry it."""
    server = build_server(GatewaySettings(_env_file=None))
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert "cosine similarity" in tools["find_researchers"].description
