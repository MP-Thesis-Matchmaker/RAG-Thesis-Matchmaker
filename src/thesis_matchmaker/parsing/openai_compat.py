"""LLM-backed query extractor over an OpenAI-compatible chat endpoint.

Used when an LLM endpoint is configured (Settings.llm_base_url). In production
that endpoint is LibreChat / the AI Buddy gateway, the point of contact; in
development it is a free local model such as Ollama. Falls back to the
rule-based parser on any error, so a flaky dev model never breaks the pipeline.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from thesis_matchmaker.contracts import DegreeLevel, ParsedQuery
from thesis_matchmaker.llm import LLMClient, LLMError
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
        self._client = LLMClient(base_url, model, api_key, timeout)
        self._fallback = fallback or RuleBasedExtractor()

    def extract(self, raw_query: str) -> ParsedQuery:
        try:
            content = self._client.chat(_SYSTEM, raw_query, json_mode=True)
            data = _QueryExtraction.model_validate_json(content)
        except (LLMError, ValidationError):
            # Endpoint unreachable or gave something off-schema; degrade quietly.
            return self._fallback.extract(raw_query)
        return ParsedQuery(
            topics=data.topics,
            keywords=data.keywords,
            degree_level=data.degree_level,
            department=data.department,
            raw_query=raw_query,
        )
