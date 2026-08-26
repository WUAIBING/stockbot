#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The two dominant exit rules stop selling, but keep reporting.

Over 40 days the exits were 97x 冲高回落 and 45x 连跌2日 against 7 T+5 expiries.
Neither predicts anything reliable: across 9.1M liquid stock-days, forward
returns AFTER they fire were +0.17/+0.23 versus baseline on 2015-2023 and
-0.03/-0.20 on 2024-2026. The sign flips and both magnitudes sit inside the
noise floor.

Weight 0 rather than deletion is the point. The reason text is still appended,
so evaluate_exit_rules.py can measure the counterfactual and say in a few weeks
whether silencing them was right. Deleting the rules would have bought the
longer holds at the cost of ever finding out.

连跌2日 also needed structural=False. That flag is read independently of score -
it sets has_confirmed_structural_break, which forces a hard exit whatever the
weight - so zeroing the score alone would have left it firing.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as t  # noqa: E402

SRC = inspect.getsource(t)


class ConstantTests(unittest.TestCase):
    def test_stop_is_a_backstop_not_a_primary_exit(self):
        """-3% fired on 67% of trades; -8% on 30%."""
        self.assertEqual(t.INTRADAY_HARD_STOP_PCT, -8.0)

    def test_hold_limit_matches_the_measured_optimum(self):
        """Net of cost: +5.60pp/yr at 10 sessions vs +3.80 at 5, -0.75 at 3."""
        self.assertEqual(t.MAX_HOLD_DAYS, 10)

    def test_stop_is_negative_and_hold_positive(self):
        self.assertLess(t.INTRADAY_HARD_STOP_PCT, 0)
        self.assertGreater(t.MAX_HOLD_DAYS, 0)


class SilencedTriggerTests(unittest.TestCase):
    """Both triggers must still record, and must no longer score."""

    def evidence_for(self, reason_fragment):
        """Extract the add_evidence call carrying this reason from the source."""
        # The reason text also appears in explanatory comments elsewhere, so
        # only look after add_evidence is defined.
        origin = SRC.find("def add_evidence(")
        self.assertNotEqual(origin, -1)
        idx = SRC.find(reason_fragment, origin)
        self.assertNotEqual(idx, -1, reason_fragment)
        start = SRC.rfind("add_evidence(", origin, idx)
        self.assertNotEqual(start, -1, reason_fragment)
        # A fixed window rather than the next ")": the reason is an f-string and
        # its own format spec closes a paren first.
        return SRC[start:start + 420]

    def test_upper_shadow_scores_zero(self):
        call = self.evidence_for("冲高回落上影线")
        self.assertIn("'daily_candle_reversal'", call)
        self.assertRegex(call, r"'daily_candle_reversal',\s*0,")

    def test_two_down_days_scores_zero(self):
        call = self.evidence_for('"连跌2日"')
        self.assertRegex(call, r"'daily_decline_sequence',\s*0,")

    def test_two_down_days_is_no_longer_structural(self):
        """structural=True forces a hard exit regardless of weight."""
        call = self.evidence_for('"连跌2日"')
        self.assertIn("structural=False", call)
        self.assertNotIn("structural=True", call)

    def test_both_reasons_are_still_emitted(self):
        """Silenced, not deleted - the counterfactual still needs the text."""
        self.assertIn("冲高回落上影线", SRC)
        self.assertIn("连跌2日", SRC)

    def test_weekly_trend_break_keeps_its_weight_and_structure(self):
        """A broken weekly trend is a different claim from two down days."""
        # Anchored on the f-string body: the plain phrase also appears in
        # the comment explaining why 连跌2日 was silenced.
        call = self.evidence_for("转负({ws:+.1f}%)")
        self.assertIn("'weekly_structure'", call)
        self.assertRegex(call, r"'weekly_structure',\s*3,")
        self.assertIn("structural=True", call)

    def test_add_evidence_records_reason_regardless_of_score(self):
        """The mechanism the whole approach depends on."""
        src = inspect.getsource(t).split("def add_evidence(", 1)[1]
        body = src.split("daily_bars", 1)[0]
        self.assertIn("reasons.append", body)


class SellRuleOrderTests(unittest.TestCase):
    def test_stop_is_checked_before_the_time_backstop(self):
        stop = SRC.find("硬止损")
        backstop = SRC.find("到期(持仓")
        self.assertNotEqual(stop, -1)
        self.assertNotEqual(backstop, -1)
        self.assertLess(stop, backstop,
                        "the stop must be evaluated before the time backstop")

    def test_stop_uses_the_constant_not_a_literal(self):
        self.assertIn("pnl_pct <= INTRADAY_HARD_STOP_PCT", SRC)

    def test_backstop_uses_the_constant_not_a_literal(self):
        self.assertIn("hold_days >= MAX_HOLD_DAYS", SRC)
        self.assertNotIn("if hold_days >= 5:\n            sell_reason", SRC)

    def test_the_two_rules_are_mutually_exclusive(self):
        """elif, so a stopped-out position is not also labelled an expiry."""
        i = SRC.find("pnl_pct <= INTRADAY_HARD_STOP_PCT")
        window = SRC[i:i + 700]
        self.assertIn("elif hold_days >= MAX_HOLD_DAYS", window)


if __name__ == "__main__":
    unittest.main()
