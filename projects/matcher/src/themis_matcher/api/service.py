"""What the HTTP layer is a front door to.

Everything expensive lives here and is built once per process: the embedding
model, the connection pool behind the store, the pipeline. `app.py` owns routes
and status codes and nothing else, the same division `themis_gateway` keeps
between `mcp_server.py` and `service.py`.

The embedder is deliberately shared between serving and indexing. Both need
`BAAI/bge-m3`, it is 2.27 GB, and the namespace ceiling is 4 GiB in total -- two
copies in one process would not fit even if it were free. This is also why
indexing runs in a thread here rather than in a pod of its own.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from themis_matcher.config import MatcherSettings, get_settings
from themis_matcher.indexing import build_embedder, build_source_reader, build_store
from themis_matcher.indexing.documents import SOURCE_POSTING, SOURCE_PUBLICATION, SOURCE_TYPES
from themis_matcher.indexing.embedder import Embedder
from themis_matcher.indexing.indexer import Indexer
from themis_matcher.indexing.runs import IndexRunStore
from themis_matcher.indexing.store import IndexManifest, VectorStore
from themis_matcher.parsing import build_extractor
from themis_matcher.pipeline import Pipeline
from themis_matcher.retrieval.vector import VectorRetriever
from themis_matcher.synthesis import build_synthesizer
from themis_shared.contracts import IndexRun, IndexRunKind

logger = logging.getLogger(__name__)

# Which document kinds each trigger covers. `all` exists so a CLI-shaped run can
# be recorded in the same table; the endpoints only offer the two single kinds.
KINDS: dict[IndexRunKind, tuple[str, ...]] = {
    IndexRunKind.publication: (SOURCE_PUBLICATION,),
    IndexRunKind.thesis_posting: (SOURCE_POSTING,),
    IndexRunKind.all: SOURCE_TYPES,
}


@dataclass
class MatcherService:
    """One process's worth of matcher.

    Built once at startup and shared by every request. Nothing here is per-call
    state, and the routes must not add any.
    """

    settings: MatcherSettings
    embedder: Embedder
    store: VectorStore
    pipeline: Pipeline
    runs: IndexRunStore

    def manifest(self) -> IndexManifest | None:
        """The built index, or None if there is not one yet."""
        return self.store.read_manifest()

    def trigger_index(self, kind: IndexRunKind, source: str | None = None) -> IndexRun:
        """Claim the single-active slot and start indexing in the background.

        Raises `IndexRunInProgress` if another run holds it. The claim happens on
        the calling thread on purpose: the caller has to learn *now* whether it
        was accepted, and a 202 that might still turn out to be a 409 would be a
        lie.
        """
        reader = build_source_reader(self.settings, source)
        run = self.runs.start(kind, reader.label)

        thread = threading.Thread(
            target=self._index,
            args=(run.id, kind, reader),
            name=f"index-run-{run.id}",
            # Daemon: a pod being torn down should not wait days for an index to
            # finish. The run is left `running` and the heartbeat reaper releases
            # its slot; the content-hash diff means the next run resumes rather
            # than starting over.
            daemon=True,
        )
        thread.start()
        return run

    def _index(self, run_id: int, kind: IndexRunKind, reader) -> None:
        indexer = Indexer(
            embedder=self.embedder,
            store=self.store,
            chunk_size=self.settings.index_chunk_size,
            on_chunk=lambda embedded, seen: self.runs.heartbeat(run_id),
        )
        try:
            result = indexer.run(reader, kinds=KINDS[kind])
        except Exception as exc:
            # Broad on purpose. This is the top of a thread: an exception that
            # escapes here is printed to stderr and the run stays `running`
            # forever, so every failure has to be recorded rather than raised.
            logger.exception("index run %d failed", run_id)
            self.runs.fail(run_id, f"{type(exc).__name__}: {exc}")
            return
        self.runs.succeed(run_id, result)


def build_service(
    settings: MatcherSettings | None = None,
    *,
    embedder: Embedder | None = None,
    store: VectorStore | None = None,
) -> MatcherService:
    """Wire a service from settings, with the two heavy parts injectable.

    `build_store` only ever returns Postgres, so injection is what lets the API
    tests run against `InMemoryVectorStore` with no database -- the same reason
    `Pipeline` takes its components rather than building them.
    """
    settings = settings or get_settings()
    embedder = embedder or build_embedder(settings)
    store = store or build_store(settings)

    retriever = VectorRetriever(
        embedder=embedder,
        store=store,
        require_uzh_author=settings.retrieval_require_uzh_author,
        require_available_posting=settings.retrieval_require_available_posting,
        ranking_strategy=settings.retrieval_ranking_strategy,
    )
    return MatcherService(
        settings=settings,
        embedder=embedder,
        store=store,
        pipeline=Pipeline(
            retriever=retriever,
            extractor=build_extractor(settings),
            synthesizer=build_synthesizer(settings),
        ),
        runs=IndexRunStore(
            dsn=settings.database_url,
            heartbeat_timeout_s=settings.index_run_heartbeat_timeout_s,
        ),
    )


__all__ = ["KINDS", "MatcherService", "build_service"]
