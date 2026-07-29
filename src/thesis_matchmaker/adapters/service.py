"""App-service functions behind the interface adapters.

Plain functions the MCP server (and later a REST adapter) wrap. They hold no
transport concern, they just drive the pipeline. Kept free of the MCP SDK so
they can be tested without it, and a pipeline can be injected for tests.
"""

from __future__ import annotations

from pathlib import Path

from thesis_matchmaker.config import get_settings
from thesis_matchmaker.indexing.indexer import MANIFEST_FILE
from thesis_matchmaker.pipeline import Pipeline
from thesis_matchmaker.retrieval import build_retriever


def _default_pipeline() -> Pipeline:
    """Pipeline over the real retriever if an index exists, else the fake one."""
    settings = get_settings()
    if (Path(settings.vector_store_path) / MANIFEST_FILE).exists():
        return Pipeline(retriever=build_retriever(settings))
    return Pipeline()


def find_researchers(query: str, top_k: int = 5, pipeline: Pipeline | None = None) -> list[dict]:
    """Ranked researchers and supervisors matching a topic, as structured data."""
    pipe = pipeline or _default_pipeline()
    return [match.model_dump(mode="json") for match in pipe.run(query, top_k=top_k)]


def recommend_supervisors(interests: str, top_k: int = 5, pipeline: Pipeline | None = None) -> str:
    """A written, grounded recommendation of supervisors for a student."""
    pipe = pipeline or _default_pipeline()
    return pipe.recommend(interests, top_k=top_k)
