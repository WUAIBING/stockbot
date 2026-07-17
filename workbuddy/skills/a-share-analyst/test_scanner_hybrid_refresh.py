#!/usr/bin/env python3

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import scanner_v10 as scanner


class ScannerHybridRefreshTests(unittest.TestCase):
    def test_write_outputs_stringifies_non_string_display_fields(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "tier": 1,
                    "code": 123456,
                    "name": 7890,
                    "entry_price": 12.34,
                    "position": 1.0,
                    "mode": 7,
                    "bz_direction": 1.25,
                    "weekly_slope": 8.6,
                    "close_vs_ma20_pct": -0.8,
                    "signal_desc": 99,
                    "bz_rt_direction": 0.4,
                    "rsi14": 63.0,
                }
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            buf = io.StringIO()
            with (
                patch.object(scanner, "OUTPUT_DIR", Path(tmpdir)),
                redirect_stdout(buf),
            ):
                scanner._write_outputs(
                    df=df,
                    run_time="2026-07-16 14:49:00",
                    total_amt_yi=4200.0,
                    market_regime="normal",
                    amount_threshold=1.5e8,
                    scanned_count=1,
                )

            stdout = buf.getvalue()
            self.assertIn("123456", stdout)
            self.assertIn("7890", stdout)
            self.assertIn("Mode: 7", stdout)
            self.assertTrue((Path(tmpdir) / "v10_scan_latest.json").exists())

    def test_decision_fast_unions_live_candidates_with_cached_universe(self) -> None:
        cached = pd.DataFrame(
            [
                {
                    "code": "000001",
                    "name": "cached-only",
                    "market": 0,
                    "weekly_align": True,
                    "weekly_slope": 6.0,
                }
            ]
        )
        stocks = pd.DataFrame(
            [
                {"code": "000001", "name": "cached-only", "market": 0},
                {"code": "000002", "name": "late-mover", "market": 1},
            ]
        )
        filtered = [
            {"code": "000002", "name": "late-mover", "market": 1, "latest_amt": 2e8},
        ]
        built_rows = {
            "000001": {"code": "000001", "name": "cached-only", "market": 0, "tier": 0},
            "000002": {"code": "000002", "name": "late-mover", "market": 1, "tier": 2},
        }

        def fake_build_signal_row(_api, *, code, **_kwargs):
            return built_rows[code]

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "v10_scan_full.csv").write_text("placeholder\n", encoding="utf-8")

            with (
                patch.object(scanner, "OUTPUT_DIR", output_dir),
                patch.object(scanner.pd, "read_csv", return_value=cached),
                patch.object(scanner, "get_stock_list", return_value=stocks),
                patch.object(scanner, "_collect_amount_snapshot", return_value=[]),
                patch.object(
                    scanner,
                    "_select_amount_candidates",
                    return_value=(filtered, 4200.0, "正常市", scanner.SCAN_CONFIG["min_amount_yuan"]),
                ),
                patch.object(scanner, "_build_signal_row", side_effect=fake_build_signal_row),
                patch.object(scanner, "_main_strategy_debug_emit"),
                patch.object(scanner, "_write_outputs") as write_outputs,
            ):
                scanner._run_decision_fast(api=object(), run_time="2026-07-15 14:49:00")

        refreshed = write_outputs.call_args.kwargs["df"]
        self.assertEqual(set(refreshed["code"]), {"000001", "000002"})
        self.assertEqual(write_outputs.call_args.kwargs["scanned_count"], 2)

    def test_build_signal_row_refreshes_daily_sensitive_fields_before_classification(self) -> None:
        fresh_daily = {
            "close": 12.34,
            "entry_price": 12.34,
            "latest_amt": 3.2e8,
            "close_vs_ma20_pct": -1.2,
            "amt_ratio": 1.8,
            "rsi14": 58.0,
            "is_green": True,
            "vol_expand": True,
        }
        cached_row = {
            "weekly_align": True,
            "weekly_slope": 9.0,
            "close_vs_ma20_pct": 8.5,
            "amt_ratio": 0.6,
            "rsi14": 82.0,
            "is_green": False,
            "vol_expand": False,
            "latest_amt": 1.0e8,
        }

        with (
            patch.object(scanner, "fetch_daily_bars", return_value=object()),
            patch.object(scanner, "_compute_daily_snapshot", return_value=fresh_daily),
            patch.object(scanner, "fetch_5min_bars_today", return_value=object()),
            patch.object(scanner, "compute_5min_signal", return_value={"bz_direction": -0.5, "bz_rt_direction": -0.2, "bz_vol_ratio": 1.3}),
            patch.object(scanner, "classify_signal", return_value=(2, "trend_ride+vol", 0.6, "fresh")) as classify_signal,
        ):
            row = scanner._build_signal_row(
                api=object(),
                code="000001",
                name="fresh",
                market=0,
                latest_snapshot={"latest_amt": 4.4e8},
                cached_row=cached_row,
                include_5min=True,
            )

        self.assertIsNotNone(row)
        self.assertEqual(row["latest_amt"], 4.4e8)
        self.assertEqual(row["close_vs_ma20_pct"], -1.2)
        self.assertEqual(row["amt_ratio"], 1.8)
        self.assertEqual(row["rsi14"], 58.0)
        self.assertTrue(row["is_green"])
        self.assertTrue(row["vol_expand"])
        classify_signal.assert_called_once_with(
            -0.5,
            -0.2,
            True,
            9.0,
            -1.2,
            True,
            58.0,
            True,
            1.8,
        )


if __name__ == "__main__":
    unittest.main()
