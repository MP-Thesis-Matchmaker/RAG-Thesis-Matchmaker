"""Tell the matcher that new publications have landed.

This replaces the CronJob that was supposed to index "after each harvest". A
schedule can only ever guess when a harvest finished; the harvest itself knows.

Deliberately incapable of failing a harvest. A run that committed 2,000
publications did its job, and turning that into a non-zero exit because the
matcher happened to be redeploying would page somebody over nothing -- and would
mark the harvest as failed in the CronJob's history, which is a lie. The next
trigger picks the same data up anyway: the indexer diffs content hashes, so
nothing is lost by a missed notification, only delayed.
"""

from __future__ import annotations

import logging

import httpx

from themis_shared.config import Settings

logger = logging.getLogger(__name__)

# The matcher answers a trigger as soon as it has claimed the run; it does not
# hold the connection for the indexing itself. Short on purpose.
_TIMEOUT_S = 30.0

PATH = "/v1/index/publications"


def trigger_index(settings: Settings) -> bool:
    """Ask the matcher to index publications. Returns whether it was accepted.

    Never raises.
    """
    if not settings.matcher_base_url:
        logger.info("MATCHER_BASE_URL is not set; skipping the index trigger")
        return False

    url = f"{settings.matcher_base_url.rstrip('/')}{PATH}"
    try:
        response = httpx.post(url, timeout=_TIMEOUT_S)
    except httpx.HTTPError as exc:
        logger.warning("could not reach the matcher at %s to trigger indexing: %s", url, exc)
        return False

    if response.status_code == 202:
        logger.info("index run %s triggered", response.json().get("run_id"))
        return True
    if response.status_code == 409:
        # Not "someone else will handle it". The run that holds the slot read its
        # source rows before this harvest committed, so it will not see them; this
        # data waits for the next trigger. Harvests are daily and an incremental
        # index is minutes, so the overlap should be rare -- but when it happens
        # the delay is real and worth saying out loud rather than logging as
        # success.
        logger.warning(
            "the matcher is already running an index; the publications this harvest "
            "committed will not be indexed until the next trigger"
        )
        return False
    logger.warning(
        "the matcher refused the index trigger (%s): %s", response.status_code, response.text[:500]
    )
    return False
