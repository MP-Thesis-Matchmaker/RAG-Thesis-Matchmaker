"""
Deployment-agnostic scheduler for continuous operation.

This exists to answer a specific problem: a GitHub Actions cron can trigger
a harvest and push results to this git repo, but a deployed, running
application isn't a git repo — it can't refresh its own data that way.
The fix isn't a different CI trigger, it's moving "when do we harvest"
inside the application itself, as a long-running process, so it works
identically no matter what ends up hosting it — a bare VM, a plain Docker
container, a Kubernetes Pod, anything that can keep a process alive.

This module changes nothing about *how* harvesting works — it calls the
exact same harvest.run() used by the one-shot CLI. It only changes *what
wakes it up*: an internal loop instead of an external CI schedule.

Output still lands on local disk at config.DATA_DIR, exactly as before.
Where that disk physically lives (a Docker volume, a Kubernetes
PersistentVolumeClaim, a plain host directory) is an infrastructure
decision for whoever deploys this, deliberately left open here rather
than assumed.

Usage:
    python -m thesis_matchmaker.zora.scheduler
    (runs forever; SIGTERM/SIGINT stop it cleanly, finishing any harvest
    already in progress rather than killing it mid-write)

Configurable via env vars:
    HARVEST_HOUR_UTC       (default 1 — hour in UTC when harvests fire)
    FULL_HARVEST_WEEKDAY   (default 0 — Monday; 0=Mon … 6=Sun)
    POLL_INTERVAL_SECONDS  (default 3600 — how often to check whether
                             a run is due; this is a cheap check, not
                             a harvest, so an hourly default is fine)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime

from . import config, harvest, state

logger = logging.getLogger(__name__)

_shutdown_requested = False

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _handle_shutdown(signum, frame) -> None:  # noqa: ARG001 — signal handler signature
    global _shutdown_requested
    logger.info("Shutdown signal received — will stop after any in-progress harvest completes.")
    _shutdown_requested = True


def _is_incremental_due(last_run_at: str | None, harvest_hour: int) -> bool:
    """Incremental is due when today's date (UTC) is later than the last
    run's date AND the current hour has reached the target."""
    now = datetime.now(UTC)
    if last_run_at is None:
        return now.hour >= harvest_hour
    last_run = datetime.fromisoformat(last_run_at)
    return now.date() > last_run.date() and now.hour >= harvest_hour


def _is_full_due(last_run_at: str | None, harvest_hour: int, weekday: int) -> bool:
    """Full is due on the designated weekday once the hour target is reached,
    and only if it hasn't already run this week (i.e., this ISO calendar week)."""
    now = datetime.now(UTC)
    if last_run_at is None:
        return now.weekday() == weekday and now.hour >= harvest_hour
    last_run = datetime.fromisoformat(last_run_at)
    # "This week" = same ISO calendar week
    same_week = now.isocalendar()[:2] == last_run.isocalendar()[:2]
    return now.weekday() == weekday and now.hour >= harvest_hour and not same_week


def _next_action(
    st: dict,
    harvest_hour: int,
    full_harvest_weekday: int,
) -> str | None:
    """Decide what to run next, if anything. Pure function — no I/O, no sleeping.

    Full takes priority over incremental when both are due (this also
    covers the very first run ever, with no state.json yet: both look
    "due" since there's no last-run timestamp, and a fresh deployment
    with no existing data should do a full harvest first, not try to
    increment from nothing).
    """
    if _is_full_due(st.get("last_full_run_at"), harvest_hour, full_harvest_weekday):
        return "full"
    if _is_incremental_due(st.get("last_incremental_run_at"), harvest_hour):
        return "incremental"
    return None


def run_forever(
    harvest_hour: int | None = None,
    full_harvest_weekday: int | None = None,
    poll_interval_seconds: float | None = None,
) -> None:
    harvest_hour = harvest_hour if harvest_hour is not None else config.HARVEST_HOUR_UTC
    full_harvest_weekday = (
        full_harvest_weekday if full_harvest_weekday is not None else config.FULL_HARVEST_WEEKDAY
    )
    poll_interval_seconds = poll_interval_seconds or float(
        os.environ.get("POLL_INTERVAL_SECONDS", 3600)
    )

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    logger.info(
        "Scheduler started. Incremental nightly at %02d:00 UTC, "
        "full weekly on %s at %02d:00 UTC, polling every %ss.",
        harvest_hour,
        _WEEKDAY_NAMES[full_harvest_weekday],
        harvest_hour,
        poll_interval_seconds,
    )

    while not _shutdown_requested:
        st = state.load_state()
        action = _next_action(st, harvest_hour, full_harvest_weekday)

        if action is not None:
            logger.info("%s harvest is due — running now.", action.capitalize())
            _run_and_log(action)
        else:
            logger.debug("Nothing due yet.")

        for _ in range(int(poll_interval_seconds)):
            if _shutdown_requested:
                break
            time.sleep(1)

    logger.info("Scheduler stopped.")


def _run_and_log(mode: str) -> None:
    try:
        exit_code = harvest.run(mode)
        if exit_code != 0:
            logger.error("%s harvest exited with code %d — will retry next poll.", mode, exit_code)
    except Exception:
        # A single failed run should never take the whole process down —
        # log it and let the next poll cycle try again.
        logger.exception("%s harvest raised an unexpected exception.", mode)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_forever()
    sys.exit(0)


if __name__ == "__main__":
    main()
