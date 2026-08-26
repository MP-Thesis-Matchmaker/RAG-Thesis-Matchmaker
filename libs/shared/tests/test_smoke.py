"""Smoke tests: the package imports and config loads with sane defaults."""

from themis_shared import __version__
from themis_shared.config import get_settings


def test_version_is_set():
    assert __version__


def test_settings_load_with_defaults():
    settings = get_settings()
    assert settings.llm_model
    assert settings.embedding_model
