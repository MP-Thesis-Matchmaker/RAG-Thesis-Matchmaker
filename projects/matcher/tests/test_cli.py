"""Tests for the CLI subcommands, offline (hash-fake embedder, in-memory store).

The store is swapped at the factory rather than pointed at a test database, so
these keep exercising the real CLI wiring -- argument parsing, the index/match
subcommands, the no-index fallback -- with no server running. Both factories are
patched because `build_indexer` and `read_manifest` resolve `build_store` from
the indexing package, while `--rebuild` calls the name the CLI imported.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from themis_matcher import cli, indexing
from themis_matcher.cli import main
from themis_shared.contracts import ThesisPosting, ZoraPublication
from themis_matcher.indexing.store import InMemoryVectorStore


@pytest.fixture()
def offline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = InMemoryVectorStore()
    monkeypatch.setattr(indexing, "build_store", lambda _settings: store)
    monkeypatch.setattr(cli, "build_store", lambda _settings: store)

    sources = tmp_path / "src"
    sources.mkdir()
    (sources / "publications.jsonl").write_text(
        ZoraPublication(
            id="zora:1",
            title="Dense retrieval for German text",
            abstract="Neural search over German corpora.",
            authors=["Prof. A. Müller"],
            uzh_authors=["Prof. A. Müller"],
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


def _feed_input(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Replace input() with a scripted session; EOFError after the last line."""
    it = iter(lines)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(it))


def test_repl_answers_and_exits(
    offline_env: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    main(["index"])
    capsys.readouterr()
    _feed_input(monkeypatch, ["dense retrieval for text", "exit"])
    main(["repl", "--top-k", "3"])
    out = capsys.readouterr().out
    assert "Prof. A. Müller" in out


def test_repl_survives_a_query_error(
    offline_env: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad query prints an error and the session keeps going -- it must not die."""
    main(["index"])
    capsys.readouterr()

    def _boom(self: object, raw_query: str, top_k: int = 5) -> list:
        raise ValueError("retrieval exploded")

    from themis_matcher.pipeline import Pipeline

    monkeypatch.setattr(Pipeline, "run", _boom)
    _feed_input(monkeypatch, ["anything", "exit"])
    main(["repl"])
    out = capsys.readouterr().out
    assert "error: ValueError: retrieval exploded" in out


def test_repl_eof_exits_cleanly(
    offline_env: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-D with no input at all is a normal way to leave, not a crash."""

    def _eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    main(["repl"])
    out = capsys.readouterr().out
    assert "themis-matcher repl" in out
