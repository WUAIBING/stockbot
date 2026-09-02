#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Volatility regime, calibrated on the universe actually archived.

Momentum in A-shares is switched by volatility. On the gate universe over
2015-2026, forward excess of strong stocks minus weak:

    calm  +0.152%  t+1.47     mid  -0.284%  t-2.41     wild  -1.596%  t-11.02

Monotonic at 5, 10 and 20 sessions; the placebo goes flat. The account record
sits on it - June at the 51st percentile was its only profitable month, July and
August at the 74th and 78th both lost.

THE UNIVERSE IS CSI 1000. The 09:31 gate publishes the constituent list from
000852cons.xls - 1,042 names with every large cap excluded (600519, 601398,
300750 all absent). The first cut of this module shipped whole-market cuts of
1.0390/1.4220, which on the archived series read CALM 28% / mid 34% / WILD 38%
instead of 33/33/33. Mild only by luck: the two series correlate 0.996. The same
error with the scan CSV vol20 column would not have been - r=0.380, agreeing
44.9% of the time against a 33% coin.

So the thresholds are recalibrated on the gate universe, and a guard refuses to
classify if that universe drifts - because widening the gate to the whole market
is the obvious next fix, and it would silently invalidate these numbers.
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
        self.assertIn("+0.152%", why)

    def test_wild_is_where_momentum_measured_negative(self):
        label, why = vr.classify(1.8)
        self.assertEqual(label, vr.WILD)
        self.assertIn("-1.596%", why)

    def test_the_middle_is_mid(self):
        self.assertEqual(vr.classify(1.2)[0], vr.MID)

    def test_the_cuts_are_the_gate_universe_ones_not_the_market_ones(self):
        """Whole-market cuts of 1.0390/1.4220 on this series read 28/34/38
        instead of 33/33/33. Population before threshold."""
        self.assertAlmostEqual(vr.CALM_BELOW, 1.0969, places=4)
        self.assertAlmostEqual(vr.WILD_ABOVE, 1.4802, places=4)
        self.assertGreater(vr.CALM_BELOW, 1.039)

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
        """Universe sized as calibrated, so the guard lets it through."""
        pad = vr.CALIBRATION_UNIVERSE_SIZE - 60
        sessions = [(d, dict(c, **{"%06d" % (700000 + k): 20.0 for k in range(pad)}))
                    for d, c in flat_series(vr.MIN_SESSIONS)]
        out = vr.regime_from_store(store_with(sessions))
        self.assertEqual(out["regime"], vr.CALM)
        self.assertEqual(out["sessions"], vr.MIN_SESSIONS)

    def test_a_missing_store_refuses(self):
        self.assertEqual(vr.regime_from_store("/nonexistent/u.jsonl")["regime"],
                         vr.UNKNOWN)


class UniverseGuardTests(unittest.TestCase):
    """Widening the gate must break loudly, not silently."""

    def rows(self, n, sessions=25):
        return [{"trade_date": "2026-01-%02d" % (i + 1), "n": n, "closes": {}}
                for i in range(sessions)]

    def test_the_calibrated_universe_passes(self):
        ok, why = vr.universe_matches_calibration(self.rows(1037))
        self.assertTrue(ok)
        self.assertIn("as calibrated", why)

    def test_a_whole_market_gate_is_refused(self):
        """5,505 codes sit in tdxhy.cfg; widening the gate to them would make
        these thresholds describe a population never measured on."""
        ok, why = vr.universe_matches_calibration(self.rows(5505))
        self.assertFalse(ok)
        self.assertIn("recalibrate", why)

    def test_a_collapsed_universe_is_refused(self):
        self.assertFalse(vr.universe_matches_calibration(self.rows(120))[0])

    def test_normal_drift_is_tolerated(self):
        """Constituents change and names suspend; not a new population."""
        for n in (900, 1000, 1100, 1250):
            self.assertTrue(vr.universe_matches_calibration(self.rows(n))[0], n)

    def test_only_recent_sessions_decide(self):
        """An old narrow stretch must not veto a healthy current universe."""
        rows = self.rows(200, sessions=20) + self.rows(1037, sessions=5)
        self.assertTrue(vr.universe_matches_calibration(rows)[0])

    def test_sessions_without_a_size_refuse(self):
        rows = [{"trade_date": "2026-01-01", "n": 0, "closes": {}}]
        self.assertFalse(vr.universe_matches_calibration(rows)[0])

    def test_the_store_refuses_when_the_universe_drifted(self):
        p = store_with([("2026-01-%02d" % (i + 1),
                         {"%06d" % (600000 + k): 10.0 for k in range(60)})
                        for i in range(vr.MIN_SESSIONS)])
        out = vr.regime_from_store(p)
        self.assertEqual(out["regime"], vr.UNKNOWN)
        self.assertIn("recalibrate", out["reason"])


class PopulationTests(unittest.TestCase):
    """The mistake this module exists to avoid."""

    def test_the_thresholds_document_the_population_they_came_from(self):
        """The scan CSV's own vol20 read 4.971 for 2026-08-27 where the liquid
        universe read 2.294 - the scan is a filtered set of volatile breakout
        names. A threshold calibrated on one population and applied to another
        is how ADD_POSITION_BIG_MEAT_SECTOR_SCORE came to block 77% of days."""
        src = Path(vr.__file__).read_text(encoding="utf-8")
        self.assertIn("CSI 1000", src)
        self.assertIn("4.971", src)
        self.assertIn("2.294", src)
        self.assertIn("000852cons.xls", src)

    def test_the_rejected_proxy_is_recorded_with_its_number(self):
        """Cross-sectional dispersion: r=0.380, tercile agreement 44.9%."""
        src = Path(vr.__file__).read_text(encoding="utf-8")
        self.assertIn("0.380", src)
        self.assertIn("44.9%", src)

    def test_the_breadth_caveat_is_recorded(self):
        """Top-300 minus next-1000 breadth ranges -18.9 to +23.1 points, so a
        CSI 1000 reading is not a market reading and no constant corrects it."""
        src = Path(vr.__file__).read_text(encoding="utf-8")
        self.assertIn("-18.9", src)
        self.assertIn("+23.1", src)


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
