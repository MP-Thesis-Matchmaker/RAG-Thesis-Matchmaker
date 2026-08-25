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


def test_no_index_raises_instead_of_serving_fake_matches(monkeypatch):
    """The MCP must never answer askUZH with invented supervisors.

    Before, an unbuilt index quietly fell back to the fake retriever, so a
    misconfigured deployment would have served canned people as if they were
    real. Failing is the only safe answer.
    """
    monkeypatch.setattr(service, "read_manifest", lambda settings: None)
    for call in (
        lambda: service.find_researchers("nlp thesis on rag"),
        lambda: service.recommend_supervisors("nlp thesis on rag"),
    ):
        try:
            call()
        except service.IndexNotBuiltError as exc:
            assert "index" in str(exc).lower()
        else:
            raise AssertionError("expected IndexNotBuiltError, got a result")


def test_real_retriever_is_used_when_an_index_exists(monkeypatch):
    """With an index present the pipeline is built over the real retriever."""
    built: list[str] = []

    monkeypatch.setattr(service, "read_manifest", lambda settings: object())
    monkeypatch.setattr(
        service,
        "build_retriever",
        lambda settings: built.append("real") or FakeRetriever(),
    )
    service.find_researchers("nlp thesis on rag", top_k=1)
    assert built == ["real"], "the service must go through build_retriever, not the fake"
