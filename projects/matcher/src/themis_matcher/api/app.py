"""The matcher's HTTP surface.

Routes, status codes, and nothing else -- the work is `service.py`'s. This is the
same split `themis_gateway` keeps between its MCP server and its service module,
and it is what lets the endpoints be tested without a transport.

**Every route is `def`, never `async def`.** Nothing underneath is async: the
psycopg pool is the synchronous one, `SentenceTransformer.encode` blocks for as
long as it blocks, and `LLMClient.chat` is a blocking `httpx.post` with a 30 s
timeout. Starlette runs a `def` route in its threadpool, which is where all three
belong; an `async def` route would hold the event loop for the whole call and
serialise every other request behind it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from themis_matcher.api.service import MatcherService, build_service
from themis_matcher.indexing.indexer import ModelMismatchError
from themis_matcher.indexing.runs import IndexRunInProgress
from themis_shared import db
from themis_shared.config import Settings
from themis_shared.contracts import (
    ApiError,
    IndexRun,
    IndexRunAccepted,
    IndexRunKind,
    IndexStatus,
    MatchRequest,
    MatchResponse,
    RecommendResponse,
)

logger = logging.getLogger(__name__)


class MatcherApiError(Exception):
    """A refusal with a machine-readable code.

    The code is the contract, not the prose: the gateway maps `index_not_built`
    back onto its own exception type, and a reworded message must not break that.
    """

    def __init__(self, status_code: int, code: str, message: str, run_id: int | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.run_id = run_id
        super().__init__(message)


def _service(request: Request) -> MatcherService:
    return request.app.state.service


def _require_index(service: MatcherService) -> None:
    """Refuse to answer from an index that does not exist.

    Falling back to the fake retriever here would be worse than an error:
    canned recommendations are indistinguishable from real ones to the caller.
    """
    if service.manifest() is None:
        raise MatcherApiError(
            status.HTTP_409_CONFLICT,
            "index_not_built",
            "No index has been built, so there is nothing to recommend from. "
            "Index the corpus before querying.",
        )


def create_app(
    settings: Settings | None = None, service: MatcherService | None = None
) -> FastAPI:
    """Build the app. `service` is injectable so tests need no database."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        # Built once. The embedding model behind it lazy-loads on first use, so
        # startup stays fast and the first query pays for the load.
        app.state.service = service or build_service(settings)
        # No stale-run sweep here on purpose. `IndexRunStore.start` reaps before
        # it claims, so a slot held by a died-in-the-night run is released at the
        # moment it would otherwise block something -- and doing it here as well
        # would put a database round trip, and a five-second pool timeout when
        # there is no database, in front of every startup.
        yield
        # The pool runs background worker threads; without this the process hangs
        # on the way out. The MCP server has never done this, which is a leak it
        # gets away with only because nothing ever asks it to stop cleanly.
        db.close_pools()

    app = FastAPI(
        title="themis-matcher",
        summary="Matching over the ZORA corpus and scraped thesis postings.",
        lifespan=lifespan,
    )

    @app.exception_handler(MatcherApiError)
    def _handle_api_error(request: Request, exc: MatcherApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ApiError(code=exc.code, message=exc.message, run_id=exc.run_id).model_dump(
                mode="json"
            ),
        )

    @app.exception_handler(IndexRunInProgress)
    def _handle_run_in_progress(request: Request, exc: IndexRunInProgress) -> JSONResponse:
        return _handle_api_error(
            request,
            MatcherApiError(
                status.HTTP_409_CONFLICT, "index_run_in_progress", str(exc), exc.run_id
            ),
        )

    @app.exception_handler(ModelMismatchError)
    def _handle_model_mismatch(request: Request, exc: ModelMismatchError) -> JSONResponse:
        return _handle_api_error(
            request,
            MatcherApiError(status.HTTP_409_CONFLICT, "model_mismatch", str(exc)),
        )

    def _handle_db_error(request: Request, exc: Exception) -> JSONResponse:
        logger.warning("database unavailable", exc_info=True)
        return _handle_api_error(
            request,
            MatcherApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "database_unavailable",
                "The database did not answer.",
            ),
        )

    # Registered one class at a time: DB_ERRORS is a tuple and Starlette's
    # exception_handler takes a single class. The tuple is still the right source
    # of truth -- PoolTimeout is not a psycopg.Error, and with max_size=5 it is
    # the failure a busy server meets first.
    for _db_error in db.DB_ERRORS:
        app.add_exception_handler(_db_error, _handle_db_error)

    @app.get("/v1/health", summary="Liveness. Touches no database.")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/index/status", response_model=IndexStatus)
    def index_status(request: Request) -> IndexStatus:
        service = _service(request)
        _require_index(service)
        manifest = service.manifest()
        assert manifest is not None  # _require_index just checked
        return IndexStatus(**manifest.model_dump())

    @app.post("/v1/match", response_model=MatchResponse)
    def match(request: Request, body: MatchRequest) -> MatchResponse:
        service = _service(request)
        _require_index(service)
        return MatchResponse(matches=service.pipeline.run(body.query, top_k=body.top_k))

    @app.post("/v1/recommend", response_model=RecommendResponse)
    def recommend(request: Request, body: MatchRequest) -> RecommendResponse:
        service = _service(request)
        _require_index(service)
        return RecommendResponse(answer=service.pipeline.recommend(body.query, top_k=body.top_k))

    def _trigger(request: Request, kind: IndexRunKind) -> IndexRunAccepted:
        run = _service(request).trigger_index(kind)
        return IndexRunAccepted(run_id=run.id, kind=run.kind)

    @app.post(
        "/v1/index/publications",
        response_model=IndexRunAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Index harvested publications. Returns a receipt, not a result.",
    )
    def index_publications(request: Request) -> IndexRunAccepted:
        return _trigger(request, IndexRunKind.publication)

    @app.post(
        "/v1/index/postings",
        response_model=IndexRunAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Index scraped thesis postings. Returns a receipt, not a result.",
    )
    def index_postings(request: Request) -> IndexRunAccepted:
        return _trigger(request, IndexRunKind.thesis_posting)

    @app.get("/v1/index/runs", response_model=list[IndexRun])
    def index_runs(request: Request, limit: int = 20) -> list[IndexRun]:
        return _service(request).runs.recent(limit=min(max(limit, 1), 100))

    @app.get("/v1/index/runs/{run_id}", response_model=IndexRun)
    def index_run(request: Request, run_id: int) -> IndexRun:
        run = _service(request).runs.get(run_id)
        if run is None:
            raise MatcherApiError(
                status.HTTP_404_NOT_FOUND, "no_such_run", f"No index run {run_id}."
            )
        return run

    return app


__all__ = ["MatcherApiError", "create_app"]
