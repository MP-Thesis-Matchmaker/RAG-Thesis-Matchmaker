"""Unit tests for the pure logic the pipeline relies on (plan §6):
topic_id stability, field normalization/transforms, profile-link matching, and
the run's escalation (quarantine) decision. All are network-free.

Run:  python -m unittest -v tests.test_units
"""

from __future__ import annotations

import unittest
from unittest import mock

from thesis_matchmaker.scraper import dataset as ST
from thesis_matchmaker.scraper import llm_extract as LX
from thesis_matchmaker.scraper import main as M
from thesis_matchmaker.scraper import spec_engine as S
from thesis_matchmaker.scraper import validate as V

URL = "https://example.uzh.ch/topics.html"


# --- topic_id -----------------------------------------------------------------


class TopicIdTest(unittest.TestCase):
    def test_deterministic(self):
        rec = {"topic_description": "Graph neural networks for X"}
        self.assertEqual(S._topic_id(URL, rec, None), S._topic_id(URL, rec, None))

    def test_normalizes_whitespace_and_case(self):
        a = S._topic_id(URL, {"topic_description": "Hello   World"}, None)
        b = S._topic_id(URL, {"topic_description": "  hello world  "}, None)
        self.assertEqual(a, b)

    def test_id_from_selects_fields(self):
        rec = {"title": "Deep Nets", "research_area": "ML", "topic_description": "ignored"}
        by_title = S._topic_id(URL, rec, ["title"])
        by_both = S._topic_id(URL, rec, ["title", "research_area"])
        # different seed fields → different ids
        self.assertNotEqual(by_title, by_both)
        # and stable for the same field set
        self.assertEqual(by_title, S._topic_id(URL, rec, ["title"]))

    def test_defaults_to_description_when_no_id_from(self):
        rec = {"title": "T", "topic_description": "the description"}
        self.assertEqual(
            S._topic_id(URL, rec, None),
            S._topic_id(URL, {"topic_description": "the description"}, None),
        )

    def test_url_is_part_of_the_hash(self):
        rec = {"topic_description": "same text"}
        self.assertNotEqual(
            S._topic_id(URL, rec, None),
            S._topic_id("https://other.uzh.ch/x.html", rec, None),
        )

    def test_is_sha1_hex(self):
        tid = S._topic_id(URL, {"topic_description": "x"}, None)
        self.assertEqual(len(tid), 40)
        int(tid, 16)  # raises if not hex


# --- transforms / normalization ----------------------------------------------


class TransformTest(unittest.TestCase):
    def _t(self, name, value):
        return S._TRANSFORMS[name](value)

    def test_name_lastfirst(self):
        self.assertEqual(
            self._t("name_lastfirst", "Backhaus, Norman, Prof. Dr."), "Norman Backhaus"
        )
        # no comma → left as-is (after title stripping)
        self.assertEqual(self._t("name_lastfirst", "Norman Backhaus"), "Norman Backhaus")

    def test_name_lastfirst_space(self):
        self.assertEqual(self._t("name_lastfirst_space", "Altmeyer Matthias"), "Matthias Altmeyer")
        self.assertEqual(
            self._t("name_lastfirst_space", "Guerreiro Stücklin Ana"), "Ana Guerreiro Stücklin"
        )

    def test_pi_surname(self):
        self.assertEqual(self._t("pi_surname", "MORSCHER_Pediatric Cancer"), "Morscher")
        self.assertEqual(self._t("pi_surname", "SCHARL_FAZILATY_Title"), "Scharl Fazilaty")
        self.assertIsNone(self._t("pi_surname", "no caps prefix"))

    def test_titlecase(self):
        self.assertEqual(self._t("titlecase", "SCHARL_FAZILATY"), "Scharl Fazilaty")

    def test_strip_titles_leading_and_trailing(self):
        self.assertEqual(self._t("strip_titles", "Prof. Dr. Hui Chen"), "Hui Chen")
        self.assertEqual(self._t("strip_titles", "Francisco Amaral, Prof. Dr."), "Francisco Amaral")

    def test_normalize_ws(self):
        self.assertEqual(self._t("normalize_ws", "  a   b  "), "a b")

    def test_deobfuscate_email(self):
        self.assertEqual(
            self._t("deobfuscate_email", "hui.chen[at]business.uzh.ch"), "hui.chen@business.uzh.ch"
        )
        self.assertEqual(self._t("deobfuscate_email", "name (at) x dot ch"), "name@x.ch")
        self.assertIsNone(self._t("deobfuscate_email", "not an email"))

    def test_degree_from_type(self):
        self.assertEqual(self._t("degree_from_type", "BA/MA"), "Bachelor, Master")
        self.assertEqual(self._t("degree_from_type", "MA"), "Master")
        self.assertEqual(self._t("degree_from_type", "BA"), "Bachelor")
        self.assertIsNone(self._t("degree_from_type", "IS"))

    def test_academic_role(self):
        self.assertEqual(self._t("academic_role", "Prof. em. Dr. X"), "Professor Emeritus")
        self.assertEqual(self._t("academic_role", "PD Dr. Y"), "Privatdozent")
        self.assertEqual(self._t("academic_role", "Prof. X"), "Professor")
        self.assertIsNone(self._t("academic_role", "Mr Nobody"))

    def test_norm_status(self):
        self.assertEqual(self._t("norm_status", "available"), "open")
        self.assertEqual(self._t("norm_status", "Taken"), "taken")

    def test_date_only(self):
        self.assertEqual(self._t("date_only", "2024-01-02T10:00:00Z"), "2024-01-02")
        self.assertEqual(self._t("date_only", "not a date"), "not a date")

    def test_mailto_email(self):
        self.assertEqual(self._t("mailto_email", "mailto:a@b.ch?subject=Hi"), "a@b.ch")
        self.assertIsNone(self._t("mailto_email", "/profile/x.html"))

    def test_non_string_passthrough(self):
        # transforms must not choke on None / non-str values
        for name in (
            "name_lastfirst",
            "pi_surname",
            "strip_titles",
            "normalize_ws",
            "academic_role",
            "deobfuscate_email",
        ):
            self.assertIsNone(S._TRANSFORMS[name](None), name)

    def test_apply_transforms_chains_in_order(self):
        # strip titles, then last-first
        out = S._apply_transforms("Amaral, Francisco, Prof. Dr.", ["name_lastfirst"], URL)
        self.assertEqual(out, "Francisco Amaral")

    def test_apply_transforms_absolute_url(self):
        out = S._apply_transforms("/de/team/x.html", "absolute_url", URL)
        self.assertEqual(out, "https://example.uzh.ch/de/team/x.html")

    def test_apply_transforms_list(self):
        out = S._apply_transforms(["  a  ", "b   c"], "normalize_ws", URL)
        self.assertEqual(out, ["a", "b c"])

    def test_unknown_transform_raises(self):
        with self.assertRaises(S.SpecError):
            S._apply_transforms("x", "no_such_transform", URL)


# --- profile-link matching ----------------------------------------------------


class FollowMatchTest(unittest.TestCase):
    def test_matches_only_the_pattern(self):
        records = [
            {"name": "A", "_profile_url": "https://x.uzh.ch/team/a.html"},
            {"name": "B", "_profile_url": "https://x.uzh.ch/news/b.html"},
            {"name": "C", "_profile_url": None},
            {"name": "D"},  # no _profile_url key at all
        ]
        follow = {"url_pattern": r"https://x\.uzh\.ch/team/"}
        got = spec_engine_follow(records, follow)
        self.assertEqual([r["name"] for r, _ in got], ["A"])

    def test_custom_url_field(self):
        records = [{"name": "A", "detail_url": "https://x.uzh.ch/topic/1.html"}]
        follow = {"url_pattern": r"https://x\.uzh\.ch/topic/", "url_field": "detail_url"}
        got = spec_engine_follow(records, follow)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1], "https://x.uzh.ch/topic/1.html")

    def test_pattern_is_anchored_at_start(self):
        # follow uses re.match (anchored) — a mid-string match should NOT count
        records = [{"name": "A", "_profile_url": "https://cdn.io/https://x.uzh.ch/team/a"}]
        follow = {"url_pattern": r"https://x\.uzh\.ch/team/"}
        self.assertEqual(spec_engine_follow(records, follow), [])


def spec_engine_follow(records, follow):
    return S.follow_candidates(records, follow)


# --- escalation / quarantine decision ----------------------------------------


def classify(
    page_type="topics",
    *,
    cached=True,
    last_status=200,
    current_sha1="a",
    verified_sha1="a",
    records=None,
    llm_ok=True,
    allow_empty=False,
):
    if records is None:
        records = [{"topic_description": "t", "topic_id": "abc", "source_link": None}]
    return V.classify(
        "src--1",
        page_type,
        cached=cached,
        last_status=last_status,
        current_sha1=current_sha1,
        verified_sha1=verified_sha1,
        records=records,
        llm_ok=llm_ok,
        allow_empty=allow_empty,
    )


class EscalationTest(unittest.TestCase):
    def test_ok_is_writable_not_flagged(self):
        res = classify()
        self.assertEqual(res.status, V.OK)
        self.assertTrue(res.writable)
        self.assertFalse(res.flagged)

    def test_page_changed_still_writable_but_flagged(self):
        res = classify(current_sha1="new", verified_sha1="old")
        self.assertEqual(res.status, V.PAGE_CHANGED)
        self.assertTrue(res.writable)  # data is still schema-valid → stored
        self.assertTrue(res.flagged)  # but surfaced for review

    def test_fetch_failed_when_not_cached(self):
        res = classify(cached=False)
        self.assertEqual(res.status, V.FETCH_FAILED)
        self.assertFalse(res.writable)

    def test_fetch_failed_on_bad_status(self):
        self.assertEqual(classify(last_status=503).status, V.FETCH_FAILED)

    def test_extract_failed_on_empty_records(self):
        res = classify(records=[])
        self.assertEqual(res.status, V.EXTRACT_FAILED)
        self.assertFalse(res.writable)

    def test_allow_empty_makes_empty_ok(self):
        # a genuinely-empty JSON market (no open topics) is not a failure
        self.assertEqual(classify(records=[], allow_empty=True).status, V.OK)

    def test_process_needs_a_summary(self):
        good = [
            {
                "degree_level": "Master",
                "source_url": URL,
                "process_description": "How to get a thesis.",
            }
        ]
        self.assertEqual(classify("process", records=good).status, V.OK)
        empty = [{"degree_level": "Master", "source_url": URL, "process_description": ""}]
        self.assertEqual(classify("process", records=empty).status, V.EXTRACT_FAILED)
        self.assertEqual(classify("process", records=good, llm_ok=False).status, V.EXTRACT_FAILED)

    def test_schema_invalid_bad_email(self):
        bad = [{"name": "X", "email": "not-an-email"}]
        res = classify("people", records=bad)
        self.assertEqual(res.status, V.SCHEMA_INVALID)
        self.assertFalse(res.writable)

    def test_schema_invalid_topic_missing_id_and_desc(self):
        bad = [{"title": "T"}]  # no topic_description/research_area, no topic_id
        self.assertEqual(classify("topics", records=bad).status, V.SCHEMA_INVALID)

    def test_valid_people_record_passes(self):
        good = [
            {
                "name": "Jane Doe",
                "email": "jane.doe@uzh.ch",
                "personal_website": "https://jane.example.org",
            }
        ]
        self.assertEqual(classify("people", records=good).status, V.OK)


# --- record-level diff (the PAGE_CHANGED report) ------------------------------


class DiffTest(unittest.TestCase):
    def test_topic_diff_counts(self):
        old = [{"topic_id": "1", "title": "A"}, {"topic_id": "2", "title": "B"}]
        new = [
            {"topic_id": "1", "title": "A"},  # unchanged
            {"topic_id": "2", "title": "B v2"},  # modified
            {"topic_id": "3", "title": "C"},
        ]  # added; "2"->removed? no
        diff = V.diff_records("topics", old, new)
        self.assertEqual(diff["added"], 1)
        self.assertEqual(diff["removed"], 0)
        self.assertEqual(diff["modified"], 1)

    def test_diff_ignores_volatile_fields(self):
        old = [{"topic_id": "1", "title": "A", "scraped_at": "t0", "_x": 1}]
        new = [{"topic_id": "1", "title": "A", "scraped_at": "t1", "_x": 2}]
        diff = V.diff_records("topics", old, new)
        self.assertEqual((diff["added"], diff["removed"], diff["modified"]), (0, 0, 0))

    def test_people_keyed_by_email_then_name(self):
        old = [{"name": "A", "email": "a@uzh.ch", "bio": "x"}]
        new = [{"name": "A (renamed)", "email": "a@uzh.ch", "bio": "y"}]
        diff = V.diff_records("people", old, new)
        self.assertEqual(diff["modified"], 1)  # same email key, changed content


# --- page_changed: quiet cosmetic-only changes -------------------------------


class QuietUnchangedTest(unittest.TestCase):
    def _empty(self):
        return {"added": 0, "removed": 0, "modified": 0}

    def _real(self):
        return {"added": 2, "removed": 0, "modified": 1}

    def test_is_empty_diff(self):
        self.assertTrue(V.is_empty_diff(self._empty()))
        self.assertFalse(V.is_empty_diff(self._real()))
        self.assertFalse(V.is_empty_diff(None))  # no diff computed → not "empty"

    def test_page_changed_with_empty_diff_downgrades_to_ok(self):
        res = V.Result("s--1", V.PAGE_CHANGED, "topics", reasons=["hash a -> b"], record_count=5)
        changed = V.downgrade_if_unchanged(res, self._empty())
        self.assertTrue(changed)
        self.assertEqual(res.status, V.OK)
        self.assertFalse(res.flagged)  # no longer flagged
        self.assertTrue(res.writable)  # data still stored
        self.assertEqual(res.reasons, [])

    def test_page_changed_with_real_diff_stays_flagged(self):
        res = V.Result("s--1", V.PAGE_CHANGED, "topics", record_count=5)
        self.assertFalse(V.downgrade_if_unchanged(res, self._real()))
        self.assertEqual(res.status, V.PAGE_CHANGED)
        self.assertTrue(res.flagged)

    def test_other_statuses_are_untouched(self):
        for status in (V.OK, V.EXTRACT_FAILED, V.SCHEMA_INVALID, V.LLM_FALLBACK):
            res = V.Result("s--1", status, "topics")
            self.assertFalse(V.downgrade_if_unchanged(res, self._empty()))
            self.assertEqual(res.status, status)


# --- quarantine policy (which statuses drop a source from the rotation) -------


class QuarantinePolicyTest(unittest.TestCase):
    def test_ok_and_page_changed_keep_scraping(self):
        # page_changed's data is good and stored → stay verified, keep scraping
        self.assertFalse(V.quarantines(V.OK))
        self.assertFalse(V.quarantines(V.PAGE_CHANGED))

    def test_failures_and_fallback_quarantine(self):
        for status in (V.FETCH_FAILED, V.EXTRACT_FAILED, V.SCHEMA_INVALID, V.LLM_FALLBACK):
            self.assertTrue(V.quarantines(status), status)

    def test_page_changed_is_still_flagged_for_the_report(self):
        # it keeps scraping, but it's still reported/alerted on
        self.assertIn(V.PAGE_CHANGED, V.FLAGGED)


# --- LLM fallback: classification --------------------------------------------


class LlmFallbackClassifyTest(unittest.TestCase):
    def test_recovered_records_are_writable_and_flagged(self):
        recs = [{"topic_description": "t", "topic_id": "x", "source_link": None}]
        res = V.classify_llm_fallback("s--1", "topics", recs)
        self.assertEqual(res.status, V.LLM_FALLBACK)
        self.assertTrue(res.writable)  # recovered data is stored ...
        self.assertTrue(res.flagged)  # ... but surfaced for review

    def test_empty_fallback_is_still_extract_failed(self):
        self.assertEqual(V.classify_llm_fallback("s--1", "topics", []).status, V.EXTRACT_FAILED)

    def test_malformed_fallback_is_schema_invalid(self):
        bad = [{"name": "X", "email": "not-an-email"}]
        self.assertEqual(V.classify_llm_fallback("s--1", "people", bad).status, V.SCHEMA_INVALID)

    def test_llm_fallback_counts_as_flagged_status(self):
        self.assertIn(V.LLM_FALLBACK, V.FLAGGED)


# --- LLM fallback: trigger predicate -----------------------------------------


class LlmFallbackTriggerTest(unittest.TestCase):
    def test_fires_on_extract_failed_and_schema_invalid(self):
        for status in (V.EXTRACT_FAILED, V.SCHEMA_INVALID):
            self.assertTrue(M._should_try_fallback(True, "topics", status), status)
            self.assertTrue(M._should_try_fallback(True, "people", status), status)

    def test_does_not_fire_on_ok_page_changed_or_llm_fallback(self):
        for status in (V.OK, V.PAGE_CHANGED, V.LLM_FALLBACK, V.FETCH_FAILED):
            self.assertFalse(M._should_try_fallback(True, "topics", status), status)

    def test_disabled_flag_suppresses_it(self):
        self.assertFalse(M._should_try_fallback(False, "topics", V.EXTRACT_FAILED))

    def test_only_topics_and_people(self):
        self.assertFalse(M._should_try_fallback(True, "process", V.EXTRACT_FAILED))
        self.assertFalse(M._should_try_fallback(True, "none", V.SCHEMA_INVALID))


# --- LLM fallback: JSON parsing ----------------------------------------------


class LlmFallbackParseTest(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(LX._parse_json_array('[{"a": 1}]'), [{"a": 1}])

    def test_code_fenced(self):
        self.assertEqual(LX._parse_json_array('```json\n[{"a": 1}]\n```'), [{"a": 1}])

    def test_wrapped_in_prose(self):
        self.assertEqual(LX._parse_json_array('Sure: [{"a": 1}] done'), [{"a": 1}])

    def test_non_array_or_garbage_is_empty(self):
        self.assertEqual(LX._parse_json_array("no json here"), [])
        self.assertEqual(LX._parse_json_array('{"a": 1}'), [])  # object, not array
        self.assertEqual(LX._parse_json_array(""), [])


# --- LLM fallback: coercion into the target model ----------------------------


class LlmFallbackCoerceTest(unittest.TestCase):
    def test_topics_get_id_normalized_supervisors_and_resolved_link(self):
        raw = [
            {
                "title": "Deep Nets",
                "supervisors": [{"name": "Prof. Dr. Jane Doe", "email": "mailto:jane@uzh.ch?x=1"}],
                "topic_description": "Study X",
                "source_link": "/theses/1.html",
            }
        ]
        rec = LX._coerce_records(raw, "topics", "s--1", URL)[0]
        self.assertTrue(rec["topic_id"])
        self.assertEqual(rec["source_link"], "https://example.uzh.ch/theses/1.html")
        self.assertEqual(rec["supervisors"], [{"name": "Jane Doe", "email": "jane@uzh.ch"}])
        self.assertEqual(rec["source_id"], "s--1")

    def test_topic_without_title_or_desc_is_dropped(self):
        raw = [{"title": None, "topic_description": None, "supervisors": []}]
        self.assertEqual(LX._coerce_records(raw, "topics", "s--1", URL), [])

    def test_topic_source_link_falls_back_to_base_url(self):
        raw = [{"title": "T", "topic_description": "d"}]
        rec = LX._coerce_records(raw, "topics", "s--1", URL)[0]
        self.assertEqual(rec["source_link"], URL)

    def test_people_require_a_name_and_resolve_urls(self):
        raw = [
            {"name": "Jane Doe", "_profile_url": "/team/jane.html"},
            {"name": "  ", "email": "x@y.ch"},
        ]  # blank name → dropped
        recs = LX._coerce_records(raw, "people", "s--1", URL)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["_profile_url"], "https://example.uzh.ch/team/jane.html")

    def test_non_dict_items_are_skipped(self):
        self.assertEqual(LX._coerce_records(["nope", 3, None], "people", "s--1", URL), [])


# --- LLM fallback: end-to-end (LLM + cache stubbed, no network) ---------------


class LlmFallbackExtractTest(unittest.TestCase):
    def test_returns_empty_when_llm_unavailable(self):
        with mock.patch("thesis_matchmaker.scraper.llm.is_available", return_value=False):
            recs, info = LX.extract_records_fallback(
                "s--1", "topics", html="<main></main>", base_url=URL
            )
        self.assertEqual(recs, [])
        self.assertEqual(info["status"], "unavailable")

    def test_unsupported_page_type(self):
        recs, info = LX.extract_records_fallback(
            "s--1", "process", html="<main></main>", base_url=URL
        )
        self.assertEqual(recs, [])
        self.assertEqual(info["status"], "unsupported_page_type")

    def test_full_offline_path_with_stubbed_llm(self):
        reply = (
            '[{"title": "Graph Learning", "topic_description": "do X", '
            '"supervisors": [{"name": "Jane Doe", "email": "jane@uzh.ch"}], '
            '"source_link": "/t/1.html"}]'
        )
        html = "<main><h1>Open topics</h1><p>Graph Learning ...</p></main>"
        with (
            mock.patch("thesis_matchmaker.scraper.llm.is_available", return_value=True),
            mock.patch("thesis_matchmaker.scraper.llm.complete", return_value=reply) as m_complete,
            mock.patch("thesis_matchmaker.scraper.cache.has_subpage", return_value=False),
            mock.patch("thesis_matchmaker.scraper.cache.write_subpage") as m_write,
        ):
            recs, info = LX.extract_records_fallback("s--1", "topics", html=html, base_url=URL)
        self.assertEqual(info["status"], "ok")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["title"], "Graph Learning")
        self.assertTrue(recs[0]["topic_id"])
        self.assertEqual(recs[0]["source_link"], "https://example.uzh.ch/t/1.html")
        m_complete.assert_called_once()  # the LLM was actually consulted
        m_write.assert_called_once()  # and the reply was cached

    def test_uses_cached_reply_without_calling_llm(self):
        reply = '[{"name": "Jane Doe", "_profile_url": "/team/jane.html"}]'
        with (
            mock.patch("thesis_matchmaker.scraper.llm.is_available", return_value=True),
            mock.patch("thesis_matchmaker.scraper.llm.complete") as m_complete,
            mock.patch("thesis_matchmaker.scraper.cache.has_subpage", return_value=True),
            mock.patch("thesis_matchmaker.scraper.cache.read_subpage", return_value=reply),
        ):
            recs, info = LX.extract_records_fallback(
                "s--1", "people", html="<main>x</main>", base_url=URL
            )
        self.assertEqual(len(recs), 1)
        self.assertTrue(info.get("cached"))
        m_complete.assert_not_called()  # cache hit → no spend


# --- store: public view cleaning ---------------------------------------------


class PublicViewTest(unittest.TestCase):
    def test_clean_record_drops_llm_and_renames_profile_url(self):
        rec = {
            "name": "Jane",
            "_profile_url": "https://x/j",
            "_llm": {"status": "ok"},
            "email": "j@uzh.ch",
        }
        out = ST._clean_record(rec)
        self.assertNotIn("_llm", out)
        self.assertNotIn("_profile_url", out)
        self.assertEqual(out["profile_url"], "https://x/j")
        self.assertEqual(out["email"], "j@uzh.ch")

    def test_clean_record_keeps_public_keys_and_order(self):
        rec = {
            "degree_level": "Master",
            "process_description": "d",
            "_llm": {"x": 1},
            "source_id": "s--1",
        }
        self.assertEqual(
            list(ST._clean_record(rec)), ["degree_level", "process_description", "source_id"]
        )

    def test_faculty_scope_source_diffs_against_faculty_records(self):
        # A scope='faculty' process source is stored at the faculty level; the
        # diff lookup must read it there, not from the (empty) unit bucket.
        from thesis_matchmaker.scraper import registry

        rec = {
            "source_id": "phil--1",
            "degree_level": "Master",
            "process_description": "how to get a thesis",
        }
        data = {
            "faculties": {
                "PhF": {
                    "faculty": "Phil",
                    "process": [rec],
                    ST._PROCESS_RAW_KEY: {"phil--1": [rec]},
                    "units": {
                        "philosophisches-seminar": {
                            "unit": "PS",
                            "people": [],
                            "process": [],
                            "concrete_topics": [],
                        }
                    },
                }
            }
        }
        src = registry.Source(
            source_id="phil--1",
            url="",
            notes="",
            unit_id="philosophisches-seminar",
            faculty_code="PhF",
            faculty="Phil",
            unit="PS",
            classification="",
        )
        # with scope → finds the faculty-level record (empty diff, not spurious +1)
        self.assertEqual(ST.records_for_source(data, src, "process", scope="faculty"), [rec])
        # without scope → the old (buggy) unit lookup returns nothing
        self.assertEqual(ST.records_for_source(data, src, "process"), [])

    def test_public_view_is_a_clean_copy_that_leaves_live_data_intact(self):
        data = {
            "faculties": {
                "WWF": {
                    "faculty": "W",
                    "process": [{"degree_level": "MA", "_llm": {"s": 1}}],
                    "units": {
                        "u--1": {
                            "unit": "U",
                            "people": [{"name": "A", "_profile_url": "https://p/a"}],
                            "process": [],
                            "concrete_topics": [],
                            "groups": {
                                "g1": {
                                    "name": "G",
                                    "people": [{"name": "B", "_profile_url": "https://p/b"}],
                                    "process": [{"degree_level": "BA", "_llm": {"s": 2}}],
                                    "concrete_topics": [],
                                }
                            },
                        }
                    },
                }
            }
        }
        view = ST._public_view(data)
        # cleaned in the view ...
        self.assertNotIn("_llm", view["faculties"]["WWF"]["process"][0])
        self.assertIn("profile_url", view["faculties"]["WWF"]["units"]["u--1"]["people"][0])
        g = view["faculties"]["WWF"]["units"]["u--1"]["groups"]["g1"]
        self.assertNotIn("_llm", g["process"][0])
        self.assertIn("profile_url", g["people"][0])
        # ... but the live structure still has the internal keys (SQLite needs them)
        self.assertIn("_llm", data["faculties"]["WWF"]["process"][0])
        self.assertIn("_profile_url", data["faculties"]["WWF"]["units"]["u--1"]["people"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
