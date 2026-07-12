"""Tests for the CLI subcommands, offline (hash-fake embedder, temp dirs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from thesis_matchmaker.cli import main
from thesis_matchmaker.contracts import ThesisPosting, ZoraRecord


@pytest.fixture()
def offline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sources = tmp_path / "src"
    sources.mkdir()
    (sources / "publications.jsonl").write_text(
        ZoraRecord(
            id="zora:1",
            title="Dense retrieval for German text",
            abstract="Neural search over German corpora.",
            authors=["Prof. A. Müller"],
        ).model_dump_json()
        + "\n"
    )
    (sources / "theses.jsonl").write_text(
        ThesisPosting(
            id="posting:1",
            title="MSc thesis on dense retrieval",
            supervisor="Prof. A. Müller",
            url="https://uzh.ch/p1",
        ).model_dump_json()
        + "\n"
    )
    monkeypatch.setenv("EMBEDDING_MODEL", "hash-fake")
    monkeypatch.setenv("VECTOR_STORE_PATH", str(tmp_path / "index"))
    monkeypatch.setenv("SOURCES_PATH", str(sources))
    return tmp_path


def test_index_command_reports_counts(offline_env: Path, capsys: pytest.CaptureFixture) -> None:
    main(["index"])
    out = capsys.readouterr().out
    assert "embedded=2" in out


def test_match_command_uses_real_index(offline_env: Path, capsys: pytest.CaptureFixture) -> None:
    main(["index"])
    capsys.readouterr()
    main(["match", "dense retrieval for text", "--top-k", "3"])
    out = capsys.readouterr().out
    assert "Prof. A. Müller" in out


def test_match_without_index_falls_back_to_fake(
    offline_env: Path, capsys: pytest.CaptureFixture
) -> None:
    main(["match", "anything"])
    out = capsys.readouterr().out
    assert "fake retriever" in out
