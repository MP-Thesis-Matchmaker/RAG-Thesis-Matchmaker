"""The incremental harvest watermark.

Now a row in `harvest_state`, not `data/state.json`. The file version had to be
persisted on whatever disk the harvester happened to be given, which is why the
deleted GitHub Actions workflow committed it back into the repository -- a
watermark coupled to git history, unable to survive two concurrent runs.

These two functions kept their signatures so that the storage move to Postgres
did not have to touch `scheduler.py`. The scheduler is now deleted and Kubernetes
CronJobs own the cadence, which leaves this module a pass-through with exactly one
caller (`harvest.py`). Folding it into `store.py` is the obvious follow-up; it is
left as a separate change rather than smuggled into the scheduler's removal.
"""

from __future__ import annotations

from . import store


def load_state() -> dict:
    """Watermark and per-mode run stamps. Timestamps are ISO strings."""
    return store.load_state()


def save_state(last_accessioned: str | None, total_publications: int, mode: str) -> None:
    """Record the watermark and stamp this run."""
    store.save_state(last_accessioned, total_publications, mode)
