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

import pytest
from pydantic_settings import SettingsConfigDict

from themis_shared import config
from themis_shared.config import Settings, warn_on_unprefixed_env


class _Prefixed(Settings):
    """Stands in for any member's settings: a prefix over the shared floor."""

    model_config = SettingsConfigDict(
        env_prefix="MEMBER_", env_file=None, extra="ignore", populate_by_name=True
    )
    listen_host: str = "127.0.0.1"


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
