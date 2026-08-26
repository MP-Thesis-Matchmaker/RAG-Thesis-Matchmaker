"""Application settings, loaded from environment variables and an optional .env file."""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central config for the thesis matchmaker.

    Values are read from environment variables first, then from a local .env
    file. See .env.example for the full list. .env is gitignored, so keys and
    local paths stay off the repo.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM used to parse the student's query into structured fields, reached over
    # an OpenAI-compatible chat endpoint. In production llm_base_url points at
    # LibreChat / the AI Buddy gateway (the point of contact); in development at
    # a free local model, e.g. Ollama at http://localhost:11434/v1. When it is
    # unset, the pipeline uses the offline rule-based parser and needs no LLM.
    llm_base_url: str | None = None
    llm_model: str = "llama3.1"
    llm_api_key: str | None = None

    # Only for reasoning models (Qwen3, o-series, DeepSeek-R1 and friends).
    # "none" turns hidden reasoning off; "low"/"medium"/"high" bound it. Left
    # unset by default: the field is meaningless for a non-reasoning model, and
    # some OpenAI-compatible servers reject request fields they do not know.
    # Worth setting locally -- an 8B reasoning model can spend 25 s thinking
    # before its first output token, which reads as a dead endpoint.
    llm_reasoning_effort: str | None = None

    # Minimum retrieval score a candidate needs before the LLM synthesiser
    # presents it as a match; below it the answer says there is no strong match
    # instead of overselling a weak one. 0 disables the filter. Only meaningful
    # with real embeddings (hash-fake scores are arbitrary).
    synthesis_min_score: float = 0.0

    # Embedding model used for semantic search. Provisional default; the final
    # choice is shared with the retrieval and index work. The special value
    # "hash-fake" selects the deterministic offline fake (tests, CI, demos
    # without the model download).
    embedding_model: str = "BAAI/bge-m3"

    # Token cap applied before embedding. bge-m3 ships max_seq_length 8192, which
    # is not a free default: the attention buffer is batch x heads x seq^2, and
    # sentence-transformers batches longest-first, so the single longest abstract
    # in the corpus sets the size of the very first batch. At 8192 that batch asked
    # for 88.77 GiB and the run died two seconds in.
    #
    # 1024 is measured, not guessed: over a 2% sample of the harvested corpus the
    # token counts are p50=240, p95=632, p99=905, so 1024 truncates 0.69% of
    # documents and costs 1% of the average document's tokens. 2048 would truncate
    # only 0.04%, but its worst-case buffer is 4.29 GiB -- over the cluster's 4 GiB
    # namespace quota on its own, before bge-m3's 2.27 GB of weights. That quota is
    # what picks 1024.
    #
    # Changing this invalidates every vector in the index. It is recorded in the
    # manifest and guarded there, because the cap changes embeddings WITHOUT
    # changing content_hash, so a re-index would otherwise skip every document and
    # leave a silently mixed-cap index.
    embedding_max_seq_length: int = 1024

    # Documents per forward pass. Bounds the transient attention buffer together
    # with the cap above; it cannot substitute for it, since at 6822 tokens even a
    # batch of 8 asks for 22 GiB.
    embedding_batch_size: int = 16

    # Which torch device the embedding model loads onto. Unset means
    # sentence-transformers auto-detects, which is right nearly always and is
    # what the cluster (CPU-only nodes) gets anyway. The escape hatch exists
    # because of one failure mode: on a Mac auto-detect picks "mps", and moving
    # the model's 2.27 GB of weights into unified memory does not raise when the
    # machine is short of it -- Metal aborts the process, so the whole run dies
    # with a bare SIGABRT and no traceback that Python could have caught. Set
    # "cpu" and the same query answers in about a second. Passed through
    # verbatim, so "cpu", "mps", "cuda", "cuda:1" all work.
    embedding_device: str | None = None

    # Documents embedded and committed per round trip. The indexer streams in
    # chunks of this size rather than embedding the whole corpus before its first
    # write, which is what keeps peak memory flat and makes an interrupted run
    # resumable through the content-hash diff.
    index_chunk_size: int = 1000

    # Postgres holding both the vector index and (from the ingestion work) the
    # harvested source rows. pgvector is a decided constraint, not a preference:
    # the deployment target is a UZH Kubernetes cluster against a managed
    # Postgres with the extension available. See docs/deployment.md.
    database_url: str = "postgresql://matchmaker:matchmaker@localhost:5432/matchmaker"

    # Where the indexer reads publications.jsonl and theses.jsonl from. Defaults
    # to the checked-in samples: 50 real records exported from the corpus, which
    # is what makes an offline index possible at all. Set it to "db" to index the
    # harvested tables instead. Not synthetic -- see data/samples/README.md.
    sources_path: str = "data/samples"

    # Whether a publication needs at least one registered UZH author to be
    # retrievable at all. This used to be hardcoded True in retrieval/vector.py and
    # mirrored by a WHERE clause in indexing/sources.py, on the reasoning that a
    # paper with no UZH author cannot yield a supervisor a student could work with.
    #
    # Now off by default, and the index no longer takes a position either way, so
    # flipping this needs no re-embed. What replaces it as the default behaviour is
    # retrieval_ranking_strategy below: the ineligible records are reachable but
    # rank underneath every UZH-authored one.
    #
    # KNOWN GAP when set to True: pgvector applies metadata filters *after* the HNSW
    # scan (see the partial-index comment in schema.sql), and the partial indexes key
    # on source_type only. Over a full index only ~43% of publications satisfy this
    # predicate, so a filtered query can return fewer than top_k. VectorRetriever
    # over-fetches to compensate; the real fix is a partial index that matches this
    # predicate, which needs a schema change.
    retrieval_require_uzh_author: bool = False

    # Whether a thesis posting has to still be available to be retrievable. False means
    # assigned and private topics compete on similarity like any other posting.
    #
    # On by default, and for a firmer reason than the knob above: a topic already
    # assigned to a student is not a recommendation under any query, from any user.
    # Note that no ranking strategy softens this one -- there is no "demote the taken
    # ones" mode, so turning it off puts them in the results outright.
    #
    # It is a setting rather than a WHERE clause in indexing/sources.py because the
    # index no longer takes a position on availability either: all 695 postings are
    # embedded, `is_available` rides along in their metadata, and flipping this needs
    # no re-embed. The 17 unavailable ones cost nothing to carry next to 214,756
    # publications, which is what makes the guarantee affordable.
    retrieval_require_available_posting: bool = True

    # How candidates are ordered once retrieval has grouped hits per person.
    #
    #   uzh_first -- people credited by at least one UZH-authored publication (or by
    #                a thesis posting, whose supervisors work here by construction)
    #                rank above everyone else; similarity breaks ties within each
    #                group. This is the default because the product recommends UZH
    #                supervisors: an external researcher is a weaker answer than any
    #                UZH one, however well their abstract matches.
    #   score     -- plain similarity, ignoring affiliation. The behaviour before
    #                this setting existed.
    #
    # Inert while retrieval_require_uzh_author is True: nothing non-UZH survives the
    # filter, so there is nothing left for the strategy to order differently.
    #
    # A Literal rather than a str so a typo fails at settings load with a message
    # naming the valid values, instead of silently selecting a fallback strategy.
    retrieval_ranking_strategy: Literal["uzh_first", "score"] = "uzh_first"

    # MCP server. This is deployed as a standalone service that the AI Buddy
    # agent points at, so the tools are served over HTTP at
    # http://<mcp_host>:<mcp_port>/mcp. Use 0.0.0.0 as the host in a container.
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000

    # The matcher's own HTTP service: match, recommend, and the index triggers.
    # 8100 rather than 8000 on purpose -- both servers run side by side on a
    # laptop, and sharing mcp_port's default would make that collide.
    api_host: str = "127.0.0.1"
    api_port: int = 8100

    # Where the gateway (and the harvester, and the scraper) find that service.
    # None means "not configured": the gateway then has nothing to call and says
    # so, while the producers skip their post-run trigger instead of failing a
    # harvest that otherwise succeeded.
    matcher_base_url: str | None = None

    # How long an index run may go without committing a chunk before it is
    # presumed dead and its single-active slot released. This bounds the gap
    # between two chunks, not the length of a run: a cold index takes days but
    # breathes every chunk.
    index_run_heartbeat_timeout_s: int = 900


def get_settings() -> Settings:
    """Return settings, read fresh from the environment."""
    return Settings()
