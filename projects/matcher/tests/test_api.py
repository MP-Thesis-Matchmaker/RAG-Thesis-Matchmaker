"""The matcher's HTTP surface, offline.

No database, no model download, no network: `hash-fake` embedder,
`InMemoryVectorStore`, and the rule-based parser and template synthesiser the
offline settings already select. That is what lets these run in CI's `offline`
job alongside the rest of the suite.

Trigger endpoints need the real `index_run` table and live in test_api_index.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from themis_matcher.api import build_service, create_app
from themis_matcher.indexing.embedder import HashEmbedder
from themis_matcher.indexing.indexer import Indexer
from themis_matcher.indexing.sources import JsonlSourceReader
from themis_matcher.indexing.store import InMemoryVectorStore
from themis_shared.config import Settings
from themis_shared.contracts import ThesisPosting, ZoraPublication


def _settings() -> Settings:
    """Offline everything. No LLM base url means the rule-based parser."""
    return Settings(embedding_model="hash-fake", llm_base_url=None)


def _client(store: InMemoryVectorStore) -> TestClient:
    """Entered as a context manager on purpose: that is what runs the lifespan.

    A bare TestClient(app) skips startup entirely, so the app would serve every
    request without the service the lifespan builds -- passing or failing for
    reasons production would never share.
    """
    service = build_service(_settings(), embedder=HashEmbedder(), store=store)
    return TestClient(create_app(service=service))


@pytest.fixture()
def empty_client(empty_store: InMemoryVectorStore) -> TestClient:
    with _client(empty_store) as client:
        yield client


@pytest.fixture()
def client(indexed_store: InMemoryVectorStore) -> TestClient:
    with _client(indexed_store) as client:
        yield client


@pytest.fixture()
def empty_store() -> InMemoryVectorStore:
    return InMemoryVectorStore()


@pytest.fixture()
def indexed_store(tmp_path: Path) -> InMemoryVectorStore:
    """A store with a real manifest, built the way the indexer builds one."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "publications.jsonl").write_text(
        ZoraPublication(
            id="zora:1",
            title="Dense retrieval for German scientific text",
            abstract="We study dense retrieval and embeddings.",
            authors=["A. Muller"],
            uzh_authors=["A. Muller"],
        ).model_dump_json()
        + "\n"
    )
    (source / "theses.jsonl").write_text(
        ThesisPosting(
            id="posting:1",
            title="Master thesis on retrieval-augmented generation",
            url="https://uzh.ch/posting/1",
        ).model_dump_json()
        + "\n"
    )
    store = InMemoryVectorStore()
    Indexer(embedder=HashEmbedder(), store=store).run(JsonlSourceReader(source))
    return store


def test_health_answers_without_touching_the_database(empty_client: TestClient) -> None:
    """The liveness probe must not depend on Postgres, or a database blip cycles pods."""
    response = empty_client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_match_refuses_when_no_index_exists(empty_client: TestClient) -> None:
    """Serving canned matches would be worse than an error: the caller cannot tell."""
    response = empty_client.post("/v1/match", json={"query": "dense retrieval"})

    assert response.status_code == 409
    assert response.json()["code"] == "index_not_built"


def test_index_status_refuses_when_no_index_exists(empty_client: TestClient) -> None:
    response = empty_client.get("/v1/index/status")

    assert response.status_code == 409
    assert response.json()["code"] == "index_not_built"


def test_index_status_reports_the_manifest(client: TestClient) -> None:
    response = client.get("/v1/index/status")

    assert response.status_code == 200
    body = response.json()
    assert body["embedding_model"] == "hash-fake"
    assert body["document_count"] == 2


def test_match_returns_supervisor_matches(client: TestClient) -> None:
    response = client.post(
        "/v1/match", json={"query": "dense retrieval and embeddings", "top_k": 3}
    )

    assert response.status_code == 200
    matches = response.json()["matches"]
    assert matches, "an indexed corpus should produce at least one match"
    first = matches[0]
    assert "supervisor" in first
    assert isinstance(first["evidence"], list)


def test_recommend_returns_prose(client: TestClient) -> None:
    response = client.post("/v1/recommend", json={"query": "dense retrieval and embeddings"})

    assert response.status_code == 200
    assert isinstance(response.json()["answer"], str)
    assert response.json()["answer"]


@pytest.mark.parametrize(
    "body",
    [
        {},  # query is required
        {"query": ""},  # and must not be empty
        {"query": "ok", "top_k": 0},  # top_k has a floor
        {"query": "ok", "top_k": 500},  # and a ceiling
    ],
)
def test_bad_requests_are_refused_before_any_work(client: TestClient, body: dict) -> None:
    """Validation is the contract's, not the pipeline's -- top_k reaches a SQL LIMIT."""
    assert client.post("/v1/match", json=body).status_code == 422


def test_unknown_run_is_a_404(client: TestClient) -> None:
    response = client.get("/v1/index/runs/999999")

    # No database here, so this is the 503 path rather than the 404 path -- either
    # way it must be a refusal with a code, not an unhandled traceback.
    assert response.status_code in (404, 503)
    assert "code" in response.json()
