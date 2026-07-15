"""Minimal client for an OpenAI-compatible chat endpoint.

Shared by the query parser and the answer synthesiser. Talks to LibreChat or
the AI Buddy gateway in production, or a free local model (e.g. Ollama) in
development, configured through Settings.
"""

from __future__ import annotations

import httpx


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
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._timeout = timeout

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = httpx.post(
                self._url, json=payload, headers=self._headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise LLMError(str(exc)) from exc
        if json_mode and response.status_code in (400, 422):
            # Endpoint may not support JSON mode; retry once without it.
            return self.chat(system, user, json_mode=False)
        try:
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            raise LLMError(str(exc)) from exc
