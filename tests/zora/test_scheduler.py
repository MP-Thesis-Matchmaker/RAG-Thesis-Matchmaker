"""Tests for scheduler — specifically _next_action, _is_incremental_due, and
_is_full_due, the pure decision functions. No sleeping, no signal handling,
no real harvest calls needed: these are just wall-clock checks, tested
directly by freezing datetime.now.
"""

from datetime import UTC, datetime
from unittest.mock import patch

from thesis_matchmaker.zora.scheduler import (
    _is_full_due,
    _is_incremental_due,
    _next_action,
)

# Fixed reference points for testing
MONDAY_2AM = datetime(2026, 7, 27, 2, 0, 0, tzinfo=UTC)  # Monday at 02:00 UTC
MONDAY_0AM = datetime(2026, 7, 27, 0, 0, 0, tzinfo=UTC)  # Monday at 00:00 UTC (before target)
TUESDAY_2AM = datetime(2026, 7, 28, 2, 0, 0, tzinfo=UTC)  # Tuesday at 02:00 UTC
SUNDAY_2AM = datetime(2026, 7, 26, 2, 0, 0, tzinfo=UTC)  # Previous Sunday at 02:00 UTC
LAST_MONDAY_2AM = datetime(2026, 7, 20, 2, 0, 0, tzinfo=UTC)  # Last Monday at 02:00 UTC


def _freeze(dt):
    """Patch datetime.now in the scheduler module to return a fixed time."""
    mock = patch("thesis_matchmaker.zora.scheduler.datetime")
    m = mock.start()
    m.now.return_value = dt
    m.fromisoformat = datetime.fromisoformat
    return mock


# --- _is_incremental_due ---------------------------------------------------


def test_incremental_due_no_prior_run_and_hour_reached():
    mock = _freeze(MONDAY_2AM)
    try:
        assert _is_incremental_due(None, harvest_hour=1) is True
    finally:
        mock.stop()


def test_incremental_due_no_prior_run_but_hour_not_reached():
    mock = _freeze(MONDAY_0AM)
    try:
        assert _is_incremental_due(None, harvest_hour=1) is False
    finally:
        mock.stop()


def test_incremental_due_when_last_run_was_yesterday():
    mock = _freeze(TUESDAY_2AM)
    try:
        assert _is_incremental_due(MONDAY_2AM.isoformat(), harvest_hour=1) is True
    finally:
        mock.stop()


def test_incremental_not_due_when_already_ran_today():
    mock = _freeze(MONDAY_2AM)
    try:
        # Last run was earlier today
        assert _is_incremental_due(MONDAY_0AM.isoformat(), harvest_hour=1) is False
    finally:
        mock.stop()


# --- _is_full_due -----------------------------------------------------------


def test_full_due_no_prior_run_on_correct_weekday():
    mock = _freeze(MONDAY_2AM)
    try:
        assert _is_full_due(None, harvest_hour=1, weekday=0) is True
    finally:
        mock.stop()


def test_full_not_due_no_prior_run_wrong_weekday():
    mock = _freeze(TUESDAY_2AM)
    try:
        assert _is_full_due(None, harvest_hour=1, weekday=0) is False
    finally:
        mock.stop()


def test_full_not_due_no_prior_run_correct_weekday_but_hour_not_reached():
    mock = _freeze(MONDAY_0AM)
    try:
        assert _is_full_due(None, harvest_hour=1, weekday=0) is False
    finally:
        mock.stop()


def test_full_due_when_last_run_was_last_week():
    mock = _freeze(MONDAY_2AM)
    try:
        assert _is_full_due(LAST_MONDAY_2AM.isoformat(), harvest_hour=1, weekday=0) is True
    finally:
        mock.stop()


def test_full_not_due_when_already_ran_this_week():
    mock = _freeze(MONDAY_2AM)
    try:
        # Already ran this Monday
        assert _is_full_due(MONDAY_0AM.isoformat(), harvest_hour=1, weekday=0) is False
    finally:
        mock.stop()


# --- _next_action -----------------------------------------------------------


def test_fresh_deployment_with_no_state_runs_full_first():
    """No last-run timestamps at all — a brand new deployment with nothing
    harvested yet should do a full run, not try to increment from nothing.
    (Only fires on the correct weekday at the target hour.)"""
    st = {"last_full_run_at": None, "last_incremental_run_at": None}
    mock = _freeze(MONDAY_2AM)
    try:
        assert _next_action(st, harvest_hour=1, full_harvest_weekday=0) == "full"
    finally:
        mock.stop()


def test_nothing_due_returns_none():
    """Both ran recently (today) — nothing should be due."""
    st = {
        "last_full_run_at": MONDAY_0AM.isoformat(),
        "last_incremental_run_at": MONDAY_0AM.isoformat(),
    }
    mock = _freeze(MONDAY_2AM)
    try:
        assert _next_action(st, harvest_hour=1, full_harvest_weekday=0) is None
    finally:
        mock.stop()


def test_incremental_due_when_past_its_day():
    """Full ran recently (this week), but incremental's last run was yesterday
    and the target hour has been reached."""
    st = {
        "last_full_run_at": MONDAY_2AM.isoformat(),  # this week, not due
        "last_incremental_run_at": MONDAY_2AM.isoformat(),  # yesterday relative to Tuesday
    }
    mock = _freeze(TUESDAY_2AM)
    try:
        assert _next_action(st, harvest_hour=1, full_harvest_weekday=0) == "incremental"
    finally:
        mock.stop()


def test_full_takes_priority_when_both_are_due():
    """Both full and incremental are due on Monday — full wins."""
    st = {
        "last_full_run_at": LAST_MONDAY_2AM.isoformat(),  # last week
        "last_incremental_run_at": SUNDAY_2AM.isoformat(),  # yesterday
    }
    mock = _freeze(MONDAY_2AM)
    try:
        assert _next_action(st, harvest_hour=1, full_harvest_weekday=0) == "full"
    finally:
        mock.stop()


def test_full_not_yet_due_falls_through_to_incremental_check():
    """It's Tuesday (not full-harvest day). Incremental's last run was
    yesterday — should return incremental."""
    st = {
        "last_full_run_at": MONDAY_2AM.isoformat(),  # this week, not due
        "last_incremental_run_at": MONDAY_2AM.isoformat(),  # yesterday relative to Tuesday
    }
    mock = _freeze(TUESDAY_2AM)
    try:
        assert _next_action(st, harvest_hour=1, full_harvest_weekday=0) == "incremental"
    finally:
        mock.stop()
