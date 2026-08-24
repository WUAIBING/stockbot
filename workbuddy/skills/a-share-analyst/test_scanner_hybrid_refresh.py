#!/usr/bin/env python3

from __future__ import annotations

import io
import json
import hashlib
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import evolving_model
import scanner_v10 as scanner


class ScannerHybridRefreshTests(unittest.TestCase):
    def test_classify_signal_prefers_trend_ride_for_strong_trend_mild_pullback(self) -> None:
        tier, mode, position, desc = scanner.classify_signal(
            -0.2,
            -0.05,
            True,
            8.5,
            -1.0,
            True,
            58.0,
            True,
            1.8,
        )

        self.assertEqual(tier, 2)
        self.assertEqual(mode, "trend_ride+vol")
        self.assertEqual(position, 0.6)
        self.assertIn("vol_expand", desc)

    def test_classify_signal_rejects_near_kill_when_tail_is_still_weakening(self) -> None:
        tier, mode, position, desc = scanner.classify_signal(
            -0.2,
            -0.28,
            True,
            3.5,
            -1.0,
            False,
            54.0,
            False,
            1.1,
        )

        self.assertEqual((tier, mode, position, desc), (0, "no_signal", 0.0, ""))

    def test_classify_signal_keeps_near_kill_when_pullback_has_stabilized(self) -> None:
        tier, mode, position, desc = scanner.classify_signal(
            -0.2,
            -0.06,
            True,
            3.5,
            -1.0,
            False,
            54.0,
            False,
            1.1,
        )

        self.assertEqual(tier, 2)
        self.assertEqual(mode, "near_kill+weekly+MA20")
        self.assertEqual(position, 0.5)
        self.assertIn("stabilizing", desc)

    def test_evolving_model_penalizes_near_kill_vs_trend_ride(self) -> None:
        base_row = {
            "code": "000001",
            "tier": 2,
            "weekly_slope": 8.0,
            "close_vs_ma20_pct": -0.8,
            "amt_ratio": 1.7,
            "rsi14": 58.0,
            "is_green": True,
            "bz_direction": -0.15,
            "bz_rt_direction": -0.04,
            "vol_expand": True,
        }
        context = {
            "state": {"weights": dict(evolving_model.DEFAULT_WEIGHTS), "tier_bias": {"2": 2.0}, "mode_bias": {}},
            "market": {"score": 62.0, "breakdown": {}},
            "industry_map": {},
            "sector_stats": {},
        }

        near_kill_score = evolving_model.score_row(
            {**base_row, "mode": "near_kill+weekly+MA20"},
            context,
        )["score"]
        trend_ride_score = evolving_model.score_row(
            {**base_row, "mode": "trend_ride+vol"},
            context,
        )["score"]

        self.assertGreater(trend_ride_score, near_kill_score)
        self.assertGreaterEqual(trend_ride_score - near_kill_score, 4.0)

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

    def test_write_outputs_accepts_empty_dataframe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(scanner, "OUTPUT_DIR", Path(tmpdir)):
                scanner._write_outputs(
                    df=pd.DataFrame(),
                    run_time="2026-08-14 14:50:00",
                    total_amt_yi=0.0,
                    market_regime="unknown",
                    amount_threshold=1.5e8,
                    scanned_count=0,
                )

            self.assertTrue((Path(tmpdir) / "v10_scan_full.csv").exists())

    def test_collect_amount_snapshot_batches_quotes_and_maps_fields(self) -> None:
        stocks = pd.DataFrame([
            {
                "code": f"{index:06d}",
                "name": f"stock-{index}",
                "market": index % 2,
            }
            for index in range(1, 162)
        ])
        requested_batches = []

        class FakeApi:
            def get_security_quotes(self, securities):
                requested_batches.append(securities)
                return [
                    {
                        "amount": int(code) * 1000.0,
                        "price": int(code) / 10.0,
                    }
                    for _market, code in securities
                ]

        snapshot = scanner._collect_amount_snapshot(FakeApi(), stocks)

        self.assertEqual([len(batch) for batch in requested_batches], [80, 80, 1])
        self.assertTrue(all(len(batch) <= 80 for batch in requested_batches))
        self.assertEqual(len(snapshot), 161)
        self.assertEqual(snapshot[0], {
            "code": "000001",
            "name": "stock-1",
            "market": 1,
            "latest_amt": 1000.0,
            "last_close": 0.1,
        })
        self.assertEqual(snapshot[-1]["latest_amt"], 161000.0)
        self.assertEqual(snapshot[-1]["last_close"], 16.1)

    def test_collect_amount_snapshot_maps_reordered_partial_quotes_by_code(self) -> None:
        stocks = pd.DataFrame([
            {"code": "000001", "name": "Alpha", "market": 0},
            {"code": "000002", "name": "Beta", "market": 0},
            {"code": "000003", "name": "Gamma", "market": 0},
        ])

        class FakeApi:
            def get_security_quotes(self, _securities):
                return [
                    {"code": "000003", "amount": 3000.0, "price": 30.0},
                    {"code": "000001", "amount": 1000.0, "price": 10.0},
                ]

        snapshot = scanner._collect_amount_snapshot(FakeApi(), stocks)

        self.assertEqual([row["code"] for row in snapshot], ["000001", "000003"])
        self.assertEqual(snapshot[0]["name"], "Alpha")
        self.assertEqual(snapshot[0]["latest_amt"], 1000.0)
        self.assertEqual(snapshot[1]["name"], "Gamma")
        self.assertEqual(snapshot[1]["last_close"], 30.0)

    def test_collect_amount_snapshot_rejects_partial_uncoded_response(self) -> None:
        stocks = pd.DataFrame([
            {"code": "000001", "name": "Alpha", "market": 0},
            {"code": "000002", "name": "Beta", "market": 0},
        ])

        class FakeApi:
            def get_security_quotes(self, _securities):
                return [{"amount": 1000.0, "price": 10.0}]

        buf = io.StringIO()
        with redirect_stdout(buf):
            snapshot = scanner._collect_amount_snapshot(FakeApi(), stocks)

        self.assertEqual(snapshot, [])
        self.assertIn("uncoded response count 1 != request count 2", buf.getvalue())

    def test_collect_amount_snapshot_treats_null_codes_as_uncoded_positional_response(self) -> None:
        stocks = pd.DataFrame([
            {"code": "000001", "name": "Alpha", "market": 0},
            {"code": "000002", "name": "Beta", "market": 0},
        ])

        class FakeApi:
            def get_security_quotes(self, _securities):
                return [
                    {"code": None, "amount": 1000.0, "price": 10.0},
                    {"code": float("nan"), "amount": 2000.0, "price": 20.0},
                ]

        snapshot = scanner._collect_amount_snapshot(FakeApi(), stocks)

        self.assertEqual([row["code"] for row in snapshot], ["000001", "000002"])
        self.assertEqual([row["latest_amt"] for row in snapshot], [1000.0, 2000.0])

    def test_collect_amount_snapshot_accepts_api_to_df_response(self) -> None:
        raw_quotes = object()

        class FakeApi:
            def __init__(self):
                self.converted = None

            def get_security_quotes(self, securities):
                self.securities = securities
                return raw_quotes

            def to_df(self, response):
                self.converted = response
                return pd.DataFrame([{"amount": 123000.0, "price": 12.3}])

        api = FakeApi()
        snapshot = scanner._collect_amount_snapshot(
            api,
            pd.DataFrame([{"code": "000001", "name": "Alpha", "market": 0}]),
        )

        self.assertEqual(api.securities, [(0, "000001")])
        self.assertIs(api.converted, raw_quotes)
        self.assertEqual(snapshot, [{
            "code": "000001",
            "name": "Alpha",
            "market": 0,
            "latest_amt": 123000.0,
            "last_close": 12.3,
        }])

    def test_collect_amount_snapshot_skips_failed_batch_with_warning(self) -> None:
        stocks = pd.DataFrame([
            {"code": f"{index:06d}", "name": str(index), "market": index % 2}
            for index in range(1, 162)
        ])

        class FakeApi:
            def __init__(self):
                self.call_count = 0

            def get_security_quotes(self, securities):
                self.call_count += 1
                if self.call_count == 2:
                    raise RuntimeError("quote timeout")
                return [{"amount": 1.0, "price": 2.0} for _security in securities]

        buf = io.StringIO()
        with redirect_stdout(buf):
            snapshot = scanner._collect_amount_snapshot(FakeApi(), stocks)

        self.assertEqual(len(snapshot), 81)
        self.assertIn("[WARN] quote batch 2 skipped", buf.getvalue())

    def test_decision_output_publishes_complete_dedicated_pointer(self) -> None:
        df = pd.DataFrame([{
            "code": "000001",
            "tier": 0,
            "source_bar_end_at": "2026-08-14 14:50:00",
        }])
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "v10_decision_latest.json").write_text(
                json.dumps({"complete": False, "status": "running"}),
                encoding="utf-8",
            )
            with (
                patch.object(scanner, "OUTPUT_DIR", output_dir),
                patch.object(scanner, "_append_walk_forward_snapshot") as append_snapshot,
                redirect_stdout(io.StringIO()),
            ):
                scanner._write_outputs(
                    df=df,
                    run_time="2026-08-14 14:50:03",
                    total_amt_yi=4200.0,
                    market_regime="normal",
                    amount_threshold=1.5e8,
                    scanned_count=3,
                    phase="decision",
                    decision_cutoff_at="2026-08-14 14:50:00",
                    producer_run_id="scheduler-run-7",
                )

            decision_pointer = json.loads(
                (output_dir / "v10_decision_latest.json").read_text(encoding="utf-8")
            )
            general_pointer = json.loads(
                (output_dir / "v10_scan_latest.json").read_text(encoding="utf-8")
            )
            scan_meta = json.loads(
                (output_dir / "v10_scan_meta.json").read_text(encoding="utf-8")
            )

            self.assertEqual(decision_pointer["phase"], "decision")
            self.assertEqual(decision_pointer["trade_date"], "2026-08-14")
            self.assertEqual(decision_pointer["decision_cutoff_at"], "2026-08-14 14:50:00")
            self.assertTrue(decision_pointer["complete"])
            self.assertEqual(
                decision_pointer["artifact_id"],
                "2026-08-14:decision:scheduler-run-7",
            )
            self.assertEqual(decision_pointer["producer_run_id"], "scheduler-run-7")
            self.assertEqual(decision_pointer["schema_version"], 1)
            self.assertEqual(decision_pointer["market_timezone"], scanner.MARKET_TIMEZONE)
            self.assertEqual(decision_pointer["cutoff_not_ready_count"], 0)
            self.assertEqual(decision_pointer["requested_count"], 3)
            self.assertEqual(decision_pointer["refreshed_count"], 1)
            self.assertEqual(decision_pointer["strategy_profile_id"], scanner._SCANNER_STRATEGY_PROFILE_ID)
            self.assertEqual(decision_pointer["strategy_profile_hash"], scanner._SCANNER_STRATEGY_PROFILE_HASH)
            self.assertTrue(Path(decision_pointer["scan_csv"]).exists())
            self.assertTrue(Path(decision_pointer["scan_meta"]).exists())
            self.assertTrue(Path(decision_pointer["latest_scan_csv"]).exists())
            self.assertTrue(Path(decision_pointer["latest_scan_meta"]).exists())
            self.assertEqual(general_pointer["phase"], "decision")
            self.assertTrue(general_pointer["complete"])
            self.assertEqual(scan_meta["trade_date"], "2026-08-14")
            self.assertTrue(scan_meta["complete"])
            self.assertEqual(scan_meta["requested_count"], 3)
            self.assertEqual(scan_meta["refreshed_count"], 1)
            protocol_fields = [
                "schema_version",
                "artifact_id",
                "producer_run_id",
                "published_at",
                "market_timezone",
                "scan_csv_sha256",
                "cutoff_not_ready_count",
            ]
            for field in protocol_fields:
                self.assertEqual(scan_meta[field], decision_pointer[field])
            csv_hash = hashlib.sha256(
                Path(decision_pointer["scan_csv"]).read_bytes()
            ).hexdigest()
            self.assertEqual(decision_pointer["scan_csv_sha256"], csv_hash)
            self.assertFalse(list(output_dir.glob("*.tmp")))
            append_snapshot.assert_called_once()

    def test_prewarm_output_publishes_dedicated_validated_pointer(self) -> None:
        df = pd.DataFrame([{
            "code": "000001",
            "tier": 0,
            "strategy_profile_id": scanner._SCANNER_STRATEGY_PROFILE_ID,
            "strategy_profile_hash": scanner._SCANNER_STRATEGY_PROFILE_HASH,
        }])
        market_now = datetime(2026, 8, 14, 14, 30, 0, tzinfo=scanner.MARKET_TZ)
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            with (
                patch.object(scanner, "OUTPUT_DIR", output_dir),
                patch.object(scanner, "_market_now", return_value=market_now),
                redirect_stdout(io.StringIO()),
            ):
                scanner._write_outputs(
                    df=df,
                    run_time="2026-08-14 14:30:00",
                    total_amt_yi=4200.0,
                    market_regime="normal",
                    amount_threshold=1.5e8,
                    scanned_count=1,
                    phase="prewarm",
                    producer_run_id="prewarm-run-1",
                )
                loaded = scanner._load_valid_prewarm_dataframe("2026-08-14")

            pointer = json.loads(
                (output_dir / "v10_prewarm_latest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(pointer["phase"], "prewarm")
        self.assertEqual(pointer["artifact_id"], "2026-08-14:prewarm:prewarm-run-1")
        self.assertEqual(loaded["code"].astype(str).str.zfill(6).tolist(), ["000001"])

    def test_decision_never_falls_back_to_generic_prewarm_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "v10_scan_full.csv").write_text("code,tier\n000001,1\n", encoding="utf-8")
            with patch.object(scanner, "OUTPUT_DIR", output_dir):
                with self.assertRaisesRegex(RuntimeError, "dedicated prewarm pointer"):
                    scanner._load_valid_prewarm_dataframe("2026-08-14")

    def test_walk_forward_failure_does_not_block_decision_pointers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            buf = io.StringIO()
            with (
                patch.object(scanner, "OUTPUT_DIR", output_dir),
                patch.object(
                    scanner,
                    "_append_walk_forward_snapshot",
                    side_effect=RuntimeError("snapshot unavailable"),
                ),
                redirect_stdout(buf),
            ):
                scanner._write_outputs(
                    df=pd.DataFrame([{"code": "000001", "tier": 0}]),
                    run_time="2026-08-14 14:50:03",
                    total_amt_yi=4200.0,
                    market_regime="normal",
                    amount_threshold=1.5e8,
                    scanned_count=1,
                    phase="decision",
                    decision_cutoff_at="2026-08-14 14:50:00",
                )

            self.assertTrue((output_dir / "v10_scan_latest.json").exists())
            self.assertTrue((output_dir / "v10_decision_latest.json").exists())
            self.assertIn("[WARN] unable to persist walk-forward snapshot", buf.getvalue())

    def test_decision_output_appends_walk_forward_snapshot(self) -> None:
        df = pd.DataFrame(
            [{
                "code": "000001",
                "name": "Alpha",
                "tier": 0,
                "mode": "no_signal",
                "position": 0.0,
                "bz_direction": -0.1,
                "bz_rt_direction": -0.05,
                "close_vs_ma20_pct": 1.0,
                "rsi14": 55.0,
                "source_bar_end_at": "2026-08-14 14:50:00",
            }]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            with patch.object(scanner, "OUTPUT_DIR", output_dir), patch.object(
                scanner, "WALK_FORWARD_SNAPSHOT_FILE", output_dir / "snapshots.csv"
            ):
                scanner._write_outputs(
                    df=df,
                    run_time="2026-08-14 14:50:00",
                    total_amt_yi=4200.0,
                    market_regime="normal",
                    amount_threshold=1.5e8,
                    scanned_count=1,
                    phase="decision",
                    decision_cutoff_at="2026-08-14 14:50:00",
                )

            snapshot = pd.read_csv(output_dir / "snapshots.csv", dtype=str)

        self.assertEqual(snapshot.loc[0, "signal_date"], "2026-08-14")
        self.assertEqual(snapshot.loc[0, "as_of"], "2026-08-14 14:50:00")
        self.assertEqual(snapshot.loc[0, "bz_dir"], "-0.1")
        self.assertEqual(snapshot.loc[0, "ma20_off"], "1.0")
        self.assertEqual(snapshot.loc[0, "walk_forward_eligible"], "1")

    def test_decision_output_rejects_snapshot_without_cutoff_bar(self) -> None:
        df = pd.DataFrame([{
            "code": "000001",
            "tier": 0,
            "source_bar_end_at": "2026-08-14 14:45:00",
        }])
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            with patch.object(scanner, "OUTPUT_DIR", output_dir), patch.object(
                scanner, "WALK_FORWARD_SNAPSHOT_FILE", output_dir / "snapshots.csv"
            ):
                scanner._write_outputs(
                    df=df,
                    run_time="2026-08-14 14:50:00",
                    total_amt_yi=4200.0,
                    market_regime="normal",
                    amount_threshold=1.5e8,
                    scanned_count=1,
                    phase="decision",
                    decision_cutoff_at="2026-08-14 14:50:00",
                )

            self.assertFalse((output_dir / "snapshots.csv").exists())

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
                patch.object(scanner, "_load_valid_prewarm_dataframe", return_value=cached),
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

    def test_decision_fast_caps_refresh_to_highest_liquidity_candidates(self) -> None:
        cached = pd.DataFrame(columns=["code", "name", "market"])
        stocks = pd.DataFrame(
            [
                {"code": "000001", "name": "one", "market": 0},
                {"code": "000002", "name": "two", "market": 0},
                {"code": "000003", "name": "three", "market": 1},
            ]
        )
        filtered = [
            {"code": "000003", "name": "three", "market": 1, "latest_amt": 3e8},
            {"code": "000002", "name": "two", "market": 0, "latest_amt": 2e8},
            {"code": "000001", "name": "one", "market": 0, "latest_amt": 1e8},
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "v10_scan_full.csv").write_text("placeholder\n", encoding="utf-8")
            with (
                patch.object(scanner, "OUTPUT_DIR", output_dir),
                patch.object(scanner, "_load_valid_prewarm_dataframe", return_value=cached),
                patch.object(scanner, "get_stock_list", return_value=stocks),
                patch.object(scanner, "_collect_amount_snapshot", return_value=[]),
                patch.object(
                    scanner,
                    "_select_amount_candidates",
                    return_value=(filtered, 4200.0, "正常市", scanner.SCAN_CONFIG["min_amount_yuan"]),
                ),
                patch.object(scanner, "DECISION_MAX_CANDIDATES", 2),
                patch.object(
                    scanner,
                    "_build_signal_row",
                    side_effect=lambda _api, *, code, **_kwargs: {"code": code, "tier": 0},
                ) as build_signal_row,
                patch.object(scanner, "_main_strategy_debug_emit"),
                patch.object(scanner, "_write_outputs") as write_outputs,
            ):
                scanner._run_decision_fast(api=object(), run_time="2026-07-15 14:49:00")

        self.assertEqual([call.kwargs["code"] for call in build_signal_row.call_args_list], ["000003", "000002"])
        self.assertEqual([call.kwargs["daily_count"] for call in build_signal_row.call_args_list], [80, 80])
        self.assertEqual(write_outputs.call_args.kwargs["scanned_count"], 2)

    def test_positive_int_env_uses_default_for_invalid_values(self) -> None:
        with patch.dict(scanner.os.environ, {"TLFZ_DECISION_MAX_CANDIDATES": "invalid"}):
            self.assertEqual(scanner._positive_int_env("TLFZ_DECISION_MAX_CANDIDATES", 120), 120)

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
            patch.object(scanner, "fetch_daily_bars", return_value=object()) as fetch_daily_bars,
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
        self.assertEqual(fetch_daily_bars.call_args.kwargs["count"], 250)
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

    def test_fetch_5min_bars_uses_cutoff_date_not_provider_tail_date(self) -> None:
        bars_df = pd.DataFrame({
            "datetime": [
                "2026-08-14 14:45:00",
                "2026-08-14 14:50:00",
                "2026-08-14 14:55:00",
                "2026-08-15 09:35:00",
            ],
            "close": [10.0, 10.1, 10.2, 10.3],
        })

        class FakeApi:
            def get_security_bars(self, *_args):
                return [{"bar": 1}]

            def to_df(self, _bars):
                return bars_df.copy()

        result = scanner.fetch_5min_bars_today(
            FakeApi(),
            0,
            "000001",
            cutoff_at=datetime(2026, 8, 14, 14, 50, tzinfo=scanner.MARKET_TZ),
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            result["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
            ["2026-08-14 14:45:00", "2026-08-14 14:50:00"],
        )

    def test_build_signal_row_forces_1445_bar_to_cutoff_not_ready(self) -> None:
        daily_fields = {
            "close": 12.34,
            "entry_price": 12.34,
            "latest_amt": 3.2e8,
            "close_vs_ma20_pct": -1.2,
            "amt_ratio": 1.8,
            "rsi14": 58.0,
            "is_green": True,
            "vol_expand": True,
        }
        min5 = pd.DataFrame({
            "datetime": pd.to_datetime([
                "2026-08-14 14:30:00",
                "2026-08-14 14:35:00",
                "2026-08-14 14:40:00",
                "2026-08-14 14:45:00",
            ])
        })
        with (
            patch.object(scanner, "fetch_daily_bars", return_value=object()),
            patch.object(scanner, "_compute_daily_snapshot", return_value=daily_fields),
            patch.object(
                scanner,
                "_resolve_weekly_features",
                return_value={"weekly_align": True, "weekly_slope": 9.0},
            ),
            patch.object(scanner, "fetch_5min_bars_today", return_value=min5),
            patch.object(
                scanner,
                "compute_5min_signal",
                return_value={
                    "bz_direction": -0.5,
                    "bz_rt_direction": -0.2,
                    "bz_vol_ratio": 1.3,
                },
            ),
            patch.object(
                scanner,
                "classify_signal",
                return_value=(1, "would_be_live", 1.0, "unsafe"),
            ) as classify_signal,
        ):
            row = scanner._build_signal_row(
                api=object(),
                code="000001",
                name="stale",
                market=0,
                include_5min=True,
                intraday_cutoff=datetime(
                    2026,
                    8,
                    14,
                    14,
                    50,
                    tzinfo=scanner.MARKET_TZ,
                ),
            )

        self.assertIsNotNone(row)
        self.assertEqual(row["source_bar_end_at"], "2026-08-14 14:45:00")
        self.assertEqual(row["decision_cutoff_at"], "2026-08-14 14:50:00")
        self.assertFalse(row["decision_cutoff_ready"])
        self.assertEqual(row["tier"], 0)
        self.assertEqual(row["mode"], "cutoff_not_ready")
        self.assertEqual(row["position"], 0.0)
        classify_signal.assert_not_called()

    def test_decision_cutoff_wait_uses_market_clock_and_is_bounded(self) -> None:
        market_date = datetime(2026, 8, 24).date()
        with (
            patch.object(
                scanner,
                "_market_now",
                side_effect=[
                    datetime(2026, 8, 24, 14, 50, 0, tzinfo=scanner.MARKET_TZ),
                    datetime(2026, 8, 24, 14, 50, 2, tzinfo=scanner.MARKET_TZ),
                ],
            ),
            patch.object(scanner.time, "sleep") as sleep_mock,
        ):
            waited = scanner._wait_for_decision_fetch_window(
                market_date,
                max_wait_seconds=5,
            )

        self.assertEqual(waited, 2.0)
        sleep_mock.assert_called_once()
        self.assertAlmostEqual(sleep_mock.call_args.args[0], 2.0, places=3)

        with (
            patch.object(
                scanner,
                "_market_now",
                return_value=datetime(
                    2026,
                    8,
                    25,
                    9,
                    30,
                    tzinfo=scanner.MARKET_TZ,
                ),
            ),
            patch.object(scanner.time, "sleep") as historical_sleep,
        ):
            self.assertEqual(
                scanner._wait_for_decision_fetch_window(market_date),
                0.0,
            )
        historical_sleep.assert_not_called()

        with (
            patch.object(
                scanner,
                "_market_now",
                return_value=datetime(
                    2026,
                    8,
                    24,
                    14,
                    45,
                    tzinfo=scanner.MARKET_TZ,
                ),
            ),
            patch.object(scanner.time, "sleep") as excessive_sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "exceeds maximum"):
                scanner._wait_for_decision_fetch_window(
                    market_date,
                    max_wait_seconds=30,
                )
        excessive_sleep.assert_not_called()

    def test_decision_run_writes_running_marker_after_lock(self) -> None:
        captured = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "v10_scan_full.csv").write_text(
                "code,name,market\n",
                encoding="utf-8",
            )

            def capture_outputs(**kwargs):
                captured["marker"] = json.loads(
                    (output_dir / "v10_decision_latest.json").read_text(
                        encoding="utf-8"
                    )
                )
                captured["output_kwargs"] = kwargs

            with (
                patch.object(scanner, "OUTPUT_DIR", output_dir),
                patch.object(
                    scanner,
                    "_load_valid_prewarm_dataframe",
                    return_value=pd.DataFrame(columns=["code", "name", "market"]),
                ),
                patch.object(
                    scanner,
                    "get_stock_list",
                    return_value=pd.DataFrame(columns=["code", "name", "market"]),
                ),
                patch.object(scanner, "_collect_amount_snapshot", return_value=[]),
                patch.object(
                    scanner,
                    "_select_amount_candidates",
                    return_value=([], 0.0, "正常市", 1e8),
                ),
                patch.object(scanner, "_main_strategy_debug_emit"),
                patch.object(scanner, "_write_outputs", side_effect=capture_outputs),
                redirect_stdout(io.StringIO()),
            ):
                scanner._run_decision_fast(
                    api=object(),
                    run_time="2026-08-14 14:49:00",
                    producer_run_id="producer-42",
                )

            marker = captured["marker"]
            self.assertEqual(marker["phase"], "decision")
            self.assertEqual(marker["trade_date"], "2026-08-14")
            self.assertFalse(marker["complete"])
            self.assertEqual(marker["decision_cutoff_at"], "2026-08-14 14:50:00")
            self.assertEqual(marker["artifact_id"], "2026-08-14:decision:producer-42")
            self.assertEqual(marker["producer_run_id"], "producer-42")
            self.assertEqual(marker["market_timezone"], scanner.MARKET_TIMEZONE)
            self.assertTrue(marker["started_at"].endswith("+08:00"))
            self.assertTrue(marker["published_at"].endswith("+08:00"))
            self.assertEqual(
                captured["output_kwargs"]["producer_run_id"],
                "producer-42",
            )
            self.assertFalse((output_dir / "v10_decision.lock").exists())

    def test_decision_wait_failure_leaves_incomplete_marker_and_skips_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "v10_scan_full.csv").write_text(
                "code,name,market\n",
                encoding="utf-8",
            )
            with (
                patch.object(scanner, "OUTPUT_DIR", output_dir),
                patch.object(
                    scanner,
                    "_load_valid_prewarm_dataframe",
                    return_value=pd.DataFrame(columns=["code", "name", "market"]),
                ),
                patch.object(
                    scanner,
                    "get_stock_list",
                    return_value=pd.DataFrame(columns=["code", "name", "market"]),
                ),
                patch.object(scanner, "_collect_amount_snapshot", return_value=[]),
                patch.object(
                    scanner,
                    "_select_amount_candidates",
                    return_value=([], 0.0, "正常市", 1e8),
                ),
                patch.object(
                    scanner,
                    "_wait_for_decision_fetch_window",
                    side_effect=RuntimeError("wait bound exceeded"),
                ),
                patch.object(scanner, "_write_outputs") as write_outputs_mock,
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(RuntimeError, "wait bound exceeded"):
                    scanner._run_decision_fast(
                        api=object(),
                        run_time="2026-08-24 14:45:00",
                        producer_run_id="failed-run",
                    )

            marker = json.loads(
                (output_dir / "v10_decision_latest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(marker["complete"])
            self.assertEqual(marker["artifact_id"], "2026-08-24:decision:failed-run")
            write_outputs_mock.assert_not_called()
            self.assertFalse((output_dir / "v10_decision.lock").exists())

    def test_decision_artifact_and_snapshot_slots_are_unique_without_run_id(self) -> None:
        pointers = []
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            with (
                patch.object(scanner, "OUTPUT_DIR", output_dir),
                patch.object(scanner, "_append_walk_forward_snapshot"),
                redirect_stdout(io.StringIO()),
            ):
                for _ in range(2):
                    scanner._write_outputs(
                        df=pd.DataFrame(columns=["tier"]),
                        run_time="2026-08-14 14:50:03",
                        total_amt_yi=0.0,
                        market_regime="normal",
                        amount_threshold=1e8,
                        scanned_count=0,
                        phase="decision",
                        decision_cutoff_at="2026-08-14 14:50:00",
                    )
                    pointers.append(json.loads(
                        (output_dir / "v10_decision_latest.json").read_text(
                            encoding="utf-8"
                        )
                    ))

            self.assertNotEqual(pointers[0]["run_slot"], pointers[1]["run_slot"])
            self.assertNotEqual(pointers[0]["artifact_id"], pointers[1]["artifact_id"])
            for pointer in pointers:
                self.assertRegex(
                    pointer["run_slot"],
                    r"^\d{4}-\d{2}-\d{2}_\d{6}_\d{6}_\d+_\d+$",
                )
                self.assertEqual(pointer["producer_run_id"], pointer["run_slot"])
                self.assertEqual(
                    pointer["artifact_id"],
                    f"2026-08-14:decision:{pointer['run_slot']}",
                )
                self.assertTrue(Path(pointer["scan_csv"]).exists())

    def test_decision_lock_conflict_reclaim_and_owner_safe_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            with patch.object(scanner, "OUTPUT_DIR", output_dir):
                owner = scanner._acquire_decision_lock()
                with self.assertRaisesRegex(RuntimeError, "already held"):
                    scanner._acquire_decision_lock()
                self.assertTrue(scanner._release_decision_lock(owner))
                self.assertFalse((output_dir / "v10_decision.lock").exists())

                stale_path = output_dir / "v10_decision.lock"
                stale_path.write_text(
                    json.dumps({
                        "pid": scanner.os.getpid(),
                        "owner_token": "stale-owner",
                        "created_at_epoch": scanner.time.time() - 60,
                    }),
                    encoding="utf-8",
                )
                replacement = scanner._acquire_decision_lock(
                    stale_after_seconds=1,
                )
                self.assertNotEqual(replacement["owner_token"], "stale-owner")
                self.assertTrue(scanner._release_decision_lock(replacement))

                stale_path.write_text(
                    json.dumps({
                        "pid": 999999,
                        "owner_token": "dead-owner",
                        "created_at_epoch": scanner.time.time(),
                    }),
                    encoding="utf-8",
                )
                with patch.object(scanner, "_pid_is_alive", return_value=False):
                    replacement = scanner._acquire_decision_lock()
                self.assertTrue(scanner._release_decision_lock(replacement))

                owner = scanner._acquire_decision_lock()
                lock_payload = json.loads(stale_path.read_text(encoding="utf-8"))
                lock_payload["owner_token"] = "different-owner"
                stale_path.write_text(json.dumps(lock_payload), encoding="utf-8")
                self.assertFalse(scanner._release_decision_lock(owner))
                self.assertTrue(stale_path.exists())
                stale_path.unlink()

    def test_cli_accepts_scheduler_protocol_arguments(self) -> None:
        args = scanner._build_arg_parser().parse_args([
            "--decision-fast",
            "--task-name",
            "Decision1450",
            "--trigger-slot",
            "14:49",
            "--run-id",
            "run-20260814-01",
        ])

        self.assertTrue(args.decision_fast)
        self.assertEqual(args.task_name, "Decision1450")
        self.assertEqual(args.trigger_slot, "14:49")
        self.assertEqual(args.run_id, "run-20260814-01")


if __name__ == "__main__":
    unittest.main()
