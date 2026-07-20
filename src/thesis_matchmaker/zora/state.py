"""
Tracks the incremental harvest watermark, plus per-mode last-run
timestamps. Lives on local disk (config.STATE_PATH) rather than any
external store — whatever's hosting the harvester (a CI job, a
long-running scheduler process, anything) just needs this file to
persist on whatever disk it's given between runs.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from . import config

_DEFAULT_STATE = {
    "last_accessioned": None,
    "last_run_at": None,
    "last_total_publications": 0,
    "last_incremental_run_at": None,
    "last_full_run_at": None,
}


def load_state() -> dict:
    if not os.path.exists(config.STATE_PATH):
        return dict(_DEFAULT_STATE)
    with open(config.STATE_PATH, encoding="utf-8") as f:
        state = json.load(f)
    # Fill in any keys an older state.json predates (e.g. per-mode
    # timestamps added after some deployments already have a state file).
    for key, default in _DEFAULT_STATE.items():
        state.setdefault(key, default)
    return state


def save_state(last_accessioned: str | None, total_publications: int, mode: str) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    state = load_state()
    now = datetime.now(UTC).isoformat()
    state["last_accessioned"] = last_accessioned
    state["last_run_at"] = now
    state["last_total_publications"] = total_publications
    state[f"last_{mode}_run_at"] = now
    with open(config.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
