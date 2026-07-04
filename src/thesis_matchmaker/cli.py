"""Command line entry point.

Runs the pipeline end to end against the fake retriever, so the shape of the
output is visible before the real retrieval and LLM steps land.
"""

from __future__ import annotations

import argparse

from thesis_matchmaker import __version__
from thesis_matchmaker.config import get_settings
from thesis_matchmaker.contracts import SupervisorMatch
from thesis_matchmaker.pipeline import Pipeline


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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="thesis-matchmaker",
        description="Thesis matchmaking assistant (early skeleton).",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("query", nargs="?", help="describe your research interests")
    parser.add_argument("--top-k", type=int, default=5, help="how many matches to show")
    args = parser.parse_args()

    settings = get_settings()

    if not args.query:
        endpoint = settings.llm_base_url or "offline (rule-based parser)"
        print("thesis-matchmaker skeleton is ready.")
        print(f"  llm endpoint:    {endpoint}")
        print(f"  llm model:       {settings.llm_model}")
        print(f"  embedding model: {settings.embedding_model}")
        print('try: thesis-matchmaker "I want an NLP thesis on RAG"')
        return

    matches = Pipeline().run(args.query, top_k=args.top_k)
    print(f"query: {args.query}")
    print("(results are canned for now, from the fake retriever)\n")
    _print_matches(matches)


if __name__ == "__main__":
    main()
