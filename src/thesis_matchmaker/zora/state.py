"""The incremental harvest watermark.

Now a row in `harvest_state`, not `data/state.json`. The file version had to be
persisted on whatever disk the harvester happened to be given, which is why the
deleted GitHub Actions workflow committed it back into the repository -- a
watermark coupled to git history, unable to survive two concurrent runs.

The function signatures are unchanged on purpose: `scheduler.py` calls these and
reads the same keys, so moving the storage did not touch it. The scheduler's own
future is a separate question (Kubernetes CronJobs make it redundant), and
conflating the two changes would have made both harder to review.
"""

from __future__ import annotations

from . import store


def load_state() -> dict:
    """Watermark and per-mode run stamps. Timestamps are ISO strings."""
    return store.load_state()


def save_state(last_accessioned: str | None, total_publications: int, mode: str) -> None:
    """Record the watermark and stamp this run."""
    store.save_state(last_accessioned, total_publications, mode)
