"""LLM-backed query extractor over an OpenAI-compatible chat endpoint.

Used when an LLM endpoint is configured (Settings.llm_base_url). In production
that endpoint is LibreChat / the AI Buddy gateway, the point of contact; in
development it is a free local model such as Ollama. The same OpenAI-compatible
Chat Completions shape covers all of them, so switching is just a base URL and
a model name. Falls back to the rule-based parser on any error, so a flaky dev
model never breaks the pipeline.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field, ValidationError

from thesis_matchmaker.contracts import DegreeLevel, ParsedQuery
from thesis_matchmaker.parsing.base import QueryExtractor
from thesis_matchmaker.parsing.rule_based import RuleBasedExtractor

_SYSTEM = (
    "You extract structured search fields from a student's description of their "
    "thesis interests. Return only JSON with these keys: topics (list of "
    'strings), keywords (list of strings), degree_level (one of "bachelor", '
    '"master", "phd", or null), department (string or null). Do not invent '
    "details that are not in the text."
)


class _QueryExtraction(BaseModel):
    topics: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    degree_level: DegreeLevel | None = None
    department: str | None = None


class OpenAICompatExtractor:
    """Parses a query with any OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        fallback: QueryExtractor | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._timeout = timeout
        self._fallback = fallback or RuleBasedExtractor()

    def extract(self, raw_query: str) -> ParsedQuery:
        try:
            content = self._call(raw_query)
            data = _QueryExtraction.model_validate_json(content)
        except (httpx.HTTPError, ValidationError, KeyError, IndexError):
            # Endpoint unreachable or gave something off-schema; degrade quietly.
            return self._fallback.extract(raw_query)
        return ParsedQuery(
            topics=data.topics,
            keywords=data.keywords,
            degree_level=data.degree_level,
            department=data.department,
            raw_query=raw_query,
        )

    def _call(self, raw_query: str, json_mode: bool = True) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": raw_query},
            ],
            "temperature": 0,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = httpx.post(self._url, json=payload, headers=self._headers, timeout=self._timeout)
        if json_mode and response.status_code in (400, 422):
            # Endpoint may not support JSON mode; retry once without it.
            return self._call(raw_query, json_mode=False)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
