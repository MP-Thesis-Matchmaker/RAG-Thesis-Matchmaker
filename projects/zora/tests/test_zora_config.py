"""What ZoraSettings reads, and -- more to the point -- what it refuses to read.

The token-resolution contract has its own module (test_config_auth.py). This one
covers the shape: which variable reaches which field, and which values are fixed
in source and unreachable from the environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from themis_zora.config import ZoraSettings


def test_the_data_dir_is_read_per_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The bug the pydantic move fixes.

    ZORA_DATA_DIR used to be resolved by an os.environ.get at import time, so
    monkeypatch.setenv could not reach it and three test modules patched the
    derived RAW_DIR constant by attribute instead.
    """
    monkeypatch.setenv("ZORA_DATA_DIR", str(tmp_path))
    assert ZoraSettings(_env_file=None).raw_dir == tmp_path / "raw"


def test_the_api_origin_cannot_be_moved_by_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requirement this refactor exists to make enforceable.

    A harvest pointed at another DSpace would write that server's records into
    `publication` under the same provenance, with nothing in the logs saying so.
    ClassVar is what makes that impossible rather than merely discouraged:
    pydantic registers no field, so there is no name for .env to set. Both the
    obvious spelling and the DSPACE_API_ENDPOINT override this used to honour are
    asserted dead.
    """
    monkeypatch.setenv("ZORA_DSPACE_API_URL", "https://evil.example/server/api")
    monkeypatch.setenv("DSPACE_API_ENDPOINT", "https://evil.example/server/api")
    assert ZoraSettings(_env_file=None).ZORA_DSPACE_API_URL == "https://www.zora.uzh.ch/server/api"


def test_the_fixed_facts_are_not_fields() -> None:
    """`Field(frozen=True)` would not do this -- it still loads from the environment."""
    fields = set(ZoraSettings.model_fields)
    for name in (
        "ZORA_DSPACE_API_URL",
        "ZORA_ROOT_COMMUNITY_UUID",
        "ZORA_PUBLICATIONS_COLLECTION_PREFIX",
        "ZORA_SCOPE_UUID",
        "ZORA_MIN_RETENTION_RATIO",
    ):
        assert name not in fields, f"{name} is env-settable and should not be"
        assert getattr(ZoraSettings, name) is not None or name == "ZORA_SCOPE_UUID"


def test_a_blank_token_variable_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env.example ships both declared and empty, which must not read as a path.

    Without the before-validator, `ZORA_UZH_API_KEY_FILE=` coerces to Path("."),
    and token resolution then fails on a directory instead of falling through to
    the inline value.
    """
    monkeypatch.setenv("ZORA_UZH_API_KEY_FILE", "")
    monkeypatch.setenv("ZORA_UZH_API_KEY", "inline-token")
    assert ZoraSettings(_env_file=None).api_token == "inline-token"


def test_the_dsn_stays_unprefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/unprefixed")
    monkeypatch.setenv("ZORA_DATABASE_URL", "postgresql://u@h/prefixed")
    assert ZoraSettings(_env_file=None).database_url == "postgresql://u@h/unprefixed"
