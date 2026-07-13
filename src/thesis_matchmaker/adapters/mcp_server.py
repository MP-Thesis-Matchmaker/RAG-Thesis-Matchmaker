"""MCP server exposing the matchmaker as tools for an LLM orchestrator.

Two thin tools over the app-service functions in ``service``:

- ``find_researchers``: structured results. This is the tool used in production
  by the UZH AI Buddy, which writes the final answer itself using its full
  conversation context.
- ``recommend_supervisors``: a full written recommendation, for standalone use.

Run with ``thesis-matchmaker-mcp`` (stdio transport). Needs the ``mcp`` extra.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from thesis_matchmaker.adapters import service

mcp = FastMCP("thesis-matchmaker")


@mcp.tool()
def find_researchers(query: str, top_k: int = 5) -> list[dict]:
    """Find UZH researchers whose work matches a topic or research interest.

    Returns a ranked list of people, each with department, a relevance score,
    whether they have an open thesis position, and evidence (their publications
    and postings). Works for general expertise questions like "who works on
    humanoid robots at UZH" as well as thesis-supervisor matching. Returns
    structured data so the caller can write the final answer.
    """
    return service.find_researchers(query, top_k=top_k)


@mcp.tool()
def recommend_supervisors(interests: str, top_k: int = 5) -> str:
    """Recommend thesis supervisors for a student as a short written answer.

    Given a student's research interests, returns a grounded recommendation that
    names suitable supervisors and cites their work. Use when a finished prose
    answer is wanted instead of structured data.
    """
    return service.recommend_supervisors(interests, top_k=top_k)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
