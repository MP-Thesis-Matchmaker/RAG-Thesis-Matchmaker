"""Minimal client for an OpenAI-compatible chat endpoint.

Shared by the query parser and the answer synthesiser. Talks to LibreChat or
the AI Buddy gateway in production, or a free local model (e.g. Ollama) in
development, configured through Settings.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when the chat endpoint is unreachable or returns something odd."""


class LLMClient:
    """Thin wrapper over the OpenAI-compatible /chat/completions endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        reasoning_effort: str | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._timeout = timeout
        # Reasoning models spend most of their wall-clock time on hidden
        # reasoning tokens before emitting a single word of the answer, which is
        # enough to blow past `timeout` on a request that would otherwise be
        # fast. Sending "none" turns that off. Left unset by default because it
        # is only meaningful for a model that reasons, and an endpoint that does
        # not know the field may reject the whole request -- see the retry below.
        self._reasoning_effort = reasoning_effort

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        return self._post(
            system,
            user,
            json_mode=json_mode,
            reasoning_effort=self._reasoning_effort,
            allow_retry=True,
        )

    def _post(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool,
        reasoning_effort: str | None,
        allow_retry: bool,
    ) -> str:
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        try:
            response = httpx.post(
                self._url, json=payload, headers=self._headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise LLMError(str(exc)) from exc
        # Both optional fields are extensions some endpoints do not implement, and
        # a 400/422 is how they say so. Retry once with a plain request rather
        # than failing: a worse-shaped answer beats no answer.
        if response.status_code in (400, 422) and allow_retry and (json_mode or reasoning_effort):
            logger.debug(
                "endpoint rejected optional fields (json_mode=%s, reasoning_effort=%s); "
                "retrying without them",
                json_mode,
                reasoning_effort,
            )
            return self._post(
                system, user, json_mode=False, reasoning_effort=None, allow_retry=False
            )
        try:
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            raise LLMError(str(exc)) from exc
