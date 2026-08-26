"""Answer synthesis: the synthesiser boundary and its implementations."""

from __future__ import annotations

from themis_matcher.config import MatcherSettings, get_settings
from themis_matcher.synthesis.base import Synthesizer
from themis_matcher.synthesis.template import TemplateSynthesizer


def build_synthesizer(settings: MatcherSettings | None = None) -> Synthesizer:
    """Pick a synthesiser from config.

    Uses the configured LLM endpoint when MatcherSettings.llm_base_url is set,
    otherwise the offline template synthesiser. Keeps the pipeline runnable
    with no LLM.
    """
    settings = settings or get_settings()
    if settings.llm_base_url:
        from themis_matcher.llm import LLMClient
        from themis_matcher.synthesis.llm import LLMSynthesizer

        client = LLMClient(
            settings.llm_base_url,
            settings.llm_model,
            settings.llm_api_key,
            reasoning_effort=settings.llm_reasoning_effort,
        )
        return LLMSynthesizer(client, min_score=settings.synthesis_min_score)
    return TemplateSynthesizer()


__all__ = ["Synthesizer", "TemplateSynthesizer", "build_synthesizer"]
