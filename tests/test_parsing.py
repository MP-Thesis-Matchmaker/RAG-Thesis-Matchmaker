"""Tests for the query parser (offline rule-based path and the factory)."""

from thesis_matchmaker.config import Settings
from thesis_matchmaker.contracts import DegreeLevel
from thesis_matchmaker.parsing import RuleBasedExtractor, build_extractor


def test_rule_based_detects_master():
    q = RuleBasedExtractor().extract("I want a master's thesis in NLP on RAG")
    assert q.degree_level is DegreeLevel.master
    assert q.raw_query == "I want a master's thesis in NLP on RAG"
    assert q.topics


def test_rule_based_detects_phd():
    q = RuleBasedExtractor().extract("Looking for a PhD on misinformation detection")
    assert q.degree_level is DegreeLevel.phd


def test_rule_based_no_degree_is_none():
    q = RuleBasedExtractor().extract("information retrieval and ranking")
    assert q.degree_level is None
    assert q.topics


def test_build_extractor_falls_back_without_endpoint():
    extractor = build_extractor(Settings(llm_base_url=None))
    assert isinstance(extractor, RuleBasedExtractor)


def test_build_extractor_uses_llm_when_endpoint_set():
    from thesis_matchmaker.parsing.openai_compat import OpenAICompatExtractor

    extractor = build_extractor(
        Settings(llm_base_url="http://localhost:11434/v1", llm_model="llama3.1")
    )
    assert isinstance(extractor, OpenAICompatExtractor)
