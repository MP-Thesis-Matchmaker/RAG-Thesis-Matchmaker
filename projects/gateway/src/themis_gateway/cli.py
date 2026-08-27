"""Command line entry point.

One subcommand today, `mcp`, which serves the matchmaker as MCP tools. The shape
is deliberate rather than speculative: every member of this workspace is
`themis-<member> <subcommand>`, so the adapter is `themis-gateway mcp` and not a
console script that spells its own subcommand in its name. See `themis_matcher.cli`,
which this mirrors.

    themis-gateway                    # what this instance is pointed at
    themis-gateway mcp                # streamable HTTP on GATEWAY_MCP_HOST:PORT/mcp
    themis-gateway mcp --stdio        # stdio, for a local MCP inspector

`python -m themis_gateway mcp` is the same program by its module name.

A REST front door for students is the other adapter this member is meant to grow
(see the README). When it lands it is a second subcommand here, not a second
console script -- which is the point of taking the top-level name now.
"""

from __future__ import annotations

import argparse

from themis_gateway import __version__
from themis_gateway.config import GatewaySettings, get_settings


def _run_mcp(settings: GatewaySettings, args: argparse.Namespace) -> None:
    """Serve the MCP tools until killed.

    Imported here rather than at module scope so that `themis-gateway` with no
    subcommand -- and `--help`, and `--version` -- keep working when the `mcp`
    extra is not installed. That extra is never installed in CI, so this is the
    difference between a status summary and a ModuleNotFoundError.
    """
    from themis_gateway.mcp_server import server

    if args.stdio:
        # Deliberately before get_settings(): stdio needs no host or port, and a
        # local inspector session should not require the deployment config to be
        # present and valid.
        server.run(transport="stdio")
        return

    print(f"themis-gateway serving MCP on http://{settings.mcp_host}:{settings.mcp_port}/mcp")
    print(f"  matcher: {settings.matcher_base_url or 'unset - every tool call will fail'}")
    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )


def _dispatch(settings: GatewaySettings, args: argparse.Namespace) -> None:
    if args.command == "mcp":
        _run_mcp(settings, args)
        return

    # No subcommand: say what this instance is pointed at. The same shape as
    # themis_matcher.cli's else-branch. For this member the matcher URL is the
    # whole story -- it holds no model and no database, so an unset or wrong
    # MATCHER_BASE_URL is the only way it can be broken, and it is the one thing
    # worth being able to read off a pod in one command.
    print("themis-gateway")
    print(f"  matcher:  {settings.matcher_base_url or 'unset - every tool call will fail'}")
    print(f"  mcp bind: {settings.mcp_host}:{settings.mcp_port}")
    print("try: themis-gateway mcp")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="themis-gateway",
        description="Front door for the thesis matchmaker.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    mcp_parser = subparsers.add_parser(
        "mcp",
        help="serve the matchmaker as MCP tools (streamable HTTP by default)",
        description="Serve the thesis matchmaker as MCP tools.",
    )
    mcp_parser.add_argument(
        "--stdio",
        action="store_true",
        help="run over stdio instead of HTTP (local testing with an MCP inspector)",
    )

    args = parser.parse_args(argv)
    _dispatch(get_settings(), args)


if __name__ == "__main__":
    main()
