#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest
from unittest.mock import patch

import security_master_refresh as refresh


class SecurityMasterRefreshTests(unittest.TestCase):
    def test_build_security_master_includes_workbuddy_candidate_pool_codes(self) -> None:
        with (
            patch.object(refresh, "_scanner_universe_records", return_value=[]),
            patch.object(refresh, "_load_track_codes", return_value=[]),
            patch.object(refresh, "_load_pending_codes", return_value=[]),
            patch.object(refresh, "_load_challenger_codes", return_value=[]),
            patch.object(refresh, "_load_workbuddy_codes", return_value=[]),
            patch.object(
                refresh,
                "_load_workbuddy_candidate_pool_codes",
                return_value=[
                    ("301199", "迈赫股份"),
                    ("300985", "致远新能"),
                    ("301499", "维科精密"),
                ],
            ),
            patch.object(
                refresh,
                "fallback_market_info",
                side_effect=lambda code: {
                    "code": code,
                    "name": "",
                    "market_char": ".SZ",
                    "exchange": "SZSE",
                    "market_tdx": 0,
                    "entity_type_name": "A股",
                    "class_name": "沪深京股票",
                    "tradable_by_current_executor": True,
                    "mapping_source": "fallback_prefix",
                    "mapping_detail": "legacy_prefix_sz",
                },
            ),
        ):
            rows = refresh.build_security_master()

        row_map = {item["code"]: item for item in rows}
        self.assertEqual(sorted(row_map.keys()), ["300985", "301199", "301499"])
        self.assertEqual(row_map["301199"]["name"], "迈赫股份")
        self.assertEqual(row_map["300985"]["source_refs"], ["workbuddy_candidate_pool"])
        self.assertTrue(row_map["301499"]["tradable_by_current_executor"])


if __name__ == "__main__":
    unittest.main()
