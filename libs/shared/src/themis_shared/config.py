"""Application settings, loaded from environment variables and an optional .env file.

This is the floor every member stands on, and it is deliberately tiny: a field
belongs here only if **more than one member reads it**. Anything read by exactly
one member lives in that member's own `config.py`, subclassing this class --
`themis_matcher.config.MatcherSettings`, `themis_gateway.config.GatewaySettings`,
`themis_zora.config.ZoraSettings`, `themis_scraper.config.ScraperSettings`.

Those subclasses carry a `MATCHER_` / `GATEWAY_` / `ZORA_` / `SCRAPER_` prefix.
The fields here stay unprefixed on purpose: `DATABASE_URL` is the spelling the
k8s Secret key, the CI job, docker-compose, conftest.py and scripts/check.sh
already use, and neither name is ambiguous about which component it belongs to.

That is not free, and the mechanism matters. `env_prefix` on a subclass
re-prefixes **inherited** fields too -- without the explicit `validation_alias`
below, `MatcherSettings` would look for `MATCHER_DATABASE_URL` and quietly fall
back to the localhost default while docker-compose was setting `DATABASE_URL`.
An alias declared here is inherited as part of the FieldInfo and wins over any
subclass prefix, so it is stated once rather than restated in four members.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """The settings shared by more than one workspace member.

    Values are read from environment variables first, then from a local .env
    file. See .env.example for the full list. .env is gitignored, so keys and
    local paths stay off the repo.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # An explicit validation_alias makes the alias the only accepted keyword
        # unless this is on -- so `Settings(database_url=...)` would be silently
        # dropped by extra="ignore" and fall back to the default. Every subclass
        # inherits this, and the test fixtures across the workspace construct by
        # field name.
        populate_by_name=True,
    )

    # Postgres holding both the vector index and the harvested source rows.
    # pgvector is a decided constraint, not a preference: the deployment target
    # is a UZH Kubernetes cluster against a managed Postgres with the extension
    # available. See docs/deployment.md.
    #
    # Read by all four deployables plus themis-init-db, which is what makes it
    # shared rather than the matcher's.
    database_url: str = Field(
        default="postgresql://matchmaker:matchmaker@localhost:5432/matchmaker",
        validation_alias="DATABASE_URL",
    )

    # Where the matcher's HTTP service lives, for everyone who calls it: the
    # gateway (which has no other way to reach it since the two stopped sharing a
    # process), and the harvester and the scraper, which POST an index trigger
    # when a run lands.
    #
    # None means "not configured": the gateway then has nothing to call and says
    # so, while the producers skip their post-run trigger instead of failing a
    # harvest that otherwise succeeded.
    matcher_base_url: str | None = Field(default=None, validation_alias="MATCHER_BASE_URL")


def get_settings() -> Settings:
    """Return settings, read fresh from the environment."""
    return Settings()
