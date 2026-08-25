"""Contract replay — the offline regression net for the spec engine (plan §6).

Every topics/people spec is replayed against its frozen snapshot with NO network
and must reproduce the committed golden baseline exactly. A failure here means an
edit to the spec engine or a spec.yaml changed what a source extracts. If the
change is intended, regenerate the baseline with `python tests/regen_golden.py`
and commit it.

Run:  python -m unittest -v tests.scraper.test_specs
"""

from __future__ import annotations

import unittest

from . import replay_util as R


class ContractReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ids = R.replayable_ids()
        cls.golden = R.load_golden()

    def test_golden_baseline_present(self):
        """The golden baseline exists and is non-empty (regen if this fails)."""
        self.assertTrue(
            self.golden,
            "no golden baseline — run `python tests/regen_golden.py`",
        )

    def test_contracts_discovered(self):
        """There are contracts to replay (guards a broken discovery/path)."""
        self.assertGreater(len(self.ids), 0, "no replayable contracts found")

    def test_every_contract_reproduces_golden(self):
        """Offline extraction of each contract equals its golden records."""
        for source_id in self.ids:
            with self.subTest(source=source_id):
                self.assertIn(
                    source_id,
                    self.golden,
                    f"{source_id} not in golden — run tests/regen_golden.py",
                )
                self.assertEqual(
                    R.replay(source_id),
                    self.golden[source_id],
                    f"{source_id} extraction drifted from the golden baseline",
                )

    def test_extraction_is_deterministic(self):
        """Replaying the same snapshot+spec twice yields identical records."""
        for source_id in self.ids:
            with self.subTest(source=source_id):
                self.assertEqual(R.replay(source_id), R.replay(source_id))

    def test_no_orphan_golden_entries(self):
        """Every golden entry still maps to a replayable contract."""
        orphans = sorted(set(self.golden) - set(self.ids))
        self.assertFalse(
            orphans,
            f"golden has entries with no current contract: {orphans} — run tests/regen_golden.py",
        )

    def test_topic_records_carry_stable_ids(self):
        """Every replayed topics record has a non-empty topic_id (needed for
        dedupe and the record-level diff)."""
        for source_id in self.ids:
            for i, rec in enumerate(self.golden.get(source_id, [])):
                if rec.get("topic_id") is not None or "topic_id" in rec:
                    with self.subTest(source=source_id, rec=i):
                        self.assertTrue(
                            rec.get("topic_id"), f"{source_id}[{i}] has an empty topic_id"
                        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
