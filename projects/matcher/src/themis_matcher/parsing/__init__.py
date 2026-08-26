"""Query parsing: the extractor boundary and its implementations."""

from __future__ import annotations

from themis_matcher.config import MatcherSettings, get_settings
from themis_matcher.parsing.base import QueryExtractor
from themis_matcher.parsing.rule_based import RuleBasedExtractor


def build_extractor(settings: MatcherSettings | None = None) -> QueryExtractor:
    """Pick an extractor from config.

    Uses the configured LLM endpoint (LibreChat in production, a free local
    model in development) when MatcherSettings.llm_base_url is set, otherwise the
    offline rule-based fallback. This keeps the pipeline runnable with no LLM.
    """
    settings = settings or get_settings()
    if settings.llm_base_url:
        from themis_matcher.parsing.openai_compat import OpenAICompatExtractor

        return OpenAICompatExtractor(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            reasoning_effort=settings.llm_reasoning_effort,
        )
    return RuleBasedExtractor()


__all__ = ["QueryExtractor", "RuleBasedExtractor", "build_extractor"]
