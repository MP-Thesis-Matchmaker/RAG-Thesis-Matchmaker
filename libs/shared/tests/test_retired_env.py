"""The migration notice for the configuration rename.

The whole reason this exists is that the rename's failure mode is silence:
`extra="ignore"` means a stale unprefixed variable is not rejected, it is simply
not read, and the value quietly becomes the default.

The fixtures below are throwaway subclasses rather than the real MatcherSettings
and GatewaySettings, and deliberately so: this module runs in CI's `boundaries`
job, where themis-shared is installed **alone**. Importing another member here
would fail with ModuleNotFoundError -- which is exactly what that job exists to
catch, and it would be catching us.

Delete this module together with `warn_on_unprefixed_env` once everyone's .env
has caught up.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic_settings import SettingsConfigDict

from themis_shared import config
from themis_shared.config import Settings, env_names_set, warn_on_unprefixed_env


class _Prefixed(Settings):
    """Stands in for any member's settings: a prefix over the shared floor."""

    model_config = SettingsConfigDict(
        env_prefix="MEMBER_", env_file=None, extra="ignore", populate_by_name=True
    )
    listen_host: str = "127.0.0.1"


def _reading(env_file: Path) -> type[Settings]:
    """`_Prefixed`, but reading a given env file -- as every real member reads `.env`."""

    class _FromFile(_Prefixed):
        model_config = SettingsConfigDict(
            env_prefix="MEMBER_", env_file=env_file, extra="ignore", populate_by_name=True
        )

    return _FromFile


@pytest.fixture(autouse=True)
def _forget_previous_warnings() -> None:
    """The dedupe set is process-wide, so a test must not depend on run order."""
    config._warned.clear()


def test_a_retired_name_is_reported_with_its_replacement(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LISTEN_HOST", "0.0.0.0")
    with caplog.at_level(logging.WARNING):
        warn_on_unprefixed_env(_Prefixed)

    assert "LISTEN_HOST is set but no longer read" in caplog.text
    assert "use MEMBER_LISTEN_HOST instead" in caplog.text


def test_a_name_retired_with_no_replacement_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`also` is for a variable whose field is gone entirely, not renamed.

    DSPACE_API_ENDPOINT is the real case: the ZORA API origin is a ClassVar now,
    so there is no field to derive an old name from and nothing to point at.
    """
    monkeypatch.setenv("DSPACE_API_ENDPOINT", "https://evil.example/server/api")
    with caplog.at_level(logging.WARNING):
        warn_on_unprefixed_env(_Prefixed, also=["DSPACE_API_ENDPOINT"])

    assert "retired with no replacement" in caplog.text


def test_the_shared_floor_is_never_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DATABASE_URL is meant to be unprefixed, and its validation_alias says so.

    Reporting it would train everyone to ignore this warning, which is the one
    way a migration notice can be worse than nothing.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/db")
    monkeypatch.setenv("MATCHER_BASE_URL", "http://matcher-api:8100")
    monkeypatch.delenv("LISTEN_HOST", raising=False)
    with caplog.at_level(logging.WARNING):
        warn_on_unprefixed_env(_Prefixed)

    assert caplog.text == ""


def test_an_unprefixed_class_reports_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """The shared floor has no prefix, so it has nothing to have renamed."""
    with caplog.at_level(logging.WARNING):
        warn_on_unprefixed_env(Settings)

    assert caplog.text == ""


def test_it_warns_once(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """get_settings() is called per read, and a harvest reads for days."""
    monkeypatch.setenv("LISTEN_HOST", "0.0.0.0")
    with caplog.at_level(logging.WARNING):
        warn_on_unprefixed_env(_Prefixed)
        warn_on_unprefixed_env(_Prefixed)

    assert caplog.text.count("LISTEN_HOST is set") == 1


def test_a_retired_name_in_the_env_file_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The case that used to stay silent: the stale name sits in `.env`.

    pydantic-settings parses its env file itself and never copies it into
    `os.environ`, so a check of the environment alone saw nothing -- while the
    value it was meant to flag went unread all the same.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("LISTEN_HOST=0.0.0.0\n")
    monkeypatch.delenv("LISTEN_HOST", raising=False)
    with caplog.at_level(logging.WARNING):
        warn_on_unprefixed_env(_reading(env_file))

    assert f"LISTEN_HOST is set in {env_file} but no longer read" in caplog.text
    assert "use MEMBER_LISTEN_HOST instead" in caplog.text


def test_an_empty_or_missing_env_file_entry_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Same rule as for the environment: set means set to something."""
    env_file = tmp_path / ".env"
    env_file.write_text("LISTEN_HOST=\n")
    monkeypatch.delenv("LISTEN_HOST", raising=False)
    with caplog.at_level(logging.WARNING):
        warn_on_unprefixed_env(_reading(env_file))
        warn_on_unprefixed_env(_reading(tmp_path / "absent.env"))

    assert caplog.text == ""


def test_env_names_set_reports_names_and_where_never_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warning is logged, so what feeds it must not be able to carry a secret."""
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=sk-not-for-logs\nlower_case=x\nEMPTY=\nBOTH=file\n")
    monkeypatch.setenv("BOTH", "shell")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    names = env_names_set(_reading(env_file))

    assert names["LLM_API_KEY"] == str(env_file)
    assert names["LOWER_CASE"] == str(env_file)
    assert "EMPTY" not in names
    # The environment wins over the file, as it does in pydantic-settings.
    assert names["BOTH"] == ""
    assert "sk-not-for-logs" not in names.values()
