"""The vector store contract, run against every implementation.

Both implementations have to behave the same way, so the tests are written once
and parametrised. `InMemoryVectorStore` always runs; `PgVectorStore` runs
whenever DATABASE_URL points at a Postgres with pgvector (always in CI).

The in-memory store scans exhaustively, so where the two disagree it is the
approximate index that is wrong -- which is the point of running both.
"""

from __future__ import annotations

import pytest

from thesis_matchmaker.indexing.documents import Document
from thesis_matchmaker.indexing.embedder import HashEmbedder
from thesis_matchmaker.indexing.store import IndexManifest, InMemoryVectorStore, VectorStore


def _doc(doc_id: str, text: str, **metadata) -> Document:
    return Document(id=doc_id, text=text, metadata=metadata, content_hash=f"hash-{text}")


@pytest.fixture(params=["in-memory", "pgvector"])
def store(request: pytest.FixtureRequest) -> VectorStore:
    if request.param == "in-memory":
        return InMemoryVectorStore()
    return request.getfixturevalue("pg_store")


def _seed(store: VectorStore) -> HashEmbedder:
    embedder = HashEmbedder()
    docs = [
        _doc("zora:1", "dense retrieval for german text", source_type="publication", year=2024),
        _doc(
            "posting:1",
            "msc thesis on rag grounding",
            source_type="thesis_posting",
            degree_level="master",
        ),
        _doc(
            "posting:2",
            "phd position in medieval history",
            source_type="thesis_posting",
            degree_level="phd",
        ),
    ]
    store.upsert(docs, embedder.embed_documents([d.text for d in docs]))
    return embedder


def test_query_returns_exact_match_first(store: VectorStore) -> None:
    embedder = _seed(store)
    hits = store.query(embedder.embed_query("dense retrieval for german text"), top_k=3)
    assert hits[0].id == "zora:1"
    assert hits[0].metadata["source_type"] == "publication"


def test_query_respects_metadata_filters(store: VectorStore) -> None:
    embedder = _seed(store)
    hits = store.query(
        embedder.embed_query("anything"),
        top_k=3,
        filters={"source_type": "thesis_posting", "degree_level": "master"},
    )
    assert [h.id for h in hits] == ["posting:1"]


def test_existing_hashes_roundtrip(store: VectorStore) -> None:
    _seed(store)
    hashes = store.existing_hashes()
    assert hashes["zora:1"] == "hash-dense retrieval for german text"
    assert len(hashes) == 3


def test_delete_removes_points(store: VectorStore) -> None:
    _seed(store)
    store.delete(["posting:2"])
    assert "posting:2" not in store.existing_hashes()


def test_upsert_overwrites_same_id(store: VectorStore) -> None:
    embedder = _seed(store)
    updated = _doc("zora:1", "totally new abstract", source_type="publication")
    store.upsert([updated], embedder.embed_documents([updated.text]))
    hashes = store.existing_hashes()
    assert hashes["zora:1"] == "hash-totally new abstract"
    assert len(hashes) == 3


def test_list_and_map_metadata_survive_a_roundtrip(store: VectorStore) -> None:
    """Lists and maps are stored natively; nothing JSON-encodes them any more."""
    embedder = HashEmbedder()
    doc = _doc(
        "zora:9",
        "multi author publication",
        source_type="publication",
        authors=["A. Müller", "X. External"],
        uzh_authors=["A. Müller"],
        author_authority_map={"A. Müller": {"type": "cris", "id": "uuid-1"}, "X. External": None},
        has_uzh_author=True,
    )
    store.upsert([doc], embedder.embed_documents([doc.text]))
    hit = store.query(embedder.embed_query("multi author publication"), top_k=1)[0]
    assert hit.metadata["authors"] == ["A. Müller", "X. External"]
    assert hit.metadata["author_authority_map"] == {
        "A. Müller": {"type": "cris", "id": "uuid-1"},
        "X. External": None,
    }
    assert hit.metadata["has_uzh_author"] is True


def test_selective_filter_still_returns_top_k(store: VectorStore) -> None:
    """A filter that matches a small minority must not silently under-return.

    pgvector applies the WHERE clause after the index scan, so a selective
    filter over one global HNSW graph returns fewer rows than asked for. The
    partial per-source_type indexes in migration 001 are what prevent that.

    Honest limit: at this corpus size Postgres will likely choose a sequential
    scan anyway, so a green result here does not prove the partial indexes work
    at scale. Verify that separately with EXPLAIN ANALYZE against the real
    corpus -- this test guards the contract, not the query plan.
    """
    embedder = HashEmbedder()
    docs = [
        _doc(
            f"zora:{i}", f"publication number {i} about neural retrieval", source_type="publication"
        )
        for i in range(40)
    ]
    docs += [
        _doc(
            f"posting:{i}",
            f"open thesis position {i} on neural retrieval",
            source_type="thesis_posting",
        )
        for i in range(3)
    ]
    store.upsert(docs, embedder.embed_documents([d.text for d in docs]))

    hits = store.query(
        embedder.embed_query("neural retrieval"),
        top_k=3,
        filters={"source_type": "thesis_posting"},
    )
    assert len(hits) == 3
    assert all(h.metadata["source_type"] == "thesis_posting" for h in hits)


def test_manifest_absent_then_written_then_replaced(store: VectorStore) -> None:
    assert store.read_manifest() is None
    store.write_manifest(
        IndexManifest(
            embedding_model="hash-fake",
            embedding_dim=1024,
            document_count=3,
            sources="data/samples",
        )
    )
    manifest = store.read_manifest()
    assert manifest is not None
    assert manifest.embedding_model == "hash-fake"
    assert manifest.document_count == 3

    store.write_manifest(
        IndexManifest(embedding_model="hash-fake", embedding_dim=1024, document_count=7)
    )
    replaced = store.read_manifest()
    assert replaced is not None
    assert replaced.document_count == 7
    assert replaced.sources is None


def test_clear_empties_documents_and_manifest(store: VectorStore) -> None:
    _seed(store)
    store.write_manifest(
        IndexManifest(embedding_model="hash-fake", embedding_dim=1024, document_count=3)
    )
    store.clear()
    assert store.existing_hashes() == {}
    assert store.read_manifest() is None
