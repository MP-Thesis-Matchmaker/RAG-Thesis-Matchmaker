"""Hides the scraper suite when the scraper itself is not installed.

Originally this guarded against the `scraping` extra being absent. That extra is
gone -- since 2026-08-27 bs4, requests, PyYAML, pypdf and openai are ordinary
dependencies of themis-scraper -- so in CI this file no longer fires: `offline` and
`pgvector` sync `--all-packages` and now genuinely run these tests, and the
boundaries scraper leg passes an explicit path.

It is kept because the failure it prevents is not CI's. The root `testpaths` names
this directory unconditionally, so a developer who runs `uv sync --package
themis-matcher` (which *replaces* the single workspace .venv rather than adding to
it) and then a bare `pytest` still lands here with no bs4 installed. Without the
guard that session dies at startup instead of running the matcher tests they asked
for.

`collect_ignore_glob` rather than `pytest.importorskip`, and the difference is not
cosmetic. This directory is named in the root `testpaths`, so pytest loads this file
as an *initial* conftest -- before a collection tree exists. `importorskip` works by
raising `Skipped`, and at that point there is no node to attach it to, so it escapes
as a startup error and takes the entire session with it, all 472 tests included. That
is precisely what it did: the `offline` and `pgvector` jobs both died here while the
`scraper` job stayed green, because the extra it installs made the import succeed.
A local run hid it for the same reason.

`find_spec` asks whether the module is installed without importing it, so this cannot
half-fail the way an import in a module body can.
"""

from __future__ import annotations

from importlib.util import find_spec

# Module-level imports of the scraper package; if these are missing, themis-scraper
# is not installed in this environment at all.
_SCRAPER_DEPS = ("bs4", "yaml")

collect_ignore_glob = (
    [] if all(find_spec(module) is not None for module in _SCRAPER_DEPS) else ["test_*.py"]
)
