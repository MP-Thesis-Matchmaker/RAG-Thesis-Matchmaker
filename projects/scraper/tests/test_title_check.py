"""Tests for the title plausibility check, repair, and its calibration.

The important test here is `CorpusCalibrationTest`: it scores every title in the
committed golden baseline and asserts the implausible set is exactly the one
record we know is broken. That is what stops a future tweak of the heuristic from
silently rewriting good titles across 50 contracts.
"""

from __future__ import annotations

import json
import unittest

from bs4 import BeautifulSoup

import replay_util as R
from themis_scraper import dataset as ST
from themis_scraper import title_check as T
from themis_scraper import validate as V

# The real ifi--5 block that started this: a posting date in the h3, the actual
# title in a bold paragraph below it.
IFI5_BLOCK = """
<div class="TextImage--content richtext">
  <h3><strong>November 3, 2021</strong>
      <span style="color:red; float:right">Taken</span></h3>
  <p><strong>Optimization of Lempel-Ziv Algorithm for Entropy Rate Estimation:</strong></p>
  <p>The Lempel-Ziv algorithm is a well known universal lossless compression algorithm.</p>
  <p>Supervisor: <a href="https://www.ifi.uzh.ch/en/dbtg/Staff/Jamal.html">Jamal Mohammed</a></p>
</div>
"""

REAL_TITLE = "Optimization of Lempel-Ziv Algorithm for Entropy Rate Estimation"


class ScoreTitleTest(unittest.TestCase):
    def assert_implausible(self, text, hint=""):
        v = T.score_title(text)
        self.assertFalse(v.plausible, f"{text!r} should be implausible ({hint}): {v}")

    def assert_plausible(self, text):
        v = T.score_title(text)
        self.assertTrue(v.plausible, f"{text!r} should be plausible: {v}")

    def test_dates_are_rejected(self):
        for text in (
            "November 3, 2021",
            "3. November 2021",
            "Nov 3 2021",
            "03.11.2021",
            "11/03/2021",
            "2021-11-03",
            "November 2021",
            "Dezember 2024",
            "1. Mai 2023",
        ):
            self.assert_implausible(text, "date")

    def test_availability_words_are_rejected(self):
        for text in (
            "Taken",
            "taken",
            "vergeben",
            "Open",
            "available",
            "reserved",
            "besetzt",
            "closed",
        ):
            self.assert_implausible(text, "status word")

    def test_labels_and_markers_are_rejected(self):
        for text in (
            "Thesis",
            "Theses",
            "Topics",
            "Masterarbeit",
            "Projekt",
            "Details",
            "PDF",
            "Download",
            "Abstract",
            "Supervisor",
            "MA",
            "BSc",
            "BA/MA",
            "30 ECTS",
            "Bachelor, Master",
            "HS24",
            "FS25",
            "WS 2024/25",
        ):
            self.assert_implausible(text, "label or marker")

    def test_non_titles_are_rejected(self):
        for text in (
            "",
            "   ",
            None,
            42,
            "123",
            "-- --",
            "someone@uzh.ch",
            "https://www.ifi.uzh.ch/x.html",
        ):
            self.assert_implausible(text, "not a title at all")

    def test_real_titles_are_plausible(self):
        for text in (
            REAL_TITLE,
            "Fair Referee Assignment",
            "Matrix Operations with Gathering in MonetDB",
            "Realizing The Concept of NOW In Now-Relative Databases",
            "Implementing Conflict-Free Replicated Data Types (CRDTs)",
        ):
            self.assert_plausible(text)

    def test_short_but_legitimate_corpus_titles_survive(self):
        # These three are the only sub-12-char titles in the whole corpus and all
        # are real; the short-token penalty must not push them under threshold.
        for text in ("Jinek group", "SDG Scout", "Strafrecht"):
            self.assert_plausible(text)

    def test_title_repeating_the_status_is_penalised(self):
        v = T.score_title("Taken ", siblings=["Taken"])
        self.assertFalse(v.plausible)

    def test_research_area_duplication_is_not_penalised(self):
        # rfw--1 legitimately titles a topic with its subject area.
        v = T.score_title("Strafrecht", siblings=[None])
        self.assertTrue(v.plausible)

    def test_a_whole_paragraph_is_not_a_title(self):
        self.assert_implausible("word " * 80, "too long")

    def test_normalize_strips_decoration(self):
        self.assertEqual(T.normalize("  Some   Title:  "), "Some Title")
        self.assertEqual(T.normalize("1. Some Title"), "Some Title")
        self.assertEqual(T.normalize(None), "")

    def test_looks_like_date(self):
        self.assertTrue(T.looks_like_date("November 3, 2021"))
        self.assertFalse(T.looks_like_date(REAL_TITLE))


class CandidateScanTest(unittest.TestCase):
    def setUp(self):
        self.block = BeautifulSoup(IFI5_BLOCK, "html.parser").select_one("div")

    def test_p_strong_is_offered_before_the_heading(self):
        cands = T.title_candidates(self.block)
        texts = [t for t, _ in cands]
        self.assertIn(REAL_TITLE, texts)
        self.assertLess(
            texts.index(REAL_TITLE), next(i for i, t in enumerate(texts) if "November" in t)
        )

    def test_provenance_is_reported(self):
        via = dict((t, p) for t, p in T.title_candidates(self.block))
        self.assertEqual(via[REAL_TITLE], "p>strong")

    def test_pdf_filename_is_a_candidate(self):
        cands = T.title_candidates(None, source_link="https://x.uzh.ch/Lempel_Ziv_entropy_rate.pdf")
        self.assertEqual(cands[0], ("Lempel Ziv entropy rate", "pdf-filename"))

    def test_description_lead_is_a_candidate(self):
        cands = T.title_candidates(
            None, description="A Fine Thesis Topic: and then the details follow."
        )
        self.assertIn(("A Fine Thesis Topic", "description-lead"), cands)

    def test_spec_supplied_selectors_come_first(self):
        cands = T.title_candidates(self.block, extra_selectors=["p:nth-of-type(2)"])
        self.assertEqual(cands[0][1], "spec")


class RepairTest(unittest.TestCase):
    def _record(self, **over):
        rec = {
            "title": "November 3, 2021",
            "status": "taken",
            "date_of_listing": None,
            "research_area": "Database Technology",
            "topic_description": None,
            "source_link": None,
        }
        rec.update(over)
        return rec

    def test_case_a_promotes_the_plausible_candidate(self):
        block = BeautifulSoup(IFI5_BLOCK, "html.parser").select_one("div")
        rec = self._record()
        verdict = T.repair(rec, block)
        self.assertFalse(verdict.plausible)  # verdict is on the ORIGINAL
        self.assertEqual(rec["title"], REAL_TITLE)
        self.assertEqual(rec["_title_repair"]["via"], "p>strong")
        self.assertEqual(rec["_title_repair"]["from"], "November 3, 2021")

    def test_case_a_parks_a_date_in_date_of_listing(self):
        block = BeautifulSoup(IFI5_BLOCK, "html.parser").select_one("div")
        rec = self._record()
        T.repair(rec, block)
        self.assertEqual(rec["date_of_listing"], "November 3, 2021")
        self.assertNotIn("_title_rejected", rec)

    def test_case_a_parks_a_non_date_in_title_rejected(self):
        block = BeautifulSoup(IFI5_BLOCK, "html.parser").select_one("div")
        rec = self._record(title="Taken")
        T.repair(rec, block)
        self.assertEqual(rec["title"], REAL_TITLE)
        self.assertEqual(rec["_title_rejected"], "Taken")
        self.assertIsNone(rec["date_of_listing"])

    def test_case_a_does_not_overwrite_an_existing_date(self):
        block = BeautifulSoup(IFI5_BLOCK, "html.parser").select_one("div")
        rec = self._record(date_of_listing="2020-01-01")
        T.repair(rec, block)
        self.assertEqual(rec["date_of_listing"], "2020-01-01")
        self.assertEqual(rec["_title_rejected"], "November 3, 2021")

    def test_case_a_deduplicates_the_description(self):
        block = BeautifulSoup(IFI5_BLOCK, "html.parser").select_one("div")
        rec = self._record(
            topic_description=f"{REAL_TITLE}: The Lempel-Ziv algorithm is well known."
        )
        T.repair(rec, block)
        self.assertEqual(rec["topic_description"], "The Lempel-Ziv algorithm is well known.")

    def test_case_b_keeps_the_implausible_title_and_flags_it(self):
        bare = BeautifulSoup("<div><p>no title material here</p></div>", "html.parser").select_one(
            "div"
        )
        rec = self._record()
        T.repair(rec, bare)
        self.assertEqual(rec["title"], "November 3, 2021")  # reserved, not discarded
        self.assertNotIn("_title_repair", rec)
        self.assertIn("_title_check", rec)
        self.assertIn("date", rec["_title_check"]["reasons"][0])

    def test_plausible_title_is_left_completely_alone(self):
        block = BeautifulSoup(IFI5_BLOCK, "html.parser").select_one("div")
        rec = self._record(title="Fair Referee Assignment")
        self.assertIsNone(T.repair(rec, block))
        self.assertEqual(rec["title"], "Fair Referee Assignment")
        self.assertNotIn("_title_repair", rec)
        self.assertNotIn("_title_check", rec)

    def test_check_only_never_repairs(self):
        rec = self._record()
        T.check_only(rec)
        self.assertEqual(rec["title"], "November 3, 2021")
        self.assertIn("_title_check", rec)

    def test_check_only_ignores_a_record_without_a_title(self):
        rec = self._record(title=None)
        self.assertIsNone(T.check_only(rec))
        self.assertNotIn("_title_check", rec)

    def test_bookkeeping_keys_never_reach_the_public_view(self):
        block = BeautifulSoup(IFI5_BLOCK, "html.parser").select_one("div")
        rec = self._record(title="Taken")
        T.repair(rec, block)
        public = ST._clean_record(rec)
        self.assertNotIn("_title_repair", public)
        self.assertNotIn("_title_rejected", public)
        self.assertNotIn("_title_check", public)
        self.assertEqual(public["title"], REAL_TITLE)


class Ifi5EndToEndTest(unittest.TestCase):
    """The real page, replayed offline from its frozen snapshot."""

    @classmethod
    def setUpClass(cls):
        cls.records = R.replay("ifi--5")

    def test_the_broken_record_is_repaired(self):
        r = self.records[0]
        self.assertTrue(
            r["title"].startswith(
                "Optimization of Lempel-Ziv Algorithm for Entropy Rate Estimation"
            )
        )
        self.assertEqual(r["date_of_listing"], "November 3, 2021")
        self.assertTrue(r["topic_description"].startswith("The Lempel-Ziv algorithm"))
        self.assertEqual(r["status"], "taken")

    def test_the_other_eleven_records_are_untouched(self):
        for i, r in enumerate(self.records[1:], start=1):
            self.assertNotIn("_title_repair", r, f"record {i} unexpectedly repaired")
            self.assertNotIn("_title_check", r, f"record {i} unexpectedly flagged")

    def test_topic_id_is_seeded_from_the_repaired_title(self):
        from themis_scraper import spec_engine

        r = self.records[0]
        self.assertEqual(
            r["topic_id"],
            spec_engine._topic_id(
                "https://www.ifi.uzh.ch/en/dbtg/teaching/theses.html", r, ["title"]
            ),
        )


class CorpusCalibrationTest(unittest.TestCase):
    """The calibration gate. Every title in the committed golden baseline must
    pass the plausibility check, and the repair must have touched exactly one
    record. Tighten the heuristic too far and the first assertion fails; loosen a
    spec or widen the repair and the second one does — either way the corpus of
    50 real contracts is what decides, not intuition."""

    @classmethod
    def setUpClass(cls):
        cls.golden = json.load(open(R.GOLDEN_PATH, encoding="utf-8"))

    def test_every_stored_title_is_plausible(self):
        implausible, titled = [], 0
        for sid, records in self.golden.items():
            for i, rec in enumerate(records):
                title = rec.get("title")
                if not isinstance(title, str) or not title.strip():
                    continue
                titled += 1
                if not T.score_title(title, siblings=[rec.get("status")]).plausible:
                    implausible.append((sid, i, title))
        self.assertGreater(titled, 200, "corpus should carry the ~247 known titles")
        self.assertEqual(implausible, [], f"implausible titles stored: {implausible}")

    def test_the_repair_touched_exactly_one_record(self):
        repaired = [
            (sid, i, rec["_title_repair"]["via"])
            for sid, records in self.golden.items()
            for i, rec in enumerate(records)
            if rec.get("_title_repair")
        ]
        self.assertEqual(repaired, [("ifi--5", 0, "p>strong")], f"unexpected repairs: {repaired}")

    def test_no_record_is_left_flagged(self):
        flagged = [
            (sid, i)
            for sid, records in self.golden.items()
            for i, rec in enumerate(records)
            if rec.get("_title_check")
        ]
        self.assertEqual(flagged, [], f"records still flagged: {flagged}")


class NeedsReviewStatusTest(unittest.TestCase):
    def _classify(self, records, **over):
        kwargs = dict(
            cached=True,
            last_status=200,
            current_sha1="a" * 40,
            verified_sha1="a" * 40,
            records=records,
        )
        kwargs.update(over)
        return V.classify("src--1", "topics", **kwargs)

    def _ok_record(self, **over):
        rec = {
            "title": "A Perfectly Fine Thesis Topic",
            "topic_id": "x" * 40,
            "topic_description": "Some description.",
            "source_link": None,
        }
        rec.update(over)
        return rec

    def test_flagged_title_raises_needs_review(self):
        rec = self._ok_record(
            _title_check={"score": 0.0, "reasons": ["is a date"], "title": "November 3, 2021"}
        )
        res = self._classify([rec])
        self.assertEqual(res.status, V.NEEDS_REVIEW)
        self.assertIn("implausible title", res.reasons[0])

    def test_needs_review_stores_and_keeps_scraping(self):
        res = V.Result("src--1", V.NEEDS_REVIEW, "topics")
        self.assertTrue(res.flagged)  # shows up in the run report
        self.assertTrue(res.writable)  # data is still written
        self.assertFalse(V.quarantines(V.NEEDS_REVIEW))  # source keeps scraping

    def test_page_changed_is_not_masked_by_a_title_flag(self):
        rec = self._ok_record(_title_check={"score": 0.0, "reasons": ["is a date"]})
        res = self._classify([rec], verified_sha1="b" * 40)
        self.assertEqual(res.status, V.PAGE_CHANGED)

    def test_a_repair_is_reported_but_does_not_flag(self):
        rec = self._ok_record(
            _title_repair={
                "from": "November 3, 2021",
                "to": "Real Title",
                "via": "p>strong",
                "score": 1.0,
            }
        )
        res = self._classify([rec])
        self.assertEqual(res.status, V.OK)
        self.assertTrue(any("title repaired" in r for r in res.reasons))

    def test_clean_records_stay_ok(self):
        self.assertEqual(self._classify([self._ok_record()]).status, V.OK)


if __name__ == "__main__":
    unittest.main()
