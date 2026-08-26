"""MCP server exposing the matchmaker as tools for an LLM orchestrator.

Two thin tools over the app-service functions in ``service``:

- ``find_researchers``: structured results. This is the tool used in production
  by the UZH AI Buddy, which writes the final answer itself using its full
  conversation context.
- ``recommend_supervisors``: a full written recommendation, for standalone use.

Deployment: this runs as a standalone server that AI Buddy points its agent at,
so the default transport is streamable HTTP and the tools are served at
``http://<MCP_HOST>:<MCP_PORT>/mcp``. Nothing is merged into AI Buddy itself.

Run with ``themis-gateway-mcp``, or ``--stdio`` for local testing with an MCP
inspector. Needs the ``mcp`` extra.
"""

from __future__ import annotations

import argparse

from mcp.server.mcpserver import MCPServer

from themis_gateway import __version__, service
from themis_shared.config import get_settings

# MCPServer is the SDK 2.x name for what 1.x called FastMCP; the class moved from
# mcp.server.fastmcp to mcp.server.mcpserver. Named `server` rather than `mcp` on
# purpose -- the old name shadowed the `mcp` package inside this very module.
#
# version is passed explicitly because 2.x defaults it to "". In 1.x the field
# was filled with the SDK's own version, which reported "1.29.0" as though that
# were this server's version; ours is the honest value.
server = MCPServer("themis", version=__version__)


@server.tool()
def find_researchers(query: str, top_k: int = 5) -> list[dict]:
    """Find UZH researchers whose work matches a topic or research interest.

    Returns a ranked list of people, each with department, a relevance score,
    whether they have an open thesis position, and evidence (their publications
    and postings). Works for general expertise questions like "who works on
    humanoid robots at UZH" as well as thesis-supervisor matching. Returns
    structured data so the caller can write the final answer.
    """
    return service.find_researchers(query, top_k=top_k)


@server.tool()
def recommend_supervisors(interests: str, top_k: int = 5) -> str:
    """Recommend thesis supervisors for a student as a short written answer.

    Given a student's research interests, returns a grounded recommendation that
    names suitable supervisors and cites their work. Use when a finished prose
    answer is wanted instead of structured data.
    """
    return service.recommend_supervisors(interests, top_k=top_k)


def main() -> None:
    """Run the MCP server, over streamable HTTP by default."""
    parser = argparse.ArgumentParser(
        prog="themis-gateway-mcp",
        description="Serve the thesis matchmaker as MCP tools.",
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="run over stdio instead of HTTP (local testing with an MCP inspector)",
    )
    args = parser.parse_args()

    if args.stdio:
        server.run(transport="stdio")
        return

    # host and port are run() keyword arguments in 2.x. In 1.x they had to be
    # poked into mcp.settings before calling run(), which meant the transport and
    # the address it binds were configured in two different places.
    settings = get_settings()
    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )


if __name__ == "__main__":
    main()
