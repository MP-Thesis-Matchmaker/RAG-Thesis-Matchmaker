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

    # Stand-in reasoning LLM used during local development, before the tool is
    # plugged into LibreChat. One of: "anthropic", "openai". Kept swappable on
    # purpose so it does not leak into the core.
    llm_provider: str = "anthropic"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # Embedding model used for semantic search. Provisional default; the final
    # choice is shared with the retrieval and index work.
    embedding_model: str = "BAAI/bge-m3"

    # Where the built vector index will live once it exists.
    vector_store_path: str = "data/index"


def get_settings() -> Settings:
    """Return settings, read fresh from the environment."""
    return Settings()
