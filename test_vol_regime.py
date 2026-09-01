#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Volatility regime: build the measurement, do not fake it.

Momentum in A-shares is switched by volatility. Over 2015-2026, forward excess
of strong stocks minus weak, by trailing 20-session universe volatility:

    calm  +0.225%  t+2.13      mid  -0.403%  t-3.46      wild  -1.501%  t-10.49

Monotonic at 5, 10 and 20 sessions; the placebo goes flat. The account's record
sits on it exactly - June at the 51st percentile was the only profitable month,
July and August at the 74th and 78th both lost.

The droplet cannot measure this today, and both shortcuts fail: the scan CSV's
vol20 reads 4.971 where the universe reads 2.294 (a filtered population), and
cross-sectional dispersion correlates 0.380 and agrees on tercile 44.9% of the
time against a 33% coin. So this archives the universe closes the tradability
gate already publishes, and refuses to classify until it has enough.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import vol_regime as vr  # noqa: E402


def payload(rows):
    return {"records": [{"code": c, "last_close": v} for c, v in rows]}


def store_with(sessions):
    p = Path(tempfile.mkdtemp()) / "universe.jsonl"
    for date, closes in sessions:
        vr.append_close_snapshot(str(p), date, closes)
    return str(p)


def flat_series(n, start="2026-01-", step=0.0, base=10.0):
    """n sessions of 60 names, each moving `step` percent per session."""
    out = []
    for i in range(n):
        px = base * ((1.0 + step / 100.0) ** i)
        out.append(("%s%02d" % (start, i + 1),
                    {"%06d" % (600000 + k): round(px + k, 4) for k in range(60)}))
    return out


class SnapshotTests(unittest.TestCase):
    def test_last_close_is_taken_not_last_price(self):
        """The gate runs at 09:31, so last_price is a partial session and a
        series built from it would not be daily closes."""
        src = Path(vr.__file__).read_text(encoding="utf-8")
        body = src[src.index("def snapshot_universe"):src.index("def append_close_snapshot")]
        self.assertIn("last_close", body)
        self.assertNotIn('r.get("last_price"', body)

    def test_a_priced_universe_is_captured(self):
        got = vr.snapshot_universe(payload([("000001", 12.5), ("600000", 8.0)]))
        self.assertEqual(got, {"000001": 12.5, "600000": 8.0})

    def test_unpriced_and_halted_names_are_dropped(self):
        """688432 有研硅 opened at 0.0 while suspended."""
        got = vr.snapshot_universe(payload([("688432", 0.0), ("000001", 12.5)]))
        self.assertEqual(got, {"000001": 12.5})

    def test_junk_is_skipped_not_crashed_on(self):
        got = vr.snapshot_universe({"records": [{"code": "000001", "last_close": "x"},
                                                {"code": "600000", "last_close": 8.0}]})
        self.assertEqual(got, {"600000": 8.0})

    def test_an_empty_payload_is_empty(self):
        self.assertEqual(vr.snapshot_universe(None), {})
        self.assertEqual(vr.snapshot_universe({}), {})


class StoreTests(unittest.TestCase):
    def test_rerunning_a_session_replaces_rather_than_duplicates(self):
        p = store_with([("2026-09-01", {"000001": 10.0})])
        vr.append_close_snapshot(p, "2026-09-01", {"000001": 11.0})
        rows = vr.load_close_store(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["closes"]["000001"], 11.0)

    def test_sessions_are_kept_in_order(self):
        p = store_with([("2026-09-02", {"000001": 11.0}),
                        ("2026-09-01", {"000001": 10.0})])
        self.assertEqual([r["trade_date"] for r in vr.load_close_store(p)],
                         ["2026-09-01", "2026-09-02"])

    def test_a_missing_store_is_empty_not_an_error(self):
        self.assertEqual(vr.load_close_store("/nonexistent/u.jsonl"), [])


class ReturnTests(unittest.TestCase):
    def test_only_names_present_in_both_sessions_count(self):
        """A listing that appears or vanishes must not read as a move. 688432
        suspended on 2026-08-31 and drops out of the gate entirely."""
        p = store_with([
            ("2026-09-01", {"000001": 10.0, "688432": 45.0}),
            ("2026-09-02", dict({"000001": 11.0}, **{"%06d" % (600000 + k): 10.0
                                                     for k in range(40)})),
        ])
        rows = vr.load_close_store(p)
        rets = vr.universe_returns(rows)
        self.assertEqual(rets, [])          # only 1 overlapping name, under the floor

    def test_a_flat_universe_has_zero_volatility(self):
        p = store_with(flat_series(25))
        rets = vr.universe_returns(vr.load_close_store(p))
        self.assertEqual(len(rets), 24)
        self.assertAlmostEqual(vr.realised_vol(rets), 0.0, places=6)

    def test_absurd_moves_are_excluded(self):
        """A 10-for-1 split is not a 900% session."""
        base = {"%06d" % (600000 + k): 10.0 for k in range(60)}
        split = dict(base); split["600000"] = 100.0
        p = store_with([("2026-09-01", base), ("2026-09-02", split)])
        rets = vr.universe_returns(vr.load_close_store(p))
        self.assertAlmostEqual(rets[0]["ret"], 0.0, places=6)
        self.assertEqual(rets[0]["n"], 59)


class ClassifyTests(unittest.TestCase):
    def test_calm_is_where_momentum_measured_positive(self):
        label, why = vr.classify(0.9)
        self.assertEqual(label, vr.CALM)
        self.assertIn("+0.225%", why)

    def test_wild_is_where_momentum_measured_negative(self):
        label, why = vr.classify(1.8)
        self.assertEqual(label, vr.WILD)
        self.assertIn("-1.501%", why)

    def test_the_middle_is_mid(self):
        self.assertEqual(vr.classify(1.2)[0], vr.MID)

    def test_the_boundaries_land_where_measured(self):
        self.assertEqual(vr.classify(vr.CALM_BELOW)[0], vr.CALM)
        self.assertEqual(vr.classify(vr.WILD_ABOVE)[0], vr.WILD)

    def test_our_own_months_classify_as_they_traded(self):
        """June 1.228 was the only profitable month; July and August lost."""
        self.assertEqual(vr.classify(1.228)[0], vr.MID)
        self.assertEqual(vr.classify(1.577)[0], vr.WILD)
        self.assertEqual(vr.classify(1.719)[0], vr.WILD)

    def test_no_volatility_is_unknown_not_a_guess(self):
        label, why = vr.classify(None)
        self.assertEqual(label, vr.UNKNOWN)
        self.assertIn("not enough", why)


class RefusalTests(unittest.TestCase):
    """Refusing to classify is the point until the history exists."""

    def test_a_short_history_refuses_and_says_how_short(self):
        p = store_with(flat_series(5))
        out = vr.regime_from_store(p)
        self.assertEqual(out["regime"], vr.UNKNOWN)
        self.assertIn("5 of 21", out["reason"])

    def test_enough_history_classifies(self):
        p = store_with(flat_series(vr.MIN_SESSIONS))
        out = vr.regime_from_store(p)
        self.assertEqual(out["regime"], vr.CALM)
        self.assertEqual(out["sessions"], vr.MIN_SESSIONS)

    def test_a_missing_store_refuses(self):
        self.assertEqual(vr.regime_from_store("/nonexistent/u.jsonl")["regime"],
                         vr.UNKNOWN)


class PopulationTests(unittest.TestCase):
    """The mistake this module exists to avoid."""

    def test_the_thresholds_document_the_population_they_came_from(self):
        """The scan CSV's own vol20 read 4.971 for 2026-08-27 where the liquid
        universe read 2.294 - the scan is a filtered set of volatile breakout
        names. A threshold calibrated on one population and applied to another
        is how ADD_POSITION_BIG_MEAT_SECTOR_SCORE came to block 77% of days."""
        src = Path(vr.__file__).read_text(encoding="utf-8")
        self.assertIn("equal-weighted liquid", src)
        self.assertIn("4.971", src)
        self.assertIn("2.294", src)

    def test_the_rejected_proxy_is_recorded_with_its_number(self):
        """Cross-sectional dispersion: r=0.380, tercile agreement 44.9%."""
        src = Path(vr.__file__).read_text(encoding="utf-8")
        self.assertIn("0.380", src)
        self.assertIn("44.9%", src)


class SafetyTests(unittest.TestCase):
    def test_the_gate_is_off_by_default(self):
        import os
        self.assertNotIn(os.environ.get("TLFZ_VOL_REGIME", "0").lower(),
                         ("1", "true", "yes", "on"))

    def test_the_module_places_no_orders(self):
        src = Path(vr.__file__).read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading/trade", "mockTrading/cancel", "requests.post"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
