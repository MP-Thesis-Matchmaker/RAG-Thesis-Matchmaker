"""App-service functions behind the interface adapters.

Plain functions the MCP server (and later a REST adapter) wrap. They hold no
transport concern, they just drive the pipeline. Kept free of the MCP SDK so
they can be tested without it, and a pipeline can be injected for tests.
"""

from __future__ import annotations

from themis_matcher.indexing import read_manifest
from themis_matcher.pipeline import Pipeline
from themis_matcher.retrieval import build_retriever
from themis_shared.config import get_settings


class IndexNotBuiltError(RuntimeError):
    """Raised when a tool is called before anything has been indexed."""


def _default_pipeline() -> Pipeline:
    """Pipeline over the real retriever.

    Deliberately has no fake-retriever fallback. These functions back the MCP
    tools askUZH calls, so an unbuilt index has to surface as an error: handing
    back invented supervisors because nothing was indexed yet is far worse than
    failing, and it is the kind of thing nobody notices until a student acts on
    it. Tests inject their own pipeline instead.
    """
    settings = get_settings()
    if read_manifest(settings) is None:
        raise IndexNotBuiltError(
            "No index has been built, so there is nothing to recommend from. "
            "Run 'themis-matcher index' against a populated database first."
        )
    return Pipeline(retriever=build_retriever(settings))


def find_researchers(query: str, top_k: int = 5, pipeline: Pipeline | None = None) -> list[dict]:
    """Ranked researchers and supervisors matching a topic, as structured data."""
    pipe = pipeline or _default_pipeline()
    return [match.model_dump(mode="json") for match in pipe.run(query, top_k=top_k)]


def recommend_supervisors(interests: str, top_k: int = 5, pipeline: Pipeline | None = None) -> str:
    """A written, grounded recommendation of supervisors for a student."""
    pipe = pipeline or _default_pipeline()
    return pipe.recommend(interests, top_k=top_k)
