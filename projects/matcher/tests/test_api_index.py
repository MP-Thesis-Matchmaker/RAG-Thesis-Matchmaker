"""The index trigger endpoints, end to end against a real Postgres.

Separate from test_api.py because the single-active rule is a partial unique
index rather than application code: an in-memory stand-in would be testing the
stand-in. Skips without DATABASE_URL like every other pgvector test.

The embedder is still `hash-fake` -- this exercises the trigger, the background
thread and the run record, not the quality of a vector.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from themis_matcher.api import build_service, create_app
from themis_matcher.config import MatcherSettings
from themis_matcher.indexing.embedder import HashEmbedder
from themis_matcher.indexing.store import PgVectorStore
from themis_shared import db
from themis_shared.contracts import IndexRunKind, IndexRunState, ThesisPosting, ZoraPublication

# Generous: the work is two hash-fake documents, so anything approaching this
# means the thread never ran rather than that it ran slowly.
_RUN_TIMEOUT_S = 30.0


@pytest.fixture()
def sources(tmp_path: Path) -> Path:
    source = tmp_path / "src"
    source.mkdir()
    (source / "publications.jsonl").write_text(
        "\n".join(
            ZoraPublication(
                id=f"zora:{n}", title=f"Paper {n}", abstract="dense retrieval"
            ).model_dump_json()
            for n in (1, 2)
        )
        + "\n"
    )
    (source / "theses.jsonl").write_text(
        ThesisPosting(
            id="posting:1", title="Thesis on RAG", url="https://uzh.ch/posting/1"
        ).model_dump_json()
        + "\n"
    )
    return source


@pytest.fixture()
def client(dsn: str, pg_store: PgVectorStore, sources: Path) -> TestClient:
    with db.connection(dsn) as conn:
        conn.execute("TRUNCATE index_run")
    settings = MatcherSettings(
        _env_file=None,
        database_url=dsn,
        embedding_model="hash-fake",
        llm_base_url=None,
        sources_path=str(sources),
    )
    service = build_service(settings, embedder=HashEmbedder(), store=pg_store)
    with TestClient(create_app(service=service)) as test_client:
        yield test_client
    with db.connection(dsn) as conn:
        conn.execute("TRUNCATE index_run")


def _await_run(client: TestClient, run_id: int) -> dict:
    """Poll until the background thread has finished, or fail loudly."""
    deadline = time.monotonic() + _RUN_TIMEOUT_S
    while time.monotonic() < deadline:
        run = client.get(f"/v1/index/runs/{run_id}").json()
        if run["state"] != IndexRunState.running.value:
            return run
        time.sleep(0.05)
    pytest.fail(f"index run {run_id} was still running after {_RUN_TIMEOUT_S}s")


def test_triggering_publications_indexes_only_publications(client: TestClient) -> None:
    accepted = client.post("/v1/index/publications")

    assert accepted.status_code == 202
    body = accepted.json()
    assert body["kind"] == IndexRunKind.publication.value

    run = _await_run(client, body["run_id"])
    assert run["state"] == IndexRunState.succeeded.value
    assert run["embedded"] == 2  # the two publications, not the posting
    assert run["deleted"] == 0

    status = client.get("/v1/index/status").json()
    assert status["document_count"] == 2


def test_indexing_postings_afterwards_keeps_the_publications(client: TestClient) -> None:
    """The orphan-sweep regression, this time through HTTP.

    Two triggers in sequence is exactly what the harvester and the scraper will
    do, so it is worth pinning at this level too and not only in test_indexer.
    """
    first = client.post("/v1/index/publications").json()
    _await_run(client, first["run_id"])

    second = client.post("/v1/index/postings").json()
    run = _await_run(client, second["run_id"])

    assert run["state"] == IndexRunState.succeeded.value
    assert run["embedded"] == 1
    assert run["deleted"] == 0, "indexing postings must not orphan the publications"
    assert client.get("/v1/index/status").json()["document_count"] == 3


def test_a_second_trigger_is_refused_while_one_is_running(client: TestClient, dsn: str) -> None:
    """409 with the id of the run that holds the slot, not a silently queued second run."""
    with db.connection(dsn) as conn:
        held = conn.execute(
            "INSERT INTO index_run (kind, state, source) VALUES ('publication', 'running', 'test')"
            " RETURNING id"
        ).fetchone()[0]

    refused = client.post("/v1/index/postings")

    assert refused.status_code == 409
    assert refused.json()["code"] == "index_run_in_progress"
    assert refused.json()["run_id"] == held


def test_runs_are_listed_newest_first(client: TestClient) -> None:
    first = client.post("/v1/index/publications").json()
    _await_run(client, first["run_id"])
    second = client.post("/v1/index/postings").json()
    _await_run(client, second["run_id"])

    listed = client.get("/v1/index/runs").json()

    assert [run["id"] for run in listed] == [second["run_id"], first["run_id"]]


def test_unknown_run_is_a_404(client: TestClient) -> None:
    response = client.get("/v1/index/runs/999999")

    assert response.status_code == 404
    assert response.json()["code"] == "no_such_run"
