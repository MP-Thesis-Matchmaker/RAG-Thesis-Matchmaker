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

    # Where the built vector index lives.
    vector_store_path: str = "data/index"

    # Directory the ingestion component writes its JSONL output to; the
    # indexer reads publications.jsonl and theses.jsonl from here. Defaults to
    # the checked-in synthetic sample data until real ingestion output exists.
    sources_path: str = "data/samples"

    # Name of the vector store collection holding the index.
    collection_name: str = "matchmaker"


def get_settings() -> Settings:
    """Return settings, read fresh from the environment."""
    return Settings()
