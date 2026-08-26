"""Skips the scraper suite when the `scraping` extra is not installed.

The offline CI job installs `uv sync --locked` and no extras at all -- that is the
seam it exists to keep honest -- so `bs4`, `yaml` and `pypdf` are absent there. These
tests then have to disappear rather than fail at import time, which is the same
contract `tests/conftest.py` has with DATABASE_URL.

The dedicated `scraper` CI job installs `--extra scraping` and does run them.
"""

from __future__ import annotations

import pytest

pytest.importorskip("bs4", reason="the scraping extra is not installed")
pytest.importorskip("yaml", reason="the scraping extra is not installed")
