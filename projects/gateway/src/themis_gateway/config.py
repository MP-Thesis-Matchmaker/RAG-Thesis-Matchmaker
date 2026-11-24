"""Settings the gateway owns, layered onto the shared floor.

Two things live here: the MCP listener address, and which of the two tools that
listener exposes. Everything else this package needs is the matcher's address,
which it inherits because the harvester and the scraper call the same service.

`GATEWAY_`-prefixed, so `mcp_host` is `GATEWAY_MCP_HOST`. The inherited
`matcher_base_url` keeps its unprefixed name through the `validation_alias`
pinned in `themis_shared.config`; see the note there on why a subclass prefix
would otherwise rename it.

There is no business logic in this package and there is no configuration for any
either. If a knob here ever describes what the matcher does rather than where the
gateway listens and what it offers there, it is in the wrong module.
"""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict

from themis_shared.config import Settings, warn_on_unprefixed_env

__all__ = ["GatewaySettings", "get_settings"]


class GatewaySettings(Settings):
    """Config for the MCP front door."""

    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # MCP server. This is deployed as a standalone service that the AI Buddy
    # agent points at, so the tools are served over HTTP at
    # http://<mcp_host>:<mcp_port>/mcp. Use 0.0.0.0 as the host in a container --
    # projects/gateway/Dockerfile already does.
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000

    # Which tools the MCP server registers. Both on by default. A tool switched
    # off is never registered, so it is absent from `tools/list` rather than
    # listed and failing on call.
    #
    # These fail open on a typo: `extra="ignore"` drops a misspelled variable
    # silently and the default, enabled, applies. `themis-gateway` with no
    # subcommand prints the resulting tool list, which is how to check.
    mcp_enable_find_researchers: bool = True
    mcp_enable_recommend_supervisors: bool = True

    @property
    def enabled_mcp_tools(self) -> tuple[str, ...]:
        """Names of the tools to register, in a fixed order.

        The one place the two flags are interpreted. It lives here rather than in
        `mcp_server` so that it needs no `mcp` import: CI never installs that
        extra, and this is the part worth testing there.
        """
        flags = {
            "find_researchers": self.mcp_enable_find_researchers,
            "recommend_supervisors": self.mcp_enable_recommend_supervisors,
        }
        return tuple(name for name, enabled in flags.items() if enabled)


def get_settings() -> GatewaySettings:
    """Return the gateway's settings, read fresh from the environment."""
    warn_on_unprefixed_env(GatewaySettings)
    return GatewaySettings()
