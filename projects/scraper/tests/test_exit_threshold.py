"""When `fetch` and `run` exit non-zero.

A CronJob decides success from the exit code alone. Both commands used to fail on
the first dead page, and with ~100 university pages some are always dead, so every
run was red -- which alerts nobody. They now fail only when more than
`MAX_UNSCRAPED_RATIO` of the sources could not be scraped (big errors, such as a
failed database write, still raise out of `main`).

What these tests pin down is what counts as "not scraped", because the obvious
answer is wrong: `run` extracts from any cached page, so a source whose fetch failed
this cycle still comes out `ok` -- on last cycle's content.
"""

from __future__ import annotations

from themis_scraper import registry, validate
from themis_scraper.main import _unscraped

_OK_FETCH = {"http_status": 200, "method": "requests", "content_sha1": "abc", "fetched_at": "t"}
_HTTP_FAIL = {"http_status": 404, "method": "requests", "error": "HTTP 404"}
_JSON_FAIL = {"http_status": 503, "method": "json"}


def _src(sid: str) -> registry.Source:
    return registry.Source(
        source_id=sid,
        url=f"https://example.org/{sid}",
        notes="",
        unit_id="u",
        faculty_code="f",
        faculty="f",
        unit="u",
        classification="",
    )


# --- the threshold ------------------------------------------------------------


def test_threshold_is_strictly_above_thirty_percent():
    assert not validate.too_many_unscraped(30, 100)
    assert validate.too_many_unscraped(31, 100)


def test_a_few_dead_pages_do_not_fail_the_run():
    # The run that prompted this: 3 of 103 pages returned 404.
    assert not validate.too_many_unscraped(3, 103)


def test_small_selections_use_the_same_ratio():
    assert validate.too_many_unscraped(1, 3)
    assert not validate.too_many_unscraped(0, 3)


def test_nothing_selected_is_not_a_failure():
    # "Nothing verified" has its own loud exit in cmd_run; the ratio stays out of it.
    assert not validate.too_many_unscraped(0, 0)


# --- last_fetch_failed --------------------------------------------------------


def test_last_fetch_failed_reads_both_failure_shapes():
    assert registry.last_fetch_failed({"last_fetch": _HTTP_FAIL})
    assert registry.last_fetch_failed({"last_fetch": _JSON_FAIL})


def test_last_fetch_succeeded_or_never_fetched_is_not_a_failure():
    assert not registry.last_fetch_failed({"last_fetch": _OK_FETCH})
    assert not registry.last_fetch_failed({})


# --- _unscraped ---------------------------------------------------------------


def _state(**entries) -> dict:
    return {"version": registry.STATE_VERSION, "sources": entries}


def test_a_stored_source_with_a_good_fetch_was_scraped():
    state = _state(a={"run": registry.RUN_DONE, "last_fetch": _OK_FETCH})
    assert _unscraped(state, [_src("a")]) == []


def test_a_source_the_run_could_not_store_was_not_scraped():
    state = _state(a={"run": registry.RUN_FAILED, "last_fetch": _OK_FETCH})
    assert _unscraped(state, [_src("a")]) == ["a"]


def test_a_stale_cache_extraction_after_a_failed_fetch_was_not_scraped():
    # run=done because extraction worked -- on the page cached by an earlier cycle.
    state = _state(a={"run": registry.RUN_DONE, "last_fetch": _HTTP_FAIL})
    assert _unscraped(state, [_src("a")]) == ["a"]


def test_only_the_given_sources_are_judged():
    state = _state(
        a={"run": registry.RUN_FAILED, "last_fetch": _HTTP_FAIL},
        b={"run": registry.RUN_DONE, "last_fetch": _OK_FETCH},
    )
    assert _unscraped(state, [_src("b")]) == []
