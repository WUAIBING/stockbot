#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The model score must survive onto the trade record.

The score picks ~4 stocks from the ~950 that match a profile rule, so it does
virtually all of the stock selection. It was never written to the ledger, which
made it the one component of the system impossible to evaluate after the fact.

Measured against 9.1M stock-days, the rule layer it sits on top of is mildly
ANTI-predictive - every testable rule underperformed its period baseline in both
train and holdout (pre_breakout -0.21/-0.23, trend_ride_green -0.61/-0.43). So
either the score is carrying the whole strategy, or the strategy has no edge.
These fields are what will settle it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as t  # noqa: E402

DECISION = {
    "score": 76.9,
    "components": {"market": 57.0, "sector": 54.0, "stock": 84.0, "flow": 58.0},
    "industry": "通信设备",
    "tier": 3,
    "mode": "pre_breakout",
}


class FieldRegistrationTests(unittest.TestCase):
    def test_every_score_column_is_registered(self):
        """Unregistered keys are dropped by the CSV writer."""
        for f in ("model_score", "score_market", "score_sector",
                  "score_stock", "score_flow", "industry"):
            self.assertIn(f, t.TRACK_FIELDNAMES, f)

    def test_last_synced_at_is_still_final(self):
        self.assertEqual(t.TRACK_FIELDNAMES[-1], "last_synced_at")

    def test_no_duplicate_columns(self):
        self.assertEqual(len(t.TRACK_FIELDNAMES), len(set(t.TRACK_FIELDNAMES)))


class ContextTests(unittest.TestCase):
    def ctx(self, **kw):
        kw.setdefault("decision_row", DECISION)
        return t._build_runtime_record_context("002396", **kw)

    def test_score_is_captured_from_the_decision_row(self):
        c = self.ctx()
        self.assertEqual(float(c["model_score"]), 76.9)
        self.assertEqual(float(c["score_market"]), 57.0)
        self.assertEqual(float(c["score_sector"]), 54.0)
        self.assertEqual(float(c["score_stock"]), 84.0)
        self.assertEqual(float(c["score_flow"]), 58.0)
        self.assertEqual(c["industry"], "通信设备")

    def test_an_existing_value_is_not_overwritten(self):
        """Context is rebuilt on every holdings sync; the entry score must pin."""
        base = {"model_score": "88.8000", "score_stock": "91.0000",
                "industry": "半导体"}
        c = self.ctx(base_record=base)
        self.assertEqual(float(c["model_score"]), 88.8)
        self.assertEqual(float(c["score_stock"]), 91.0)
        self.assertEqual(c["industry"], "半导体")

    def test_a_missing_decision_row_leaves_fields_blank(self):
        c = t._build_runtime_record_context("002396")
        for f in ("model_score", "score_market", "score_stock", "industry"):
            self.assertEqual(c[f], "", f)

    def test_zero_scores_are_blank_not_zero(self):
        """A real 0 and 'never recorded' must not look the same later."""
        c = self.ctx(decision_row={"score": 0.0, "components": {}})
        self.assertEqual(c["model_score"], "")
        self.assertEqual(c["score_market"], "")

    def test_malformed_components_do_not_raise(self):
        for bad in (None, [], "nonsense", 5):
            c = self.ctx(decision_row={"score": 70.0, "components": bad})
            self.assertEqual(float(c["model_score"]), 70.0)
            self.assertEqual(c["score_market"], "")


class RecordTests(unittest.TestCase):
    def test_score_reaches_the_record(self):
        ctx = t._build_runtime_record_context("002396", decision_row=DECISION)
        rec = t._make_record_from_context("002396", pos={"count": 700,
                                                         "cost_price": 30.36}, ctx=ctx)
        self.assertEqual(float(rec["model_score"]), 76.9)
        self.assertEqual(float(rec["score_stock"]), 84.0)
        self.assertEqual(rec["industry"], "通信设备")

    def test_record_still_normalises_without_a_score(self):
        rec = t._make_record_from_context("002396", pos={"count": 100,
                                                         "cost_price": 10.0}, ctx={})
        for f in ("model_score", "score_market", "industry"):
            self.assertEqual(rec[f], "", f)
        self.assertEqual(rec["status"], "holding")

    def test_every_registered_column_is_present(self):
        """A record missing a column breaks the DictWriter on flush."""
        ctx = t._build_runtime_record_context("002396", decision_row=DECISION)
        rec = t._make_record_from_context("002396", ctx=ctx)
        for f in t.TRACK_FIELDNAMES:
            self.assertIn(f, rec, f)


if __name__ == "__main__":
    unittest.main()
