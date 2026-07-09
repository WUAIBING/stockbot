#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workbuddy_runtime as runtime


class WorkbuddyRuntimeValidationTests(unittest.TestCase):
    def test_validate_candidate_pool_artifact_accepts_valid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "workbuddy_candidate_pool_latest.json"
            payload = {
                "generated_at": "2026-07-06 09:40:00",
                "trade_date": "2026-07-06",
                "status": "ok",
                "selected_count": 1,
                "candidate_count": 3,
                "selected_records": [{"code": "300001", "name": "Alpha"}],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            report = runtime.validate_candidate_pool_artifact(path=path, expected_trade_date="2026-07-06")

        self.assertEqual(report.details["selected_count"], 1)
        self.assertEqual(report.details["trade_date"], "2026-07-06")

    def test_validate_opening_tradability_artifact_rejects_trade_date_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "opening_tradability_latest.json"
            payload = {
                "generated_at": "2026-07-05 09:31:00",
                "trade_date": "2026-07-05",
                "status": "ok",
                "record_count": 1,
                "records": [{"code": "300001"}],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(runtime.RuntimeValidationError, "交易日不匹配"):
                runtime.validate_opening_tradability_artifact(path=path, expected_trade_date="2026-07-06")

    def test_validate_challenger_execution_consistency_detects_missing_state(self) -> None:
        records = [
            {
                "code": "300001",
                "status": "holding",
                "quantity": "1000",
                "target_amount": "20000",
                "date": "2026-07-06",
            }
        ]
        state = {"positions": {}, "history": []}

        report = runtime.validate_challenger_execution_consistency(records, state)

        self.assertFalse(report["ok"])
        self.assertEqual(report["missing_in_state"], ["300001"])

    def test_preflight_phase_skips_opening_tradability_for_workbuddy_status(self) -> None:
        fake_report = runtime.ValidationReport(
            name="opening_tradability_latest.json",
            path=Path("demo.json"),
            details={"trade_date": "2026-07-06", "record_count": 10},
        )
        with patch.object(runtime, "validate_opening_tradability_artifact", return_value=fake_report) as validate_mock:
            reports = runtime.preflight_phase("workbuddy-status", expected_trade_date="2026-07-06")

        validate_mock.assert_not_called()
        self.assertEqual(reports, [])

    def test_preflight_phase_still_checks_opening_tradability_for_workbuddy_buy(self) -> None:
        fake_report = runtime.ValidationReport(
            name="opening_tradability_latest.json",
            path=Path("demo.json"),
            details={"trade_date": "2026-07-06", "record_count": 10},
        )
        with patch.object(runtime, "validate_opening_tradability_artifact", return_value=fake_report) as validate_mock:
            reports = runtime.preflight_phase("workbuddy-buy", expected_trade_date="2026-07-06")

        validate_mock.assert_called_once()
        self.assertEqual(reports[0].name, "opening_tradability_latest.json")


if __name__ == "__main__":
    unittest.main()
