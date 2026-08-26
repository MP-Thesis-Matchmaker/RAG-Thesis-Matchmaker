"""Tests for the embedding seam, exercised through the deterministic fake."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from themis_matcher.indexing.embedder import (
    HashEmbedder,
    SentenceTransformerEmbedder,
    _limit_thread_pools,
    cpu_limit,
)


def test_hash_embedder_is_deterministic() -> None:
    e = HashEmbedder()
    assert e.embed_documents(["same text"]) == e.embed_documents(["same text"])


def test_hash_embedder_distinguishes_texts() -> None:
    e = HashEmbedder()
    a, b = e.embed_documents(["dense retrieval", "medieval history"])
    assert a != b


def test_hash_embedder_dimensions_consistent() -> None:
    e = HashEmbedder(dim=32)
    vectors = e.embed_documents(["one", "two"])
    assert all(len(v) == 32 for v in vectors)
    assert len(e.embed_query("query")) == 32


def test_query_and_document_share_vector_space() -> None:
    e = HashEmbedder()
    assert e.embed_query("dense retrieval") == e.embed_documents(["dense retrieval"])[0]


def test_model_name_reported() -> None:
    assert HashEmbedder().model_name == "hash-fake"


# --- cpu_limit -------------------------------------------------------------
#
# These cover the real embedder without needing torch or a model download: the
# quota is read from files, so a fake cgroup tree is enough. Worth covering
# because os.cpu_count() and os.sched_getaffinity() both ignore a CFS quota, so
# this function is the only thing standing between a 2-core pod and torch
# starting one thread per node core.


def _v2(tmp_path: Path, contents: str) -> Path:
    (tmp_path / "cpu.max").write_text(contents)
    return tmp_path


def _v1(tmp_path: Path, quota: str, period: str = "100000") -> Path:
    cpu = tmp_path / "cpu"
    cpu.mkdir()
    (cpu / "cpu.cfs_quota_us").write_text(quota)
    (cpu / "cpu.cfs_period_us").write_text(period)
    return tmp_path


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("200000 100000", 2),  # what `--cpus 2` and `limits.cpu: 2` actually write
        ("400000 100000\n", 4),
        ("100000 100000", 1),
        ("50000 100000", 1),  # cpu: 500m floors to one thread, never zero
        ("max 100000", None),  # v2 spelling of "no limit"
    ],
)
def test_cpu_limit_reads_cgroup_v2(tmp_path: Path, contents: str, expected: int | None) -> None:
    assert cpu_limit(_v2(tmp_path, contents)) == expected


@pytest.mark.parametrize(
    ("quota", "expected"),
    [("200000", 2), ("-1", None)],  # v1 spells "no limit" as -1
)
def test_cpu_limit_reads_cgroup_v1(tmp_path: Path, quota: str, expected: int | None) -> None:
    assert cpu_limit(_v1(tmp_path, quota)) == expected


def test_cpu_limit_prefers_v2_when_both_exist(tmp_path: Path) -> None:
    _v1(tmp_path, "800000")
    _v2(tmp_path, "200000 100000")
    assert cpu_limit(tmp_path) == 2


def test_cpu_limit_none_when_no_cgroup_files(tmp_path: Path) -> None:
    assert cpu_limit(tmp_path) is None


@pytest.mark.parametrize("contents", ["", "garbage", "notanumber 100000", "200000 0"])
def test_cpu_limit_none_on_unparseable_input(tmp_path: Path, contents: str) -> None:
    """An unreadable quota must not be guessed at: None means "leave torch alone"."""
    assert cpu_limit(_v2(tmp_path, contents)) is None


def test_limit_thread_pools_respects_an_explicit_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who set OMP_NUM_THREADS outranks anything we infer."""
    monkeypatch.setenv("OMP_NUM_THREADS", "7")
    _limit_thread_pools()
    assert os.environ["OMP_NUM_THREADS"] == "7"


def test_hash_embedder_has_no_token_window() -> None:
    """It hashes every token it is given, so there is no window to fall out of."""
    embedder = HashEmbedder()
    assert embedder.max_seq_length is None
    embedder.embed_documents(["a much longer document " * 500])
    assert embedder.last_truncated == 0


class _FakeSentenceTransformer:
    """Records how it was constructed; no torch, no weights, no download."""

    constructed: list[dict] = []

    def __init__(self, model_name, device=None, **kwargs) -> None:
        type(self).constructed.append({"model_name": model_name, "device": device})
        self.max_seq_length = 8192


def _stub_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> type:
    """Install a fake `sentence_transformers` module for the lazy import in _load."""
    module = types.ModuleType("sentence_transformers")
    fake = type("_Recorded", (_FakeSentenceTransformer,), {"constructed": []})
    module.SentenceTransformer = fake
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return fake


def test_device_reaches_the_model_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """EMBEDDING_DEVICE=cpu is the escape hatch from an MPS abort.

    On a Mac sentence-transformers auto-detects mps, and moving bge-m3's weights
    into unified memory aborts the process (SIGABRT, no traceback) when the
    machine is short of memory. Nothing can catch that, so choosing the device
    has to work.
    """
    fake = _stub_sentence_transformers(monkeypatch)
    SentenceTransformerEmbedder("BAAI/bge-m3", device="cpu")._load()
    assert fake.constructed == [{"model_name": "BAAI/bge-m3", "device": "cpu"}]


def test_no_device_configured_leaves_auto_detect_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """None is sentence-transformers' own "auto-detect", i.e. today's behaviour."""
    fake = _stub_sentence_transformers(monkeypatch)
    SentenceTransformerEmbedder("BAAI/bge-m3")._load()
    assert fake.constructed[0]["device"] is None
