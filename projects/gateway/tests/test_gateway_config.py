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


_FIND = "GATEWAY_MCP_ENABLE_FIND_RESEARCHERS"
_RECOMMEND = "GATEWAY_MCP_ENABLE_RECOMMEND_SUPERVISORS"


def test_both_tools_are_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_FIND, raising=False)
    monkeypatch.delenv(_RECOMMEND, raising=False)
    settings = GatewaySettings(_env_file=None)
    assert settings.enabled_mcp_tools == ("find_researchers", "recommend_supervisors")


@pytest.mark.parametrize(
    ("variable", "remaining"),
    [
        (_FIND, ("recommend_supervisors",)),
        (_RECOMMEND, ("find_researchers",)),
    ],
)
def test_each_flag_disables_exactly_its_own_tool(
    monkeypatch: pytest.MonkeyPatch, variable: str, remaining: tuple[str, ...]
) -> None:
    monkeypatch.delenv(_FIND, raising=False)
    monkeypatch.delenv(_RECOMMEND, raising=False)
    monkeypatch.setenv(variable, "false")
    assert GatewaySettings(_env_file=None).enabled_mcp_tools == remaining


def test_both_flags_off_leaves_no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings accept it; refusing to serve is `themis-gateway mcp`'s job.

    A validator here would also break the bare status command, which exists to
    show exactly this kind of misconfiguration.
    """
    monkeypatch.setenv(_FIND, "false")
    monkeypatch.setenv(_RECOMMEND, "false")
    assert GatewaySettings(_env_file=None).enabled_mcp_tools == ()


def test_a_misspelled_flag_leaves_the_tool_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flags fail open, for the same `extra="ignore"` reason as above.

    Someone who meant to hide a tool and mistyped the variable still serves it.
    Asserted so the behaviour is a recorded fact rather than a surprise.
    """
    monkeypatch.delenv(_RECOMMEND, raising=False)
    monkeypatch.setenv("GATEWAY_MCP_ENABLE_RECOMMEND_SUPERVISOR", "false")
    assert "recommend_supervisors" in GatewaySettings(_env_file=None).enabled_mcp_tools
