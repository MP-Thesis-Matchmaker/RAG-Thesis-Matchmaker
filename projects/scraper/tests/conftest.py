"""Hides the scraper suite when the `scraping` extra is not installed.

The offline CI job installs `uv sync --locked` and no extras at all -- that is the
seam it exists to keep honest -- so `bs4`, `yaml` and `pypdf` are absent there. These
tests then have to disappear rather than fail at import time, which is the same
contract `tests/conftest.py` has with DATABASE_URL.

The dedicated `scraper` CI job installs `--extra scraping` and does run them.

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

_SCRAPING_EXTRA = ("bs4", "yaml")

collect_ignore_glob = (
    [] if all(find_spec(module) is not None for module in _SCRAPING_EXTRA) else ["test_*.py"]
)
