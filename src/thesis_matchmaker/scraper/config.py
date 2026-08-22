"""Application settings, loaded from environment variables and an optional .env file.

This is the *only* place a configurable default lives. Before it existed, config was
scattered across four shapes — `os.environ.get` at import time, `os.environ.get` inside
functions, module constants, and class attributes — so half the knobs needed a code edit
and `SCRAPER_DATA_ROOT` only relocated part of the data tree (the output paths were bound
at import). Everything now resolves through `get_settings()`, at call time.

Deliberately *not* configurable: the title-plausibility thresholds in `title_check.py`
and the field lists, regexes and prompts elsewhere. Those are calibrated against the
frozen corpus in `tests/scraper/golden_specs.json`; letting an environment variable move them
would break the determinism invariant ("same cached page + same template => identical
records") and the calibration test that guards it.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central config for the posting scraper.

    Values are read from environment variables first, then from a local .env file.
    Every variable is prefixed `SCRAPER_` (so `data_root` is `SCRAPER_DATA_ROOT`),
    which keeps them from colliding with the unprefixed settings of the backend-core
    project this prototype gets folded into. See .env.example for the full list.
    .env is gitignored, so keys and local paths stay off the repo.
    """

    model_config = SettingsConfigDict(
        env_prefix="SCRAPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Data tree ----------------------------------------------------------
    # Root of everything the scraper reads or writes (registry/, specs/, cache/,
    # output/, var/). Relative to the working directory, which is the same choice the
    # rest of the repository makes -- `sources_path = "data/samples"` in
    # thesis_matchmaker/config.py, `ZORA_DATA_DIR` defaulting to "data" in
    # zora/config.py. Deriving it from the package location instead would break in the
    # container image, where `uv sync --no-editable` installs into site-packages and
    # there is no repository above the module at all.
    data_root: Path = Path("data/scraper")

    # --- Politeness and HTTP ------------------------------------------------
    # Address advertised in the User-Agent so site owners can reach a human. Politeness
    # is non-negotiable: sequential fetching, a real delay, an honest UA.
    #
    # No default on purpose. This used to carry a team member's private address, which
    # then went out in the User-Agent of every request the scraper made -- personal data
    # hardcoded in a repository that gets pushed and graded. It has to be set
    # deliberately (SCRAPER_CONTACT), and `user_agent` below refuses to build a UA
    # without it rather than advertising a blank contact.
    contact: str | None = None

    # Seconds to wait between requests. Lower it only with a reason.
    polite_delay_seconds: float = 2.0

    # Hard per-request timeout for the static `requests` fetch (also the page.goto
    # timeout for the Playwright renderer, in milliseconds).
    http_timeout_seconds: int = 30

    # Bounded wait for network-idle before giving up on a rendered page: some UZH pages
    # keep a connection open and never reach idle, so this must never be unbounded.
    render_idle_ms: int = 6000

    # Small settle after DOM/idle so client-side JS finishes painting.
    render_settle_ms: int = 700

    # --- Cache and onboarding ----------------------------------------------
    # Previous versions of a page kept under cache/<source_id>/history/.
    cache_history_keep: int = 3

    # Default number of person profiles followed during onboarding (the
    # `onboard --profile-limit` flag overrides it per run).
    profile_limit: int = 3

    # --- LLM ----------------------------------------------------------------
    # Which provider module backs `llm.complete`. "openai" is built in; any other name
    # loads `thesis_matchmaker/scraper/llm_<name>.py`, which must expose a `Provider` class.
    llm_provider: str = "openai"
    llm_model: str = "gpt-5-mini"

    # OpenAI-compatible base URL. Leave unset for the OpenAI API itself; point it at a
    # gateway (LibreChat / AI Buddy) or a local model (e.g. Ollama at
    # http://localhost:11434/v1) to keep traffic off the public API.
    llm_base_url: str | None = None

    # API key. `OPENAI_API_KEY` is accepted as-is so existing .env files keep working;
    # `SCRAPER_LLM_API_KEY` takes precedence when both are set. When neither is present,
    # `llm.is_available()` is False and every caller keeps its deterministic output.
    llm_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SCRAPER_LLM_API_KEY", "OPENAI_API_KEY"),
    )

    # Retry budget for one completion. Transient errors back off exponentially;
    # auth failures are never retried.
    llm_max_attempts: int = 3
    llm_timeout_seconds: int = 120

    # Characters of page text sent to the model when summarizing a process page or a
    # thesis PDF. A budget, not a limit on the page: text beyond it is dropped.
    llm_page_text_budget: int = 6000

    # Characters of cleaned main HTML sent to the run-time extraction fallback. Larger
    # than the text budget because the model needs the links, not just the prose.
    llm_fallback_html_budget: int = 14000

    # Characters of cleaned HTML shown to the spec-drafting model at onboarding.
    spec_draft_html_budget: int = 16000

    # --- Derived locations --------------------------------------------------
    # Properties, never fields: nothing can set these out of step with data_root.

    @property
    def registry_path(self) -> Path:
        """The immutable, human-authored source list."""
        return self.data_root / "registry" / "scraping_sources.json"

    @property
    def specs_dir(self) -> Path:
        """Per-source spec.yaml + frozen snapshot + expected.json (the test oracle).

        Named `specs/` here, not `contracts/` as in the prototype: this repository
        already has a `contracts` package holding the Pydantic models every component
        speaks, and two directories called the same thing meaning opposite things is a
        trap for the next reader.
        """
        return self.data_root / "specs"

    @property
    def var_dir(self) -> Path:
        """Machine-written state; never tracked."""
        return self.data_root / "var"

    @property
    def state_path(self) -> Path:
        return self.var_dir / "state.json"

    @property
    def cache_dir(self) -> Path:
        return self.data_root / "cache"

    @property
    def output_dir(self) -> Path:
        return self.data_root / "output"

    @property
    def preview_dir(self) -> Path:
        return self.output_dir / "preview"

    @property
    def runs_dir(self) -> Path:
        return self.output_dir / "runs"

    @property
    def data_path(self) -> Path:
        """The populated target data model (public view)."""
        return self.output_dir / "extracted_data.json"

    @property
    def raw_people_path(self) -> Path:
        """Pre-merge per-source people, kept in a sidecar so the main file stays clean."""
        return self.output_dir / "people_raw.json"

    @property
    def raw_process_path(self) -> Path:
        return self.output_dir / "process_raw.json"

    @property
    def raw_faculty_process_path(self) -> Path:
        return self.output_dir / "faculty_process_raw.json"

    @property
    def user_agent(self) -> str:
        """Honest UA: names the tool, its purpose, and a way to reach a human.

        Raises when `contact` is unset. Fetching anonymously is the one failure mode
        worth being loud about -- a scraper that cannot be traced back to a person is
        exactly what site owners are entitled to block.
        """
        if not self.contact:
            raise RuntimeError(
                "SCRAPER_CONTACT is not set. The scraper advertises a contact address "
                "in its User-Agent so site owners can reach a human; it will not fetch "
                "without one."
            )
        return f"UZH-Thesis-Scraper/0.1 (academic research; +mailto:{self.contact})"


def get_settings() -> Settings:
    """Return settings, read fresh from the environment."""
    return Settings()
