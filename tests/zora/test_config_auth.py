"""
Token resolution for the ZORA API.

The token is the one credential the harvester cannot run without, and the
resolution order is a promise made in four places (docker-compose.yml,
.env.example, docs/deployment.md, the package README) — so it is pinned here.
"""

from __future__ import annotations

import pytest

from thesis_matchmaker.zora import config


@pytest.fixture(autouse=True)
def _clear_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Unset both variables for every test in this module. Without this a
    developer with a real token exported in their shell would see different
    results than CI, and the "nothing configured" case would never fail.
    """
    monkeypatch.delenv(config.ENV_API_KEY_FILE, raising=False)
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)


def test_reads_token_from_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token.secret"
    token_file.write_text("file-token", encoding="utf-8")
    monkeypatch.setenv(config.ENV_API_KEY_FILE, str(token_file))

    assert config.resolve_api_token() == "file-token"


def test_reads_token_from_inline_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ENV_API_KEY, "inline-token")

    assert config.resolve_api_token() == "inline-token"


def test_file_takes_precedence_over_inline(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token.secret"
    token_file.write_text("file-token", encoding="utf-8")
    monkeypatch.setenv(config.ENV_API_KEY_FILE, str(token_file))
    monkeypatch.setenv(config.ENV_API_KEY, "inline-token")

    assert config.resolve_api_token() == "file-token"


def test_surrounding_whitespace_is_stripped(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token written with `echo` or an editor carries a trailing newline."""
    token_file = tmp_path / "token.secret"
    token_file.write_text("  file-token\n", encoding="utf-8")
    monkeypatch.setenv(config.ENV_API_KEY_FILE, str(token_file))

    assert config.resolve_api_token() == "file-token"

    monkeypatch.delenv(config.ENV_API_KEY_FILE)
    monkeypatch.setenv(config.ENV_API_KEY, "inline-token\n")

    assert config.resolve_api_token() == "inline-token"


def test_missing_file_raises_and_does_not_fall_back(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that is set but broken is a misconfiguration, not a fallback."""
    missing = tmp_path / "nope.secret"
    monkeypatch.setenv(config.ENV_API_KEY_FILE, str(missing))
    monkeypatch.setenv(config.ENV_API_KEY, "inline-token")

    with pytest.raises(RuntimeError, match="could not be read"):
        config.resolve_api_token()


def test_empty_file_raises(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token.secret"
    token_file.write_text("\n", encoding="utf-8")
    monkeypatch.setenv(config.ENV_API_KEY_FILE, str(token_file))

    with pytest.raises(RuntimeError, match="is empty"):
        config.resolve_api_token()


def test_nothing_configured_raises_naming_both_vars() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        config.resolve_api_token()

    message = str(excinfo.value)
    assert config.ENV_API_KEY_FILE in message
    assert config.ENV_API_KEY in message
