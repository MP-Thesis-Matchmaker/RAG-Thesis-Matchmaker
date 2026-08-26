"""What GatewaySettings reads, and what it leaves to the shared floor."""

from __future__ import annotations

import pytest

from themis_gateway.config import GatewaySettings


def test_the_listener_address_is_gateway_prefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("GATEWAY_MCP_PORT", "9000")
    settings = GatewaySettings(_env_file=None)
    assert (settings.mcp_host, settings.mcp_port) == ("0.0.0.0", 9000)


def test_an_unprefixed_name_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """`extra="ignore"` means a stale MCP_HOST is silent, not an error.

    Worth an assertion because the consequence is specific and hard to read from
    a log: the gateway binds 127.0.0.1 inside its container and the Service in
    front of it has nothing to talk to.
    """
    monkeypatch.delenv("GATEWAY_MCP_HOST", raising=False)
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    assert GatewaySettings(_env_file=None).mcp_host == "127.0.0.1"


def test_the_matcher_address_stays_unprefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inherited, and deliberately not renamed by the GATEWAY_ prefix.

    The harvester and the scraper set the same variable to reach the same
    service; prefixing it per caller would make one address need three spellings.
    """
    monkeypatch.setenv("MATCHER_BASE_URL", "http://matcher-api:8100")
    assert GatewaySettings(_env_file=None).matcher_base_url == "http://matcher-api:8100"
