"""LLM abstraction — the *only* place the scraper talks to a language model.

Public surface is deliberately tiny (the plan): one function

    complete(system, prompt) -> str

Everything else — which provider, which model, keys, retries — lives in
``config.Settings``. The provider is chosen by ``SCRAPER_LLM_PROVIDER`` (default
``openai``). Swapping in another provider means adding one file
``themis_scraper/llm_<name>.py`` that exposes a ``Provider`` class with
``available()`` and ``complete(system, prompt, **opts)`` — no call site anywhere
else changes.

Degrades gracefully: if no provider is configured (e.g. no ``OPENAI_API_KEY``),
``is_available()`` is False and callers keep their deterministic output instead
of crashing.
"""

from __future__ import annotations

import importlib
import time

from .config import get_settings


def provider_name() -> str:
    return get_settings().llm_provider


def model_name() -> str:
    return get_settings().llm_model


def _get_provider():
    name = provider_name()
    if name == "openai":
        return _OpenAIProvider()
    # Any other provider is a pluggable module, imported by name.
    mod = importlib.import_module(f".llm_{name}", __package__)
    return mod.Provider()


def is_available() -> bool:
    try:
        return _get_provider().available()
    except Exception:
        return False


def complete(system: str, prompt: str, **opts) -> str:
    """Run one completion. Raises if the provider is unavailable or errors out
    after its retry budget — callers that want graceful degradation should gate
    on ``is_available()`` first."""
    return _get_provider().complete(system, prompt, **opts)


# --- Built-in OpenAI provider ----------------------------------------------


class _OpenAIProvider:
    """Chat Completions. No temperature is sent (gpt-5.x rejects != 1). Own
    retry loop with a hard per-request timeout so a hung call can't freeze a
    whole run; auth failures are never retried. `base_url` is only passed when
    configured, so the default stays the OpenAI API itself."""

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.llm_api_key
        self.model = s.llm_model
        self.base_url = s.llm_base_url
        self.max_attempts = s.llm_max_attempts
        self.timeout = s.llm_timeout_seconds

    def available(self) -> bool:
        return bool(self.api_key)

    def _client(self):
        from openai import OpenAI

        opts = {"base_url": self.base_url} if self.base_url else {}
        return OpenAI(api_key=self.api_key, timeout=self.timeout, max_retries=0, **opts)

    def complete(self, system: str, prompt: str, **opts) -> str:
        if not self.available():
            raise RuntimeError(
                "no LLM API key set — see .env.example (SCRAPER_LLM_API_KEY or OPENAI_API_KEY)"
            )
        from openai import (
            APIConnectionError,
            APITimeoutError,
            AuthenticationError,
            InternalServerError,
            RateLimitError,
        )

        client = self._client()
        transient = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)
        last = None
        for attempt in range(self.max_attempts):
            try:
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                )
                return (resp.choices[0].message.content or "").strip()
            except AuthenticationError:
                raise  # bad/expired key — retrying is pointless
            except transient as exc:
                last = exc
                time.sleep(2**attempt)
        raise RuntimeError(f"LLM failed after {self.max_attempts} attempts: {last}")
