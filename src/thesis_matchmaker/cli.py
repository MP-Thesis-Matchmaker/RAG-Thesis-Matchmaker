"""Command line entry point.

Two subcommands: `index` builds or refreshes the vector index from the
ingested JSONL files, `match` runs a query against it. When no index has been
built yet, `match` falls back to the fake retriever so the output shape stays
visible.
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

from thesis_matchmaker import __version__
from thesis_matchmaker.config import Settings, get_settings
from thesis_matchmaker.contracts import SupervisorMatch
from thesis_matchmaker.indexing import build_indexer
from thesis_matchmaker.indexing.indexer import MANIFEST_FILE
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
    return (Path(settings.vector_store_path) / MANIFEST_FILE).exists()


def _run_index(settings: Settings, args: argparse.Namespace) -> None:
    if args.rebuild:
        shutil.rmtree(settings.vector_store_path, ignore_errors=True)
    indexer = build_indexer(settings)
    result = indexer.run(Path(args.source or settings.sources_path))
    print(
        f"index run complete: embedded={result.embedded} skipped={result.skipped} "
        f"deleted={result.deleted} invalid_lines={result.invalid_lines}"
    )
    print(f"index at: {settings.vector_store_path} (model: {indexer.embedder.model_name})")


def _run_match(settings: Settings, args: argparse.Namespace) -> None:
    if _index_exists(settings):
        pipeline = Pipeline(retriever=build_retriever(settings))
    else:
        pipeline = Pipeline()
        print("no index found - run 'thesis-matchmaker index' first.")
        print("(results are canned for now, from the fake retriever)\n")
    matches = pipeline.run(args.query, top_k=args.top_k)
    print(f"query: {args.query}")
    _print_matches(matches)


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
        "--source", help="directory with publications.jsonl / theses.jsonl "
        "(default: SOURCES_PATH setting)"
    )
    index_parser.add_argument(
        "--rebuild", action="store_true",
        help="delete the existing index first (required after changing the embedding model)",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    if args.command == "index":
        _run_index(settings, args)
    elif args.command == "match":
        _run_match(settings, args)
    else:
        endpoint = settings.llm_base_url or "offline (rule-based parser)"
        print("thesis-matchmaker")
        print(f"  llm endpoint:    {endpoint}")
        print(f"  llm model:       {settings.llm_model}")
        print(f"  embedding model: {settings.embedding_model}")
        print(f"  index:           {'built' if _index_exists(settings) else 'not built yet'}")
        print('try: thesis-matchmaker index && thesis-matchmaker match "NLP thesis on RAG"')


if __name__ == "__main__":
    main()
