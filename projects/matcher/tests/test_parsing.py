"""Tests for the query parser (offline rule-based path and the factory)."""

import pytest

from themis_matcher.parsing import RuleBasedExtractor, build_extractor
from themis_shared.config import Settings
from themis_shared.contracts import DegreeLevel


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
    from themis_matcher.parsing.openai_compat import OpenAICompatExtractor

    extractor = build_extractor(
        Settings(llm_base_url="http://localhost:11434/v1", llm_model="llama3.1")
    )
    assert isinstance(extractor, OpenAICompatExtractor)


# Every pair here was produced by the parser bugs recorded in docs/example-run.md
# or found while fixing them. Left column is what a student types; right column is
# what the topics must be. See parsing/rule_based.py for why each case is what it
# is.
@pytest.mark.parametrize(
    ("query", "topics"),
    [
        # Filler removal leaves grammatical glue behind; edge-trimming takes it.
        (
            "I am interested in doing a master's thesis on machine learning for medical images",
            ["machine learning for medical images"],
        ),
        (
            "I want a master's thesis on multilingual embeddings and machine translation",
            ["multilingual embeddings", "machine translation"],
        ),
        # "looking for a phd on" is in no filler phrase; the glue pass handles it.
        ("Looking for a PhD on misinformation detection", ["misinformation detection"]),
        ("I'd like to do a project on RAG", ["RAG"]),
        ("Bachelor thesis in the area of computer vision", ["computer vision"]),
        # "/" joins a compound, so it must not split.
        ("Something about AI/ML in healthcare", ["AI/ML in healthcare"]),
        # Two-letter topics survive the length filter.
        ("AI and knowledge graphs", ["AI", "knowledge graphs"]),
        # Interior glue is part of the phrase, not debris.
        ("internet of things", ["internet of things"]),
        (
            "philosophy of mind and theory of computation",
            ["philosophy of mind", "theory of computation"],
        ),
        # A plain topical query passes through untouched.
        (
            "machine learning for medical image analysis",
            ["machine learning for medical image analysis"],
        ),
        ("information retrieval and ranking", ["information retrieval", "ranking"]),
    ],
)
def test_rule_based_topics(query: str, topics: list[str]) -> None:
    assert RuleBasedExtractor().extract(query).topics == topics


def test_rule_based_preserves_query_casing():
    """Topics keep the source casing, so acronyms stay readable and the topics
    embedded at query time look like the corpus rather than lowercased text."""
    assert RuleBasedExtractor().extract("I want to work on NLP and RAG").topics == ["NLP", "RAG"]


def test_rule_based_degree_survives_topic_trimming():
    """The degree words are trimmed out of the topics but read from the raw text,
    so detection cannot be weakened by the trimming."""
    q = RuleBasedExtractor().extract("Looking for a PhD on misinformation detection")
    assert q.degree_level is DegreeLevel.phd
    assert q.topics == ["misinformation detection"]


def test_rule_based_blank_query_yields_no_topic():
    """An empty string is not a topic; the old fallback produced [''], which then
    got embedded as an empty query vector."""
    assert RuleBasedExtractor().extract("   ").topics == []
