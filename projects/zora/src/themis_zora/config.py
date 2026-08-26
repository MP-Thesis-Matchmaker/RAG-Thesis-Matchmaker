"""Settings the ZORA harvester owns, layered onto the shared floor.

This used to be a module of bare constants and `os.environ.get` calls -- the one
place in the workspace that did not speak pydantic. That cost real things: no
validation, no `.env`, and `ZORA_DATA_DIR` resolved at *import* time, so a test
could not reach it with `monkeypatch.setenv` and three test modules patched
`config.RAW_DIR` by attribute instead.

`ZORA_`-prefixed, so `data_dir` is `ZORA_DATA_DIR` -- the names the k8s CronJobs
and .env.example already use, unchanged. `database_url` is inherited and stays
unprefixed through the `validation_alias` pinned in `themis_shared.config`.

The DSpace metadata field names moved to `fields.py`: they are a vocabulary
rather than configuration, and nothing reads them from the environment.

**On the ClassVars below.** They are values the harvester needs and callers read
through the settings object, but that must not be reachable from `.env` -- the
ZORA API origin does not vary by deployment, and a run pointed somewhere else by
an environment variable would write the wrong provenance into `publication` with
nothing in the logs to say so. `ClassVar` is what enforces that: pydantic
registers no field for it, so there is no environment name to override and no
`.env` key that does anything. Note that `Field(frozen=True)` is *not* an
alternative -- it blocks assignment after construction but still loads from the
environment -- and a bare `Final` is deprecated for this in pydantic 2.11.
Changing one of these is a source edit and a commit, which is the point.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import field_validator
from pydantic_settings import SettingsConfigDict

from themis_shared.config import Settings

__all__ = ["ZoraSettings", "get_settings"]


class ZoraSettings(Settings):
    """Config for the ZORA harvester."""

    model_config = SettingsConfigDict(
        env_prefix="ZORA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- Paths -----------------------------------------------------------
    # Harvest output and the watermark live in Postgres (see zora/store.py); the
    # only thing still written to disk is the raw-response cache under raw_dir,
    # which keeps ingestion reproducible without re-hitting ZORA.
    #
    # Relative to the working directory, the same choice the rest of the
    # repository makes. Deriving it from the package location would break in the
    # container image, where `uv sync --no-editable` installs into site-packages
    # and there is no repository above the module at all.
    data_dir: Path = Path("data")

    # --- Auth ------------------------------------------------------------
    # The ZORA personal API token, from one of two variables. The file wins when
    # both are set: in the cluster the token arrives as a mounted Secret, so a
    # file is the deployed truth, whereas an inline value is usually a stale
    # export in someone's shell. Resolution happens in `api_token` below.
    uzh_api_key_file: Path | None = None
    uzh_api_key: str | None = None

    @field_validator("uzh_api_key_file", "uzh_api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """An empty variable means "not configured", which pydantic would not infer.

        .env.example ships both of these declared and empty, and the old
        os.environ.get(...).strip() treated that as absent. Without this,
        `ZORA_UZH_API_KEY_FILE=` coerces to Path("."), and the token resolution
        below then fails on a directory instead of falling through to the inline
        value. The strip is here for the same reason it is in `api_token`: an
        editor leaves a trailing newline behind.
        """
        if isinstance(value, str):
            return value.strip() or None
        return value

    # --- Fixed facts about ZORA ------------------------------------------
    # Not settings. See the module docstring on why these are ClassVar.

    # The production DSpace-CRIS entry point. One origin, one corpus; a harvest
    # against anything else is a different dataset wearing the same table.
    ZORA_DSPACE_API_URL: ClassVar[str] = "https://www.zora.uzh.ch/server/api"

    # Root of the UZH community tree: the org structure (faculties, institutes)
    # lives in communities below this node. Walked by zora_client.iter_org_tree
    # for the org_unit mirror; ZORA's OrgUnit entity type is empty upstream.
    ZORA_ROOT_COMMUNITY_UUID: ClassVar[str] = "323725a5-950d-4b89-8765-1b955e305664"

    # Departments are resolved dynamically per item by parsing the
    # owningCollection name (see normalize._get_department). No hardcoded
    # mapping needed -- this covers all 291 departments across every UZH faculty.
    # Each org unit's publications live in a collection named with this prefix;
    # normalize strips it for `publication.department` and uses it to pick the
    # publications collection out of a community's collection list.
    ZORA_PUBLICATIONS_COLLECTION_PREFIX: ClassVar[str] = "Publications of "

    # A community UUID restricts a harvest to a single faculty; None harvests all
    # of ZORA (~238K items). The fallback for `--scope`, not a replacement for it.
    ZORA_SCOPE_UUID: ClassVar[str | None] = None

    # If a harvest run returns dramatically fewer publications than the previous
    # run recorded, something is probably wrong upstream (auth failure returning
    # an empty-but-200 response, scope UUID typo, API outage) rather than the
    # faculty genuinely losing most of its publications overnight. Abort instead
    # of committing a destructive update.
    ZORA_MIN_RETENTION_RATIO: ClassVar[float] = 0.5  # new total >= 50% of previous

    # --- Derived ----------------------------------------------------------

    @property
    def raw_dir(self) -> Path:
        """The raw-response cache. A property so it cannot drift from data_dir."""
        return self.data_dir / "raw"

    @property
    def api_token(self) -> str:
        """The ZORA personal API token, resolved fresh from this settings object.

        Assigned to `DSpaceClient.api_token` in `zora_client.get_client`. The
        vendored client has its own lookup -- PERSONAL_API_TOKEN_FILE, then
        .dspace-personal-api-token.secret in the working and home directories --
        but our assignment overrides it, so those are not part of the contract
        and are not documented anywhere else.

        @raise RuntimeError: if neither variable is set, or if the key file
                             cannot be read or is empty. A broken path fails
                             loudly rather than falling back to the inline token:
                             authenticating with a different credential than the
                             one asked for hides the mistake.
        """
        if self.uzh_api_key_file is not None:
            path = self.uzh_api_key_file
            try:
                # .strip() because writing a token with echo or an editor leaves
                # a trailing newline, which the API rejects as part of the header.
                token = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise RuntimeError(
                    f"ZORA_UZH_API_KEY_FILE={path} could not be read: {exc}"
                ) from exc
            if not token:
                raise RuntimeError(f"ZORA_UZH_API_KEY_FILE={path} is empty.")
            return token

        token = (self.uzh_api_key or "").strip()
        if token:
            return token

        raise RuntimeError(
            "No ZORA API token configured. Set ZORA_UZH_API_KEY_FILE to a file containing "
            "the token, or ZORA_UZH_API_KEY to the token itself. ZORA_UZH_API_KEY_FILE takes "
            "precedence if both are set."
        )


def get_settings() -> ZoraSettings:
    """Return the harvester's settings, read fresh from the environment."""
    return ZoraSettings()
