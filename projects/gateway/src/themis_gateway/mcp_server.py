"""MCP server exposing the matchmaker as tools for an LLM orchestrator.

Two thin tools over the app-service functions in ``service``:

- ``find_researchers``: structured results. This is the tool used in production
  by the UZH AI Buddy, which writes the final answer itself using its full
  conversation context.
- ``recommend_supervisors``: a full written recommendation, for standalone use.

Either can be switched off per deployment with ``GATEWAY_MCP_ENABLE_FIND_RESEARCHERS``
and ``GATEWAY_MCP_ENABLE_RECOMMEND_SUPERVISORS`` (both default to on). That is why
the tools are registered in ``build_server`` rather than by decorator at import
time: a disabled tool must never be registered, so that it is absent from
``tools/list`` instead of advertised and then failing when called.

Deployment: this runs as a standalone server that AI Buddy points its agent at,
so the default transport is streamable HTTP and the tools are served at
``http://<GATEWAY_MCP_HOST>:<GATEWAY_MCP_PORT>/mcp``. Nothing is merged into AI Buddy itself.

Run with ``themis-gateway mcp``, or ``themis-gateway mcp --stdio`` for local
testing with an MCP inspector. Needs the ``mcp`` extra -- which is why cli.py
imports this module inside its handler rather than at module scope: without that,
an environment lacking the extra could not even print ``--help``.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from themis_gateway import __version__, service
from themis_gateway.config import GatewaySettings


def find_researchers(query: str, top_k: int = 5) -> list[dict]:
    """Find UZH researchers whose work matches a topic or research interest.

    Returns a ranked list of people, each with a relevance score, department,
    any thesis postings they are named on, and evidence (their publications and
    postings). A posting is not proof of an open position: its status may be
    pending or unknown. The score is a cosine similarity in [-1, 1], not a percentage
    or a confidence -- do not present it as one, and do not multiply it by 100.

    Works for general expertise questions like "who works on humanoid robots at
    UZH" as well as thesis-supervisor matching. Returns structured data so the
    caller can write the final answer.
    """
    return service.find_researchers(query, top_k=top_k)


def recommend_supervisors(interests: str, top_k: int = 5) -> str:
    """Recommend thesis supervisors for a student as a short written answer.

    Given a student's research interests, returns a grounded recommendation that
    names suitable supervisors and cites their work. Use when a finished prose
    answer is wanted instead of structured data.
    """
    return service.recommend_supervisors(interests, top_k=top_k)


# Keyed by the names GatewaySettings.enabled_mcp_tools yields. A name there with
# no entry here is a KeyError at startup, which is the right time to find out.
_TOOLS = {
    "find_researchers": find_researchers,
    "recommend_supervisors": recommend_supervisors,
}


def build_server(settings: GatewaySettings) -> MCPServer:
    """Build the MCP server with the tools this deployment has enabled.

    The function's docstring becomes the tool description the calling model
    reads, and its name the tool name -- hence the plain functions above.
    """
    # MCPServer is the SDK 2.x name for what 1.x called FastMCP; the class moved
    # from mcp.server.fastmcp to mcp.server.mcpserver. Named `server` rather than
    # `mcp` on purpose -- the old name shadowed the `mcp` package inside this
    # very module.
    #
    # version is passed explicitly because 2.x defaults it to "". In 1.x the
    # field was filled with the SDK's own version, which reported "1.29.0" as
    # though that were this server's version; ours is the honest value.
    server = MCPServer("themis", version=__version__)
    for name in settings.enabled_mcp_tools:
        server.add_tool(_TOOLS[name])
    return server
