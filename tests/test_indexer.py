"""Tests for the index build: load JSONL, diff, embed, upsert, manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thesis_matchmaker.contracts import ThesisPosting, ZoraRecord
from thesis_matchmaker.indexing.embedder import HashEmbedder
from thesis_matchmaker.indexing.indexer import Indexer, ModelMismatchError
from thesis_matchmaker.indexing.store import ChromaVectorStore


def _write_sources(sources: Path, publications: list[ZoraRecord],
                   postings: list[ThesisPosting]) -> None:
    sources.mkdir(parents=True, exist_ok=True)
    (sources / "publications.jsonl").write_text(
        "".join(p.model_dump_json() + "\n" for p in publications)
    )
    (sources / "theses.jsonl").write_text(
        "".join(t.model_dump_json() + "\n" for t in postings)
    )


def _publication(pub_id: str = "zora:1", abstract: str = "We study dense retrieval.") -> ZoraRecord:
    return ZoraRecord(id=pub_id, title=f"Paper {pub_id}", abstract=abstract)


def _posting(post_id: str = "posting:1") -> ThesisPosting:
    return ThesisPosting(id=post_id, title=f"Thesis {post_id}", url=f"https://uzh.ch/{post_id}")


@pytest.fixture()
def index_dir(tmp_path: Path) -> Path:
    return tmp_path / "index"


def _indexer(index_dir: Path) -> Indexer:
    store = ChromaVectorStore(path=str(index_dir), collection_name="test")
    return Indexer(embedder=HashEmbedder(), store=store, index_path=index_dir)


def test_fresh_build_embeds_everything(tmp_path: Path, index_dir: Path) -> None:
    _write_sources(tmp_path / "src", [_publication()], [_posting()])
    result = _indexer(index_dir).run(tmp_path / "src")
    assert result.embedded == 2
    assert result.skipped == 0
    assert result.deleted == 0


def test_rerun_embeds_nothing(tmp_path: Path, index_dir: Path) -> None:
    _write_sources(tmp_path / "src", [_publication()], [_posting()])
    _indexer(index_dir).run(tmp_path / "src")
    result = _indexer(index_dir).run(tmp_path / "src")
    assert result.embedded == 0
    assert result.skipped == 2


def test_changed_record_reembedded(tmp_path: Path, index_dir: Path) -> None:
    _write_sources(tmp_path / "src", [_publication()], [_posting()])
    _indexer(index_dir).run(tmp_path / "src")
    _write_sources(tmp_path / "src", [_publication(abstract="Different now.")], [_posting()])
    result = _indexer(index_dir).run(tmp_path / "src")
    assert result.embedded == 1
    assert result.skipped == 1


def test_removed_record_deleted(tmp_path: Path, index_dir: Path) -> None:
    _write_sources(tmp_path / "src", [_publication(), _publication("zora:2")], [_posting()])
    _indexer(index_dir).run(tmp_path / "src")
    _write_sources(tmp_path / "src", [_publication()], [_posting()])
    result = _indexer(index_dir).run(tmp_path / "src")
    assert result.deleted == 1


def test_malformed_lines_skipped_not_fatal(tmp_path: Path, index_dir: Path) -> None:
    sources = tmp_path / "src"
    _write_sources(sources, [_publication()], [])
    with (sources / "publications.jsonl").open("a") as f:
        f.write("{not valid json\n")
        f.write(json.dumps({"title": "missing required id"}) + "\n")
    result = _indexer(index_dir).run(sources)
    assert result.embedded == 1
    assert result.invalid_lines == 2


def test_manifest_written_and_model_guarded(tmp_path: Path, index_dir: Path) -> None:
    _write_sources(tmp_path / "src", [_publication()], [_posting()])
    _indexer(index_dir).run(tmp_path / "src")

    manifest = json.loads((index_dir / "manifest.json").read_text())
    assert manifest["embedding_model"] == "hash-fake"
    assert manifest["document_count"] == 2

    store = ChromaVectorStore(path=str(index_dir), collection_name="test")
    mismatched = Indexer(embedder=_RenamedEmbedder(), store=store, index_path=index_dir)
    with pytest.raises(ModelMismatchError):
        mismatched.run(tmp_path / "src")


class _RenamedEmbedder(HashEmbedder):
    @property
    def model_name(self) -> str:
        return "some-other-model"
