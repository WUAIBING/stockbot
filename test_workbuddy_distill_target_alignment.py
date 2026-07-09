import unittest

import build_workbuddy_distill_pool as pool
from workbuddy_distill.scripts import distill_local_templates as templates


class DistillTargetAlignmentTests(unittest.TestCase):
    def test_classify_verdict_requires_profit_signal_not_just_hit_proxy(self) -> None:
        verdict, action = templates.classify_verdict(
            0.24,
            0.14,
            0.90,
            candidate_win_rate=0.44,
            candidate_avg_return=0.8,
            portfolio_positive_day_rate=0.46,
            profit_priority_score=70.0,
        )

        self.assertEqual(verdict, "fail")
        self.assertEqual(action, "downgrade")

    def test_business_score_prioritizes_win_rate_and_return(self) -> None:
        alpha_first_score, alpha_first_priority = templates.compute_business_score(
            quality_score=92.0,
            candidate_win_rate=0.60,
            candidate_avg_return=2.40,
            portfolio_positive_day_rate=0.62,
            candidate_retention_rate=0.90,
            gain_loss_ratio=1.8,
        )
        proxy_first_score, proxy_first_priority = templates.compute_business_score(
            quality_score=112.0,
            candidate_win_rate=0.47,
            candidate_avg_return=1.20,
            portfolio_positive_day_rate=0.51,
            candidate_retention_rate=0.95,
            gain_loss_ratio=1.1,
        )

        self.assertGreater(alpha_first_priority, proxy_first_priority)
        self.assertGreater(alpha_first_score, proxy_first_score)

    def test_template_weight_uses_profit_priority_as_main_driver(self) -> None:
        alpha_metrics = {
            "business_score": 88.0,
            "profit_priority_score": 110.0,
            "candidate_win_rate": 0.61,
            "candidate_avg_return": 2.3,
            "top100_hit_rate": 0.18,
        }
        proxy_metrics = {
            "business_score": 101.0,
            "profit_priority_score": 82.0,
            "candidate_win_rate": 0.49,
            "candidate_avg_return": 1.1,
            "top100_hit_rate": 0.24,
        }

        self.assertGreater(pool._compute_template_weight(alpha_metrics), pool._compute_template_weight(proxy_metrics))

    def test_classify_combination_promotes_on_profit_priority_even_without_full_proxy_stability(self) -> None:
        base_metrics = {
            "top100_hit_rate": 0.20,
            "top50_hit_rate": 0.15,
            "top30_hit_rate": 0.10,
            "hit_day_rate": 0.86,
            "front_shift_score": 0.12,
            "avg_hit_rank": 22.0,
            "candidate_avg_return": 1.9,
            "candidate_win_rate": 0.54,
            "candidate_retention_rate": 0.82,
            "profit_priority_score": 88.0,
        }
        upgraded_metrics = {
            "top100_hit_rate": 0.195,
            "top50_hit_rate": 0.145,
            "top30_hit_rate": 0.095,
            "hit_day_rate": 0.83,
            "front_shift_score": 0.13,
            "avg_hit_rank": 23.0,
            "candidate_avg_return": 2.2,
            "candidate_win_rate": 0.57,
            "candidate_retention_rate": 0.8,
            "profit_priority_score": 98.0,
        }

        self.assertEqual(templates.classify_combination(base_metrics, upgraded_metrics), "promote")

    def test_rank_sort_key_prioritizes_profitability_over_champion_proxy(self) -> None:
        profitable_item = {
            "classification": "primary",
            "avg_profitability_priority": 112.0,
            "selection_score": 78.0,
            "avg_candidate_avg_return": 2.6,
            "avg_candidate_win_rate": 0.59,
            "champion_hits": 0,
            "raw_selection_score": 92.0,
            "template_hits": 2,
            "avg_business_score": 90.0,
            "latest_rank": 35,
            "code": "300001",
        }
        proxy_item = {
            "classification": "primary",
            "avg_profitability_priority": 86.0,
            "selection_score": 84.0,
            "avg_candidate_avg_return": 1.3,
            "avg_candidate_win_rate": 0.48,
            "champion_hits": 2,
            "raw_selection_score": 105.0,
            "template_hits": 4,
            "avg_business_score": 101.0,
            "latest_rank": 20,
            "code": "300002",
        }

        ranked = sorted([proxy_item, profitable_item], key=pool._rank_sort_key)
        self.assertEqual(ranked[0]["code"], "300001")

    def test_apply_guardrails_uses_profitability_priority_for_hot_crowding_order(self) -> None:
        hot_high_profit = {
            "code": "300001",
            "avg_profitability_priority": 112.0,
            "avg_candidate_avg_return": 2.5,
            "avg_candidate_win_rate": 0.58,
            "raw_selection_score": 90.0,
            "champion_hits": 0,
            "latest_rank": 12,
            "heat_profile": {"is_hot": True, "level": "warm", "penalty": 0.0},
        }
        hot_proxy_first = {
            "code": "300002",
            "avg_profitability_priority": 84.0,
            "avg_candidate_avg_return": 1.1,
            "avg_candidate_win_rate": 0.47,
            "raw_selection_score": 108.0,
            "champion_hits": 2,
            "latest_rank": 2,
            "heat_profile": {"is_hot": True, "level": "warm", "penalty": 0.0},
        }
        scoring_profile = pool.default_scoring_profile()
        scoring_profile["hot_cluster_safe_count"] = 1
        scoring_profile["hot_cluster_penalty_step"] = 2.0
        execution_context = {"available": False, "penalty_scale": 0.0}

        pool._apply_guardrails([hot_high_profit, hot_proxy_first], scoring_profile, execution_context)

        self.assertEqual(hot_high_profit["crowding_position"], 1)
        self.assertEqual(hot_high_profit["crowding_penalty"], 0.0)
        self.assertEqual(hot_proxy_first["crowding_position"], 2)
        self.assertEqual(hot_proxy_first["crowding_penalty"], 2.0)


if __name__ == "__main__":
    unittest.main()
