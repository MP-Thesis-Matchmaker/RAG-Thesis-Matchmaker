"""Answer synthesis: the synthesiser boundary and its implementations."""

from __future__ import annotations

from thesis_matchmaker.config import Settings, get_settings
from thesis_matchmaker.synthesis.base import Synthesizer
from thesis_matchmaker.synthesis.template import TemplateSynthesizer


def build_synthesizer(settings: Settings | None = None) -> Synthesizer:
    """Pick a synthesiser from config.

    Uses the configured LLM endpoint when Settings.llm_base_url is set,
    otherwise the offline template synthesiser. Keeps the pipeline runnable
    with no LLM.
    """
    settings = settings or get_settings()
    if settings.llm_base_url:
        from thesis_matchmaker.llm import LLMClient
        from thesis_matchmaker.synthesis.llm import LLMSynthesizer

        client = LLMClient(settings.llm_base_url, settings.llm_model, settings.llm_api_key)
        return LLMSynthesizer(client)
    return TemplateSynthesizer()


__all__ = ["Synthesizer", "TemplateSynthesizer", "build_synthesizer"]
