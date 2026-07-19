"""Tests for scheduler — specifically _next_action, the pure decision
function. No sleeping, no signal handling, no real harvest calls needed:
these are just timestamp arithmetic, tested directly.
"""
from datetime import UTC, datetime, timedelta

from thesis_matchmaker.zora.scheduler import _next_action

NOW = datetime.now(UTC)


def _hours_ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def test_fresh_deployment_with_no_state_runs_full_first():
    # No last-run timestamps at all — a brand new deployment with nothing
    # harvested yet should do a full run, not try to increment from nothing.
    st = {"last_full_run_at": None, "last_incremental_run_at": None}

    assert _next_action(st, incremental_interval_hours=24, full_interval_hours=168) == "full"


def test_nothing_due_returns_none():
    st = {
        "last_full_run_at": _hours_ago(1),
        "last_incremental_run_at": _hours_ago(1),
    }

    assert _next_action(st, incremental_interval_hours=24, full_interval_hours=168) is None


def test_incremental_due_when_past_its_interval():
    st = {
        "last_full_run_at": _hours_ago(1),      # recent, not due
        "last_incremental_run_at": _hours_ago(30),  # past the 24h interval
    }

    assert _next_action(st, incremental_interval_hours=24, full_interval_hours=168) == "incremental"


def test_full_takes_priority_when_both_are_due():
    st = {
        "last_full_run_at": _hours_ago(200),        # past the 168h interval
        "last_incremental_run_at": _hours_ago(30),  # also past its interval
    }

    assert _next_action(st, incremental_interval_hours=24, full_interval_hours=168) == "full"


def test_full_not_yet_due_falls_through_to_incremental_check():
    st = {
        "last_full_run_at": _hours_ago(2),          # recent, not due
        "last_incremental_run_at": _hours_ago(30),  # past its interval
    }

    assert _next_action(st, incremental_interval_hours=24, full_interval_hours=168) == "incremental"
