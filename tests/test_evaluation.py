"""Tests for the evaluation harness (offline)."""

import pytest

from thesis_matchmaker.evaluation import GroundTruthQuery, evaluate, load_dataset
from thesis_matchmaker.evaluation.metrics import (
    ndcg_at_k,
    normalize_name,
    recall_at_k,
    reciprocal_rank,
)
from thesis_matchmaker.parsing import RuleBasedExtractor
from thesis_matchmaker.pipeline import Pipeline
from thesis_matchmaker.retrieval import FakeRetriever
from thesis_matchmaker.synthesis import TemplateSynthesizer


def test_name_normalisation_survives_formatting():
    assert normalize_name("Mahl, Daniela") == normalize_name("Prof. Daniela Mahl")
    assert normalize_name("Zeng, Jing") != normalize_name("Zeng, Wei")


def test_ranking_metrics():
    ranked = ["Wrong, One", "Mahl, Daniela", "Other, Person"]
    relevant = ["Mahl, Daniela", "Zeng, Jing"]
    assert recall_at_k(ranked, relevant, 5) == 0.5
    assert reciprocal_rank(ranked, relevant, 5) == 0.5
    assert 0 < ndcg_at_k(ranked, relevant, 5) < 1
    assert reciprocal_rank(["Nobody, Here"], relevant, 5) == 0.0


def test_perfect_ranking_scores_one():
    relevant = ["A, One", "B, Two"]
    assert recall_at_k(relevant, relevant, 5) == 1.0
    assert reciprocal_rank(relevant, relevant, 5) == 1.0
    assert ndcg_at_k(relevant, relevant, 5) == pytest.approx(1.0)


def test_dataset_rejects_inconsistent_rows(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"id": "x", "query": "q", "no_match": true, "relevant_supervisors": ["A, B"]}\n'
    )
    with pytest.raises(ValueError, match="cannot list relevant supervisors"):
        load_dataset(bad)


def test_seed_dataset_loads_and_has_negatives():
    queries = load_dataset("eval/ground_truth.jsonl")
    assert len(queries) >= 5
    assert any(q.no_match for q in queries), "the set must contain no-match cases"
    assert all(q.contributor for q in queries)


def test_evaluate_reports_both_query_types():
    queries = [
        GroundTruthQuery(
            id="a", query="nlp thesis on rag", relevant_supervisors=["Prof. A. Müller"]
        ),
        GroundTruthQuery(id="b", query="history of dentistry", no_match=True),
    ]
    pipeline = Pipeline(FakeRetriever(), RuleBasedExtractor(), TemplateSynthesizer())
    report = evaluate(queries, pipeline, top_k=3)
    summary = report.summary()
    assert summary["answerable"] == 1
    assert summary["no_match"] == 1
    # The fake retriever always returns Müller first, so this one is found.
    assert summary["recall@3"] == 1.0
    # It never abstains, so the no-match query is missed. That is the point of
    # measuring the two separately.
    assert summary["abstention_rate"] == 0.0
    assert "missed" in report.format()


def test_high_min_score_forces_abstention():
    queries = [GroundTruthQuery(id="b", query="history of dentistry", no_match=True)]
    pipeline = Pipeline(FakeRetriever(), RuleBasedExtractor(), TemplateSynthesizer())
    report = evaluate(queries, pipeline, top_k=3, min_score=0.99)
    assert report.summary()["abstention_rate"] == 1.0
