"""Tests for the app-service functions behind the MCP adapter (offline).

These test the service layer with an injected offline pipeline, so they need
neither the MCP SDK, a built index, nor a network.
"""

from thesis_matchmaker.adapters import service
from thesis_matchmaker.parsing import RuleBasedExtractor
from thesis_matchmaker.pipeline import Pipeline
from thesis_matchmaker.retrieval import FakeRetriever
from thesis_matchmaker.synthesis import TemplateSynthesizer


def _offline_pipeline() -> Pipeline:
    return Pipeline(FakeRetriever(), RuleBasedExtractor(), TemplateSynthesizer())


def test_find_researchers_returns_structured_matches():
    results = service.find_researchers("nlp thesis on rag", top_k=2, pipeline=_offline_pipeline())
    assert isinstance(results, list)
    assert len(results) == 2
    assert "supervisor" in results[0]
    assert isinstance(results[0]["evidence"], list)


def test_recommend_supervisors_returns_text():
    text = service.recommend_supervisors("nlp thesis on rag", top_k=2, pipeline=_offline_pipeline())
    assert isinstance(text, str)
    assert "Prof. A. Müller" in text
