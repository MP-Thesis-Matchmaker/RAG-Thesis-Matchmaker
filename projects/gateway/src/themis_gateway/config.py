"""Settings the gateway owns, layered onto the shared floor.

Only the MCP listener address lives here -- everything else this package needs is
the matcher's address, which it inherits because the harvester and the scraper
call the same service.

`GATEWAY_`-prefixed, so `mcp_host` is `GATEWAY_MCP_HOST`. The inherited
`matcher_base_url` keeps its unprefixed name through the `validation_alias`
pinned in `themis_shared.config`; see the note there on why a subclass prefix
would otherwise rename it.

There is no business logic in this package and there is no configuration for any
either. If a knob here ever describes what the matcher does rather than where the
gateway listens, it is in the wrong module.
"""

from __future__ import annotations

from pydantic_settings import SettingsConfigDict

from themis_shared.config import Settings

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


def get_settings() -> GatewaySettings:
    """Return the gateway's settings, read fresh from the environment."""
    return GatewaySettings()
