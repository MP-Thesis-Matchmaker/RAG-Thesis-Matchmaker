"""Tests for the orchestration skeleton against fakes for all boundaries."""

from thesis_matchmaker.parsing import RuleBasedExtractor
from thesis_matchmaker.pipeline import Pipeline, parse_query
from thesis_matchmaker.retrieval import FakeRetriever
from thesis_matchmaker.synthesis import TemplateSynthesizer


def test_parse_query_keeps_raw_text():
    q = parse_query("nlp thesis on rag")
    assert q.raw_query == "nlp thesis on rag"
    assert q.topics


def test_pipeline_runs_and_is_sorted():
    pipeline = Pipeline(FakeRetriever(), RuleBasedExtractor())
    matches = pipeline.run("nlp thesis on rag", top_k=2)
    assert len(matches) == 2
    assert matches[0].score >= matches[1].score


def test_pipeline_recommend_returns_grounded_text():
    pipeline = Pipeline(FakeRetriever(), RuleBasedExtractor(), TemplateSynthesizer())
    answer = pipeline.recommend("nlp thesis on rag", top_k=2)
    assert isinstance(answer, str)
    assert "Prof. A. Müller" in answer
