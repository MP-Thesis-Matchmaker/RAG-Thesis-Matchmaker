"""Tests for the index build: load JSONL, diff, embed, upsert, manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thesis_matchmaker.contracts import ThesisPosting, ZoraPublication
from thesis_matchmaker.indexing.embedder import HashEmbedder
from thesis_matchmaker.indexing.indexer import Indexer, ModelMismatchError
from thesis_matchmaker.indexing.sources import JsonlSourceReader
from thesis_matchmaker.indexing.store import InMemoryVectorStore


def _write_sources(
    sources: Path, publications: list[ZoraPublication], postings: list[ThesisPosting]
) -> None:
    sources.mkdir(parents=True, exist_ok=True)
    (sources / "publications.jsonl").write_text(
        "".join(p.model_dump_json() + "\n" for p in publications)
    )
    (sources / "theses.jsonl").write_text("".join(t.model_dump_json() + "\n" for t in postings))


def _publication(
    pub_id: str = "zora:1", abstract: str = "We study dense retrieval."
) -> ZoraPublication:
    return ZoraPublication(id=pub_id, title=f"Paper {pub_id}", abstract=abstract)


def _posting(post_id: str = "posting:1") -> ThesisPosting:
    return ThesisPosting(id=post_id, title=f"Thesis {post_id}", url=f"https://uzh.ch/{post_id}")


@pytest.fixture()
def store() -> InMemoryVectorStore:
    """One store shared across the runs in a test, standing in for one database."""
    return InMemoryVectorStore()


def _indexer(store: InMemoryVectorStore) -> Indexer:
    return Indexer(embedder=HashEmbedder(), store=store)


def test_fresh_build_embeds_everything(tmp_path: Path, store: InMemoryVectorStore) -> None:
    _write_sources(tmp_path / "src", [_publication()], [_posting()])
    result = _indexer(store).run(JsonlSourceReader(tmp_path / "src"))
    assert result.embedded == 2
    assert result.skipped == 0
    assert result.deleted == 0


def test_rerun_embeds_nothing(tmp_path: Path, store: InMemoryVectorStore) -> None:
    _write_sources(tmp_path / "src", [_publication()], [_posting()])
    _indexer(store).run(JsonlSourceReader(tmp_path / "src"))
    result = _indexer(store).run(JsonlSourceReader(tmp_path / "src"))
    assert result.embedded == 0
    assert result.skipped == 2


def test_changed_record_reembedded(tmp_path: Path, store: InMemoryVectorStore) -> None:
    _write_sources(tmp_path / "src", [_publication()], [_posting()])
    _indexer(store).run(JsonlSourceReader(tmp_path / "src"))
    _write_sources(tmp_path / "src", [_publication(abstract="Different now.")], [_posting()])
    result = _indexer(store).run(JsonlSourceReader(tmp_path / "src"))
    assert result.embedded == 1
    assert result.skipped == 1


def test_removed_record_deleted(tmp_path: Path, store: InMemoryVectorStore) -> None:
    _write_sources(tmp_path / "src", [_publication(), _publication("zora:2")], [_posting()])
    _indexer(store).run(JsonlSourceReader(tmp_path / "src"))
    _write_sources(tmp_path / "src", [_publication()], [_posting()])
    result = _indexer(store).run(JsonlSourceReader(tmp_path / "src"))
    assert result.deleted == 1


def test_malformed_lines_skipped_not_fatal(tmp_path: Path, store: InMemoryVectorStore) -> None:
    sources = tmp_path / "src"
    _write_sources(sources, [_publication()], [])
    with (sources / "publications.jsonl").open("a") as f:
        f.write("{not valid json\n")
        f.write(json.dumps({"title": "missing required id"}) + "\n")
    result = _indexer(store).run(JsonlSourceReader(sources))
    assert result.embedded == 1
    assert result.invalid_lines == 2


def test_manifest_written_and_model_guarded(tmp_path: Path, store: InMemoryVectorStore) -> None:
    _write_sources(tmp_path / "src", [_publication()], [_posting()])
    _indexer(store).run(JsonlSourceReader(tmp_path / "src"))

    manifest = store.read_manifest()
    assert manifest is not None
    assert manifest.embedding_model == "hash-fake"
    assert manifest.document_count == 2

    mismatched = Indexer(embedder=_RenamedEmbedder(), store=store)
    with pytest.raises(ModelMismatchError):
        mismatched.run(JsonlSourceReader(tmp_path / "src"))


def test_changed_vector_width_is_a_migration_not_a_rebuild(
    tmp_path: Path, store: InMemoryVectorStore
) -> None:
    """A narrower model does not fit `vector(n)`, so it must be refused up front."""
    _write_sources(tmp_path / "src", [_publication()], [_posting()])
    _indexer(store).run(JsonlSourceReader(tmp_path / "src"))

    narrower = Indexer(embedder=HashEmbedder(dim=32), store=store)
    with pytest.raises(ModelMismatchError, match="migration"):
        narrower.run(JsonlSourceReader(tmp_path / "src"))


class _RenamedEmbedder(HashEmbedder):
    @property
    def model_name(self) -> str:
        return "some-other-model"


class _CappedEmbedder(HashEmbedder):
    """Same model name and width, different token window."""

    @property
    def max_seq_length(self) -> int | None:
        return 512


class _TruncatingEmbedder(HashEmbedder):
    """Reports one truncated document per call, so the counter can be checked."""

    @property
    def last_truncated(self) -> int:
        return 1


class _FailsOnSecondChunk(InMemoryVectorStore):
    """Dies mid-run, so the committed-so-far guarantee can be checked."""

    def __init__(self) -> None:
        super().__init__()
        self.upserts = 0

    def upsert(self, documents, vectors) -> None:
        self.upserts += 1
        if self.upserts == 2:
            raise RuntimeError("connection lost")
        super().upsert(documents, vectors)


def test_chunk_size_does_not_change_the_outcome(tmp_path: Path) -> None:
    """Chunking is a memory bound, not a semantic one, so the counts must match."""
    publications = [_publication(f"zora:{i}") for i in range(5)]
    _write_sources(tmp_path / "src", publications, [_posting()])

    outcomes = []
    for chunk_size in (1, 2, 1000):
        fresh = InMemoryVectorStore()
        result = Indexer(embedder=HashEmbedder(), store=fresh, chunk_size=chunk_size).run(
            JsonlSourceReader(tmp_path / "src")
        )
        outcomes.append((result.embedded, result.skipped, result.deleted))
        assert len(fresh.existing_hashes()) == 6
    assert outcomes == [(6, 0, 0)] * 3


def test_invalid_lines_counted_after_the_stream_is_drained(
    tmp_path: Path, store: InMemoryVectorStore
) -> None:
    """A reader counts bad lines as it yields, so the total is only final at the end."""
    sources = tmp_path / "src"
    _write_sources(sources, [_publication()], [])
    with (sources / "publications.jsonl").open("a") as f:
        f.write("{not valid json\n")

    result = Indexer(embedder=HashEmbedder(), store=store, chunk_size=1).run(
        JsonlSourceReader(sources)
    )
    assert result.embedded == 1
    assert result.invalid_lines == 1


def test_removed_records_still_detected_when_chunked(tmp_path: Path) -> None:
    """Deletion needs the complete id set, which only exists after the last chunk."""
    store = InMemoryVectorStore()
    _write_sources(tmp_path / "src", [_publication(f"zora:{i}") for i in range(4)], [])
    Indexer(embedder=HashEmbedder(), store=store, chunk_size=1).run(
        JsonlSourceReader(tmp_path / "src")
    )
    _write_sources(tmp_path / "src", [_publication("zora:0")], [])
    result = Indexer(embedder=HashEmbedder(), store=store, chunk_size=1).run(
        JsonlSourceReader(tmp_path / "src")
    )
    assert result.deleted == 3


def test_truncated_is_summed_across_chunks(tmp_path: Path, store: InMemoryVectorStore) -> None:
    _write_sources(tmp_path / "src", [_publication(f"zora:{i}") for i in range(4)], [])
    result = Indexer(embedder=_TruncatingEmbedder(), store=store, chunk_size=2).run(
        JsonlSourceReader(tmp_path / "src")
    )
    assert result.embedded == 4
    assert result.truncated == 2  # two chunks, one reported truncation each
    manifest = store.read_manifest()
    assert manifest is not None
    assert manifest.truncated_docs == 2


def test_interrupted_run_keeps_earlier_chunks_and_resumes(tmp_path: Path) -> None:
    """The resumability store.py documents, which the eager build could not deliver."""
    _write_sources(tmp_path / "src", [_publication(f"zora:{i}") for i in range(4)], [])
    store = _FailsOnSecondChunk()

    with pytest.raises(RuntimeError, match="connection lost"):
        Indexer(embedder=HashEmbedder(), store=store, chunk_size=2).run(
            JsonlSourceReader(tmp_path / "src")
        )
    assert len(store.existing_hashes()) == 2

    resumed = Indexer(embedder=HashEmbedder(), store=store, chunk_size=2).run(
        JsonlSourceReader(tmp_path / "src")
    )
    assert resumed.embedded == 2  # only the half that never landed
    assert resumed.skipped == 2


def test_changed_token_window_is_refused(tmp_path: Path, store: InMemoryVectorStore) -> None:
    """The window changes every vector but no content hash, so a re-index would skip all."""
    _write_sources(tmp_path / "src", [_publication()], [])
    _indexer(store).run(JsonlSourceReader(tmp_path / "src"))

    with pytest.raises(ModelMismatchError, match="token window"):
        Indexer(embedder=_CappedEmbedder(), store=store).run(JsonlSourceReader(tmp_path / "src"))
