"""What MatcherSettings reads from the environment, and what it refuses to.

These assert the *shape* of the configuration rather than its values: which
environment variable reaches which field. Defaults are asserted where the value
is load-bearing elsewhere (the 1024 token cap is baked into the vector column
width) and left alone otherwise.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from themis_matcher.config import MatcherSettings


def test_the_prefix_is_what_the_environment_has_to_say(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATCHER_EMBEDDING_MODEL", "hash-fake")
    assert MatcherSettings(_env_file=None).embedding_model == "hash-fake"


def test_an_unprefixed_name_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rename has to actually take effect, and its failure mode is silent.

    `extra="ignore"` means a stale `EMBEDDING_MODEL` left over from before the
    split is not an error -- it is simply not read. Asserting that here is what
    stops the two spellings quietly both appearing to work.
    """
    monkeypatch.delenv("MATCHER_EMBEDDING_MODEL", raising=False)
    monkeypatch.setenv("EMBEDDING_MODEL", "hash-fake")
    assert MatcherSettings(_env_file=None).embedding_model == "BAAI/bge-m3"


def test_the_dsn_stays_unprefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression this file exists for.

    `env_prefix` re-prefixes inherited fields, so without the `validation_alias`
    pinned in themis_shared.config this class would read MATCHER_DATABASE_URL --
    and fall back to the localhost default in every container, where compose and
    the k8s Secret both set DATABASE_URL.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/unprefixed")
    monkeypatch.setenv("MATCHER_DATABASE_URL", "postgresql://u@h/prefixed")
    assert MatcherSettings(_env_file=None).database_url == "postgresql://u@h/unprefixed"


def test_matcher_base_url_stays_unprefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATCHER_BASE_URL", "http://matcher-api:8100")
    assert MatcherSettings(_env_file=None).matcher_base_url == "http://matcher-api:8100"


def test_an_unknown_ranking_strategy_is_refused_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must fail loudly, not silently select a strategy nobody asked for."""
    monkeypatch.setenv("MATCHER_RETRIEVAL_RANKING_STRATEGY", "uzh-first")
    with pytest.raises(ValidationError, match="uzh_first"):
        MatcherSettings(_env_file=None)


def test_the_token_cap_default_is_the_one_the_schema_was_built_for() -> None:
    """1024 is measured, and document.embedding is vector(1024). Not a free knob."""
    assert MatcherSettings(_env_file=None).embedding_max_seq_length == 1024
