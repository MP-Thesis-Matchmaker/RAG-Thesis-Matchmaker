"""
Token resolution for the ZORA API.

The token is the one credential the harvester cannot run without, and the
resolution order is a promise made in four places (docker-compose.yml,
.env.example, docs/deployment.md, the package README) — so it is pinned here.

`_env_file=None` throughout: the resolution these tests describe is between two
environment variables, and a developer's local .env supplying a third answer
would make the module pass or fail depending on whose machine it ran on.
"""

from __future__ import annotations

import pytest

from themis_zora import config


@pytest.fixture(autouse=True)
def _clear_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Unset both variables for every test in this module. Without this a
    developer with a real token exported in their shell would see different
    results than CI, and the "nothing configured" case would never fail.
    """
    monkeypatch.delenv("ZORA_UZH_API_KEY_FILE", raising=False)
    monkeypatch.delenv("ZORA_UZH_API_KEY", raising=False)


def test_reads_token_from_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token.secret"
    token_file.write_text("file-token", encoding="utf-8")
    monkeypatch.setenv("ZORA_UZH_API_KEY_FILE", str(token_file))

    assert config.ZoraSettings(_env_file=None).api_token == "file-token"


def test_reads_token_from_inline_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZORA_UZH_API_KEY", "inline-token")

    assert config.ZoraSettings(_env_file=None).api_token == "inline-token"


def test_file_takes_precedence_over_inline(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token.secret"
    token_file.write_text("file-token", encoding="utf-8")
    monkeypatch.setenv("ZORA_UZH_API_KEY_FILE", str(token_file))
    monkeypatch.setenv("ZORA_UZH_API_KEY", "inline-token")

    assert config.ZoraSettings(_env_file=None).api_token == "file-token"


def test_surrounding_whitespace_is_stripped(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token written with `echo` or an editor carries a trailing newline."""
    token_file = tmp_path / "token.secret"
    token_file.write_text("  file-token\n", encoding="utf-8")
    monkeypatch.setenv("ZORA_UZH_API_KEY_FILE", str(token_file))

    assert config.ZoraSettings(_env_file=None).api_token == "file-token"

    monkeypatch.delenv("ZORA_UZH_API_KEY_FILE")
    monkeypatch.setenv("ZORA_UZH_API_KEY", "inline-token\n")

    assert config.ZoraSettings(_env_file=None).api_token == "inline-token"


def test_missing_file_raises_and_does_not_fall_back(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that is set but broken is a misconfiguration, not a fallback."""
    missing = tmp_path / "nope.secret"
    monkeypatch.setenv("ZORA_UZH_API_KEY_FILE", str(missing))
    monkeypatch.setenv("ZORA_UZH_API_KEY", "inline-token")

    with pytest.raises(RuntimeError, match="could not be read"):
        _ = config.ZoraSettings(_env_file=None).api_token


def test_empty_file_raises(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token.secret"
    token_file.write_text("\n", encoding="utf-8")
    monkeypatch.setenv("ZORA_UZH_API_KEY_FILE", str(token_file))

    with pytest.raises(RuntimeError, match="is empty"):
        _ = config.ZoraSettings(_env_file=None).api_token


def test_nothing_configured_raises_naming_both_vars() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        _ = config.ZoraSettings(_env_file=None).api_token

    message = str(excinfo.value)
    assert "ZORA_UZH_API_KEY_FILE" in message
    assert "ZORA_UZH_API_KEY" in message
