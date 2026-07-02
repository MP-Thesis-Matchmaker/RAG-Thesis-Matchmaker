"""Query parsing: the extractor boundary and its implementations."""

from __future__ import annotations

from thesis_matchmaker.config import Settings, get_settings
from thesis_matchmaker.parsing.base import QueryExtractor
from thesis_matchmaker.parsing.rule_based import RuleBasedExtractor


def build_extractor(settings: Settings | None = None) -> QueryExtractor:
    """Pick an extractor from config.

    Uses the configured LLM endpoint (LibreChat in production, a free local
    model in development) when Settings.llm_base_url is set, otherwise the
    offline rule-based fallback. This keeps the pipeline runnable with no LLM.
    """
    settings = settings or get_settings()
    if settings.llm_base_url:
        from thesis_matchmaker.parsing.openai_compat import OpenAICompatExtractor

        return OpenAICompatExtractor(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
        )
    return RuleBasedExtractor()


__all__ = ["QueryExtractor", "RuleBasedExtractor", "build_extractor"]
