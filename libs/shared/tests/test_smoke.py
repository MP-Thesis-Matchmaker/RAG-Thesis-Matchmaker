"""Smoke tests: the package imports and config loads with sane defaults."""

from themis_shared import __version__
from themis_shared.config import Settings, get_settings


def test_version_is_set():
    assert __version__


def test_settings_load_with_defaults():
    settings = get_settings()
    assert settings.database_url
    assert settings.matcher_base_url is None


def test_the_shared_floor_holds_only_what_more_than_one_member_reads():
    """A regression guard on the split, not a restatement of the class.

    Every other knob belongs to the member that reads it -- MatcherSettings,
    GatewaySettings, ZoraSettings, ScraperSettings. A field added here is a field
    every member inherits, so adding one should be a deliberate act that fails
    this test first.
    """
    assert set(Settings.model_fields) == {"database_url", "matcher_base_url"}
