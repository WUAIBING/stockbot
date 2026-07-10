#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import evolving_model
import external_market_review
import register_workbuddy_tasks as task_register
import v10_auto_runner as auto_runner
import v10_moni_trader as trader
import workbuddy_local_challenger as challenger
import workbuddy_local_review as challenger_review


class RunPhaseWatchTests(unittest.TestCase):
    def test_run_step_reports_semantic_detail_for_challenger_window_skip(self) -> None:
        fake_result = type("FakeResult", (), {"returncode": auto_runner.EXIT_WINDOW_SKIPPED})()
        with patch.object(auto_runner.subprocess, "run", return_value=fake_result):
            code, detail, _started_at, _finished_at = auto_runner.run_step("workbuddy-buy", ["workbuddy_local_challenger.py", "--buy"])

        self.assertEqual(code, auto_runner.EXIT_WINDOW_SKIPPED)
        self.assertEqual(detail, "step finished with window skipped: workbuddy_local_challenger.py")

    def test_buy_watch_retries_on_decision_not_ready(self) -> None:
        fixed_now = datetime(2026, 6, 25, 14, 50, 0)
        with (
            patch.object(auto_runner, "run_phase_once", side_effect=[1, 0]) as run_once,
            patch.object(auto_runner, "record_status") as record_status,
            patch.object(auto_runner.time, "sleep") as sleep_mock,
            patch.object(auto_runner, "datetime") as mock_datetime,
        ):
            mock_datetime.now.return_value = fixed_now
            code = auto_runner.run_phase_watch(
                "buy",
                run_meta={"task_name": "TLFZ-WorkBuddy-BuyWatch", "trigger_slot": "14:50", "run_id": "rid"},
                with_email=False,
                max_attempts=3,
                interval_seconds=30,
            )

        self.assertEqual(code, 0)
        self.assertEqual(run_once.call_count, 2)
        record_status.assert_called_once()
        sleep_mock.assert_called_once()

    def test_workbuddy_refresh_phase_includes_distill_refresh(self) -> None:
        steps = auto_runner.build_steps("workbuddy-refresh", with_email=False)
        self.assertEqual(steps[0], [str(auto_runner.REFRESH_DISTILL_PIPELINE_SCRIPT)])

    def test_workbuddy_buy_phase_calls_challenger_only(self) -> None:
        steps = auto_runner.build_steps("workbuddy-buy", with_email=False)
        self.assertEqual(steps, [["workbuddy_local_challenger.py", "--buy"]])

    def test_run_phase_once_fails_fast_when_preflight_breaks(self) -> None:
        with (
            patch.object(auto_runner, "should_skip_phase_for_calendar", return_value=(False, "")),
            patch.object(auto_runner, "should_stop_phase_for_deadline", return_value=(False, "")),
            patch.object(auto_runner, "preflight_phase", side_effect=auto_runner.RuntimeValidationError("preflight failed")),
            patch.object(auto_runner, "record_status") as record_status,
        ):
            code = auto_runner.run_phase_once(
                "workbuddy-refresh",
                run_meta={"task_name": "TLFZ-WorkBuddy-WorkBuddyRefresh", "trigger_slot": "13:38", "run_id": "rid"},
                with_email=False,
            )

        self.assertEqual(code, 3)
        record_status.assert_called_once()

    def test_run_phase_once_records_step_running_before_exec(self) -> None:
        started_at = datetime(2026, 7, 7, 10, 0, 0)
        finished_at = datetime(2026, 7, 7, 10, 0, 5)
        with (
            patch.object(auto_runner, "should_skip_phase_for_calendar", return_value=(False, "")),
            patch.object(auto_runner, "should_stop_phase_for_deadline", return_value=(False, "")),
            patch.object(auto_runner, "preflight_phase", return_value=[]),
            patch.object(auto_runner, "build_steps", return_value=[["workbuddy_local_challenger.py", "--status"]]),
            patch.object(auto_runner, "run_step", return_value=(0, "step finished", started_at, finished_at)),
            patch.object(auto_runner, "validate_step_outputs", return_value=[]),
            patch.object(auto_runner, "record_status") as record_status,
        ):
            code = auto_runner.run_phase_once(
                "workbuddy-status",
                run_meta={"task_name": "TLFZ-WorkBuddy-Status0933", "trigger_slot": "09:33", "run_id": "rid"},
                with_email=False,
            )

        self.assertEqual(code, 0)
        running_calls = [
            call for call in record_status.call_args_list
            if call.kwargs.get("step") == "workbuddy_local_challenger.py" and call.kwargs.get("status") == "running"
        ]
        self.assertEqual(len(running_calls), 1)

    def test_run_phase_once_records_runner_failure_on_unhandled_exception(self) -> None:
        with (
            patch.object(auto_runner, "should_skip_phase_for_calendar", return_value=(False, "")),
            patch.object(auto_runner, "should_stop_phase_for_deadline", return_value=(False, "")),
            patch.object(auto_runner, "preflight_phase", return_value=[]),
            patch.object(auto_runner, "build_steps", return_value=[["workbuddy_local_challenger.py", "--status"]]),
            patch.object(auto_runner, "run_step", side_effect=RuntimeError("boom")),
            patch.object(auto_runner, "record_status") as record_status,
        ):
            code = auto_runner.run_phase_once(
                "workbuddy-status",
                run_meta={"task_name": "TLFZ-WorkBuddy-Status0933", "trigger_slot": "09:33", "run_id": "rid"},
                with_email=False,
            )

        self.assertEqual(code, 3)
        failure_calls = [
            call for call in record_status.call_args_list
            if call.kwargs.get("step") == "phase" and call.kwargs.get("status") == "failed"
        ]
        self.assertTrue(failure_calls)


class PhaseInspectionSnapshotTests(unittest.TestCase):
    def test_write_phase_inspection_snapshot_generates_opening_node_file(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            opening_file = tmp / "opening_tradability_latest.json"
            security_file = tmp / "security_master_latest.json"
            external_file = tmp / "v10_external_market_review_latest.json"
            output_file = tmp / "v10_opening_node_latest.json"
            opening_file.write_text(
                json.dumps(
                    {
                        "generated_at": f"{today} 09:31:05",
                        "trade_date": today,
                        "record_count": 12,
                        "excluded_today_count": 1,
                        "review_only_count": 0,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            security_file.write_text(
                json.dumps(
                    {
                        "generated_at": f"{today} 09:31:03",
                        "trade_date": today,
                        "record_count": 12,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            external_file.write_text(
                json.dumps(
                    {
                        "generated_at": f"{today} 09:31:10",
                        "trade_date": today,
                        "window_tag": "opening_0931",
                        "risk_level": "balanced",
                        "a_share_bias": "neutral",
                        "impact_summary": "ok",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with (
                patch.object(auto_runner, "OPENING_TRADABILITY_FILE", opening_file),
                patch.object(auto_runner, "SECURITY_MASTER_FILE", security_file),
                patch.object(auto_runner, "EXTERNAL_MARKET_REVIEW_FILE", external_file),
                patch.object(auto_runner, "OPENING_NODE_FILE", output_file),
            ):
                written_path = auto_runner.write_phase_inspection_snapshot(
                    run_meta={"task_name": "TLFZ-WorkBuddy-OpeningData", "trigger_slot": "09:31", "run_id": "rid-open"},
                    phase="opening-data",
                    phase_status="ok",
                    phase_exit_code=0,
                )

            self.assertEqual(written_path, output_file)
            payload = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["node"], "opening_node")
            self.assertEqual(payload["node_status"], "ok")
            self.assertTrue(payload["checklist"]["opening_tradability_today"])
            self.assertEqual(payload["summary"]["record_count"], 12)

    def test_write_phase_inspection_snapshot_generates_midday_inspection_file(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            midday_review_file = tmp / "v10_midday_review_latest.json"
            midday_node_file = tmp / "v10_midday_node_latest.json"
            midday_gate_file = tmp / "v10_midday_gate_latest.json"
            pm_gate_file = tmp / "v10_pm_gate_status.json"
            account_summary_file = tmp / "v10_account_summary_latest.json"
            output_file = tmp / "v10_midday_inspection_latest.json"
            midday_review_file.write_text(
                json.dumps({"generated_at": f"{today} 11:35:02", "date": today, "market_temperature": "warm"}, ensure_ascii=False),
                encoding="utf-8",
            )
            midday_node_file.write_text(
                json.dumps(
                    {
                        "generated_at": f"{today} 11:35:05",
                        "date": today,
                        "stage": "midday_node",
                        "review_status": "ok",
                        "pm_gate_status": "pass",
                        "blocked_buy_codes": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            midday_gate_file.write_text(
                json.dumps(
                    {
                        "generated_at": f"{today} 13:00:05",
                        "date": today,
                        "stage": "pm_gate",
                        "review_status": "ok",
                        "pm_gate_status": "pass",
                        "blocked_buy_codes": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            pm_gate_file.write_text(
                json.dumps(
                    {
                        "generated_at": f"{today} 13:00:05",
                        "date": today,
                        "stage": "pm_gate",
                        "review_status": "ok",
                        "pm_gate_status": "pass",
                        "blocked_buy_codes": [],
                        "reason_codes": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            account_summary_file.write_text(
                json.dumps(
                    {
                        "generated_at": f"{today} 13:00:08",
                        "trade_date": today,
                        "latest_execution_result": {"action": "smart_sell", "status": "ok"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with (
                patch.object(auto_runner, "MIDDAY_REVIEW_FILE", midday_review_file),
                patch.object(auto_runner, "MIDDAY_NODE_FILE", midday_node_file),
                patch.object(auto_runner, "MIDDAY_GATE_FILE", midday_gate_file),
                patch.object(auto_runner, "PM_GATE_FILE", pm_gate_file),
                patch.object(auto_runner, "ACCOUNT_SUMMARY_FILE", account_summary_file),
                patch.object(auto_runner, "MIDDAY_INSPECTION_FILE", output_file),
            ):
                written_path = auto_runner.write_phase_inspection_snapshot(
                    run_meta={"task_name": "TLFZ-WorkBuddy-MiddayGate", "trigger_slot": "13:00", "run_id": "rid-mid"},
                    phase="midday-gate",
                    phase_status="ok",
                    phase_exit_code=0,
                )

            self.assertEqual(written_path, output_file)
            payload = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["node"], "midday_inspection")
            self.assertEqual(payload["inspection_status"], "ok")
            self.assertTrue(payload["checklist"]["pm_gate_today"])
            self.assertEqual(payload["account_summary"]["latest_execution_action"], "smart_sell")


class TaskRegisterValidationTests(unittest.TestCase):
    def test_current_task_specs_pass_validation(self) -> None:
        task_register.validate_task_specs(task_register.TASK_SPECS)

    def test_missing_required_task_fails_fast(self) -> None:
        filtered = [spec for spec in task_register.TASK_SPECS if spec.suffix != "SmartSell0945"]
        with self.assertRaisesRegex(ValueError, "缺少关键任务定义"):
            task_register.validate_task_specs(filtered)

    def test_challenger_multislot_tasks_are_all_registered(self) -> None:
        suffixes = {spec.suffix for spec in task_register.TASK_SPECS}
        self.assertTrue(
            {
                "ChallengerBuy1002",
                "ChallengerBuy1034",
                "ChallengerBuy1102",
                "ChallengerBuy1332",
                "ChallengerBuy1402",
                "ChallengerBuy1432",
                "ChallengerBuy1454",
                "ChallengerSell0947",
                "ChallengerSell1032",
                "ChallengerSell1452",
            }.issubset(suffixes)
        )

    def test_build_task_args_uses_explicit_trigger_slot_when_provided(self) -> None:
        spec = task_register.TaskSpec(
            "ChallengerBuy1002",
            "10:02",
            "workbuddy-buy",
            trigger_slot="10:00",
        )
        args = task_register.build_task_args(spec)

        self.assertIn("--trigger-slot", args)
        trigger_slot = args[args.index("--trigger-slot") + 1]
        self.assertEqual(trigger_slot, "10:00")
        self.assertNotEqual(trigger_slot, spec.time_hhmm)

    def test_opening_data_and_close_node_include_external_market_review_step(self) -> None:
        opening_steps = auto_runner.build_steps("opening-data", with_email=False)
        close_steps = auto_runner.build_steps("close-node", with_email=False)

        self.assertIn(["external_market_review.py"], opening_steps)
        self.assertIn(["external_market_review.py"], close_steps)


class ExternalMarketReviewTests(unittest.TestCase):
    def test_build_external_market_review_derives_sector_bias_and_actions(self) -> None:
        sample_item = {
            "title": "美债收益率上行叠加关税扰动，AI算力与半导体承压，黄金走强，股指期货空单增加",
            "content": "昨夜今晨美元走强、地缘冲突升级，算力、半导体承压，黄金受益，量化做空与北向流出抬升卖压。",
            "informationType": "新闻",
            "insName": "东方财富研究中心",
            "date": "2026-06-26 08:12:00",
        }

        class FakeClient:
            def search(self, _query):
                return {"data": {"data": {"llmSearchResponse": {"data": [sample_item, sample_item]}}}}

        opening_payload = {
            "records": [
                {"code": "300264", "name": "佳创视讯", "open_price": 10.0, "last_price": 9.6, "last_close": 10.3, "amount": 300000000.0},
                {"code": "688596", "name": "正帆科技", "open_price": 20.2, "last_price": 19.5, "last_close": 20.6, "amount": 280000000.0},
                {"code": "600519", "name": "贵州茅台", "open_price": 1400.0, "last_price": 1370.0, "last_close": 1412.0, "amount": 900000000.0},
                {"code": "601318", "name": "中国平安", "open_price": 50.0, "last_price": 48.8, "last_close": 50.2, "amount": 800000000.0},
            ]
        }
        pool_payload = {
            "selected_records": [
                {"code": "300264", "name": "佳创视讯", "selection_rank": 1, "latest_rank": 1, "latest_chg_pct": 20.02, "role": "distill_champion_core"},
                {"code": "688596", "name": "正帆科技", "selection_rank": 2, "latest_rank": 2, "latest_chg_pct": 20.0, "role": "distill_champion_core"},
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            output_json = tmpdir_path / "external_review.json"
            output_csv = tmpdir_path / "external_review.csv"
            output_history = tmpdir_path / "external_review.jsonl"
            def fake_read_json(path: Path):
                path_str = str(path)
                if "opening_tradability_latest.json" in path_str:
                    return opening_payload
                if "workbuddy_candidate_pool_latest.json" in path_str:
                    return pool_payload
                return {}
            with (
                patch.object(external_market_review, "OUTPUT_JSON", output_json),
                patch.object(external_market_review, "OUTPUT_CSV", output_csv),
                patch.object(external_market_review, "OUTPUT_HISTORY", output_history),
                patch.object(external_market_review, "MX_SEARCH_SKILL", Path("fake-skill.py")),
                patch.object(Path, "exists", return_value=True),
                patch.object(external_market_review, "_read_json", side_effect=fake_read_json),
                patch.object(external_market_review, "_load_module", return_value=type("FakeMod", (), {"MXSearch": lambda self=None: FakeClient()})()),
                patch.dict("os.environ", {"MX_APIKEY": "demo-key"}),
            ):
                payload = external_market_review.build_external_market_review(
                    run_id="rid",
                    task_name="TLFZ-WorkBuddy-OpeningData",
                    trigger_slot="09:31",
                )

        self.assertEqual(payload["window_tag"], "opening_0931")
        self.assertEqual(payload["a_share_bias"], "risk_off")
        self.assertIn(payload["risk_level"], {"medium", "high"})
        self.assertIn("AI硬件", payload["negative_sectors"])
        self.assertIn("半导体", payload["negative_sectors"])
        self.assertIn("黄金", payload["positive_sectors"])
        self.assertFalse(payload["recommended_actions"]["broad_rebound_allowed"])
        self.assertIn("short_term", payload["horizon_assessment"])
        self.assertEqual(payload["horizon_assessment"]["short_term"]["bias"], "negative")
        self.assertIn(payload["short_flow_monitor"]["pressure_level"], {"medium", "high"})
        self.assertIn("AI硬件", payload["short_flow_monitor"]["targeted_sectors"])
        self.assertIn(payload["opening_anchor_break_monitor"]["pressure_level"], {"medium", "high"})
        self.assertIn("佳创视讯", payload["opening_anchor_break_monitor"]["broken_anchor_names"])

    def test_build_external_market_review_enables_weekend_digest_on_monday_open(self) -> None:
        sample_item = {
            "title": "周末政策催化叠加海外风险波动，AI算力承压，军工与黄金受关注",
            "content": "周末多条宏观与产业消息发酵，周一开盘需重点观察科技承压与避险方向。",
            "informationType": "新闻",
            "insName": "东方财富研究中心",
            "date": "2026-06-29 08:10:00",
        }

        class FakeClient:
            def search(self, _query):
                return {"data": {"data": {"llmSearchResponse": {"data": [sample_item, sample_item]}}}}

        class FakeMondayDateTime:
            @classmethod
            def now(cls):
                return datetime(2026, 6, 29, 9, 31, 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            with (
                patch.object(external_market_review, "OUTPUT_JSON", tmpdir_path / "monday.json"),
                patch.object(external_market_review, "OUTPUT_CSV", tmpdir_path / "monday.csv"),
                patch.object(external_market_review, "OUTPUT_HISTORY", tmpdir_path / "monday.jsonl"),
                patch.object(external_market_review, "MX_SEARCH_SKILL", Path("fake-skill.py")),
                patch.object(Path, "exists", return_value=True),
                patch.object(external_market_review, "_read_json", return_value={"records": []}),
                patch.object(external_market_review, "_load_module", return_value=type("FakeMod", (), {"MXSearch": lambda self=None: FakeClient()})()),
                patch.object(external_market_review, "datetime", FakeMondayDateTime),
                patch.dict("os.environ", {"MX_APIKEY": "demo-key"}),
            ):
                payload = external_market_review.build_external_market_review(
                    run_id="rid",
                    task_name="TLFZ-WorkBuddy-OpeningData",
                    trigger_slot="09:31",
                )

        self.assertTrue(payload["weekend_digest_monitor"]["active"])
        self.assertIn(payload["weekend_digest_monitor"]["bias"], {"negative", "mixed", "neutral", "positive"})
        self.assertEqual(payload["raw_query_count"], len(external_market_review.SCENARIOS) + 1)

    def test_build_external_market_review_keeps_explicit_neutral_state(self) -> None:
        sample_item = {
            "title": "盘前市场分化加剧，资金观望等待政策落地，消费与医药维持结构性轮动",
            "content": "今晨多家机构认为市场仍以震荡博弈为主，影响有限，先观察消费、医药等方向。",
            "informationType": "新闻",
            "insName": "东方财富研究中心",
            "date": "2026-06-26 08:20:00",
        }

        class FakeClient:
            def search(self, _query):
                return {"data": {"data": {"llmSearchResponse": {"data": [sample_item, sample_item]}}}}

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            with (
                patch.object(external_market_review, "OUTPUT_JSON", tmpdir_path / "neutral.json"),
                patch.object(external_market_review, "OUTPUT_CSV", tmpdir_path / "neutral.csv"),
                patch.object(external_market_review, "OUTPUT_HISTORY", tmpdir_path / "neutral.jsonl"),
                patch.object(external_market_review, "MX_SEARCH_SKILL", Path("fake-skill.py")),
                patch.object(Path, "exists", return_value=True),
                patch.object(external_market_review, "_read_json", return_value={"records": []}),
                patch.object(external_market_review, "_load_module", return_value=type("FakeMod", (), {"MXSearch": lambda self=None: FakeClient()})()),
                patch.dict("os.environ", {"MX_APIKEY": "demo-key"}),
            ):
                payload = external_market_review.build_external_market_review(
                    run_id="rid",
                    task_name="TLFZ-WorkBuddy-OpeningData",
                    trigger_slot="09:31",
                )

        self.assertEqual(payload["a_share_bias"], "neutral")
        self.assertIn("消费", payload["neutral_sectors"])
        self.assertTrue(payload["recommended_actions"]["allow_only_selective_rebound"])
        self.assertFalse(payload["recommended_actions"]["broad_rebound_allowed"])
        self.assertEqual(payload["horizon_assessment"]["short_term"]["bias"], "neutral")

    def test_resolve_sector_views_removes_focus_avoid_overlap(self) -> None:
        summaries = [
            {
                "weight": 1.2,
                "negative_sectors": ["AI硬件", "半导体", "消费"],
                "neutral_sectors": ["消费"],
                "positive_sectors": ["AI硬件", "军工", "半导体"],
            },
            {
                "weight": 1.1,
                "negative_sectors": ["医药"],
                "neutral_sectors": ["消费"],
                "positive_sectors": ["AI硬件", "半导体", "军工"],
            },
        ]

        negative, neutral, positive = external_market_review._resolve_sector_views(summaries)

        self.assertIn("AI硬件", positive)
        self.assertNotIn("AI硬件", negative)
        self.assertNotIn("AI硬件", neutral)
        self.assertTrue(set(negative).isdisjoint(set(positive)))
        self.assertTrue(set(negative).isdisjoint(set(neutral)))
        self.assertTrue(set(neutral).isdisjoint(set(positive)))

    def test_close_node_merge_external_market_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            close_node_file = tmpdir_path / "close_node.json"
            external_file = tmpdir_path / "external_review.json"
            close_node_file.write_text(json.dumps({"notes": []}, ensure_ascii=False), encoding="utf-8")
            external_file.write_text(
                json.dumps(
                    {
                        "trade_date": "2026-06-26",
                        "generated_at": "2026-06-26 09:31:30",
                        "window_tag": "opening_0931",
                        "risk_level": "high",
                        "a_share_bias": "risk_off",
                        "confidence": 0.83,
                        "negative_sectors": ["AI硬件", "半导体"],
                        "positive_sectors": ["黄金"],
                        "negative_flags": ["risk_asset_selloff", "rates_usd_pressure"],
                        "positive_flags": ["safe_haven_support"],
                        "impact_summary": "外部情报偏风险收缩。",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with (
                patch.object(auto_runner, "CLOSE_NODE_FILE", close_node_file),
                patch.object(auto_runner, "EXTERNAL_MARKET_REVIEW_FILE", external_file),
            ):
                auto_runner.merge_close_node_external_market_review()
                payload = json.loads(close_node_file.read_text(encoding="utf-8"))

        self.assertEqual(payload["external_market_review"]["risk_level"], "high")
        self.assertEqual(payload["external_market_review"]["a_share_bias"], "risk_off")
        self.assertEqual(payload["external_market_review"]["negative_sectors"], ["AI硬件", "半导体"])


class SmartSellApplyTests(unittest.TestCase):
    def test_do_smart_sell_fast_exits_without_live_holdings_or_active_pending(self) -> None:
        with (
            patch.object(trader, "ensure_trade_window", return_value=True),
            patch.object(trader, "load_track_record", return_value=[]),
            patch.object(trader, "get_positions", return_value=[]),
            patch.object(
                trader,
                "load_pending_orders",
                return_value=[{"action": "sell", "status": "filled", "code": "300001", "quantity": 100}],
            ),
            patch.object(
                trader,
                "summarize_pending_orders",
                return_value={"active_buy_codes": [], "active_sell_codes": [], "counts": {}},
            ),
            patch.object(trader, "get_balance") as balance_mock,
            patch.object(trader, "get_orders") as orders_mock,
            patch.object(trader, "refresh_pending_orders") as refresh_mock,
        ):
            code = trader.do_smart_sell(dry_run=False)

        self.assertEqual(code, trader.EXIT_NO_ACTION)
        balance_mock.assert_not_called()
        orders_mock.assert_not_called()
        refresh_mock.assert_not_called()

    def test_build_intraday_judgment_respects_supportive_external_alignment(self) -> None:
        context = {
            "balance": {
                "total_assets": 1000000.0,
                "avail_balance": 760000.0,
                "total_pos_value": 240000.0,
            },
            "pending_summary": {
                "active_sell_codes": [],
            },
        }
        review_payload = {
            "avg_profit_pct": 1.8,
            "market_temperature": "neutral",
            "opening_liquidity": {
                "available": True,
                "in_0931_window": True,
                "verdict": "healthy",
            },
            "external_market": {
                "available": True,
                "risk_level": "low",
                "a_share_bias": "selective_supportive",
                "negative_sectors": [],
                "neutral_sectors": ["机器人"],
                "positive_sectors": ["半导体", "AI硬件", "军工"],
                "recommended_actions": {
                    "opening_gate_bias": "supportive",
                    "allow_only_selective_rebound": False,
                    "broad_rebound_allowed": True,
                },
                "horizon_assessment": {
                    "short_term": {"bias": "broad_positive", "summary": "短期偏积极。"},
                    "mid_term": {"bias": "selective_positive", "summary": "中期结构偏强。"},
                    "long_term": {"bias": "selective_positive", "summary": "长期方向保留。"},
                },
                "short_flow_monitor": {
                    "pressure_level": "low",
                    "targeted_sectors": [],
                    "summary": "暂未识别到显著做空资金线索。",
                },
                "opening_anchor_break_monitor": {
                    "pressure_level": "low",
                    "broken_anchor_names": [],
                    "summary": "开盘锚股承接稳定。",
                },
                "weekend_digest_monitor": {
                    "active": True,
                    "bias": "neutral",
                    "summary": "周末信息中性分化。",
                },
            },
            "afternoon_watchlist": {
                "high_priority_review": [],
                "watch_close": [],
            },
            "holdings_review_top15": [],
        }

        payload = trader._build_intraday_judgment(
            context=context,
            review_payload=review_payload,
            review_status="PASS",
            pm_gate_status="pass",
        )

        self.assertEqual(payload["risk_bias"], "offensive")
        self.assertEqual(payload["rebound_bias"], "can_expand")
        self.assertEqual(payload["external_market"]["a_share_bias"], "selective_supportive")

    def test_build_intraday_judgment_releases_from_defensive_when_risk_on_scan_is_strong(self) -> None:
        context = {
            "balance": {
                "total_assets": 1000000.0,
                "avail_balance": 910000.0,
                "total_pos_value": 90000.0,
            },
            "pending_summary": {
                "active_sell_codes": [],
            },
        }
        review_payload = {
            "avg_profit_pct": 0.3,
            "market_temperature": "risk_on",
            "opening_liquidity": {
                "available": True,
                "in_0931_window": True,
                "verdict": "mixed",
            },
            "external_market": {
                "available": True,
                "risk_level": "high",
                "a_share_bias": "risk_off",
                "negative_sectors": ["原油化工"],
                "neutral_sectors": [],
                "positive_sectors": ["军工"],
                "recommended_actions": {
                    "opening_gate_bias": "defensive",
                    "allow_only_selective_rebound": True,
                    "broad_rebound_allowed": False,
                },
                "horizon_assessment": {
                    "short_term": {"bias": "negative", "summary": "短期仍偏谨慎。"},
                },
                "short_flow_monitor": {
                    "pressure_level": "medium",
                    "targeted_sectors": ["原油化工"],
                    "summary": "卖压仍在，但未继续恶化。",
                },
                "opening_anchor_break_monitor": {
                    "pressure_level": "medium",
                    "broken_anchor_names": ["上海合晶"],
                    "summary": "早盘锚股承压但午后未继续破位。",
                },
                "weekend_digest_monitor": {
                    "active": False,
                    "bias": "neutral",
                    "summary": "",
                },
            },
            "scan_status": {
                "is_fresh": True,
                "stocks_with_signal": 48,
                "signals_by_tier": {
                    "T1": 1,
                    "T2": 6,
                    "T3": 41,
                },
            },
            "afternoon_watchlist": {
                "high_priority_review": [],
                "watch_close": [],
            },
            "holdings_review_top15": [],
        }

        payload = trader._build_intraday_judgment(
            context=context,
            review_payload=review_payload,
            review_status="PASS",
            pm_gate_status="pass",
        )

        self.assertEqual(payload["risk_bias"], "balanced")
        self.assertEqual(payload["rebound_bias"], "selective_only")
        self.assertTrue(payload["scan_status"]["midday_release_ready"])
        self.assertTrue(payload["scan_status"]["midday_release_override"])

    def test_apply_successful_sell_reconciles_and_marks_confirmed(self) -> None:
        records = [{"code": "002947", "status": "holding"}]
        closed_records = [{"code": "002947", "status": "closed", "sell_order_id": "OID-1"}]
        with (
            patch.object(
                trader,
                "reconcile_after_trade",
                return_value=(closed_records, {"avail_balance": 1}, [{"code": "002947"}], True),
            ) as reconcile_mock,
            patch.object(trader, "save_track_record") as save_mock,
            patch.object(trader, "load_pending_orders", return_value=[]),
            patch.object(trader, "_active_pending_context_by_code", return_value={}),
            patch.object(trader, "_debug_report_smart_sell"),
        ):
            result = trader._apply_successful_sell(
                "002947",
                quantity=1200,
                price=12.34,
                close_reason="unit_test",
                records=records,
                balance={"avail_balance": 0},
                positions=[],
            )

        reconcile_mock.assert_called_once()
        save_mock.assert_called_once_with(closed_records)
        self.assertTrue(result["sell_confirmed"])
        self.assertEqual(result["record"]["status"], "closed")

    def test_apply_successful_sell_can_defer_reconcile_for_smart_sell(self) -> None:
        records = [{"code": "002947", "status": "holding"}]
        with (
            patch.object(trader, "reconcile_after_trade") as reconcile_mock,
            patch.object(trader, "save_track_record") as save_mock,
            patch.object(trader, "load_pending_orders", return_value=[]),
            patch.object(trader, "_active_pending_context_by_code", return_value={}),
            patch.object(trader, "_debug_report_smart_sell"),
        ):
            result = trader._apply_successful_sell(
                "002947",
                quantity=1200,
                price=12.34,
                close_reason="unit_test",
                records=records,
                balance={"avail_balance": 0},
                positions=[],
                defer_reconcile=True,
            )

        reconcile_mock.assert_not_called()
        save_mock.assert_not_called()
        self.assertFalse(result["sell_confirmed"])
        self.assertEqual(result["pending_ctx"]["reserved_qty"], 1200)
        self.assertEqual(result["record"]["status"], "holding")

    def test_resolve_trade_max_retries_uses_smart_sell_phase_overrides(self) -> None:
        self.assertEqual(
            trader._resolve_trade_max_retries({"strategy_action": "smart_sell", "execution_phase": "primary"}),
            3,
        )
        self.assertEqual(
            trader._resolve_trade_max_retries({"strategy_action": "smart_sell", "execution_phase": "tail_retry"}),
            2,
        )

    def test_do_sell_core_refreshes_live_state_before_writing_artifacts(self) -> None:
        old_date = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        holding_record = {
            "code": "688206",
            "name": "概伦电子",
            "tier": "1",
            "mode": "V9_full",
            "date": old_date,
            "entry_price": "38.98",
            "quantity": "2900",
            "status": "holding",
        }
        initial_positions = [
            {
                "code": "688206",
                "name": "概伦电子",
                "count": 2900,
                "avail_count": 2900,
                "value": 111650,
                "price": 38.5,
                "cost_price": 38.98,
                "profit_pct": -1.2,
            }
        ]
        refreshed_records = [
            {
                **holding_record,
                "status": "closed",
                "sell_order_id": "OID-1",
            }
        ]
        live_state = {
            "balance": {"avail_balance": 759985.88, "total_assets": 1130738.88, "total_pos_value": 370753.0},
            "positions": [],
            "orders": [{"id": "OID-1", "status": 4, "trade_count": 2900}],
            "pending_items": [{"code": "688206", "action": "sell", "status": "filled", "filled_quantity": 2900}],
            "records": refreshed_records,
            "reconcile_summary": {},
        }
        with (
            patch.object(trader, "ensure_trade_window", return_value=True),
            patch.object(trader, "load_track_record", return_value=[holding_record]),
            patch.object(trader, "get_positions", return_value=initial_positions),
            patch.object(trader, "get_balance", return_value={"avail_balance": 1000, "total_assets": 1000, "total_pos_value": 111650}),
            patch.object(trader, "get_orders", return_value=[]),
            patch.object(trader, "refresh_pending_orders", return_value=[]),
            patch.object(trader, "cleanup_pending_orders", return_value={"attempted": 0, "cancelled": 0, "failed": 0}),
            patch.object(trader, "sync_track_record", return_value=([holding_record], False)),
            patch.object(trader, "full_reconcile_positions", return_value=([holding_record], False, {})),
            patch.object(trader, "_active_position_map", return_value={"688206": initial_positions[0]}),
            patch.object(trader, "_load_today_tradability_exclusions", return_value={}),
            patch.object(trader, "execute_trade_action", return_value={"success": True, "order_id": "OID-1"}),
            patch.object(
                trader,
                "_apply_successful_sell",
                return_value={
                    "records": [holding_record],
                    "balance": {"avail_balance": 1000},
                    "positions": initial_positions,
                    "record": holding_record,
                    "sell_confirmed": False,
                    "pending_ctx": {"reserved_qty": 2900},
                },
            ),
            patch.object(trader, "_refresh_live_artifact_state", return_value=live_state) as live_mock,
            patch.object(trader, "write_account_artifacts") as write_mock,
            patch.object(trader, "_print_stats"),
        ):
            code = trader._do_sell_core(smart=False, dry_run=False)

        self.assertEqual(code, trader.EXIT_OK)
        live_mock.assert_called_once()
        self.assertEqual(write_mock.call_args.kwargs["pending_items"], live_state["pending_items"])
        self.assertEqual(write_mock.call_args.kwargs["positions"], [])
        self.assertEqual(write_mock.call_args.kwargs["records"], refreshed_records)

    def test_effective_sellable_quantity_respects_zero_avail_count(self) -> None:
        qty = trader._effective_sellable_quantity(
            {"code": "002254", "count": 1300, "avail_count": 0},
            tracked_qty=1300,
            pending_reserved_qty=0,
        )

        self.assertEqual(qty, 0)

    def test_sell_tail_retry_skips_when_only_t1_position_remains(self) -> None:
        sell_retry_queue = [
            {
                "code": "002254",
                "name": "泰和新材",
                "tier": "1",
                "qty": 1300,
                "cur_price": 19.68,
                "sell_reason": "unit_test",
            }
        ]
        positions = [{"code": "002254", "name": "泰和新材", "count": 1300, "avail_count": 0}]

        with (
            patch.object(trader.time, "sleep"),
            patch.object(trader, "get_positions", return_value=positions),
            patch.object(trader, "_active_position_map", return_value={"002254": positions[0]}),
            patch.object(trader, "execute_trade_action") as trade_mock,
            patch.object(trader, "_mark_smart_sell_rate_limit") as mark_mock,
        ):
            _, _, _, sold_count, confirmed_count, skipped_count = trader._run_sell_tail_retry_queue(
                sell_retry_queue,
                action="smart_sell",
                records=[],
                balance={"avail_balance": 0},
                positions=positions,
                sold_count=0,
                confirmed_count=0,
                skipped_count=0,
            )

        trade_mock.assert_not_called()
        mark_mock.assert_called_once_with("002254", 1300)
        self.assertEqual(sold_count, 0)
        self.assertEqual(confirmed_count, 0)
        self.assertEqual(skipped_count, 1)


class AddPositionGuardTests(unittest.TestCase):
    def test_throttle_trade_api_respects_rate_limit_cooldown(self) -> None:
        old_last_ts = trader._LAST_TRADE_API_TS
        old_cooldown_ts = trader._TRADE_API_COOLDOWN_UNTIL_TS
        trader._LAST_TRADE_API_TS = 99.0
        trader._TRADE_API_COOLDOWN_UNTIL_TS = 112.5
        try:
            with (
                patch.object(trader.time, "time", side_effect=[100.0, 112.6]),
                patch.object(trader.time, "sleep") as sleep_mock,
            ):
                trader._throttle_trade_api(2.0)

            sleep_mock.assert_called_once()
            self.assertAlmostEqual(sleep_mock.call_args.args[0], 12.5, places=2)
            self.assertAlmostEqual(trader._LAST_TRADE_API_TS, 112.6, places=2)
        finally:
            trader._LAST_TRADE_API_TS = old_last_ts
            trader._TRADE_API_COOLDOWN_UNTIL_TS = old_cooldown_ts

    def test_opening_add_position_uses_stricter_min_interval(self) -> None:
        class FakeOpeningDateTime:
            @classmethod
            def now(cls):
                return datetime(2026, 6, 29, 9, 36, 0)

        class FakeLateDateTime:
            @classmethod
            def now(cls):
                return datetime(2026, 6, 29, 10, 15, 0)

        order_context = {"execution_phase": "add_position", "strategy_action": "add_position"}
        with patch.object(trader, "datetime", FakeOpeningDateTime):
            opening_interval = trader._resolve_trade_min_interval(
                trader.TRADE_BUY_MIN_INTERVAL_SECONDS,
                order_context,
            )
        with patch.object(trader, "datetime", FakeLateDateTime):
            late_interval = trader._resolve_trade_min_interval(
                trader.TRADE_BUY_MIN_INTERVAL_SECONDS,
                order_context,
            )

        self.assertGreater(opening_interval, late_interval)
        self.assertAlmostEqual(opening_interval - late_interval, trader.TRADE_OPENING_BURST_EXTRA_SECONDS, places=2)

    def test_do_add_position_skips_codes_with_active_pending_buy(self) -> None:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        records = [
            {
                "code": "002947",
                "name": "Test",
                "tier": "1",
                "entry_price": "10.0",
                "target_amount": "30000",
                "quantity": "1000",
                "status": "holding",
                "mode": "unit",
                "date": yesterday,
            }
        ]
        positions = [
            {
                "code": "002947",
                "name": "Test",
                "count": 1000,
                "avail_count": 1000,
                "value": 10000,
                "price": 10.0,
                "cost_price": 10.0,
            }
        ]
        with (
            patch.object(trader, "ensure_trade_window", return_value=True),
            patch.object(trader, "load_track_record", return_value=records),
            patch.object(trader, "get_balance", return_value={"avail_balance": 50000}),
            patch.object(trader, "get_positions", return_value=positions),
            patch.object(trader, "get_orders", return_value=[]),
            patch.object(trader, "refresh_pending_orders", return_value=[{"code": "002947"}]),
            patch.object(trader, "sync_track_record", return_value=(records, False)),
            patch.object(trader, "full_reconcile_positions", return_value=(records, False, {})),
            patch.object(trader, "_active_position_map", return_value={"002947": positions[0]}),
            patch.object(trader, "summarize_pending_orders", return_value={"active_buy_codes": ["002947"]}),
            patch.object(trader, "_active_pending_context_by_code", return_value={"002947": {"reserved_qty": 500}}),
            patch.object(trader, "_load_today_tradability_exclusions", return_value={}),
            patch.object(trader, "connect_tdx", return_value=None),
            patch.object(trader, "execute_trade_action") as trade_mock,
        ):
            code = trader.do_add_position(dry_run=True)

        self.assertEqual(code, trader.EXIT_NO_ACTION)
        trade_mock.assert_not_called()

    def test_do_add_position_short_circuits_when_no_native_holding(self) -> None:
        records = [
            {
                "code": "300163",
                "name": "ExternalOnly",
                "status": "holding",
                "build_note": "[LIVE_POSITION_ONLY] imported_from_other_scope",
                "target_amount": "0",
            }
        ]
        positions = [
            {
                "code": "300163",
                "name": "ExternalOnly",
                "count": 1000,
                "avail_count": 1000,
                "value": 10000,
                "price": 10.0,
                "cost_price": 10.0,
            }
        ]
        with (
            patch.object(trader, "ensure_trade_window", return_value=True),
            patch.object(trader, "load_track_record", return_value=records),
            patch.object(trader, "get_balance", return_value={"avail_balance": 50000, "total_assets": 100000}),
            patch.object(trader, "get_positions", return_value=positions),
            patch.object(trader, "get_orders") as get_orders_mock,
            patch.object(trader, "refresh_pending_orders") as pending_mock,
            patch.object(trader, "_resolve_learning_preflight_guard") as learning_mock,
            patch.object(trader, "sync_track_record") as sync_mock,
            patch.object(trader, "full_reconcile_positions") as reconcile_mock,
        ):
            code = trader.do_add_position(dry_run=True)

        self.assertEqual(code, trader.EXIT_NO_ACTION)
        get_orders_mock.assert_not_called()
        pending_mock.assert_not_called()
        learning_mock.assert_not_called()
        sync_mock.assert_not_called()
        reconcile_mock.assert_not_called()

    def test_do_add_position_short_circuits_before_stale_learning_gate_when_empty(self) -> None:
        with (
            patch.object(trader, "ensure_trade_window", return_value=True),
            patch.object(trader, "load_track_record", return_value=[]),
            patch.object(trader, "get_balance", return_value={"avail_balance": 50000, "total_assets": 100000}),
            patch.object(trader, "get_positions", return_value=[]),
            patch.object(trader, "_resolve_learning_preflight_guard") as learning_mock,
        ):
            code = trader.do_add_position(dry_run=True)

        self.assertEqual(code, trader.EXIT_NO_ACTION)
        learning_mock.assert_not_called()


class AddPositionAggressiveTests(unittest.TestCase):
    def test_identity_profile_scores_v9_full_t1_big_meat(self) -> None:
        record = {
            "tier": "1",
            "mode": "V9_full",
            "build_note": "超级大行情满仓首建",
        }
        profile = trader._build_big_meat_identity_profile(record, profit_pct=20.0)
        self.assertGreaterEqual(profile["score"], 5)
        self.assertIn("V9_full", profile["notes"])

    def test_do_add_position_uses_extended_target_for_big_meat_candidate(self) -> None:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        records = [
            {
                "code": "002254",
                "name": "BigMeat",
                "tier": "1",
                "entry_price": "10.0",
                "target_amount": "30000",
                "quantity": "1000",
                "status": "holding",
                "mode": "V9_full",
                "date": yesterday,
            }
        ]
        positions = [
            {
                "code": "002254",
                "name": "BigMeat",
                "count": 1000,
                "avail_count": 1000,
                "value": 10000,
                "price": 10.0,
                "cost_price": 10.0,
                "profit_pct": 20.0,
            }
        ]
        with (
            patch.object(trader, "ensure_trade_window", return_value=True),
            patch.object(trader, "load_track_record", return_value=records),
            patch.object(trader, "get_balance", return_value={"avail_balance": 50000}),
            patch.object(trader, "get_positions", return_value=positions),
            patch.object(trader, "get_orders", return_value=[]),
            patch.object(trader, "refresh_pending_orders", return_value=[]),
            patch.object(trader, "sync_track_record", return_value=(records, False)),
            patch.object(trader, "full_reconcile_positions", return_value=(records, False, {})),
            patch.object(trader, "_active_position_map", return_value={"002254": positions[0]}),
            patch.object(trader, "summarize_pending_orders", return_value={"active_buy_codes": []}),
            patch.object(trader, "_active_pending_context_by_code", return_value={}),
            patch.object(trader, "_load_today_tradability_exclusions", return_value={}),
            patch.object(
                trader,
                "_resolve_learning_preflight_guard",
                return_value={"allow_add_position": True, "allow_aggressive_add": True, "status": "allow", "notes": []},
            ),
            patch.object(trader, "connect_tdx", return_value=object()),
            patch.object(trader, "evaluate_signal_decay", return_value=(False, "信号完好", 0)),
            patch.object(
                trader,
                "_build_big_meat_add_profile",
                return_value={"eligible": True, "target_multiplier": 1.3, "reason": "unit"},
            ),
            patch.object(
                trader,
                "_build_holding_big_meat_profile",
                return_value={
                    "holding_score": 4.2,
                    "reason": "unit-prelock",
                    "late_bloom_eligible": False,
                    "dominant_winner": False,
                    "prelock_candidate": True,
                    "core_ratio": 0.7,
                    "hold_lock_days": 0,
                    "profit_pct": 20.0,
                    "allow_core_hold": True,
                },
            ),
            patch.object(trader, "execute_trade_action", return_value={"success": True}) as trade_mock,
            patch.object(
                trader,
                "_apply_successful_buys",
                return_value={"records": records, "balance": {"avail_balance": 21000}, "positions": positions},
            ),
            patch.object(trader, "write_account_artifacts") as write_mock,
        ):
            code = trader.do_add_position(dry_run=False)

        self.assertEqual(code, trader.EXIT_OK)
        self.assertEqual(trade_mock.call_args.args[2], 2000)
        execution_result = write_mock.call_args.kwargs["execution_result"]
        self.assertEqual(execution_result["aggressive_add_count"], 0)
        self.assertEqual(execution_result["aggressive_add_codes"], [])
        self.assertEqual(execution_result["aggressive_add_items"], [])

    def test_big_meat_add_profile_uses_identity_and_trend_score(self) -> None:
        class FakeApi:
            def get_security_bars(self, category, market, code, start, count):
                if category == 9:
                    rows = []
                    for idx in range(25):
                        close = 10.0 + idx * 0.25
                        rows.append({"datetime": f"2026-06-{idx+1:02d}", "open": close * 0.995, "close": close, "high": close * 1.01})
                    rows[-2]["close"] = 15.0
                    rows[-2]["high"] = 15.1
                    rows[-1]["close"] = 16.2
                    rows[-1]["high"] = 16.23
                    rows[-1]["open"] = 15.9
                    return rows
                if category == 0:
                    return [
                        {"datetime": "2026-06-25 10:30", "close": 14.6},
                        {"datetime": "2026-06-25 10:35", "close": 14.7},
                        {"datetime": "2026-06-25 10:40", "close": 14.8},
                        {"datetime": "2026-06-25 10:45", "close": 14.9},
                        {"datetime": "2026-06-25 10:50", "close": 15.0},
                        {"datetime": "2026-06-25 10:55", "close": 15.1},
                    ]
                if category == 5:
                    return [
                        {"datetime": "2026-W24", "close": 12.0},
                        {"datetime": "2026-W25", "close": 15.0},
                    ]
                return []

            @staticmethod
            def to_df(rows):
                import pandas as pd
                return pd.DataFrame(rows)

        profile = trader._build_big_meat_add_profile(
            FakeApi(),
            "002254",
            record={"tier": "1", "mode": "V9_full", "build_note": "超级大行情满仓首建"},
            profit_pct=20.0,
        )
        self.assertTrue(profile["eligible"])
        self.assertGreaterEqual(profile["score"], trader.ADD_POSITION_BIG_MEAT_SCORE_THRESHOLD)
        self.assertIn("V9_full", profile["reason"])


class ProfitCapitalAllocationTests(unittest.TestCase):
    def test_mode_capital_profile_prefers_profitable_modes(self) -> None:
        records = [
            {"status": "closed", "mode": "trend_only", "target_amount": 30000, "pnl_pct": 8.0},
            {"status": "closed", "mode": "trend_only", "target_amount": 30000, "pnl_pct": 5.0},
            {"status": "closed", "mode": "vol_breakout", "target_amount": 30000, "pnl_pct": -6.0},
            {"status": "closed", "mode": "vol_breakout", "target_amount": 30000, "pnl_pct": -3.0},
            {"status": "closed", "mode": "external_sync", "target_amount": 30000, "pnl_pct": 12.0},
        ]

        profile = trader._build_mode_capital_profile(records)

        self.assertEqual(profile["trend_only"]["target_multiplier"], 1.0)
        self.assertEqual(profile["trend_only"]["initial_multiplier"], 1.0)
        self.assertGreater(profile["trend_only"]["legacy_target_multiplier"], 1.0)
        self.assertGreater(profile["trend_only"]["legacy_initial_multiplier"], 1.0)
        self.assertEqual(profile["trend_only"]["bias_label"], "profit_priority")
        self.assertEqual(profile["vol_breakout"]["target_multiplier"], 1.0)
        self.assertEqual(profile["vol_breakout"]["initial_multiplier"], 1.0)
        self.assertLess(profile["vol_breakout"]["legacy_target_multiplier"], 1.0)
        self.assertLess(profile["vol_breakout"]["legacy_initial_multiplier"], 1.0)
        self.assertEqual(profile["vol_breakout"]["bias_label"], "capital_conserve")
        self.assertIn("external_sync", profile)

    def test_mode_capital_profile_supports_market_regime_specific_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            decision_file = Path(tmpdir) / "decisions.jsonl"
            decision_rows = [
                {
                    "selected": True,
                    "decision_id": "2026-06-25|2026-06-25 14:50|300602",
                    "trade_date": "2026-06-25",
                    "code": "300602",
                    "market_regime": "active",
                }
            ]
            decision_file.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in decision_rows), encoding="utf-8")
            records = [
                {
                    "status": "closed",
                    "date": "2026-06-25",
                    "code": "300602",
                    "mode": "trend_only",
                    "target_amount": 30000,
                    "pnl_pct": 8.0,
                    "decision_id": "2026-06-25|2026-06-25 14:50|300602",
                },
                {
                    "status": "closed",
                    "date": "2026-06-26",
                    "code": "300603",
                    "mode": "trend_only",
                    "target_amount": 30000,
                    "pnl_pct": 6.0,
                },
            ]
            with patch.object(trader, "MODEL_DECISIONS_FILE", decision_file):
                profile = trader._build_mode_capital_profile(records)

        self.assertIn("trend_only", profile)
        self.assertIn("active::trend_only", profile)
        self.assertEqual(profile["active::trend_only"]["market_regime"], "active")
        self.assertEqual(profile["active::trend_only"]["sample_count"], 1)

    def test_resolve_mode_capital_plan_adjusts_target_and_initial_amount(self) -> None:
        profile = {
            "trend_only": {
                "sample_count": 3,
                "avg_return_pct": 6.2,
                "target_multiplier": 1.15,
                "initial_multiplier": 1.10,
            }
        }

        plan = trader._resolve_mode_capital_plan(
            "trend_only",
            base_target_amount=60000,
            base_initial_amount=36000,
            mode_capital_profile=profile,
        )

        self.assertEqual(plan["target_amount"], 60000.0)
        self.assertEqual(plan["initial_amount"], 36000.0)
        self.assertEqual(plan["note"], "")

    def test_resolve_mode_capital_plan_prefers_regime_specific_profile(self) -> None:
        profile = {
            "trend_only": {
                "sample_count": 5,
                "avg_return_pct": 3.0,
                "target_multiplier": 1.05,
                "initial_multiplier": 1.02,
            },
            "active::trend_only": {
                "sample_count": 2,
                "avg_return_pct": 9.0,
                "target_multiplier": 1.18,
                "initial_multiplier": 1.12,
                "market_regime": "active",
            },
        }

        plan = trader._resolve_mode_capital_plan(
            "trend_only",
            base_target_amount=60000,
            base_initial_amount=36000,
            mode_capital_profile=profile,
            market_regime="active",
        )

        self.assertEqual(plan["target_amount"], 60000.0)
        self.assertEqual(plan["initial_amount"], 36000.0)
        self.assertEqual(plan["note"], "")

    def test_capital_allocation_feedback_reports_positive_verdict(self) -> None:
        records = [
            {
                "status": "closed",
                "date": "2026-06-24",
                "sell_date": "2026-06-25",
                "code": "300602",
                "mode": "trend_only",
                "target_amount": 30000,
                "pnl_pct": 9.0,
                "build_note": "首建60%; 盈利倾斜加码(目1.10x/首1.05x/样本4/均收+6.00%)",
            },
            {
                "status": "closed",
                "date": "2026-06-24",
                "sell_date": "2026-06-25",
                "code": "300603",
                "mode": "trend_only",
                "target_amount": 30000,
                "pnl_pct": 2.0,
                "build_note": "首建60%",
            },
        ]

        feedback = trader._build_capital_allocation_feedback(records, trade_date="2026-06-25", decision_reference={})

        self.assertEqual(feedback["verdict"], "positive")
        self.assertEqual(feedback["today_biased_closed"]["count"], 1)
        self.assertEqual(feedback["today_biased_closed"]["codes"], ["300602"])
        self.assertGreater(feedback["historical_biased_closed"]["avg_return_pct"], feedback["historical_unbiased_closed"]["avg_return_pct"])

    def test_resolve_add_position_target_amount_keeps_big_meat_priority(self) -> None:
        profile = {
            "trend_only": {
                "sample_count": 4,
                "avg_return_pct": 7.0,
                "target_multiplier": 1.10,
                "initial_multiplier": 1.10,
            }
        }

        plan = trader._resolve_add_position_target_amount(
            30000,
            record={"mode": "trend_only"},
            mode_capital_profile=profile,
            big_meat_profile={"eligible": True, "target_multiplier": 1.3, "reason": "unit"},
        )

        self.assertEqual(plan["target_amount"], 39000.0)
        self.assertEqual(plan["capital_note"], "")
        self.assertIn("大肉激进加仓", plan["aggressive_add_note"])


class ReviewSummaryTests(unittest.TestCase):
    def test_extract_aggressive_add_summary_from_latest_execution(self) -> None:
        summary = {
            "latest_execution_result": {
                "action": "add_position",
                "aggressive_add_count": 1,
                "aggressive_add_items": [
                    {"code": "002254", "reason": "大肉激进加仓: 目标提升至1.30x"}
                ],
            }
        }
        review = trader._extract_aggressive_add_summary(summary)
        self.assertTrue(review["available"])
        self.assertEqual(review["count"], 1)
        self.assertEqual(review["codes"], ["002254"])

    def test_midday_and_close_review_include_aggressive_add_summary(self) -> None:
        summary = {
            "latest_execution_result": {
                "action": "add_position",
                "aggressive_add_count": 1,
                "aggressive_add_codes": ["002254"],
                "aggressive_add_items": [
                    {"code": "002254", "reason": "大肉激进加仓: 目标提升至1.30x"}
                ],
            },
            "performance": {"closed_count": 1, "holding_count": 2, "win_rate_pct": 50.0, "avg_return_pct": 3.2, "realized_pnl": 1000},
            "account_status": {"live": True},
        }
        with (
            patch.object(trader, "_read_json", return_value=summary),
            patch.object(trader.os.path, "exists", return_value=True),
            patch.object(trader, "refresh_pending_orders", return_value=[]),
            patch.object(trader, "summarize_pending_orders", return_value={"counts": {"stale": 0, "submitted": 0}, "active_buy_codes": [], "active_sell_codes": []}),
            patch.object(trader, "_active_position_map", return_value={}),
        ):
            midday = trader.build_midday_review(balance={}, positions=[], orders=[], records=[])

        close_payload = trader._build_close_node_payload(summary=summary, reconcile_summary={"pending": {"counts": {"stale": 0}}})
        self.assertEqual(midday["aggressive_add_review"]["codes"], ["002254"])
        self.assertEqual(close_payload["aggressive_add_review"]["codes"], ["002254"])


class EvolutionAbsorptionTests(unittest.TestCase):
    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_record_decisions_writes_provenance_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            decision_file = Path(tmpdir) / "decisions.jsonl"
            candidates = [
                {
                    "code": "2947",
                    "name": "BigMeat",
                    "tier": 1,
                    "mode": "V9_full",
                    "model_score": 88.12,
                    "model_market_score": 81.2,
                    "model_sector_score": 76.5,
                    "model_stock_score": 90.1,
                    "model_flow_score": 79.8,
                    "selection_rank": 1,
                    "target_amount": 30000,
                }
            ]
            with patch.object(evolving_model, "MODEL_DECISIONS_FILE", decision_file):
                evolving_model.record_decisions(
                    "2026-06-25 14:50",
                    candidates,
                    selected_codes={"002947"},
                    scan_context={"meta": {"market_regime": "活跃市", "signals_by_tier": {"T1": 1}}},
                )

            rows = [json.loads(line) for line in decision_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["code"], "002947")
            self.assertEqual(row["decision_id"], "2026-06-25|2026-06-25 14:50|002947")
            self.assertEqual(row["decision_run_slot"], "2026-06-25 14:50")
            self.assertEqual(row["selection_rank"], 1)
            self.assertEqual(row["target_amount"], 30000.0)
            self.assertTrue(row["selected"])
            self.assertEqual(len(row["selected_reason_hash"]), 16)

    def test_build_daily_evolution_bundle_splits_learn_observe_damage_and_profit_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            decision_file = Path(tmpdir) / "decisions.jsonl"
            trade_log_file = Path(tmpdir) / "trade_log.jsonl"
            self._write_jsonl(
                decision_file,
                [
                    {"trade_date": "2026-06-25", "code": "300602", "selected": True, "decision_id": "d1", "decision_run_slot": "2026-06-25 14:50", "selected_reason_hash": "hash300602"},
                    {"trade_date": "2026-06-25", "code": "688107", "selected": True, "decision_id": "d2", "decision_run_slot": "2026-06-25 14:50", "selected_reason_hash": "hash688107"},
                    {"trade_date": "2026-06-25", "code": "002947", "selected": True, "decision_id": "d3", "decision_run_slot": "2026-06-25 14:50", "selected_reason_hash": "hash002947"},
                ],
            )
            self._write_jsonl(
                trade_log_file,
                [
                    {
                        "logged_at": "2026-06-25 11:15:00",
                        "event_type": "retry_wait",
                        "action": "sell",
                        "code": "688107",
                        "result_code": "112",
                        "retry_attempt": 1,
                        "retry_total_attempts": 3,
                    }
                ],
            )
            records = [
                {
                    "date": "2026-06-25",
                    "buy_time": "09:35:00",
                    "code": "300602",
                    "name": "TrendWinner",
                    "tier": "2",
                    "entry_price": "10.0",
                    "quantity": "1000",
                    "buy_amount": "10000",
                    "sell_date": "2026-06-25",
                    "sell_time": "14:45:00",
                    "sell_price": "10.65",
                    "pnl": "653",
                    "pnl_pct": "6.53",
                    "status": "closed",
                    "mode": "trend_only",
                    "build_note": "",
                    "target_amount": "20000",
                    "close_reason": "正常止盈",
                },
                {
                    "date": "2026-06-25",
                    "buy_time": "09:45:00",
                    "code": "688107",
                    "name": "RetrySell",
                    "tier": "2",
                    "entry_price": "20.0",
                    "quantity": "500",
                    "buy_amount": "10000",
                    "sell_date": "2026-06-25",
                    "sell_time": "13:15:00",
                    "sell_price": "19.5",
                    "pnl": "-250",
                    "pnl_pct": "-2.5",
                    "status": "closed",
                    "mode": "trend_only",
                    "build_note": "",
                    "target_amount": "20000",
                    "close_reason": "正常卖出",
                },
                {
                    "date": "2026-06-25",
                    "buy_time": "09:50:00",
                    "code": "002947",
                    "name": "BigMeat",
                    "tier": "1",
                    "entry_price": "10.0",
                    "quantity": "1000",
                    "buy_amount": "10000",
                    "sell_date": "2026-06-25",
                    "sell_time": "10:45:00",
                    "sell_price": "10.55",
                    "pnl": "550",
                    "pnl_pct": "5.5",
                    "status": "closed",
                    "mode": "V9_full",
                    "build_note": "超级大行情满仓首建",
                    "target_amount": "30000",
                    "close_reason": "信号衰减",
                },
            ]
            with (
                patch.object(trader, "MODEL_DECISIONS_FILE", str(decision_file)),
                patch.object(trader, "TRADE_API_LOG_FILE", str(trade_log_file)),
                patch.object(trader, "_collect_alpha_loss_events", wraps=trader._collect_alpha_loss_events),
            ):
                bundle = trader._build_daily_evolution_bundle(summary={}, records=records, trade_date="2026-06-25")

        self.assertEqual(bundle["summary"]["learnable_sample_count"], 1)
        self.assertEqual(bundle["summary"]["observe_only_count"], 2)
        self.assertEqual(bundle["summary"]["execution_damaged_count"], 1)
        self.assertEqual(bundle["summary"]["profit_truncation_count"], 1)
        self.assertEqual(bundle["summary"]["alpha_loss_event_count"], 1)
        self.assertEqual([item["code"] for item in bundle["direct_learn_items"]], ["300602"])
        self.assertCountEqual([item["code"] for item in bundle["observe_only_items"]], ["688107", "002947"])
        self.assertEqual(bundle["execution_damaged_items"][0]["code"], "688107")
        self.assertEqual(bundle["profit_truncation_items"][0]["code"], "002947")
        truncation_item = next(item for item in bundle["observe_only_items"] if item["code"] == "002947")
        self.assertIn("profit_truncation", truncation_item["blocked_reasons"])

    def test_build_daily_evolution_bundle_adds_missed_opportunity_posterior_returns(self) -> None:
        decisions = [
            {
                "trade_date": "2026-07-08",
                "code": "300602",
                "name": "飞荣达",
                "tier": 2,
                "mode": "trend_only",
                "score": 88.2,
                "selected": True,
                "run_slot": "2026-07-08_1451",
                "market_regime": "活跃市",
            },
            {
                "trade_date": "2026-07-09",
                "code": "002254",
                "name": "泰和新材",
                "tier": 1,
                "mode": "V9_full",
                "score": 91.0,
                "selected": True,
                "run_slot": "2026-07-09_1451",
                "market_regime": "活跃市",
            },
        ]

        def fake_read_jsonl(path, limit=5000):
            if path == trader.MODEL_DECISIONS_FILE:
                return decisions
            return []

        def fake_load_scan_snapshot_rows(*, trade_date="", scan_manifest=None):
            if trade_date == "2026-07-08":
                return {
                    "trade_date": "2026-07-08",
                    "csv_path": "scan-0708.csv",
                    "rows": [],
                    "by_code": {
                        "300602": {"code": "300602", "entry_price": "10.0", "close": "10.0"},
                    },
                }
            if trade_date == "2026-07-09":
                return {
                    "trade_date": "2026-07-09",
                    "csv_path": "scan-0709.csv",
                    "rows": [],
                    "by_code": {
                        "300602": {"code": "300602", "entry_price": "11.5", "close": "11.5"},
                        "002254": {"code": "002254", "entry_price": "20.0", "close": "20.0"},
                    },
                }
            return {"trade_date": trade_date, "csv_path": "", "rows": [], "by_code": {}}

        with (
            patch.object(trader, "_read_jsonl", side_effect=fake_read_jsonl),
            patch.object(trader, "_collect_alpha_loss_events", return_value=[]),
            patch.object(trader, "_build_scan_snapshot_manifest", return_value={"2026-07-08": "scan-0708.csv", "2026-07-09": "scan-0709.csv"}),
            patch.object(trader, "_load_scan_snapshot_rows", side_effect=fake_load_scan_snapshot_rows),
        ):
            bundle = trader._build_daily_evolution_bundle(summary={}, records=[], trade_date="2026-07-09")

        self.assertEqual(bundle["summary"]["missed_opportunity_count"], 1)
        self.assertEqual(bundle["summary"]["missed_opportunity_pending_count"], 1)
        self.assertEqual(bundle["missed_opportunity_items"][0]["code"], "300602")
        self.assertAlmostEqual(bundle["missed_opportunity_items"][0]["posterior_return_pct"], 15.0, places=4)
        self.assertTrue(bundle["missed_opportunity_items"][0]["positive_opportunity"])
        self.assertEqual(bundle["missed_opportunity_pending_items"][0]["code"], "002254")
        self.assertEqual(bundle["missed_opportunity_pending_items"][0]["posterior_status"], "pending_next_session_snapshot")

    def test_close_node_payload_includes_missed_opportunity_followup(self) -> None:
        bundle = {
            "trade_date": "2026-07-09",
            "source_stats": {
                "today_closed_count": 0,
            },
            "summary": {
                "learnable_sample_count": 0,
                "observe_only_count": 0,
                "execution_damaged_count": 0,
                "profit_truncation_count": 0,
                "alpha_loss_event_count": 0,
            },
            "missed_opportunity_summary": {
                "matured_count": 2,
                "positive_count": 1,
                "strong_positive_count": 1,
                "avg_return_pct": 2.3,
                "pending_count": 1,
                "positive_codes": ["300602"],
                "strong_positive_codes": ["300602"],
            },
            "missed_opportunity_items": [{"code": "300602"}],
            "direct_learn_items": [],
            "observe_only_items": [],
            "execution_damaged_items": [],
            "profit_truncation_items": [],
            "intraday_judgment_review": {
                "available": True,
                "trade_date": "2026-07-09",
                "verdict": "mixed",
                "score": 70,
            },
            "regime_execution_review": {
                "available": True,
                "trade_date": "2026-07-09",
                "verdict": "mixed",
                "score": 70,
                "positive_sample": False,
                "label": "defensive-low-exposure-needs-calibration",
            },
            "capital_allocation_feedback": {
                "verdict": "neutral",
                "today_biased_closed": {"count": 0},
                "historical_biased_closed": {"count": 0},
            },
        }

        with patch.object(
            trader,
            "_build_engineering_review",
            return_value={
                "available": True,
                "trade_date": "2026-07-09",
                "verdict": "clean",
                "incident_count": 0,
                "recurring_incident_count": 0,
                "category_counts": {},
                "high_severity_count": 0,
                "incident_codes": [],
                "hardening_actions": [],
                "incidents": [],
                "summary": "clean",
            },
        ):
            payload = trader._build_close_node_payload(
                summary={
                    "performance": {
                        "closed_count": 0,
                        "holding_count": 0,
                        "win_rate_pct": 0.0,
                        "avg_return_pct": 0.0,
                        "realized_pnl": 0.0,
                    },
                    "account_status": {"live": True},
                },
                reconcile_summary={"pending": {"counts": {"stale": 0}}},
                daily_evolution_bundle=bundle,
            )

        self.assertEqual(payload["learning_gate_basis"]["missed_opportunity_count"], 2)
        self.assertEqual(payload["learning_gate_basis"]["missed_opportunity_positive_count"], 1)
        self.assertEqual(payload["learning_gate_basis"]["missed_opportunity_pending_count"], 1)
        self.assertAlmostEqual(payload["learning_gate_basis"]["missed_opportunity_avg_return_pct"], 2.3, places=4)
        followup_codes = [item["code"] for item in payload["evolution_followups"]]
        self.assertIn("missed_positive_opportunities_present", followup_codes)

    def test_regime_bias_action_no_longer_rewards_defensive_low_exposure(self) -> None:
        bundle = {
            "trade_date": "2026-07-09",
            "regime_execution_review": {
                "trade_date": "2026-07-09",
                "positive_sample": True,
                "label": "defensive-low-exposure-good-execution",
            },
        }
        history = [
            {"trade_date": "2026-07-07", "positive_sample": True, "label": "defensive-low-exposure-good-execution"},
            {"trade_date": "2026-07-08", "positive_sample": True, "label": "defensive-low-exposure-good-execution"},
            {"trade_date": "2026-07-09", "positive_sample": True, "label": "defensive-low-exposure-good-execution"},
        ]

        with patch.object(trader, "_load_regime_execution_history", return_value=history):
            action = trader._build_regime_bias_action(bundle, trade_date="2026-07-09")

        self.assertFalse(action["active"])
        self.assertEqual(action["stage"], "observe_only")
        self.assertEqual(action["target_amount_ratio"], 1.0)
        self.assertEqual(action["initial_amount_ratio"], 1.0)
        self.assertEqual(action["add_position_target_ratio"], 1.0)
        self.assertTrue(action["allow_aggressive_add"])
        self.assertEqual(action["positive_sample_count"], 3)
        self.assertIn("不触发仓位收缩", " ".join(action["notes"]))

    def test_pm_buy_guardrails_prioritizes_release_when_missed_profit_exists(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        midday_payload = {
            "date": today,
            "pm_gate_status": "pass",
            "intraday_judgment": {
                "trade_date": today,
                "pm_gate_status": "pass",
                "risk_bias": "balanced",
                "rebound_bias": "selective_only",
                "market_temperature": "risk_on",
                "confidence": 0.62,
                "scan_status": {
                    "midday_release_ready": True,
                    "midday_release_override": True,
                },
            },
        }
        learning_actions = {
            "summary": {
                "missed_opportunity_positive_count": 2,
                "missed_opportunity_avg_return_pct": 2.6,
            }
        }

        with (
            patch.object(trader, "_load_latest_midday_payload", return_value=midday_payload),
            patch.object(trader, "_load_learning_actions", return_value=learning_actions),
        ):
            payload = trader._build_pm_buy_guardrails()

        self.assertEqual(payload["reason"], "release_opportunity_confirm")
        self.assertTrue(payload["allow_buy"])
        self.assertNotIn("V9_full", payload["blocked_modes"])
        self.assertGreaterEqual(payload["max_new_positions"], 2)
        self.assertIn("盈利机会", " ".join(payload["notes"]))

    def test_attach_intraday_judgment_review_validates_midday_defensive_call(self) -> None:
        bundle = {
            "trade_date": "2026-06-25",
            "summary": {
                "learnable_sample_count": 1,
                "observe_only_count": 1,
                "execution_damaged_count": 0,
                "profit_truncation_count": 0,
                "alpha_loss_event_count": 0,
            },
            "direct_learn_items": [{"code": "002254"}],
            "observe_only_items": [{"code": "002947", "blocked_reasons": ["profit_truncation"]}],
        }
        midday_payload = {
            "date": "2026-06-25",
            "stage": "pm_gate",
            "intraday_judgment": {
                "available": True,
                "trade_date": "2026-06-25",
                "generated_at": "2026-06-25 11:31:00",
                "risk_bias": "defensive",
                "rebound_bias": "avoid_broad_rebound",
                "confidence": 0.72,
                "cash_ratio": 0.42,
                "position_exposure_ratio": 0.58,
                "reduce_watch_codes": ["002254", "002947"],
                "strong_hold_codes": ["300323"],
                "opening_liquidity": {
                    "available": True,
                    "generated_at": "2026-06-25 09:35:00",
                    "verdict": "fragile",
                    "in_0931_window": True,
                    "issue_ratio": 0.061,
                },
                "external_market": {
                    "available": True,
                    "generated_at": "2026-06-25 09:28:00",
                    "window_tag": "opening_0931",
                    "risk_level": "high",
                    "a_share_bias": "risk_off",
                    "negative_sectors": ["AI硬件", "算力"],
                    "neutral_sectors": ["消费"],
                    "positive_sectors": ["黄金"],
                    "horizon_assessment": {
                        "short_term": {"bias": "negative", "summary": "短期偏谨慎。"},
                        "mid_term": {"bias": "neutral", "summary": "中期偏中性分化。"},
                        "long_term": {"bias": "selective_positive", "summary": "长期保留结构性方向。"},
                    },
                    "recommended_actions": {
                        "opening_gate_bias": "defensive",
                        "allow_only_selective_rebound": True,
                    },
                    "short_flow_monitor": {
                        "pressure_level": "high",
                        "pressure_score": 8.4,
                        "signals": ["index_future_short", "quant_sell"],
                        "targeted_sectors": ["AI硬件", "算力"],
                        "summary": "做空资金信号偏强，优先防范高弹性板块被空头集中压制。",
                    },
                    "opening_anchor_break_monitor": {
                        "pressure_level": "high",
                        "pressure_points": 5,
                        "broken_anchor_names": ["佳创视讯", "正帆科技"],
                        "summary": "前期领涨锚股与大成交权重锚同时走弱，属于明显的开盘做空验证信号。",
                    },
                    "weekend_digest_monitor": {
                        "active": True,
                        "bias": "negative",
                        "negative_sectors": ["AI硬件"],
                        "positive_sectors": ["黄金"],
                        "summary": "周末汇总整体偏谨慎，周一开盘先防止利空集中兑现。",
                    },
                    "headline": "隔夜风险资产走弱",
                },
            },
        }
        summary = {
            "account": {
                "total_assets": 1000000.0,
                "avail_balance": 820000.0,
                "total_pos_value": 180000.0,
            }
        }
        records = [
            {"code": "300323", "status": "holding"},
            {"code": "002254", "status": "closed"},
            {"code": "002947", "status": "closed"},
        ]

        with patch.object(trader, "_load_latest_midday_payload", return_value=midday_payload):
            reviewed_bundle = trader._attach_intraday_judgment_review(
                bundle,
                summary=summary,
                records=records,
                positions=[{"code": "300323", "count": 4300}],
            )

        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["verdict"], "validated")
        self.assertEqual(reviewed_bundle["summary"]["intraday_judgment_verdict"], "validated")
        self.assertGreaterEqual(reviewed_bundle["summary"]["intraday_judgment_score"], 80)
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["retained_strong_codes"], ["300323"])
        self.assertCountEqual(reviewed_bundle["intraday_judgment_review"]["reduced_focus_codes"], ["002254", "002947"])
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["opening_liquidity"]["verdict"], "fragile")
        self.assertTrue(reviewed_bundle["intraday_judgment_review"]["opening_liquidity"]["in_0931_window"])
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["external_market"]["risk_level"], "high")
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["external_market"]["negative_sectors"], ["AI硬件", "算力"])
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["external_market"]["neutral_sectors"], ["消费"])
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["external_market"]["short_flow_monitor"]["pressure_level"], "high")
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["external_market"]["opening_anchor_break_monitor"]["pressure_level"], "high")
        self.assertTrue(reviewed_bundle["intraday_judgment_review"]["external_market"]["weekend_digest_monitor"]["active"])
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["external_market"]["weekend_digest_monitor"]["bias"], "negative")

    def test_intraday_judgment_review_downgrades_risk_on_pass_zero_opening_day(self) -> None:
        bundle = {
            "trade_date": "2026-07-09",
            "source_stats": {
                "today_opened_count": 0,
            },
            "direct_learn_items": [{"code": "002254"}],
            "observe_only_items": [{"code": "002947", "blocked_reasons": ["profit_truncation"]}],
        }
        midday_payload = {
            "date": "2026-07-09",
            "stage": "pm_gate",
            "pm_gate_status": "pass",
            "intraday_judgment": {
                "available": True,
                "trade_date": "2026-07-09",
                "generated_at": "2026-07-09 11:35:00",
                "risk_bias": "defensive",
                "market_temperature": "risk_on",
                "pm_gate_status": "pass",
                "rebound_bias": "avoid_broad_rebound",
                "confidence": 0.70,
                "cash_ratio": 0.42,
                "position_exposure_ratio": 0.58,
                "reduce_watch_codes": ["002254", "002947"],
                "strong_hold_codes": ["300323"],
                "opening_liquidity": {
                    "available": True,
                    "generated_at": "2026-07-09 09:31:00",
                    "verdict": "mixed",
                    "in_0931_window": True,
                    "issue_ratio": 0.01,
                },
                "external_market": {
                    "available": True,
                    "generated_at": "2026-07-09 09:31:00",
                    "window_tag": "opening_0931",
                    "risk_level": "high",
                    "a_share_bias": "risk_off",
                    "negative_sectors": ["AI硬件", "算力"],
                    "neutral_sectors": ["消费"],
                    "positive_sectors": ["黄金"],
                    "horizon_assessment": {
                        "short_term": {"bias": "negative", "summary": "短期偏谨慎。"},
                    },
                    "recommended_actions": {
                        "opening_gate_bias": "defensive",
                    },
                    "short_flow_monitor": {
                        "pressure_level": "high",
                        "summary": "做空资金偏强。",
                    },
                    "opening_anchor_break_monitor": {
                        "pressure_level": "high",
                        "summary": "开盘锚股承压。",
                    },
                    "headline": "隔夜风险资产走弱",
                },
            },
        }
        summary = {
            "account": {
                "total_assets": 1000000.0,
                "avail_balance": 950000.0,
                "total_pos_value": 50000.0,
            },
            "scan_status": {
                "stocks_with_signal": 48,
            },
        }
        records = [
            {"code": "300323", "status": "holding"},
            {"code": "002254", "status": "closed"},
            {"code": "002947", "status": "closed"},
        ]

        with patch.object(trader, "_load_latest_midday_payload", return_value=midday_payload):
            reviewed_bundle = trader._attach_intraday_judgment_review(
                bundle,
                summary=summary,
                records=records,
                positions=[{"code": "300323", "count": 4300}],
            )

        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["verdict"], "mixed")
        self.assertTrue(reviewed_bundle["intraday_judgment_review"]["missed_risk_on_deployment"])
        self.assertEqual(reviewed_bundle["summary"]["intraday_judgment_verdict"], "mixed")
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["market_temperature"], "risk_on")
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["pm_gate_status"], "pass")
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["stocks_with_signal"], 48)
        self.assertEqual(reviewed_bundle["intraday_judgment_review"]["today_opened_count"], 0)

    def test_regime_execution_review_does_not_reward_risk_on_zero_opening_day(self) -> None:
        bundle = {
            "trade_date": "2026-07-09",
            "source_stats": {
                "today_opened_count": 0,
                "today_closed_count": 0,
            },
            "intraday_judgment_review": {
                "available": True,
                "trade_date": "2026-07-09",
                "risk_bias": "defensive",
                "market_temperature": "risk_on",
                "pm_gate_status": "pass",
                "verdict": "mixed",
                "score": 70,
            },
        }
        summary = {
            "account": {
                "total_assets": 1000000.0,
                "total_pos_value": 18000.0,
            },
            "account_status": {"live": True},
            "scan_status": {"stocks_with_signal": 48},
            "pending_orders": {
                "active_buy_codes": [],
                "active_sell_codes": [],
            },
        }
        external_rows = [
            {
                "trade_date": "2026-07-07",
                "generated_at": "2026-07-07 09:31:00",
                "risk_level": "high",
                "a_share_bias": "risk_off",
                "negative_sectors": ["AI硬件", "算力"],
            },
            {
                "trade_date": "2026-07-08",
                "generated_at": "2026-07-08 09:31:00",
                "risk_level": "medium",
                "a_share_bias": "neutral",
                "negative_sectors": ["半导体", "机器人"],
            },
            {
                "trade_date": "2026-07-09",
                "generated_at": "2026-07-09 09:31:00",
                "risk_level": "high",
                "a_share_bias": "risk_off",
                "negative_sectors": ["创业板", "AI硬件"],
            },
        ]
        nav_rows = [
            {"date": "2026-07-07", "time": "15:00:00", "tag": "report", "total_assets": 1000000.0, "total_pos_value": 260000.0},
            {"date": "2026-07-08", "time": "15:00:00", "tag": "report", "total_assets": 996000.0, "total_pos_value": 120000.0},
            {"date": "2026-07-09", "time": "15:00:00", "tag": "report", "total_assets": 995500.0, "total_pos_value": 18000.0},
        ]

        with (
            patch.object(trader, "_read_jsonl", return_value=external_rows),
            patch.object(trader, "_read_csv_rows", return_value=nav_rows),
        ):
            review = trader._build_regime_execution_review(bundle=bundle, summary=summary)

        self.assertEqual(review["verdict"], "mixed")
        self.assertEqual(review["label"], "defensive-low-exposure-needs-calibration")
        self.assertFalse(review["positive_sample"])
        self.assertTrue(review["risk_on_release_missed"])
        self.assertEqual(review["today_opened_count"], 0)
        self.assertEqual(review["stocks_with_signal"], 48)

    def test_close_node_payload_includes_evolution_absorption_summary(self) -> None:
        bundle = {
            "source_stats": {
                "today_closed_count": 3,
            },
            "summary": {
                "learnable_sample_count": 1,
                "observe_only_count": 2,
                "execution_damaged_count": 1,
                "profit_truncation_count": 1,
                "alpha_loss_event_count": 1,
                "intraday_judgment_verdict": "validated",
                "intraday_judgment_score": 80,
            },
            "direct_learn_items": [{"code": "300602"}],
            "profit_truncation_items": [{"code": "002947"}],
            "capital_allocation_feedback": {
                "verdict": "positive",
                "today_biased_closed": {"count": 1},
                "historical_biased_closed": {"count": 2},
            },
            "intraday_judgment_review": {
                "available": True,
                "trade_date": "2026-06-25",
                "verdict": "validated",
                "score": 80,
                "risk_bias": "defensive",
                "rebound_bias": "avoid_broad_rebound",
            },
        }
        payload = trader._build_close_node_payload(
            summary={
                "performance": {
                    "closed_count": 3,
                    "holding_count": 2,
                    "win_rate_pct": 50.0,
                    "avg_return_pct": 3.2,
                    "realized_pnl": 1000.0,
                },
                "account_status": {"live": True},
                "learning_notes": ["unit"],
            },
            reconcile_summary={"pending": {"counts": {"stale": 0}}},
            daily_evolution_bundle=bundle,
        )

        self.assertEqual(payload["learning_gate_status"], "allow")
        self.assertEqual(payload["review_status"], "PASS")
        self.assertEqual(payload["evolution_absorption"]["learnable_sample_count"], 1)
        self.assertEqual(payload["evolution_absorption"]["profit_expansion_codes"], ["002947"])
        self.assertEqual(payload["evolution_absorption"]["learnable_codes"], ["300602"])
        self.assertEqual(payload["learning_gate_basis"]["blocked_reason_counts"], {})
        self.assertEqual(payload["learning_gate_basis"]["capital_allocation_verdict"], "positive")
        self.assertEqual(payload["learning_gate_basis"]["intraday_judgment_verdict"], "validated")
        self.assertEqual(payload["learning_gate_basis"]["intraday_judgment_score"], 80)
        self.assertEqual(payload["capital_allocation_feedback"]["verdict"], "positive")
        self.assertEqual(payload["intraday_judgment_review"]["verdict"], "validated")

    def test_close_node_payload_holds_when_absorption_has_no_learnable_samples(self) -> None:
        bundle = {
            "source_stats": {
                "today_closed_count": 2,
            },
            "summary": {
                "learnable_sample_count": 0,
                "observe_only_count": 2,
                "execution_damaged_count": 1,
                "profit_truncation_count": 1,
                "alpha_loss_event_count": 1,
            },
            "observe_only_items": [
                {"code": "688107", "blocked_reasons": ["execution_damaged"]},
                {"code": "002947", "blocked_reasons": ["profit_truncation"]},
            ],
            "execution_damaged_items": [{"code": "688107"}],
            "profit_truncation_items": [{"code": "002947"}],
        }
        with patch.object(
            trader,
            "_build_engineering_review",
            return_value={
                "available": True,
                "trade_date": "2026-06-25",
                "verdict": "clean",
                "incident_count": 0,
                "recurring_incident_count": 0,
                "category_counts": {},
                "high_severity_count": 0,
                "incident_codes": [],
                "hardening_actions": [],
                "incidents": [],
                "summary": "clean",
            },
        ):
            payload = trader._build_close_node_payload(
                summary={
                    "performance": {
                        "closed_count": 2,
                        "holding_count": 0,
                        "win_rate_pct": 0.0,
                        "avg_return_pct": -1.0,
                        "realized_pnl": -200.0,
                    },
                    "account_status": {"live": True},
                },
                reconcile_summary={"pending": {"counts": {"stale": 0}}},
                daily_evolution_bundle=bundle,
            )
        gate_payload = trader._build_learning_gate_payload(payload)

        self.assertEqual(payload["learning_gate_status"], "hold")
        self.assertEqual(payload["review_status"], "WARN")
        self.assertIn("no_learnable_samples_after_absorption", [item["code"] for item in payload["issues"]])
        self.assertEqual(payload["learning_gate_basis"]["blocked_reason_counts"]["execution_damaged"], 1)
        self.assertEqual(payload["learning_gate_basis"]["blocked_reason_counts"]["profit_truncation"], 1)
        self.assertEqual([item["code"] for item in payload["evolution_followups"]], ["execution_damaged_samples_present", "profit_expansion_followup_required"])
        self.assertEqual(gate_payload["learning_gate_status"], "hold")
        self.assertEqual(gate_payload["followup_codes"], ["execution_damaged_samples_present", "profit_expansion_followup_required"])
        self.assertEqual(gate_payload["evolution_absorption"]["execution_damaged_codes"], ["688107"])

    def test_close_node_payload_uses_today_closed_count_instead_of_cumulative_closed_count(self) -> None:
        bundle = {
            "source_stats": {
                "today_closed_count": 0,
            },
            "summary": {
                "learnable_sample_count": 0,
                "observe_only_count": 0,
                "execution_damaged_count": 0,
                "profit_truncation_count": 0,
                "alpha_loss_event_count": 0,
            },
            "direct_learn_items": [],
            "observe_only_items": [],
        }
        with patch.object(
            trader,
            "_build_engineering_review",
            return_value={
                "available": True,
                "trade_date": "2026-06-25",
                "verdict": "clean",
                "incident_count": 0,
                "recurring_incident_count": 0,
                "category_counts": {},
                "high_severity_count": 0,
                "incident_codes": [],
                "hardening_actions": [],
                "incidents": [],
                "summary": "clean",
            },
        ):
            payload = trader._build_close_node_payload(
                summary={
                    "performance": {
                        "closed_count": 55,
                        "holding_count": 0,
                        "win_rate_pct": 29.0,
                        "avg_return_pct": -2.4,
                        "realized_pnl": -1000.0,
                    },
                    "account_status": {"live": True},
                },
                reconcile_summary={"pending": {"counts": {"stale": 0}}},
                daily_evolution_bundle=bundle,
            )

        issue_codes = [item["code"] for item in payload["issues"]]
        self.assertIn("no_closed_samples_today", issue_codes)
        self.assertNotIn("no_learnable_samples_after_absorption", issue_codes)
        self.assertEqual(payload["learning_gate_basis"]["closed_count"], 55)
        self.assertEqual(payload["learning_gate_basis"]["today_closed_count"], 0)

    def test_close_node_payload_includes_engineering_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            phase_history_file = Path(tmpdir) / "phase_history_detailed.csv"
            phase_history_file.write_text(
                "\n".join(
                    [
                        "generated_at,run_id,task_name,trigger_slot,phase,step,attempt,status,exit_code,started_at,finished_at,duration_seconds,detail,command,learning_action,learning_note,decision_buffer_seconds,root,data_dir",
                        "2026-06-24 13:00:31,r1,TLFZ-WorkBuddy-MiddayGate,13:00,midday-gate,v10_moni_trader.py,1,failed,11,2026-06-24 13:00:00,2026-06-24 13:00:31,31,step failed: v10_moni_trader.py,C:\\Python314\\python.exe v10_moni_trader.py --midday-gate,,,,C:\\skills,C:\\data",
                        "2026-06-25 13:00:32,r2,TLFZ-WorkBuddy-MiddayGate,13:00,midday-gate,v10_moni_trader.py,1,failed,11,2026-06-25 13:00:01,2026-06-25 13:00:32,31,step failed: v10_moni_trader.py,C:\\Python314\\python.exe v10_moni_trader.py --midday-gate,,,,C:\\skills,C:\\data",
                    ]
                ),
                encoding="utf-8",
            )
            bundle = {
                "trade_date": "2026-06-25",
            "source_stats": {
                "today_closed_count": 3,
            },
                "summary": {
                    "learnable_sample_count": 1,
                    "observe_only_count": 0,
                    "execution_damaged_count": 0,
                    "profit_truncation_count": 0,
                    "alpha_loss_event_count": 0,
                },
                "direct_learn_items": [{"code": "301626"}],
            }
            with patch.object(trader, "PHASE_HISTORY_DETAILED_FILE", str(phase_history_file)):
                payload = trader._build_close_node_payload(
                    summary={
                        "performance": {
                            "closed_count": 1,
                            "holding_count": 0,
                            "win_rate_pct": 100.0,
                            "avg_return_pct": 8.15,
                            "realized_pnl": 5289.6,
                        },
                        "account_status": {"live": True},
                    },
                    reconcile_summary={"pending": {"counts": {"stale": 0}}},
                    daily_evolution_bundle=bundle,
                )

        engineering_review = payload["engineering_review"]
        self.assertTrue(engineering_review["available"])
        self.assertEqual(engineering_review["trade_date"], "2026-06-25")
        self.assertEqual(engineering_review["incident_count"], 1)
        self.assertEqual(engineering_review["recurring_incident_count"], 1)
        self.assertEqual(engineering_review["verdict"], "needs_hardening")
        self.assertEqual(engineering_review["incidents"][0]["phase"], "midday-gate")
        self.assertEqual(engineering_review["incidents"][0]["recurrence_count"], 2)
        self.assertEqual(payload["learning_gate_basis"]["engineering_incident_count"], 1)
        self.assertEqual(payload["learning_gate_basis"]["engineering_verdict"], "needs_hardening")
        self.assertIn("engineering_incidents_need_guardrails", [item["code"] for item in payload["evolution_followups"]])

    def test_learning_preflight_uses_previous_close_for_intraday_trade_date(self) -> None:
        gate_payload = {
            "date": "2026-07-06",
            "learning_gate_status": "allow",
            "reason_codes": [],
            "learning_gate_basis": {},
        }
        with (
            patch.object(trader, "_load_learning_gate_payload", return_value=gate_payload),
            patch.object(trader, "_load_learning_actions", return_value={}),
        ):
            resolved = trader._resolve_learning_preflight_guard(trade_date="2026-07-07")

        self.assertTrue(resolved["available"])
        self.assertEqual(resolved["status"], "allow")
        self.assertTrue(resolved["allow_buy"])
        self.assertTrue(resolved["allow_add_position"])

    def test_learning_preflight_rejects_same_day_gate_for_intraday_trade_date(self) -> None:
        gate_payload = {
            "date": "2026-07-07",
            "learning_gate_status": "allow",
            "reason_codes": [],
            "learning_gate_basis": {},
        }
        with (
            patch.object(trader, "_load_learning_gate_payload", return_value=gate_payload),
            patch.object(trader, "_load_learning_actions", return_value={}),
        ):
            resolved = trader._resolve_learning_preflight_guard(trade_date="2026-07-07")

        self.assertFalse(resolved["available"])
        self.assertEqual(resolved["status"], "stale")
        self.assertFalse(resolved["allow_buy"])
        self.assertIn("预期最近有效收盘=2026-07-06", " ".join(resolved["notes"]))

    def test_learning_preflight_treats_sample_only_hold_as_benign_even_with_engineering_incidents(self) -> None:
        gate_payload = {
            "date": "2026-07-06",
            "learning_gate_status": "hold",
            "reason_codes": ["no_learnable_samples_after_absorption"],
            "learning_gate_basis": {
                "closed_count": 55,
                "today_closed_count": 0,
                "learnable_sample_count": 0,
                "engineering_incident_count": 6,
                "engineering_verdict": "priority_hardening",
            },
        }
        with (
            patch.object(trader, "_load_learning_gate_payload", return_value=gate_payload),
            patch.object(trader, "_load_learning_actions", return_value={}),
        ):
            resolved = trader._resolve_learning_preflight_guard(trade_date="2026-07-07")

        self.assertTrue(resolved["available"])
        self.assertEqual(resolved["reason"], "learning_gate_hold_benign")
        self.assertTrue(resolved["allow_buy"])
        self.assertTrue(resolved["allow_add_position"])
        self.assertFalse(resolved["allow_aggressive_add"])

    def test_evolving_model_reads_intraday_judgment_calibration_from_close_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            close_node_file = tmpdir_path / "close_node.json"
            state_file = tmpdir_path / "state.json"
            baseline_file = tmpdir_path / "baseline.json"
            changelog_file = tmpdir_path / "changelog.jsonl"
            decisions_file = tmpdir_path / "decisions.jsonl"
            trade_log_file = tmpdir_path / "trade_log.jsonl"
            close_node_file.write_text(
                json.dumps(
                    {
                        "intraday_judgment_review": {
                            "available": True,
                            "trade_date": "2026-06-25",
                            "verdict": "validated",
                            "score": 88,
                            "risk_bias": "defensive",
                            "rebound_bias": "avoid_broad_rebound",
                            "confidence": 0.76,
                        "opening_liquidity": {
                            "verdict": "fragile",
                            "in_0931_window": True,
                        },
                        "external_market": {
                            "risk_level": "high",
                            "window_tag": "opening_0931",
                            "negative_sectors": ["AI硬件", "算力"],
                            "opening_anchor_break_monitor": {
                                "pressure_level": "high",
                                "broken_anchor_names": ["佳创视讯", "正帆科技"],
                            },
                            "weekend_digest_monitor": {
                                "active": True,
                                "bias": "negative",
                            },
                        },
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(evolving_model, "CLOSE_NODE_FILE", close_node_file),
                patch.object(evolving_model, "MODEL_STATE_FILE", state_file),
                patch.object(evolving_model, "MODEL_BASELINE_FILE", baseline_file),
                patch.object(evolving_model, "MODEL_CHANGELOG_FILE", changelog_file),
                patch.object(evolving_model, "MODEL_DECISIONS_FILE", decisions_file),
                patch.object(evolving_model, "TRADE_API_LOG_FILE", trade_log_file),
            ):
                payload = evolving_model.refresh_model_state([])

        calibration = payload["learning"]["judgment_calibration"]
        self.assertTrue(calibration["available"])
        self.assertEqual(calibration["verdict"], "validated")
        self.assertEqual(calibration["score"], 88)
        self.assertEqual(calibration["risk_bias"], "defensive")
        self.assertEqual(calibration["opening_liquidity_verdict"], "fragile")
        self.assertTrue(calibration["opening_window_confirmed"])
        self.assertEqual(calibration["external_risk_level"], "high")
        self.assertEqual(calibration["external_window_tag"], "opening_0931")
        self.assertEqual(calibration["external_negative_sectors"], ["AI硬件", "算力"])
        self.assertEqual(calibration["opening_anchor_pressure_level"], "high")
        self.assertEqual(calibration["broken_anchor_names"], ["佳创视讯", "正帆科技"])
        self.assertTrue(calibration["weekend_digest_active"])
        self.assertEqual(calibration["weekend_digest_bias"], "negative")

    def test_evolving_model_reads_engineering_evolution_from_review_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            close_node_file = tmpdir_path / "close_node.json"
            engineering_review_file = tmpdir_path / "engineering_review.json"
            state_file = tmpdir_path / "state.json"
            baseline_file = tmpdir_path / "baseline.json"
            changelog_file = tmpdir_path / "changelog.jsonl"
            decisions_file = tmpdir_path / "decisions.jsonl"
            trade_log_file = tmpdir_path / "trade_log.jsonl"
            close_node_file.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
            engineering_review_file.write_text(
                json.dumps(
                    {
                        "available": True,
                        "trade_date": "2026-06-26",
                        "verdict": "needs_hardening",
                        "incident_count": 2,
                        "recurring_incident_count": 1,
                        "high_severity_count": 1,
                        "category_counts": {"runtime_failure": 2},
                        "incident_codes": ["midday_gate_v10_moni_trader_runtime_failure"],
                        "hardening_actions": ["midday-gate 改动后必须先跑同阶段定向回归，严禁未定义 helper/常量直接上线。"],
                        "summary": "当日识别到 2 个代码能力事件。",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(evolving_model, "CLOSE_NODE_FILE", close_node_file),
                patch.object(evolving_model, "ENGINEERING_REVIEW_FILE", engineering_review_file),
                patch.object(evolving_model, "MODEL_STATE_FILE", state_file),
                patch.object(evolving_model, "MODEL_BASELINE_FILE", baseline_file),
                patch.object(evolving_model, "MODEL_CHANGELOG_FILE", changelog_file),
                patch.object(evolving_model, "MODEL_DECISIONS_FILE", decisions_file),
                patch.object(evolving_model, "TRADE_API_LOG_FILE", trade_log_file),
            ):
                payload = evolving_model.refresh_model_state([])

        engineering = payload["learning"]["engineering_evolution"]
        self.assertTrue(engineering["available"])
        self.assertEqual(engineering["trade_date"], "2026-06-26")
        self.assertEqual(engineering["verdict"], "needs_hardening")
        self.assertEqual(engineering["incident_count"], 2)
        self.assertEqual(engineering["recurring_incident_count"], 1)
        self.assertEqual(engineering["high_severity_count"], 1)
        self.assertEqual(engineering["category_counts"], {"runtime_failure": 2})
        self.assertEqual(engineering["incident_codes"], ["midday_gate_v10_moni_trader_runtime_failure"])
        self.assertEqual(
            engineering["hardening_actions"],
            ["midday-gate 改动后必须先跑同阶段定向回归，严禁未定义 helper/常量直接上线。"],
        )


class MainStrategyFillFallbackTests(unittest.TestCase):
    def test_capture_trade_fill_does_not_use_order_price_as_trade_price(self) -> None:
        with patch.object(
            trader,
            "find_recent_filled_order",
            return_value={
                "id": "ORDER-1",
                "datetime": datetime(2026, 7, 6, 10, 0, 0),
                "actual_trade_price": 0.0,
                "trade_price": 12.34,
                "price": 12.34,
                "trade_count": 100,
                "count": 100,
            },
        ):
            fill = trader.capture_trade_fill("300001", "sell", 100)

        self.assertIsNotNone(fill)
        self.assertEqual(fill["trade_price"], 0.0)

    def test_sync_track_record_pauses_pending_sell_without_trade_price(self) -> None:
        records = [
            {
                "code": "300001",
                "name": "Alpha",
                "status": "holding",
                "quantity": "1000",
                "entry_price": "10.0",
                "build_note": "native",
            }
        ]
        pending_items = [
            {
                "action": "sell",
                "status": "filled",
                "code": "300001",
                "order_id": "SELL-1",
                "recorded_at": "2026-07-06 10:00:00",
                "ref_price": 11.2,
            }
        ]

        updated, changed = trader.sync_track_record(records, positions=[], orders=[], pending_items=pending_items)

        self.assertTrue(changed)
        self.assertEqual(updated[0]["status"], "paused")
        self.assertEqual(updated[0]["sell_price"], "")
        self.assertEqual(updated[0]["close_reason"], "pending_filled_sell_missing_trade_price")

    def test_sync_track_record_can_close_paused_record_when_trade_price_arrives(self) -> None:
        records = [
            {
                "code": "300001",
                "name": "Alpha",
                "status": "paused",
                "quantity": "1000",
                "entry_price": "10.0",
                "build_note": "native",
            }
        ]
        orders = [
            {
                "id": "SELL-1",
                "code": "300001",
                "direction": 2,
                "status": 4,
                "trade_count": 1000,
                "count": 1000,
                "price": 11.2,
                "actual_trade_price": 11.05,
                "trade_price": 11.05,
                "time": 1,
                "datetime": datetime(2026, 7, 6, 10, 1, 0),
            }
        ]

        updated, changed = trader.sync_track_record(records, positions=[], orders=orders, pending_items=[])

        self.assertTrue(changed)
        self.assertEqual(updated[0]["status"], "closed")
        self.assertEqual(updated[0]["sell_price"], "11.05")
        self.assertEqual(updated[0]["sell_order_id"], "SELL-1")

    def test_reconcile_after_trade_pauses_sell_without_trade_price(self) -> None:
        records = [
            {
                "code": "300001",
                "name": "Alpha",
                "status": "holding",
                "quantity": "1000",
                "entry_price": "10.0",
                "build_note": "native",
            }
        ]
        with (
            patch.object(trader, "get_balance", return_value={}),
            patch.object(trader, "get_positions", return_value=[]),
            patch.object(trader, "get_orders", return_value=[]),
            patch.object(trader, "refresh_pending_orders", return_value=[]),
            patch.object(trader, "sync_track_record", return_value=(records, False)),
            patch.object(
                trader,
                "capture_trade_fill",
                return_value={
                    "order_id": "SELL-1",
                    "trade_time": "10:05:00",
                    "trade_date": "2026-07-06",
                    "trade_price": 0.0,
                    "trade_count": 1000,
                },
            ),
        ):
            updated, _balance, _positions, changed = trader.reconcile_after_trade(
                ["300001"],
                records=records,
                trade_contexts={"300001": {"direction": "sell", "expected_quantity": 1000}},
            )

        self.assertTrue(changed)
        self.assertEqual(updated[0]["status"], "paused")
        self.assertEqual(updated[0]["close_reason"], "post_trade_reconcile_sell_missing_trade_price")


class ChallengerExecutionTests(unittest.TestCase):
    def test_ensure_fresh_source_payload_refreshes_previous_trade_date(self) -> None:
        stale_payload = {"status": "ok", "trade_date": "2026-06-23"}
        fresh_payload = {"status": "ok", "trade_date": "2026-06-24"}
        with (
            patch.object(challenger, "_expected_source_trade_date", return_value="2026-06-24"),
            patch.object(challenger, "_load_source_payload", return_value=stale_payload),
            patch.object(challenger, "_refresh_source_payload", return_value=(fresh_payload, "ok")) as refresh_mock,
        ):
            payload = challenger._ensure_fresh_source_payload()

        refresh_mock.assert_called_once_with("2026-06-24")
        self.assertEqual(payload["trade_date"], "2026-06-24")

    def test_ensure_fresh_source_payload_accepts_newer_same_day_source(self) -> None:
        fresh_payload = {"status": "ok", "trade_date": "2026-06-26"}
        with (
            patch.object(challenger, "_expected_source_trade_date", return_value="2026-06-25"),
            patch.object(challenger, "_load_source_payload", return_value=fresh_payload),
            patch.object(challenger, "_refresh_source_payload") as refresh_mock,
        ):
            payload = challenger._ensure_fresh_source_payload()

        refresh_mock.assert_not_called()
        self.assertEqual(payload["trade_date"], "2026-06-26")

    def test_challenger_do_buy_writes_local_fill(self) -> None:
        buy_list = [
            {
                "code": "300001",
                "name": "Alpha",
                "tier": 1,
                "mode": "workbuddy_local_challenger",
                "build_note": "note",
                "target_amount": 20000,
                "target_weight_pct": 2.0,
                "entry_price": 10.0,
                "quantity": 1000,
                "cost": 10000.0,
                "window_key": "10:00",
                "buy_action": "core_buy",
                "intent": "fast_realize",
                "target_build_ratio": 1.0,
                "readiness_score": 88.0,
                "readiness_components": {},
                "exit_plan": {"intent": "fast_realize"},
            }
        ]
        with (
            patch.object(challenger, "_ensure_trade_window", return_value=True),
            patch.object(challenger, "_resolve_buy_window", return_value={"key": "10:00", "label": "opening_probe"}),
            patch.object(challenger, "build_buy_plan", return_value=({"source_file": "pool.json"}, buy_list, [], {"positions": {}, "history": []})),
            patch.object(challenger, "load_track_record", return_value=[]),
            patch.object(challenger, "_compute_cash_balance", return_value=100000.0),
            patch.object(challenger, "_write_order_log", return_value="WB-1"),
            patch.object(challenger.base, "apply_buy_fill", side_effect=lambda record, *_args, **_kwargs: {**record, "status": "holding", "quantity": "1000"}),
            patch.object(challenger, "_persist_local_state", return_value={"ok": True}) as persist_mock,
            patch.object(challenger, "write_account_summary") as summary_mock,
        ):
            code = challenger.do_buy(dry_run=False, force=False)

        self.assertEqual(code, challenger.base.EXIT_OK)
        persist_mock.assert_called_once()
        summary_mock.assert_called_once()

    def test_challenger_trade_window_accepts_buy_trigger_slot_even_if_current_time_outside_base_window(self) -> None:
        with patch.object(challenger.base, "ensure_trade_window", return_value=False):
            allowed = challenger._ensure_trade_window("buy", dry_run=False, force=False, trigger_slot="10:30")

        self.assertTrue(allowed)

    def test_challenger_trade_window_accepts_sell_trigger_slot_even_if_current_time_outside_base_window(self) -> None:
        with patch.object(challenger.base, "ensure_trade_window", return_value=False):
            allowed = challenger._ensure_trade_window("smart_sell", dry_run=False, force=False, trigger_slot="D1_0945")

        self.assertTrue(allowed)

    def test_challenger_build_buy_plan_skips_when_only_last_close_is_available(self) -> None:
        payload = {
            "status": "ok",
            "selected_records": [
                {
                    "code": "300001",
                    "name": "Alpha",
                    "selection_rank": 1,
                    "selection_score": 98.0,
                    "target_weight_pct": 2.0,
                }
            ],
        }
        with (
            patch.object(challenger, "validate_opening_tradability_artifact"),
            patch.object(challenger, "_ensure_fresh_source_payload", return_value=payload),
            patch.object(challenger, "_resolve_buy_window", return_value={"key": "10:00", "label": "opening_probe"}),
            patch.object(challenger, "load_track_record", return_value=[]),
            patch.object(challenger, "_prune_execution_state", return_value={"positions": {}, "history": []}),
            patch.object(challenger, "_build_account_snapshot", return_value=({"cash_balance": 100000.0, "total_assets": 100000.0}, [], {})),
            patch.object(challenger, "_load_today_tradability_exclusions", return_value={}),
            patch.object(challenger, "load_quote_map", return_value={"300001": {"last_close": 10.5}}),
            patch.object(challenger, "_is_risk_warning_candidate", return_value=False),
            patch.object(challenger, "resolve_market_info", return_value={"tradable_by_current_executor": True, "exchange": "SZ", "market_char": "A", "resolver_source": "mock"}),
            patch.object(challenger, "_recent_closed_same_code", return_value=[]),
            patch.object(challenger, "_build_execution_readiness", return_value={"score": 88.0, "components": {}}),
            patch.object(challenger, "_resolve_entry_action", return_value={"action": "core_buy", "buy_ratio": 1.0, "target_build_ratio": 1.0}),
            patch.object(challenger, "_classify_exit_intent", return_value={"intent": "fast_realize"}),
        ):
            _plan_payload, buy_list, skipped, _execution_state = challenger.build_buy_plan(trigger_slot="10:00", persist_plan=False)

        self.assertEqual(buy_list, [])
        self.assertEqual(skipped, [{"code": "300001", "name": "Alpha", "reason": "quote_unavailable"}])

    def test_challenger_build_buy_plan_can_skip_plan_persist_for_tests(self) -> None:
        payload = {
            "status": "ok",
            "selected_records": [
                {
                    "code": "300001",
                    "name": "Alpha",
                    "selection_rank": 1,
                    "selection_score": 98.0,
                    "target_weight_pct": 2.0,
                }
            ],
        }
        with (
            patch.object(challenger, "validate_opening_tradability_artifact"),
            patch.object(challenger, "_ensure_fresh_source_payload", return_value=payload),
            patch.object(challenger, "_resolve_buy_window", return_value={"key": "10:00", "label": "opening_probe"}),
            patch.object(challenger, "load_track_record", return_value=[]),
            patch.object(challenger, "_prune_execution_state", return_value={"positions": {}, "history": []}),
            patch.object(challenger, "_build_account_snapshot", return_value=({"cash_balance": 100000.0, "total_assets": 100000.0}, [], {})),
            patch.object(challenger, "_load_today_tradability_exclusions", return_value={}),
            patch.object(challenger, "load_quote_map", return_value={"300001": {"last_close": 10.5}}),
            patch.object(challenger, "_is_risk_warning_candidate", return_value=False),
            patch.object(challenger, "resolve_market_info", return_value={"tradable_by_current_executor": True, "exchange": "SZ", "market_char": "A", "resolver_source": "mock"}),
            patch.object(challenger, "_write_json_atomic") as write_mock,
        ):
            challenger.build_buy_plan(trigger_slot="10:00", persist_plan=False)

        write_mock.assert_not_called()

    def test_challenger_readiness_keeps_zero_intraday_change_without_falling_back(self) -> None:
        readiness = challenger._build_execution_readiness(
            {
                "selection_rank": 1,
                "heat_level": "normal",
                "guardrail_status": "normal",
                "latest_chg_pct": 20.0,
            },
            {
                "price": 10.0,
                "last_close": 10.0,
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
            },
            window_key="14:00",
        )

        self.assertEqual(readiness["current_chg_pct"], 0.0)
        self.assertEqual(readiness["components"]["continuity"], 26.0)

    def test_challenger_readiness_prioritizes_profitability_signal_over_rank_proxy(self) -> None:
        quote = {
            "price": 10.3,
            "last_close": 10.0,
            "open": 10.0,
            "high": 10.6,
            "low": 9.9,
        }
        high_profit = challenger._build_execution_readiness(
            {
                "selection_rank": 4,
                "selection_score": 90.0,
                "avg_profitability_priority": 108.0,
                "avg_candidate_win_rate": 0.59,
                "avg_candidate_avg_return": 2.4,
                "heat_level": "normal",
                "guardrail_status": "normal",
                "latest_chg_pct": 6.0,
            },
            quote,
            window_key="10:30",
        )
        proxy_high = challenger._build_execution_readiness(
            {
                "selection_rank": 1,
                "selection_score": 98.0,
                "avg_profitability_priority": 78.0,
                "avg_candidate_win_rate": 0.48,
                "avg_candidate_avg_return": 1.0,
                "heat_level": "normal",
                "guardrail_status": "normal",
                "latest_chg_pct": 6.0,
            },
            quote,
            window_key="10:30",
        )

        self.assertGreater(high_profit["score"], proxy_high["score"])
        self.assertGreater(high_profit["components"]["profit_conviction"], proxy_high["components"]["profit_conviction"])

    def test_challenger_exit_intent_uses_profitability_for_runner(self) -> None:
        intent = challenger._classify_exit_intent(
            {
                "selection_rank": 1,
                "avg_profitability_priority": 106.0,
                "avg_candidate_win_rate": 0.6,
                "avg_candidate_avg_return": 2.2,
            },
            {
                "score": 80.0,
                "heat_level": "normal",
                "profitability_priority": 106.0,
                "avg_candidate_win_rate": 0.6,
                "avg_candidate_avg_return": 2.2,
            },
            window_key="10:30",
        )

        self.assertEqual(intent["intent"], challenger.EXIT_INTENT_RUNNER)
        self.assertEqual(intent["final_exit_window"], "D1_1450")

    def test_challenger_build_buy_plan_sorts_by_profitability_priority(self) -> None:
        payload = {
            "status": "ok",
            "selected_records": [
                {
                    "code": "300001",
                    "name": "LowAlpha",
                    "selection_rank": 1,
                    "selection_score": 98.0,
                    "avg_profitability_priority": 82.0,
                    "avg_candidate_win_rate": 0.49,
                    "avg_candidate_avg_return": 1.1,
                    "target_weight_pct": 2.0,
                },
                {
                    "code": "300002",
                    "name": "HighAlpha",
                    "selection_rank": 4,
                    "selection_score": 88.0,
                    "avg_profitability_priority": 108.0,
                    "avg_candidate_win_rate": 0.58,
                    "avg_candidate_avg_return": 2.4,
                    "target_weight_pct": 2.0,
                },
            ],
        }
        quote_map = {
            "300001": {"price": 10.0},
            "300002": {"price": 10.0},
        }

        def fake_readiness(row, _quote, *, window_key, recent_closed=None):
            return {
                "score": 82.0,
                "components": {},
                "profitability_priority": row.get("avg_profitability_priority", 0.0),
                "avg_candidate_win_rate": row.get("avg_candidate_win_rate", 0.0),
                "avg_candidate_avg_return": row.get("avg_candidate_avg_return", 0.0),
                "heat_level": "normal",
                "guardrail_status": "normal",
            }

        with (
            patch.object(challenger, "validate_opening_tradability_artifact"),
            patch.object(challenger, "_ensure_fresh_source_payload", return_value=payload),
            patch.object(challenger, "_resolve_buy_window", return_value={"key": "10:30", "label": "continuity_confirm"}),
            patch.object(challenger, "load_track_record", return_value=[]),
            patch.object(challenger, "_prune_execution_state", return_value={"positions": {}, "history": []}),
            patch.object(challenger, "_build_account_snapshot", return_value=({"cash_balance": 100000.0, "total_assets": 100000.0}, [], {})),
            patch.object(challenger, "_load_today_tradability_exclusions", return_value={}),
            patch.object(challenger, "load_quote_map", return_value=quote_map),
            patch.object(challenger, "_is_risk_warning_candidate", return_value=False),
            patch.object(challenger, "resolve_market_info", return_value={"tradable_by_current_executor": True, "exchange": "SZ", "market_char": "A", "resolver_source": "mock"}),
            patch.object(challenger, "_recent_closed_same_code", return_value=[]),
            patch.object(challenger, "_build_execution_readiness", side_effect=fake_readiness),
            patch.object(challenger, "_resolve_entry_action", return_value={"action": "core_buy", "buy_ratio": 1.0, "target_build_ratio": 1.0}),
            patch.object(challenger, "_classify_exit_intent", return_value={"intent": "fast_realize"}),
        ):
            _plan_payload, buy_list, _skipped, _execution_state = challenger.build_buy_plan(trigger_slot="10:30", persist_plan=False)

        self.assertEqual([item["code"] for item in buy_list], ["300002", "300001"])
        self.assertGreater(buy_list[0]["avg_profitability_priority"], buy_list[1]["avg_profitability_priority"])

    def test_challenger_holding_rows_marks_fallback_price_source(self) -> None:
        rows = challenger._holding_rows(
            [
                {
                    "code": "300001",
                    "name": "Alpha",
                    "quantity": "1000",
                    "entry_price": "10.0",
                    "status": "holding",
                    "date": "2026-06-26",
                }
            ],
            {"300001": {"last_close": 10.5}},
        )

        self.assertEqual(rows[0]["current_price"], 10.5)
        self.assertEqual(rows[0]["current_price_source"], "last_close_fallback")
        self.assertTrue(rows[0]["price_is_estimated"])

    def test_challenger_review_accepts_previous_trade_date_as_fresh(self) -> None:
        summary = {
            "generated_at": "2026-06-25 15:05:00",
            "source_status": "ok",
            "source_trade_date": "2026-06-24",
            "portfolio_name": "Workbuddy",
            "portfolio_type": "local_challenger_paper_account",
            "account_snapshot": {"holding_count": 1},
            "performance": {},
            "holdings": [],
        }
        with (
            patch.object(challenger_review, "_read_json", return_value=summary),
            patch.object(challenger_review, "_load_track_record", return_value=[{"status": "closed"}]),
            patch.object(challenger_review, "_read_jsonl", return_value=[{"logged_at": "2026-06-25 14:54:00", "action": "buy"}]),
            patch.object(challenger_review, "_today_str", return_value="2026-06-25"),
            patch.object(challenger_review, "_opening_data_status", return_value={"status": "ok", "trade_date": "2026-06-25", "record_count": 1, "excluded_today_count": 0}),
            patch.object(
                challenger_review.base,
                "compute_track_stats",
                return_value={
                    "closed_count": 1,
                    "holding_count": 1,
                    "win_count": 1,
                    "win_rate_pct": 100.0,
                    "avg_return_pct": 5.0,
                    "realized_pnl": 1000.0,
                },
            ),
            patch.object(challenger_review, "_expected_source_trade_date", return_value="2026-06-24"),
        ):
            review = challenger_review.build_review()

        self.assertEqual(review["review_verdict"], "ok")
        self.assertNotIn("candidate_source_trade_date_stale", review["execution_health"]["blockers"])
        self.assertEqual(review["source_alignment"]["expected_source_trade_date"], "2026-06-24")

    def test_challenger_review_uses_summary_generated_at_for_source_cutoff(self) -> None:
        summary = {
            "generated_at": "2026-06-26 15:05:00",
            "source_status": "ok",
            "source_trade_date": "2026-06-25",
            "portfolio_name": "Workbuddy",
            "portfolio_type": "local_challenger_paper_account",
            "account_snapshot": {"holding_count": 1},
            "performance": {},
            "holdings": [],
        }

        def expected_trade_date(now_dt=None) -> str:
            if isinstance(now_dt, datetime) and now_dt.strftime("%Y-%m-%d %H:%M:%S") == "2026-06-26 15:05:00":
                return "2026-06-25"
            return "2099-01-01"

        with (
            patch.object(challenger_review, "_read_json", return_value=summary),
            patch.object(challenger_review, "_load_track_record", return_value=[{"status": "closed"}]),
            patch.object(challenger_review, "_read_jsonl", return_value=[{"logged_at": "2026-06-26 14:54:00", "action": "buy"}]),
            patch.object(challenger_review, "_today_str", return_value="2026-06-26"),
            patch.object(challenger_review, "_opening_data_status", return_value={"status": "ok", "trade_date": "2026-06-26", "record_count": 1, "excluded_today_count": 0}),
            patch.object(
                challenger_review.base,
                "compute_track_stats",
                return_value={
                    "closed_count": 1,
                    "holding_count": 1,
                    "win_count": 1,
                    "win_rate_pct": 100.0,
                    "avg_return_pct": 5.0,
                    "realized_pnl": 1000.0,
                },
            ),
            patch.object(challenger_review, "_expected_source_trade_date", side_effect=expected_trade_date),
        ):
            review = challenger_review.build_review()

        self.assertEqual(review["review_verdict"], "ok")
        self.assertNotIn("candidate_source_trade_date_stale", review["execution_health"]["blockers"])
        self.assertEqual(review["source_alignment"]["expected_source_trade_date"], "2026-06-25")

    def test_challenger_fast_summary_prefers_latest_source_payload(self) -> None:
        account_snapshot = {"holding_count": 0}
        stats = {"win_rate_pct": 0.0, "avg_return_pct": 0.0}
        previous_summary = {
            "source_trade_date": "2026-06-25",
            "source_status": "ok",
            "performance": {"champion_candidate_win_rate": 11.0},
        }
        source_payload = {
            "trade_date": "2026-06-26",
            "status": "ok",
            "champion_template": {"candidate_win_rate": 66.0},
        }
        with (
            patch.object(challenger, "_load_previous_positions_snapshot", return_value=[]),
            patch.object(challenger, "_build_account_snapshot", return_value=(account_snapshot, [], stats)),
            patch.object(challenger, "_read_json", return_value=previous_summary),
            patch.object(challenger, "_load_source_payload", return_value=source_payload),
            patch.object(challenger, "_write_json_atomic") as write_json_mock,
            patch.object(challenger, "_write_positions_snapshot") as write_positions_mock,
        ):
            summary = challenger.write_account_summary("status", [], fast=True)

        self.assertEqual(summary["source_trade_date"], "2026-06-26")
        self.assertEqual(summary["source_status"], "ok")
        self.assertEqual(summary["performance"]["champion_candidate_win_rate"], 66.0)
        write_json_mock.assert_called_once()
        write_positions_mock.assert_called_once()

    def test_challenger_do_smart_sell_executes_local_fill(self) -> None:
        old_date = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        records = [
            {
                "code": "300001",
                "name": "Alpha",
                "quantity": "1000",
                "entry_price": "10.0",
                "status": "holding",
                "date": old_date,
                "mode": "workbuddy_local_challenger",
            }
        ]
        with (
            patch.object(challenger, "_ensure_trade_window", return_value=True),
            patch.object(challenger, "validate_opening_tradability_artifact"),
            patch.object(challenger, "_resolve_sell_window", return_value={"key": "D1_0945", "minute": 585, "tolerance": 12}),
            patch.object(challenger, "load_track_record", return_value=records),
            patch.object(challenger, "_load_today_tradability_exclusions", return_value={}),
            patch.object(challenger, "load_quote_map", return_value={"300001": {"price": 11.0}}),
            patch.object(challenger.base, "connect_tdx", return_value=None),
            patch.object(challenger, "_emit_local_sell_fill", return_value=(True, "SELL-1", "full")),
            patch.object(challenger, "_persist_local_state", return_value={"ok": True}) as persist_mock,
            patch.object(challenger, "write_account_summary") as summary_mock,
        ):
            code = challenger.do_smart_sell(dry_run=False, force=False)

        self.assertEqual(code, challenger.base.EXIT_OK)
        persist_mock.assert_called_once()
        summary_mock.assert_called_once_with("smart_sell", records)

    def test_challenger_do_smart_sell_skips_when_only_last_close_is_available(self) -> None:
        old_date = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        records = [
            {
                "code": "300001",
                "name": "Alpha",
                "quantity": "1000",
                "entry_price": "10.0",
                "status": "holding",
                "date": old_date,
                "mode": "workbuddy_local_challenger",
            }
        ]
        with (
            patch.object(challenger, "_ensure_trade_window", return_value=True),
            patch.object(challenger, "validate_opening_tradability_artifact"),
            patch.object(challenger, "_resolve_sell_window", return_value={"key": "D1_0945", "minute": 585, "tolerance": 12}),
            patch.object(challenger, "load_track_record", return_value=records),
            patch.object(challenger, "_load_today_tradability_exclusions", return_value={}),
            patch.object(challenger, "load_quote_map", return_value={"300001": {"last_close": 10.5}}),
            patch.object(challenger.base, "connect_tdx", return_value=None),
            patch.object(challenger, "_emit_local_sell_fill") as emit_mock,
            patch.object(challenger, "_persist_local_state") as persist_mock,
            patch.object(challenger, "write_account_summary") as summary_mock,
        ):
            code = challenger.do_smart_sell(dry_run=False, force=False)

        self.assertEqual(code, challenger.base.EXIT_NO_ACTION)
        emit_mock.assert_not_called()
        persist_mock.assert_not_called()
        summary_mock.assert_not_called()

    def test_challenger_status_uses_fast_summary_mode(self) -> None:
        with (
            patch.object(challenger, "validate_opening_tradability_artifact"),
            patch.object(challenger, "load_track_record", return_value=[]),
            patch.object(challenger, "write_account_summary", return_value={"ok": True}) as summary_mock,
        ):
            code = challenger.do_status()

        self.assertEqual(code, challenger.base.EXIT_OK)
        summary_mock.assert_called_once_with("status", [], fast=True)

    def test_challenger_status_tolerates_stale_opening_tradability(self) -> None:
        with (
            patch.object(
                challenger,
                "validate_opening_tradability_artifact",
                side_effect=challenger.RuntimeValidationError("opening_tradability 交易日不匹配"),
            ),
            patch.object(challenger, "load_track_record", return_value=[]),
            patch.object(challenger, "write_account_summary", return_value={"ok": True}) as summary_mock,
        ):
            code = challenger.do_status()

        self.assertEqual(code, challenger.base.EXIT_OK)
        summary_mock.assert_called_once_with("status", [], fast=True)


if __name__ == "__main__":
    unittest.main()
