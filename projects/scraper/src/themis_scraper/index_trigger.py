"""Tell the matcher that new postings have landed.

The posting-side twin of `themis_zora.index_trigger`, and the same reasoning: a
schedule can only guess when a scrape finished, the scrape itself knows.

`requests`, not httpx. This package speaks requests everywhere -- see the long
note in its pyproject.toml -- and adding a second HTTP client for one POST would
be a worse trade than the small duplication of having two trigger modules. That
also means the import is local to the call: `requests` arrives with the
`[scraping]` extra, and `themis-scraper` installed bare has to keep importing.
"""

from __future__ import annotations

import logging

from themis_shared.config import Settings

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30.0

PATH = "/v1/index/postings"


def trigger_index(settings: Settings) -> bool:
    """Ask the matcher to index postings. Returns whether it was accepted.

    Never raises, and never changes the run's exit code. A scrape that wrote 695
    postings did its job; failing it because the matcher was redeploying would
    mark a good run bad. Nothing is lost by a missed trigger, only delayed --
    the indexer diffs content hashes, so the next run picks the same rows up.
    """
    if not settings.matcher_base_url:
        logger.info("MATCHER_BASE_URL is not set; skipping the index trigger")
        return False

    import requests

    url = f"{settings.matcher_base_url.rstrip('/')}{PATH}"
    try:
        response = requests.post(url, timeout=_TIMEOUT_S)
    except requests.RequestException as exc:
        logger.warning("could not reach the matcher at %s to trigger indexing: %s", url, exc)
        return False

    if response.status_code == 202:
        logger.info("index run %s triggered", response.json().get("run_id"))
        return True
    if response.status_code == 409:
        # The run holding the slot read its source rows before this scrape
        # committed, so it will not see them. This data waits for the next
        # trigger rather than being covered by the run in progress.
        logger.warning(
            "the matcher is already running an index; the postings this run committed "
            "will not be indexed until the next trigger"
        )
        return False
    logger.warning(
        "the matcher refused the index trigger (%s): %s", response.status_code, response.text[:500]
    )
    return False
