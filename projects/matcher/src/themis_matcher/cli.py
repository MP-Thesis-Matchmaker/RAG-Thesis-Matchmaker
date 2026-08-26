"""Command line entry point.

Four subcommands: `init-db` applies the database schema, `index` builds or
refreshes the vector index from the ingested JSONL files, `match` runs one
query against it and writes a recommendation, and `repl` keeps a session open
for many queries -- the embedding model and connection pool load once instead
of per invocation. When no index has been built yet, `match` and `repl` fall
back to the fake retriever so the output shape stays visible.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
from urllib.parse import urlsplit, urlunsplit

from themis_matcher import __version__
from themis_matcher.indexing import (
    DATABASE_SOURCE,
    build_indexer,
    build_source_reader,
    build_store,
    read_manifest,
)
from themis_matcher.pipeline import Pipeline
from themis_matcher.retrieval import build_retriever
from themis_shared import db, initdb
from themis_shared.config import Settings, get_settings
from themis_shared.contracts import SupervisorMatch


def _print_matches(matches: list[SupervisorMatch]) -> None:
    if not matches:
        print("no matches.")
        return
    for rank, m in enumerate(matches, start=1):
        print(f"{rank}. {m.supervisor}  (score {m.score:.2f})")
        if m.department:
            print(f"   {m.department}")
        topics = ", ".join(m.matched_topics) or "n/a"
        # Silent on zero, for the reason spelled out in synthesis/template.py.
        details = [f"topics: {topics}", f"{m.publication_count} papers"]
        if m.posting_count:
            details.append(f"{m.posting_count} open postings")
        print("   " + "  |  ".join(details))
        for e in m.evidence:
            print(f"     - {e.title}")


def _index_exists(settings: Settings) -> bool:
    return read_manifest(settings) is not None


def _run_init_db(settings: Settings, args: argparse.Namespace) -> None:
    initdb.run(settings, reset=args.reset)


def _run_index(settings: Settings, args: argparse.Namespace) -> None:
    if args.rebuild:
        build_store(settings).clear()
    indexer = build_indexer(settings)
    reader = build_source_reader(settings, args.source)
    result = indexer.run(reader)
    print(
        f"index run complete: embedded={result.embedded} skipped={result.skipped} "
        f"deleted={result.deleted} invalid_lines={result.invalid_lines} "
        f"truncated={result.truncated}"
    )
    print(f"source: {reader.label}")
    print(f"model: {indexer.embedder.model_name} ({indexer.embedder.dimensions} dimensions)")
    window = indexer.embedder.max_seq_length
    if window is not None:
        print(
            f"token window: {window} tokens "
            f"(truncated {result.truncated} of {result.embedded} embedded)"
        )


def _build_pipeline(settings: Settings) -> Pipeline:
    """Pipeline over the real retriever if an index exists, else the fake one."""
    if _index_exists(settings):
        return Pipeline(retriever=build_retriever(settings))
    print("no index found - run 'themis-matcher index' first.")
    print("(results are canned for now, from the fake retriever)\n")
    return Pipeline()


def _answer(pipeline: Pipeline, query: str, top_k: int) -> None:
    """One query through the full flow: recommendation prose, then the detail table."""
    matches = pipeline.run(query, top_k=top_k)
    answer = pipeline.synthesizer.synthesize(query, matches)
    print(f"query: {query}\n")
    print(answer)
    print("\nmatches (retrieval detail):")
    _print_matches(matches)


def _run_match(settings: Settings, args: argparse.Namespace) -> None:
    _answer(_build_pipeline(settings), args.query, args.top_k)


def _run_repl(settings: Settings, args: argparse.Namespace) -> None:
    """Interactive local session: build the pipeline once, answer until EOF.

    Purely stdin/stdout -- nothing listens on a port, so "local only" holds by
    construction. Deployed instances are served through the MCP adapter; this
    exists so a human can poke at the pipeline without paying the model load
    (and, with real embeddings, a 2 GB weight read) on every single query.
    """
    # Arrow-key history for free where the stdlib provides it; the loop works
    # identically without it.
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401

    endpoint = settings.llm_base_url or "offline (rule-based parser, template prose)"
    print("themis-matcher repl -- type a research interest, 'exit' to leave,")
    print("':k N' to change how many matches are shown.")
    print(f"  llm endpoint:    {endpoint}")
    print(f"  embedding model: {settings.embedding_model}")
    print(f"  database:        {_redacted_dsn(settings.database_url)}")
    print(f"  index:           {_index_status(settings)}\n")

    pipeline = _build_pipeline(settings)
    top_k = args.top_k
    while True:
        try:
            query = input("match> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query.lower() in {"exit", "quit", ":q"}:
            break
        if query.startswith(":k"):
            try:
                top_k = int(query[2:])
                print(f"top-k set to {top_k}")
            except ValueError:
                print(f"usage: :k N  (currently {top_k})")
            continue
        try:
            _answer(pipeline, query, top_k)
        except Exception as exc:  # noqa: BLE001 -- a bad query must not end the session
            print(f"error: {exc.__class__.__name__}: {exc}")
        print()


def _redacted_dsn(dsn: str) -> str:
    """The DSN without its password, safe to print."""
    parts = urlsplit(dsn)
    if parts.password is None:
        return dsn
    netloc = f"{parts.username or ''}:***@{parts.hostname or ''}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _index_status(settings: Settings) -> str:
    """Human-readable index state, without turning a dead database into a crash."""
    try:
        manifest = read_manifest(settings)
    except db.DB_ERRORS as exc:
        return f"unknown - database unreachable ({exc.__class__.__name__})"
    if manifest is None:
        return "not built yet"
    return f"built ({manifest.document_count} documents, {manifest.embedding_model})"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="themis-matcher",
        description="Thesis matchmaking assistant.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    match_parser = subparsers.add_parser("match", help="find supervisors for a query")
    match_parser.add_argument("query", help="describe your research interests")
    match_parser.add_argument("--top-k", type=int, default=5, help="how many matches to show")

    repl_parser = subparsers.add_parser(
        "repl",
        help="interactive local session (deployed instances use the MCP server instead)",
    )
    repl_parser.add_argument("--top-k", type=int, default=5, help="how many matches to show")

    index_parser = subparsers.add_parser("index", help="build or refresh the vector index")
    index_parser.add_argument(
        "--source",
        help=(
            f"'{DATABASE_SOURCE}' to index the harvested publication table, or a "
            "directory holding publications.jsonl / theses.jsonl "
            "(default: SOURCES_PATH setting)"
        ),
    )
    index_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="empty the existing index first (required after changing the embedding model)",
    )

    init_db_parser = subparsers.add_parser(
        "init-db",
        help="create the database schema (idempotent; safe to re-run)",
    )
    initdb.add_arguments(init_db_parser)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    try:
        _dispatch(settings, args)
    finally:
        # The pool runs background worker threads; without this the process
        # hangs for the pool's stop timeout on the way out and complains.
        db.close_pools()


def _dispatch(settings: Settings, args: argparse.Namespace) -> None:
    if args.command == "init-db":
        _run_init_db(settings, args)
    elif args.command == "index":
        _run_index(settings, args)
    elif args.command == "match":
        _run_match(settings, args)
    elif args.command == "repl":
        _run_repl(settings, args)
    else:
        endpoint = settings.llm_base_url or "offline (rule-based parser)"
        print("themis-matcher")
        print(f"  llm endpoint:    {endpoint}")
        print(f"  llm model:       {settings.llm_model}")
        print(f"  embedding model: {settings.embedding_model}")
        print(f"  database:        {_redacted_dsn(settings.database_url)}")
        print(f"  index:           {_index_status(settings)}")
        print(
            "try: themis-matcher init-db && themis-matcher index && "
            'themis-matcher match "NLP thesis on RAG"'
        )


if __name__ == "__main__":
    main()
