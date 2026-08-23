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
        # Request fields this endpoint has already rejected, so the doomed
        # request is paid once per process instead of once per call. Learned
        # rather than configured: the same code talks to OpenAI, LibreChat and
        # Ollama, and they disagree about which fields exist.
        self._unsupported: set[str] = set()
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
            temperature=True,
            allow_retry=True,
        )

    def _blamed_fields(self, response: httpx.Response, sent: set[str]) -> set[str]:
        """Which of the fields we sent the endpoint is complaining about.

        OpenAI names it in `error.param`; others only say so in prose, so the
        message is searched too. With no clue at all every optional field is
        blamed, which is the old behaviour: a worse-shaped answer beats none.
        """
        try:
            error = response.json().get("error") or {}
        except ValueError:
            return set(sent)
        if not isinstance(error, dict):
            return set(sent)
        param = error.get("param")
        if isinstance(param, str) and param in sent:
            return {param}
        message = error.get("message")
        if isinstance(message, str):
            named = {field for field in sent if field in message}
            if named:
                return named
        return set(sent)

    def _post(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool,
        reasoning_effort: str | None,
        temperature: bool,
        allow_retry: bool,
    ) -> str:
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # Every optional field below is an extension some endpoint does not
        # implement, and a 400/422 is how it says so. Anything already refused
        # once is left out from the start.
        sent: set[str] = set()
        # Determinism where the model supports it. OpenAI's gpt-5/o-series
        # reject any temperature but the default.
        if temperature and "temperature" not in self._unsupported:
            payload["temperature"] = 0
            sent.add("temperature")
        if json_mode and "response_format" not in self._unsupported:
            payload["response_format"] = {"type": "json_object"}
            sent.add("response_format")
        if reasoning_effort and "reasoning_effort" not in self._unsupported:
            payload["reasoning_effort"] = reasoning_effort
            sent.add("reasoning_effort")
        try:
            response = httpx.post(
                self._url, json=payload, headers=self._headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise LLMError(str(exc)) from exc
        # Retry once without whatever was refused rather than failing outright.
        # Only the fields actually blamed are dropped, so a rejected temperature
        # does not also cost the JSON response format the parser depends on.
        if response.status_code in (400, 422) and allow_retry and sent:
            blamed = self._blamed_fields(response, sent)
            self._unsupported |= blamed
            logger.info(
                "endpoint rejected %s (of %s); retrying without it and omitting it from "
                "later requests",
                ", ".join(sorted(blamed)),
                ", ".join(sorted(sent)),
            )
            return self._post(
                system,
                user,
                json_mode=json_mode,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
                allow_retry=False,
            )
        try:
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            raise LLMError(str(exc)) from exc
