"""A schema_invalid caused by a dead LLM must say so, not impersonate page drift.

The real incident: ifi--17's descriptions come partly from pdf_enrich, which
summarises PDF proposals through the LLM. With the LLM out of credits the records
extracted fine but topic_description stayed None, validate.py rejected them, and the
flag read "schema_invalid: no topic_description/research_area" -- indistinguishable
from a restructured page. The operator (and the operator's AI) both misread it as
drift. The hint makes the likelier cause the headline, because the per-source line
and the RUN SUMMARY table print only reasons[0].
"""

from __future__ import annotations

import os
from unittest import mock

from themis_scraper import registry, report, validate
from themis_scraper.main import _apply_result, _flag_llm_outage

_ENRICHED_SPEC = {"page_type": "topics", "pdf_enrich": {"url_field": "source_link"}}
_PLAIN_SPEC = {"page_type": "topics"}


def _schema_invalid() -> validate.Result:
    return validate.Result(
        source_id="ifi--17",
        status=validate.SCHEMA_INVALID,
        page_type="topics",
        reasons=["topics[0]: no topic_description/research_area"],
        record_count=26,
    )


def test_llm_outage_becomes_the_headline_reason() -> None:
    result = _schema_invalid()
    with mock.patch("themis_scraper.llm.is_available", return_value=False):
        _flag_llm_outage(result, _ENRICHED_SPEC)
    assert "LLM unavailable" in result.reasons[0]
    # The original diagnosis is kept, demoted -- it is still true, just not the story.
    assert result.reasons[1] == "topics[0]: no topic_description/research_area"


def test_no_hint_when_the_llm_is_up() -> None:
    """With a working LLM, a schema failure on an enriched spec IS suspicious."""
    result = _schema_invalid()
    with mock.patch("themis_scraper.llm.is_available", return_value=True):
        _flag_llm_outage(result, _ENRICHED_SPEC)
    assert len(result.reasons) == 1


def test_no_hint_for_a_spec_without_llm_enrichment() -> None:
    """A spec whose fields are all deterministic cannot blame the LLM."""
    result = _schema_invalid()
    with mock.patch("themis_scraper.llm.is_available", return_value=False):
        _flag_llm_outage(result, _PLAIN_SPEC)
    assert len(result.reasons) == 1


def test_no_hint_on_other_statuses() -> None:
    """extract_failed etc. keep their own stories even with the LLM down."""
    result = validate.Result(
        source_id="x", status=validate.EXTRACT_FAILED, page_type="topics", reasons=["boom"]
    )
    with mock.patch("themis_scraper.llm.is_available", return_value=False):
        _flag_llm_outage(result, _ENRICHED_SPEC)
    assert result.reasons == ["boom"]


# --- process pages, and what an outage does to onboarding -------------------
#
# A process page is summarised by the LLM alone. Before the cluster's scraper had
# an LLM key, all 50 process pages would have come out extract_failed -- and
# quarantined, in a state file on the PVC that outlives the missing key.


def _process_failed(llm_status: str) -> tuple[validate.Result, list[dict]]:
    result = validate.Result(
        source_id="p--1",
        status=validate.EXTRACT_FAILED,
        page_type="process",
        reasons=["no usable process summary"],
    )
    return result, [{"process_description": None, "_llm": {"status": llm_status}}]


def test_the_enrichment_outage_is_marked_as_the_llms():
    result = _schema_invalid()
    with mock.patch("themis_scraper.llm.is_available", return_value=False):
        _flag_llm_outage(result, _ENRICHED_SPEC)
    assert result.llm_outage


def test_a_process_page_without_an_llm_is_the_llms_failure():
    for status in ("unavailable", "error: APIConnectionError: proxy down"):
        result, records = _process_failed(status)
        _flag_llm_outage(result, None, records)
        assert result.llm_outage, status
        assert "LLM could not summarise" in result.reasons[0]
        assert result.reasons[1] == "no usable process summary"


def test_a_pdf_without_text_is_the_pages_failure():
    result, records = _process_failed("binary_no_text")
    _flag_llm_outage(result, None, records)
    assert not result.llm_outage
    assert result.reasons == ["no usable process summary"]


def test_a_topics_failure_is_never_blamed_on_the_llm():
    result = validate.Result(
        source_id="x", status=validate.EXTRACT_FAILED, page_type="topics", reasons=["boom"]
    )
    _flag_llm_outage(result, None, [{"_llm": {"status": "unavailable"}}])
    assert not result.llm_outage


def _apply(tmp_path, result):
    """Run `_apply_result` for one source in a scratch data root; return its entry."""
    root = tmp_path / "scraper"
    root.mkdir()
    (root / "registry").symlink_to(os.path.abspath("data/scraper/registry"))
    with mock.patch.dict(os.environ, {"SCRAPER_DATA_ROOT": str(root)}):
        src = next(iter(registry.iter_sources()))
        result.source_id = src.source_id
        state = {
            "version": registry.STATE_VERSION,
            "sources": {src.source_id: {"onboarding": registry.ONBOARD_VERIFIED}},
        }
        _apply_result(state, {}, src, "process", result, [], report.new_report())
    return state["sources"][src.source_id]


def test_an_llm_outage_fails_the_run_but_does_not_quarantine(tmp_path):
    result, records = _process_failed("unavailable")
    _flag_llm_outage(result, None, records)
    entry = _apply(tmp_path, result)
    assert entry["onboarding"] == registry.ONBOARD_VERIFIED
    assert entry["run"] == registry.RUN_FAILED  # still counts toward the exit threshold
    assert "quarantined_contract" not in entry


def test_a_page_failure_still_quarantines(tmp_path):
    result, records = _process_failed("binary_no_text")
    _flag_llm_outage(result, None, records)
    entry = _apply(tmp_path, result)
    assert entry["onboarding"] == registry.ONBOARD_QUARANTINED
    assert "quarantined_contract" in entry
