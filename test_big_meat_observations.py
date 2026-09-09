#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The big-meat label has to survive the trade it describes.

It did not. The label lives in v10_position_state.json, which keeps only
status=='holding' rows, so the entry vanished in the same pass the trade
closed. Across 160 closed records in three stores it is empty in every one,
while eight live positions carry 'big_meat_candidate' today.

That makes the flag unfalsifiable: months of labelling, never once scored
against an outcome. These tests pin the seal that fixes it, and - just as
importantly - pin that it cannot touch anything else.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
import json
import shutil
import tempfile
from unittest import mock

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as mt  # noqa: E402


STATE_ROWS = [
    {"key": "600623|2026-09-07", "code": "600623", "date": "2026-09-07",
     "observed_on": "2026-09-08", "big_meat_state": "big_meat_candidate",
     "big_meat_score": "8.00", "big_meat_first_seen_at": "2026-09-08"},
    {"key": "600623|2026-09-07", "code": "600623", "date": "2026-09-07",
     "observed_on": "2026-09-09", "big_meat_state": "big_meat_candidate",
     "big_meat_score": "9.20", "big_meat_first_seen_at": "2026-09-08"},
    {"key": "002605|2026-09-04", "code": "002605", "date": "2026-09-04",
     "observed_on": "2026-09-08", "big_meat_state": "big_meat_candidate",
     "big_meat_score": "6.00"},
]


class ObservationLogTests(unittest.TestCase):
    """The log is the only thing that survives; everything else is rebuilt."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = str(Path(self.tmp) / "obs.jsonl")
        self.patch = mock.patch.object(mt, "BIG_MEAT_OBSERVATIONS_FILE", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, rows):
        with open(self.path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_a_holding_label_is_written(self):
        n = mt._record_big_meat_observations([
            {"code": "600623", "status": "holding", "date": "2026-09-07",
             "big_meat_state": "big_meat_candidate", "big_meat_score": "8.00"}])
        self.assertEqual(n, 1)
        self.assertTrue(Path(self.path).exists())

    def test_an_unlabelled_holding_writes_nothing(self):
        self.assertEqual(mt._record_big_meat_observations([
            {"code": "600623", "status": "holding", "date": "2026-09-07"}]), 0)

    def test_a_closed_record_writes_nothing(self):
        """Only live positions carry a current label; closed ones are rebuilt."""
        self.assertEqual(mt._record_big_meat_observations([
            {"code": "600623", "status": "closed", "date": "2026-09-07",
             "big_meat_state": "big_meat_candidate"}]), 0)

    def test_rerunning_a_phase_does_not_duplicate(self):
        rec = [{"code": "600623", "status": "holding", "date": "2026-09-07",
                "big_meat_state": "big_meat_candidate", "big_meat_score": "8.00"}]
        self.assertEqual(mt._record_big_meat_observations(rec), 1)
        self.assertEqual(mt._record_big_meat_observations(rec), 0)

    def test_the_peak_score_is_kept_not_the_last(self):
        """A label that weakened before the close still had its best day."""
        self.write(STATE_ROWS)
        idx = mt._load_big_meat_observation_index()
        got = mt._big_meat_history_for({"code": "600623", "date": "2026-09-07"}, idx)
        self.assertEqual(got["big_meat_peak_score"], "9.20")
        self.assertEqual(got["big_meat_observed_days"], "2")

    def test_a_label_that_went_blank_is_still_recovered(self):
        """600649 was a candidate on 09-08 and blank on 09-09 while still held.
        The trade must still be scoreable as one that carried the label."""
        self.write([STATE_ROWS[2]])
        idx = mt._load_big_meat_observation_index()
        got = mt._big_meat_history_for(
            {"code": "002605", "date": "2026-09-04", "big_meat_state": ""}, idx)
        self.assertEqual(got["big_meat_state"], "big_meat_candidate")

    def test_a_different_buy_date_in_the_same_stock_gets_nothing(self):
        """The grafting guard, now on the episode key."""
        self.write(STATE_ROWS)
        idx = mt._load_big_meat_observation_index()
        self.assertEqual(
            mt._big_meat_history_for({"code": "600623", "date": "2026-07-14"}, idx), {})

    def test_a_never_labelled_trade_gets_nothing(self):
        self.write(STATE_ROWS)
        idx = mt._load_big_meat_observation_index()
        self.assertEqual(
            mt._big_meat_history_for({"code": "999999", "date": "2026-09-07"}, idx), {})

    def test_a_missing_log_is_survivable(self):
        self.assertEqual(mt._load_big_meat_observation_index(), {})

    def test_a_record_without_a_date_is_refused(self):
        """No identity, no label - a wrong one is worse than a missing one."""
        self.write(STATE_ROWS)
        idx = mt._load_big_meat_observation_index()
        self.assertEqual(mt._big_meat_history_for({"code": "600623"}, idx), {})

    def test_junk_records_do_not_crash_the_save_path(self):
        self.assertEqual(mt._record_big_meat_observations([None, "x", 42, {}]), 0)


class WiringTests(unittest.TestCase):
    """Yesterday's fix passed every unit test and was inert. These pin the path."""

    def test_the_recorder_runs_inside_save_track_record(self):
        src = Path(mt.__file__).read_text(encoding="utf-8")
        body = src[src.index("def save_track_record"):]
        self.assertIn("_record_big_meat_observations", body[:900])

    def test_the_episode_builder_consults_the_log(self):
        src = Path(mt.__file__).read_text(encoding="utf-8")
        body = src[src.index("def _build_trade_episode_history"):]
        self.assertIn("_load_big_meat_observation_index", body[:2000])
        self.assertIn("_big_meat_history_for", body[:6000])

    def test_the_label_reaches_the_emitted_episode(self):
        src = Path(mt.__file__).read_text(encoding="utf-8")
        body = src[src.index("def _build_trade_episode_history"):]
        self.assertIn("'big_meat_peak_score'", body[:14000])

    def test_the_inert_seal_is_gone(self):
        src = Path(mt.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_seal_big_meat_outcome", src)


class SafetyTests(unittest.TestCase):
    def test_it_holds_no_execution_path(self):
        src = Path(mt.__file__).read_text(encoding="utf-8")
        seg = src[src.index("def _record_big_meat_observations"):src.index("def save_track_record")]
        for bad in ("buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading/trade", "mockTrading/cancel", "requests."):
            self.assertNotIn(bad, seg)

    def test_it_only_appends(self):
        src = Path(mt.__file__).read_text(encoding="utf-8")
        seg = src[src.index("def _record_big_meat_observations"):src.index("def _load_big_meat_observation_index")]
        self.assertIn("'a'", seg)
        self.assertNotIn("'w'", seg)


if __name__ == "__main__":
    unittest.main()
