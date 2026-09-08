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
from unittest import mock

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as mt  # noqa: E402


STATE = {
    "600623": {
        "big_meat_state": "big_meat_candidate",
        "big_meat_score": "8.00",
        "big_meat_aggressive_score": "7.50",
        "big_meat_first_seen_at": "2026-09-08",
        "mode": "pre_breakout+",
        "date": "2026-09-04",
        "buy_time": "14:52:25",
        "decision_id": "2026-09-04|slot-a|600623",
    },
    "002396": {
        "big_meat_state": "big_meat_confirmed",
        "big_meat_score": "9.00",
        "big_meat_confirmed_at": "2026-09-01",
        "date": "2026-08-26",
        "buy_time": "14:51:03",
    },
}


def rec_(code, **kw):
    """A closed record for the SAME episode as that code's state entry."""
    base = {"code": code, "status": "closed"}
    src = STATE.get(str(code).zfill(6), {})
    for f in ("date", "buy_time", "decision_id"):
        if f in src:
            base[f] = src[f]
    base.update(kw)
    return base


def seal(records, state=None):
    with mock.patch.object(mt, "_load_position_state",
                           return_value=dict(STATE if state is None else state)):
        return mt._seal_big_meat_outcome(records)


class SealingTests(unittest.TestCase):
    def test_a_closing_trade_keeps_its_label(self):
        """THE BUG. 002396 was the one big fish (+27.4%) and closed unlabelled."""
        rec = rec_("002396", pnl_pct="27.39")
        self.assertEqual(seal([rec]), 1)
        self.assertEqual(rec["big_meat_state"], "big_meat_confirmed")
        self.assertEqual(rec["big_meat_confirmed_at"], "2026-09-01")

    def test_the_score_is_sealed_with_the_state(self):
        """A label without its score cannot be calibrated, only counted."""
        rec = rec_("600623")
        seal([rec])
        self.assertEqual(rec["big_meat_score"], "8.00")
        self.assertEqual(rec["big_meat_aggressive_score"], "7.50")
        self.assertEqual(rec["big_meat_first_seen_at"], "2026-09-08")

    def test_a_holding_record_is_left_alone(self):
        """Live positions get these injected at runtime; writing them here
        would fight the live path for ownership of the same fields."""
        rec = rec_("600623", status="holding")
        self.assertEqual(seal([rec]), 0)
        self.assertNotIn("big_meat_state", rec)

    def test_an_existing_label_is_never_overwritten(self):
        """A sealed record is history. Re-running must not rewrite it."""
        rec = rec_("600623", big_meat_state="big_meat_confirmed", big_meat_score="9.90")
        self.assertEqual(seal([rec]), 0)
        self.assertEqual(rec["big_meat_state"], "big_meat_confirmed")
        self.assertEqual(rec["big_meat_score"], "9.90")

    def test_sealing_twice_changes_nothing(self):
        rec = rec_("002396")
        self.assertEqual(seal([rec]), 1)
        snapshot = dict(rec)
        self.assertEqual(seal([rec]), 0)
        self.assertEqual(rec, snapshot)

    def test_a_code_with_no_state_entry_is_skipped(self):
        """Positions predating the label must not gain a fabricated one."""
        rec = {"code": "999999", "status": "closed"}
        self.assertEqual(seal([rec]), 0)
        self.assertNotIn("big_meat_state", rec)

    def test_an_unlabelled_holding_seals_nothing(self):
        """Absent is not empty-string: a position never flagged stays unflagged."""
        rec = rec_("600623")
        self.assertEqual(seal([rec], state={"600623": {"big_meat_state": "", "date": rec["date"]}}), 0)
        self.assertNotIn("big_meat_state", rec)

    def test_codes_are_zero_padded_before_lookup(self):
        """Shanghai codes arrive as ints often enough to matter."""
        rec = rec_("002396"); rec["code"] = 2396
        self.assertEqual(seal([rec]), 1)
        self.assertEqual(rec["big_meat_state"], "big_meat_confirmed")

    def test_an_empty_state_file_is_survivable(self):
        rec = rec_("002396")
        self.assertEqual(seal([rec], state={}), 0)

    def test_junk_records_do_not_crash_the_save_path(self):
        """save_track_record runs on every sell. It must not raise here."""
        self.assertEqual(seal([None, "nonsense", 42, {}]), 0)


class GraftingTests(unittest.TestCase):
    """The failure that would have corrupted the record rather than fixed it.

    The state file is keyed by code and holds only what is open now. Every one
    of the 160 closed records is currently blank, so a code-only match would
    have back-filled today's labels across all of history on the first run.
    """

    def test_an_older_trade_in_the_same_stock_is_not_labelled(self):
        """We held 600623 in July, closed it, and hold it again today. The July
        trade never had a label and must not acquire today's."""
        old = {"code": "600623", "status": "closed", "date": "2026-07-14",
               "buy_time": "14:50:10", "pnl_pct": "-3.20"}
        self.assertEqual(seal([old]), 0)
        self.assertNotIn("big_meat_state", old)

    def test_a_different_decision_id_in_the_same_stock_is_refused(self):
        old = {"code": "600623", "status": "closed", "date": "2026-09-04",
               "buy_time": "14:52:25", "decision_id": "2026-07-14|slot-z|600623"}
        self.assertEqual(seal([old]), 0)
        self.assertNotIn("big_meat_state", old)

    def test_the_matching_episode_still_seals(self):
        """The guard must not refuse everything - that would be a silent no-op."""
        self.assertEqual(seal([rec_("600623")]), 1)

    def test_an_unjudgeable_record_is_refused_not_guessed(self):
        """No decision_id, no date: a wrong label is worse than a missing one."""
        bare = {"code": "600623", "status": "closed"}
        self.assertEqual(seal([bare]), 0)
        self.assertNotIn("big_meat_state", bare)

    def test_the_whole_real_history_would_gain_nothing_by_accident(self):
        """Simulates the first run over blank history: 3 old trades in names we
        hold today, none of them the open episode. Zero seals."""
        history = [
            {"code": "600623", "status": "closed", "date": "2026-07-14"},
            {"code": "002396", "status": "closed", "date": "2026-08-03"},
            {"code": "600623", "status": "closed", "date": "2026-08-20"},
        ]
        self.assertEqual(seal(history), 0)
        self.assertFalse(any("big_meat_state" in r for r in history))


class OrderingTests(unittest.TestCase):
    def test_the_seal_runs_before_the_prune(self):
        """The whole fix is ordering: the state entry and the closing record
        coexist for exactly one moment, and this is it."""
        src = Path(mt.__file__).read_text(encoding="utf-8")
        body = src[src.index("def save_track_record"):]
        self.assertLess(body.index("_seal_big_meat_outcome"),
                        body.index("!= 'holding'"))


class SafetyTests(unittest.TestCase):
    def test_it_holds_no_execution_path(self):
        src = Path(mt.__file__).read_text(encoding="utf-8")
        start = src.index("def _seal_big_meat_outcome")
        seg = src[start:src.index("def save_track_record")]
        for bad in ("buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading/trade", "mockTrading/cancel", "requests."):
            self.assertNotIn(bad, seg)

    def test_it_writes_no_files_of_its_own(self):
        src = Path(mt.__file__).read_text(encoding="utf-8")
        start = src.index("def _seal_big_meat_outcome")
        seg = src[start:src.index("def save_track_record")]
        for bad in ("_write_json_atomic", "open(", "_save_position_state"):
            self.assertNotIn(bad, seg)


if __name__ == "__main__":
    unittest.main()
