"""Tests for the orchestration skeleton against fakes for both boundaries."""

from thesis_matchmaker.parsing import RuleBasedExtractor
from thesis_matchmaker.pipeline import Pipeline, parse_query
from thesis_matchmaker.retrieval import FakeRetriever


def test_parse_query_keeps_raw_text():
    q = parse_query("nlp thesis on rag")
    assert q.raw_query == "nlp thesis on rag"
    assert q.topics


def test_pipeline_runs_and_is_sorted():
    pipeline = Pipeline(FakeRetriever(), RuleBasedExtractor())
    matches = pipeline.run("nlp thesis on rag", top_k=2)
    assert len(matches) == 2
    assert matches[0].score >= matches[1].score
