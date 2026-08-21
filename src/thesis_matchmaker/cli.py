"""Command line entry point.

Three subcommands: `init-db` applies the database schema, `index` builds or
refreshes the vector index from the ingested JSONL files, and `match` runs a
query against it and writes a recommendation. When no index has been built yet,
`match` falls back to the fake retriever so the output shape stays visible.
"""

from __future__ import annotations

import argparse
import logging
from urllib.parse import urlsplit, urlunsplit

from thesis_matchmaker import __version__, db, schema
from thesis_matchmaker.config import Settings, get_settings
from thesis_matchmaker.contracts import SupervisorMatch
from thesis_matchmaker.indexing import (
    DATABASE_SOURCE,
    build_indexer,
    build_source_reader,
    build_store,
    read_manifest,
)
from thesis_matchmaker.pipeline import Pipeline
from thesis_matchmaker.retrieval import build_retriever


def _print_matches(matches: list[SupervisorMatch]) -> None:
    if not matches:
        print("no matches.")
        return
    for rank, m in enumerate(matches, start=1):
        position = "open position" if m.has_open_position else "no open position"
        print(f"{rank}. {m.supervisor}  (score {m.score:.2f})")
        if m.department:
            print(f"   {m.department}")
        topics = ", ".join(m.matched_topics) or "n/a"
        print(f"   topics: {topics}  |  {m.publication_count} papers  |  {position}")
        for e in m.evidence:
            print(f"     - {e.title}")


def _index_exists(settings: Settings) -> bool:
    return read_manifest(settings) is not None


def _run_init_db(settings: Settings, args: argparse.Namespace) -> None:
    try:
        result = schema.apply(settings.database_url, reset=args.reset)
    except schema.SchemaChangedError as exc:
        raise SystemExit(f"error: {exc}") from exc
    for name in result.dropped:
        print(f"dropped table {name}")
    if result.applied:
        print(f"schema applied ({result.fingerprint})")
    else:
        print(f"schema already up to date ({result.fingerprint})")


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


def _run_match(settings: Settings, args: argparse.Namespace) -> None:
    if _index_exists(settings):
        pipeline = Pipeline(retriever=build_retriever(settings))
    else:
        pipeline = Pipeline()
        print("no index found - run 'thesis-matchmaker index' first.")
        print("(results are canned for now, from the fake retriever)\n")
    matches = pipeline.run(args.query, top_k=args.top_k)
    answer = pipeline.synthesizer.synthesize(args.query, matches)
    print(f"query: {args.query}\n")
    print(answer)
    print("\nmatches (retrieval detail):")
    _print_matches(matches)


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
        prog="thesis-matchmaker",
        description="Thesis matchmaking assistant.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    match_parser = subparsers.add_parser("match", help="find supervisors for a query")
    match_parser.add_argument("query", help="describe your research interests")
    match_parser.add_argument("--top-k", type=int, default=5, help="how many matches to show")

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
    init_db_parser.add_argument(
        "--reset",
        action="store_true",
        help="DROP every table first, then recreate. Destroys all data. Needed after "
        "editing schema.sql, until the first harvest worth keeping exists.",
    )

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
    else:
        endpoint = settings.llm_base_url or "offline (rule-based parser)"
        print("thesis-matchmaker")
        print(f"  llm endpoint:    {endpoint}")
        print(f"  llm model:       {settings.llm_model}")
        print(f"  embedding model: {settings.embedding_model}")
        print(f"  database:        {_redacted_dsn(settings.database_url)}")
        print(f"  index:           {_index_status(settings)}")
        print(
            "try: thesis-matchmaker init-db && thesis-matchmaker index && "
            'thesis-matchmaker match "NLP thesis on RAG"'
        )


if __name__ == "__main__":
    main()
