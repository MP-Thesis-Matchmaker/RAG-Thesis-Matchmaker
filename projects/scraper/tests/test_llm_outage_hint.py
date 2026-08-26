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

from unittest import mock

from themis_scraper import validate
from themis_scraper.main import _flag_llm_outage

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
