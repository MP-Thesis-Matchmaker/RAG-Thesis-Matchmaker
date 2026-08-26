"""Tests for the standalone init-db entry point.

The point of `dbcli` is that it reaches the schema without the matcher's
dependency closure, and that it says the same things `thesis-matchmaker init-db`
said. Both are asserted here; neither was covered before the carve.
"""

from __future__ import annotations

import argparse

import pytest

from thesis_matchmaker import dbcli, schema
from thesis_matchmaker.config import Settings


class _Result:
    """Stand-in for schema.ApplyResult, which is a pydantic model."""

    def __init__(self, *, applied: bool, dropped: list[str]) -> None:
        self.applied = applied
        self.dropped = dropped
        self.fingerprint = "deadbeef"


def _settings() -> Settings:
    return Settings(database_url="postgresql://u@h/db")


def test_run_reports_a_fresh_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(schema, "apply", lambda dsn, reset=False: _Result(applied=True, dropped=[]))

    dbcli.run(_settings(), reset=False)

    assert "schema applied (deadbeef)" in capsys.readouterr().out


def test_run_distinguishes_an_idempotent_re_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Saying "already up to date" is the whole reason init-db is safe to re-run."""
    monkeypatch.setattr(
        schema, "apply", lambda dsn, reset=False: _Result(applied=False, dropped=[])
    )

    dbcli.run(_settings(), reset=False)

    assert "already up to date" in capsys.readouterr().out


def test_run_names_every_table_it_dropped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """--reset destroys data, so what it destroyed is printed rather than counted."""
    monkeypatch.setattr(
        schema,
        "apply",
        lambda dsn, reset=False: _Result(applied=True, dropped=["publication", "document"]),
    )

    dbcli.run(_settings(), reset=True)

    out = capsys.readouterr().out
    assert "dropped table publication" in out
    assert "dropped table document" in out


def test_run_passes_reset_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _apply(dsn: str, *, reset: bool = False) -> _Result:
        seen["dsn"] = dsn
        seen["reset"] = reset
        return _Result(applied=True, dropped=[])

    monkeypatch.setattr(schema, "apply", _apply)

    dbcli.run(_settings(), reset=True)

    assert seen == {"dsn": "postgresql://u@h/db", "reset": True}


def test_a_changed_schema_exits_rather_than_applying_over_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SchemaChangedError is a refusal, not a crash: it becomes a message and an exit."""

    def _apply(dsn: str, *, reset: bool = False) -> _Result:
        raise schema.SchemaChangedError("fingerprint mismatch")

    monkeypatch.setattr(schema, "apply", _apply)

    with pytest.raises(SystemExit) as exc:
        dbcli.run(_settings(), reset=False)

    assert "fingerprint mismatch" in str(exc.value)


def test_the_reset_flag_defaults_to_off() -> None:
    """A parser that defaulted --reset on would silently destroy a harvest."""
    parser = argparse.ArgumentParser()
    dbcli.add_arguments(parser)

    assert parser.parse_args([]).reset is False
    assert parser.parse_args(["--reset"]).reset is True


def test_the_cli_subcommand_and_the_entry_point_do_the_same_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`thesis-matchmaker init-db` must not drift from `thesis-matchmaker-init-db`."""
    from thesis_matchmaker import cli

    calls: list[bool] = []
    monkeypatch.setattr(dbcli, "run", lambda settings, *, reset: calls.append(reset))
    monkeypatch.setattr(cli.db, "close_pools", lambda: None)

    cli.main(["init-db", "--reset"])

    assert calls == [True]
