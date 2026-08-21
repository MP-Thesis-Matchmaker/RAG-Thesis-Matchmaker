"""Application settings, loaded from environment variables and an optional .env file."""

from __future__ import annotations

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

    # Directory the ingestion component writes its JSONL output to; the
    # indexer reads publications.jsonl and theses.jsonl from here. Defaults to
    # the checked-in synthetic sample data until real ingestion output exists.
    sources_path: str = "data/samples"

    # MCP server. This is deployed as a standalone service that the AI Buddy
    # agent points at, so the tools are served over HTTP at
    # http://<mcp_host>:<mcp_port>/mcp. Use 0.0.0.0 as the host in a container.
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000


def get_settings() -> Settings:
    """Return settings, read fresh from the environment."""
    return Settings()
