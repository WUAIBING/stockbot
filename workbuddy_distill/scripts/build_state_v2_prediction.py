from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import distill_local_templates as distill


ARTIFACTS_ROOT = distill.ARTIFACTS_ROOT
STATE_V2_JSON = ARTIFACTS_ROOT / "market_state_v2_latest.json"
STATE_V2_MD = ARTIFACTS_ROOT / "market_state_v2_latest.md"
PREDICTION_JSON = ARTIFACTS_ROOT / "market_state_t1_prediction_latest.json"
SCORECARD_JSON = ARTIFACTS_ROOT / "template_state_scorecard_latest.json"

ROUTING_RULES = {
    "attack_switch": {
        "min_confidence": 0.4,
        "min_same_cluster_support_count": 2,
        "max_attack_vs_stability_gap": 6.0,
        "min_return_edge": 0.0,
        "min_front_shift_edge": -0.01,
        "min_state_sample_days": 3,
    },
    "balanced_shadow": {
        "min_confidence": 0.32,
        "states": ["mid_hot_transition", "rotation_broad_hot", "front_exhaustion_watch"],
    },
}

RECENT_TRANSITION_DECAY = 0.82
RECENT_TRANSITION_BLEND = 0.65
ROLLING_TRANSITION_WINDOW = 10
STATE_PRIOR_BASE_PENALTY = {
    "front_dominant": 0.06,
}
STATE_PRIOR_CONTEXT_PENALTY = {
    ("broad_expansion", "front_dominant"): 0.05,
    ("front_exhaustion_watch", "front_dominant"): 0.08,
    ("rotation_broad_hot", "front_dominant"): 0.03,
}
V3_DIRECT_ROUTE_GAP = 6.0
V31_ATTACK_ROUTE_GAP = 4.0
V31_ATTACK_SHADOW_GAP = 1.2
V32_ATTACK_ROUTE_GAP = 4.5
V32_ATTACK_SHADOW_GAP = 1.0


def _write_json(file_path: Path, payload: object) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _avg_pct(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(float(row["pct_change"]) for row in rows) / len(rows)


def _share_above(rows: list[dict[str, Any]], threshold: float) -> float:
    if not rows:
        return 0.0
    hits = sum(1 for row in rows if float(row["pct_change"]) >= threshold)
    return hits / len(rows)


def _score_to_band(score: float) -> str:
    if score >= 75:
        return "hot"
    if score >= 55:
        return "warm"
    if score >= 35:
        return "neutral"
    return "cool"


def _state_label_v2(profile: dict[str, Any]) -> str:
    front_avg = float(profile["front_avg_pct"])
    front_gap = float(profile["front_mid_gap"])
    mid_avg = float(profile["mid_avg_pct"])
    mid_tail_gap = float(profile["mid_tail_gap"])
    breadth = float(profile["broad_strength"])
    top100_limit = float(profile["top100_limit_share"])
    front_limit = float(profile["front_limit_share"])
    front_concentration_gap = float(profile["front_concentration_gap"])
    breadth_ratio = float(profile["breadth_to_front_ratio"])
    rear_avg = float(profile["rear_avg_pct"])

    if front_avg <= 9.5 or breadth <= 6.5:
        return "risk_off"
    if front_avg >= 19.0 and front_gap >= 8.8 and rear_avg <= 9.7 and breadth_ratio <= 0.5:
        return "front_exhaustion_watch"
    if breadth >= 13.0 and front_gap <= 4.8:
        return "rotation_broad_hot"
    if front_avg >= 18.8 and front_gap >= 8.4 and front_concentration_gap >= 4.8 and breadth <= 10.5 and front_limit >= 0.9:
        return "front_dominant"
    if front_avg >= 18.8 and breadth >= 10.4 and mid_tail_gap >= 1.5 and top100_limit >= 0.9 and breadth_ratio >= 0.53:
        return "broad_expansion"
    if mid_avg >= 11.3 and front_gap >= 5.2:
        return "mid_hot_transition"
    return "balanced_momentum"


def _state_cluster_v2(label: str) -> str:
    if label in {"front_dominant", "broad_expansion"}:
        return "attack"
    if label in {"mid_hot_transition", "rotation_broad_hot", "balanced_momentum", "front_exhaustion_watch"}:
        return "balanced"
    return "defense"


def build_state_profiles_v2(dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for day in dataset:
        rows = day["full_rows"]
        top10 = rows[:10]
        top20 = rows[:20]
        top30 = rows[:30]
        top50 = rows[:50]
        top100 = rows[:100]
        middle = rows[19:60]
        rear = rows[60:100]
        front_avg = _avg_pct(top10)
        mid_avg = _avg_pct(middle)
        breadth = _avg_pct(top100[30:100])
        profile = {
            "trade_date": day["trade_date"],
            "front_avg_pct": round(front_avg, 4),
            "top20_avg_pct": round(_avg_pct(top20), 4),
            "top30_avg_pct": round(_avg_pct(top30), 4),
            "top50_avg_pct": round(_avg_pct(top50), 4),
            "top100_avg_pct": round(_avg_pct(top100), 4),
            "mid_avg_pct": round(mid_avg, 4),
            "rear_avg_pct": round(_avg_pct(rear), 4),
            "broad_strength": round(breadth, 4),
            "front_limit_share": round(_share_above(top10, 9.9), 4),
            "top30_limit_share": round(_share_above(top30, 9.9), 4),
            "top100_limit_share": round(_share_above(top100, 9.9), 4),
            "front_15_share": round(_share_above(top10, 15.0), 4),
            "mid_10_share": round(_share_above(middle, 10.0), 4),
            "rear_8_share": round(_share_above(rear, 8.0), 4),
            "front_mid_gap": round(front_avg - mid_avg, 4),
            "front_concentration_gap": round(front_avg - _avg_pct(top50), 4),
            "mid_tail_gap": round(mid_avg - _avg_pct(rear), 4),
            "breadth_to_front_ratio": round(breadth / max(front_avg, 0.1), 4),
        }
        attack_score = (
            min(profile["front_avg_pct"], 20.0) / 20.0 * 30
            + profile["front_limit_share"] * 20
            + min(profile["front_mid_gap"], 10.0) / 10.0 * 20
            + min(profile["broad_strength"], 14.0) / 14.0 * 20
            + profile["mid_10_share"] * 10
        )
        stability_score = (
            profile["top100_limit_share"] * 35
            + min(profile["top50_avg_pct"], 13.0) / 13.0 * 20
            + min(profile["rear_avg_pct"], 10.5) / 10.5 * 15
            + max(0.0, 1.0 - abs(profile["front_mid_gap"] - 6.0) / 6.0) * 20
            + profile["rear_8_share"] * 10
        )
        profile["attack_score"] = round(attack_score, 4)
        profile["stability_score"] = round(stability_score, 4)
        profile["attack_band"] = _score_to_band(attack_score)
        profile["stability_band"] = _score_to_band(stability_score)
        profile["state_label_v2"] = _state_label_v2(profile)
        profile["state_cluster_v2"] = _state_cluster_v2(profile["state_label_v2"])
        profiles.append(profile)
    return profiles


def _template_name(params: dict[str, Any], veto_params: dict[str, Any] | None) -> str:
    name = distill.build_template_name(params)
    if veto_params:
        name = f"{name}__veto__{distill.build_veto_name(veto_params)}"
    return name


def _load_templates(
    registry_path: Path,
    challenger_path: Path,
) -> dict[str, dict[str, Any]]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    challenger_payload = json.loads(challenger_path.read_text(encoding="utf-8"))
    incumbent = registry["champion_template"]
    challenger = challenger_payload.get("challenger") or {}
    templates = {
        "champion": {
            "label": "champion",
            "template_name": _template_name(incumbent["params"], incumbent.get("negative_veto")),
            "params": incumbent["params"],
            "negative_veto": incumbent.get("negative_veto"),
        }
    }
    if challenger:
        templates["attack"] = {
            "label": "attack",
            "template_name": challenger["template_name"],
            "params": challenger["params"],
            "negative_veto": challenger.get("negative_veto"),
        }
    return templates


def evaluate_template_by_state(
    dataset: list[dict[str, Any]],
    state_by_date: dict[str, dict[str, Any]],
    params: dict[str, Any],
    veto_params: dict[str, Any] | None,
) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "days": 0,
            "candidate_count": 0,
            "positive_count": 0,
            "return_sum": 0.0,
            "top50_hit_count": 0,
            "front_shift_points": 0.0,
            "hit_days": 0,
        }
    )
    for idx in range(1, len(dataset)):
        history = dataset[:idx]
        target = dataset[idx]
        state = state_by_date[target["trade_date"]]["state_label_v2"]
        candidates, _ = distill.select_candidates(history, params, veto_params=veto_params)
        candidate_codes = [item["code"] for item in candidates]
        outcome = distill.summarize_candidate_outcomes(candidate_codes, target)
        top50_hits = [code for code in candidate_codes if code in target["top50_codes"]]
        hit_ranks = [int(target["row_map"][code]["rank"]) for code in candidate_codes if code in target["row_map"]]
        front_shift_points = sum(distill.front_bucket_points(rank) for rank in hit_ranks)
        bucket = grouped[state]
        bucket["days"] += 1
        bucket["candidate_count"] += outcome["return_count"]
        bucket["positive_count"] += outcome["positive_count"]
        bucket["return_sum"] += sum(outcome["returns"])
        bucket["top50_hit_count"] += len(top50_hits)
        bucket["front_shift_points"] += front_shift_points
        if top50_hits:
            bucket["hit_days"] += 1

    result: dict[str, Any] = {}
    for state, item in grouped.items():
        candidate_count = item["candidate_count"] or 1
        result[state] = {
            "days": item["days"],
            "candidate_win_rate": round(item["positive_count"] / candidate_count, 4),
            "candidate_avg_return": round(item["return_sum"] / candidate_count, 4),
            "top50_hit_rate": round(item["top50_hit_count"] / candidate_count, 4),
            "front_shift_score": round(item["front_shift_points"] / (candidate_count * 4), 4),
            "hit_day_rate": round(item["hit_days"] / item["days"], 4) if item["days"] else 0.0,
        }
    return result


def build_template_state_scorecards(template_state_scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scorecards: dict[str, Any] = {}
    state_preference: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for label, state_scores in template_state_scores.items():
        scorecards[label] = {}
        for state, metrics in state_scores.items():
            state_score = round(
                metrics["candidate_win_rate"] * 22
                + metrics["candidate_avg_return"] * 8
                + metrics["top50_hit_rate"] * 110
                + metrics["front_shift_score"] * 90
                + metrics["hit_day_rate"] * 15,
                4,
            )
            role_hint = "attack" if metrics["candidate_avg_return"] >= 2.1 or metrics["front_shift_score"] >= 0.14 else "stable"
            item = {
                **metrics,
                "state_score": state_score,
                "role_hint": role_hint,
            }
            scorecards[label][state] = item
            state_preference[state].append(
                {
                    "template_label": label,
                    "state_score": state_score,
                    "template_name": None,
                }
            )
    return {
        "templates": scorecards,
        "state_preference": state_preference,
    }


def build_template_advantage_report(
    template_state_scorecards: dict[str, dict[str, Any]],
    templates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    champion_scores = template_state_scorecards.get("champion", {})
    attack_scores = template_state_scorecards.get("attack", {})
    states = sorted(set(champion_scores) | set(attack_scores))
    rows: list[dict[str, Any]] = []
    for state in states:
        champion = champion_scores.get(state, {})
        attack = attack_scores.get(state, {})
        champion_days = int(champion.get("days", 0))
        attack_days = int(attack.get("days", 0))
        min_days = min(champion_days, attack_days)
        score_gap = round(float(attack.get("state_score", 0.0)) - float(champion.get("state_score", 0.0)), 4)
        return_gap = round(float(attack.get("candidate_avg_return", 0.0)) - float(champion.get("candidate_avg_return", 0.0)), 4)
        front_gap = round(float(attack.get("front_shift_score", 0.0)) - float(champion.get("front_shift_score", 0.0)), 4)
        hit_day_gap = round(float(attack.get("hit_day_rate", 0.0)) - float(champion.get("hit_day_rate", 0.0)), 4)
        if min_days >= 5:
            reliability = "high"
        elif min_days >= 3:
            reliability = "medium"
        else:
            reliability = "low"
        preferred_template = "champion"
        if score_gap > 5.0 and return_gap >= 0.0:
            preferred_template = "attack"
        rows.append(
            {
                "state": state,
                "champion_days": champion_days,
                "attack_days": attack_days,
                "sample_reliability": reliability,
                "attack_state_score_gap": score_gap,
                "attack_return_gap": return_gap,
                "attack_front_shift_gap": front_gap,
                "attack_hit_day_gap": hit_day_gap,
                "preferred_template": preferred_template,
                "preferred_template_name": templates.get(preferred_template, {}).get("template_name"),
            }
        )
    return {
        "rows": rows,
        "summary": {
            "states_with_attack_edge": sum(1 for row in rows if row["preferred_template"] == "attack"),
            "states_with_high_reliability": sum(1 for row in rows if row["sample_reliability"] == "high"),
        },
    }


def _advantage_row_for_state(template_advantage_report: dict[str, Any], state: str) -> dict[str, Any]:
    return next((item for item in template_advantage_report.get("rows", []) if item["state"] == state), {})


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _scale_feature(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clip01((value - low) / (high - low))


def _inverse_scale_feature(value: float, low: float, high: float) -> float:
    return 1.0 - _scale_feature(value, low, high)


def _centered_feature(value: float, center: float, tolerance: float) -> float:
    if tolerance <= 0:
        return 0.0
    return _clip01(1.0 - abs(value - center) / tolerance)


def _feature_contribution(name: str, side: str, strength: float, weight: float, note: str) -> dict[str, Any]:
    contribution = round(_clip01(strength) * weight, 4)
    return {
        "name": name,
        "side": side,
        "strength": round(_clip01(strength), 4),
        "weight": round(weight, 4),
        "contribution": contribution,
        "note": note,
    }


def build_v31_light_route_signal(
    current_profile: dict[str, Any],
    templates: dict[str, dict[str, Any]],
    template_advantage_report: dict[str, Any],
) -> dict[str, Any]:
    state_label = str(current_profile["state_label_v2"])
    current_advantage = _advantage_row_for_state(template_advantage_report, state_label)
    reliability_multiplier = {
        "high": 1.0,
        "medium": 0.65,
        "low": 0.3,
    }.get(str(current_advantage.get("sample_reliability", "low")), 0.3)

    attack_features = [
        _feature_contribution(
            "state_attack_prior",
            "attack",
            {"front_dominant": 1.0, "mid_hot_transition": 0.72, "balanced_momentum": 0.52}.get(state_label, 0.25),
            10.0,
            "当前状态本身偏攻击还是偏稳态。",
        ),
        _feature_contribution(
            "front_avg_pct",
            "attack",
            _scale_feature(float(current_profile["front_avg_pct"]), 16.2, 20.2),
            13.0,
            "前排平均强度越高，越偏进攻模板。",
        ),
        _feature_contribution(
            "front_mid_gap",
            "attack",
            _scale_feature(float(current_profile["front_mid_gap"]), 4.8, 9.2),
            12.0,
            "前中分层越明显，越偏前排进攻。",
        ),
        _feature_contribution(
            "front_limit_share",
            "attack",
            _scale_feature(float(current_profile["front_limit_share"]), 0.35, 1.0),
            9.0,
            "前排涨停/强封比例高时，进攻模板受益更大。",
        ),
        _feature_contribution(
            "front_concentration_gap",
            "attack",
            _scale_feature(float(current_profile["front_concentration_gap"]), 2.4, 5.4),
            8.0,
            "强度越集中在最前排，越适合进攻型。",
        ),
        _feature_contribution(
            "rear_avg_pct",
            "attack",
            _inverse_scale_feature(float(current_profile["rear_avg_pct"]), 9.8, 11.6),
            4.0,
            "后排越弱，越说明资金集中在前排。",
        ),
        _feature_contribution(
            "attack_bias",
            "attack",
            _scale_feature(float(current_profile["attack_score"]) - float(current_profile["stability_score"]), -10.0, 14.0),
            7.0,
            "状态画像中的攻击偏置。",
        ),
    ]
    champion_features = [
        _feature_contribution(
            "state_stable_prior",
            "champion",
            {"broad_expansion": 1.0, "front_exhaustion_watch": 0.92, "rotation_broad_hot": 0.78, "risk_off": 0.85}.get(state_label, 0.28),
            10.0,
            "当前状态本身偏稳态还是偏进攻。",
        ),
        _feature_contribution(
            "broad_strength",
            "champion",
            _scale_feature(float(current_profile["broad_strength"]), 8.5, 13.8),
            10.0,
            "广度越好，冠军模板的稳态覆盖更强。",
        ),
        _feature_contribution(
            "breadth_to_front_ratio",
            "champion",
            _scale_feature(float(current_profile["breadth_to_front_ratio"]), 0.48, 0.72),
            13.0,
            "广度占比越高，越不需要过度追逐最前排。",
        ),
        _feature_contribution(
            "top100_limit_share",
            "champion",
            _scale_feature(float(current_profile["top100_limit_share"]), 0.04, 0.15),
            8.0,
            "强势向更宽范围扩散时，冠军模板更适合。",
        ),
        _feature_contribution(
            "rear_avg_pct",
            "champion",
            _scale_feature(float(current_profile["rear_avg_pct"]), 8.6, 11.2),
            5.0,
            "后排承接越强，越偏稳态冠军。",
        ),
        _feature_contribution(
            "balanced_front_gap",
            "champion",
            _centered_feature(float(current_profile["front_mid_gap"]), 6.0, 3.2),
            8.0,
            "前中差距更接近均衡区间时，冠军模板更占优。",
        ),
        _feature_contribution(
            "stability_bias",
            "champion",
            _scale_feature(float(current_profile["stability_score"]) - float(current_profile["attack_score"]), -10.0, 14.0),
            7.0,
            "状态画像中的稳态偏置。",
        ),
    ]
    score_gap = float(current_advantage.get("attack_state_score_gap", 0.0))
    return_gap = float(current_advantage.get("attack_return_gap", 0.0))
    front_gap = float(current_advantage.get("attack_front_shift_gap", 0.0))
    hit_day_gap = float(current_advantage.get("attack_hit_day_gap", 0.0))
    attack_features.extend(
        [
            _feature_contribution(
                "state_history_attack_score_gap",
                "attack",
                _scale_feature(score_gap, 0.0, 18.0) * reliability_multiplier,
                10.0,
                "当前状态下进攻模板的历史综合优势。",
            ),
            _feature_contribution(
                "state_history_attack_return_gap",
                "attack",
                _scale_feature(return_gap, 0.0, 1.2) * reliability_multiplier,
                7.0,
                "当前状态下进攻模板的历史收益优势。",
            ),
            _feature_contribution(
                "state_history_attack_front_gap",
                "attack",
                _scale_feature(front_gap, 0.0, 0.05) * reliability_multiplier,
                6.0,
                "当前状态下进攻模板的前排命中优势。",
            ),
        ]
    )
    champion_features.extend(
        [
            _feature_contribution(
                "state_history_champion_score_gap",
                "champion",
                _scale_feature(-score_gap, 0.0, 18.0) * reliability_multiplier,
                10.0,
                "当前状态下冠军模板的历史综合优势。",
            ),
            _feature_contribution(
                "state_history_champion_hitday_gap",
                "champion",
                _scale_feature(-hit_day_gap, 0.0, 0.25) * reliability_multiplier,
                7.0,
                "当前状态下冠军模板的命中日稳定性优势。",
            ),
            _feature_contribution(
                "state_history_champion_return_defense",
                "champion",
                _scale_feature(-return_gap, 0.0, 0.8) * reliability_multiplier,
                5.0,
                "当前状态下冠军模板在收益回撤侧的防守优势。",
            ),
        ]
    )

    attack_score = round(sum(item["contribution"] for item in attack_features), 4)
    champion_score = round(sum(item["contribution"] for item in champion_features), 4)
    direct_gap = round(attack_score - champion_score, 4)
    attack_probability = round(attack_score / max(attack_score + champion_score, 0.1), 4)
    direct_confidence = round(max(0.0, min(1.0, abs(direct_gap) / 16.0 + abs(attack_probability - 0.5) * 0.8 + 0.18)), 4)

    attack = templates.get("attack")
    champion = templates["champion"]
    if attack and direct_gap >= V31_ATTACK_ROUTE_GAP:
        route_action = "prefer_attack_template"
        primary_template = attack["template_name"]
        shadow_template = champion["template_name"]
        summary_reason = "连续特征打分显示当前结构更偏前排攻击，进攻模板主切。"
    elif attack and direct_gap >= V31_ATTACK_SHADOW_GAP:
        route_action = "champion_with_attack_shadow"
        primary_template = champion["template_name"]
        shadow_template = attack["template_name"]
        summary_reason = "进攻侧略占优，但优势未大到足以主切，先保留为影子。"
    else:
        route_action = "prefer_champion_template"
        primary_template = champion["template_name"]
        shadow_template = attack["template_name"] if attack else None
        summary_reason = "连续特征打分仍偏稳态，冠军模板继续主导。"
    return {
        "route_action": route_action,
        "primary_template": primary_template,
        "shadow_template": shadow_template,
        "direct_gap": direct_gap,
        "direct_confidence": direct_confidence,
        "attack_direct_score": attack_score,
        "champion_direct_score": champion_score,
        "attack_probability": attack_probability,
        "summary_reason": summary_reason,
        "feature_contributions": {
            "attack": attack_features,
            "champion": champion_features,
        },
    }


def build_v32_calibrated_route_signal(
    current_profile: dict[str, Any],
    templates: dict[str, dict[str, Any]],
    template_advantage_report: dict[str, Any],
) -> dict[str, Any]:
    base = build_v31_light_route_signal(current_profile, templates, template_advantage_report)
    state_label = str(current_profile["state_label_v2"])
    attack_score = float(base["attack_direct_score"])
    champion_score = float(base["champion_direct_score"])
    attack_probability = float(base["attack_probability"])
    front_gap = float(current_profile["front_mid_gap"])
    front_concentration_gap = float(current_profile["front_concentration_gap"])
    breadth_ratio = float(current_profile["breadth_to_front_ratio"])
    top100_limit_share = float(current_profile["top100_limit_share"])
    adjustments: list[dict[str, Any]] = []

    if state_label == "broad_expansion":
        if breadth_ratio >= 0.53 and top100_limit_share >= 0.95:
            boost = 3.6
            champion_score += boost
            adjustments.append(
                {
                    "side": "champion",
                    "delta": round(boost, 4),
                    "reason": "广度扩散确认较强，优先压低误切进攻型的概率。",
                }
            )
        if attack_probability <= 0.52:
            boost = 1.4
            champion_score += boost
            adjustments.append(
                {
                    "side": "champion",
                    "delta": round(boost, 4),
                    "reason": "进攻概率未明显超过五五开，稳态侧再加一道缓冲。",
                }
            )

    if state_label == "front_dominant":
        extreme_front = front_concentration_gap >= 6.0 and front_gap >= 8.5
        if extreme_front:
            boost = 1.8
            attack_score += boost
            adjustments.append(
                {
                    "side": "attack",
                    "delta": round(boost, 4),
                    "reason": "前排集中和前中断层都达到极值，允许进攻型更积极。",
                }
            )
        else:
            boost = 5.2
            champion_score += boost
            adjustments.append(
                {
                    "side": "champion",
                    "delta": round(boost, 4),
                    "reason": "虽然标签落在前排主导，但集中度不够极致，先防误切。",
                }
            )

    if state_label == "front_exhaustion_watch":
        boost = 2.4
        champion_score += boost
        adjustments.append(
            {
                "side": "champion",
                "delta": round(boost, 4),
                "reason": "前排透支观察状态下，默认再偏保守一档。",
            }
        )

    direct_gap = round(attack_score - champion_score, 4)
    calibrated_attack_probability = round(attack_score / max(attack_score + champion_score, 0.1), 4)
    direct_confidence = round(
        max(
            0.0,
            min(
                1.0,
                abs(direct_gap) / 15.0 + abs(calibrated_attack_probability - 0.5) * 0.9 + 0.16,
            ),
        ),
        4,
    )
    attack = templates.get("attack")
    champion = templates["champion"]
    if attack and direct_gap >= V32_ATTACK_ROUTE_GAP:
        route_action = "prefer_attack_template"
        primary_template = attack["template_name"]
        shadow_template = champion["template_name"]
        summary_reason = "V3.2 校准后仍明显偏进攻，保留进攻模板主切。"
    elif attack and direct_gap >= V32_ATTACK_SHADOW_GAP:
        route_action = "champion_with_attack_shadow"
        primary_template = champion["template_name"]
        shadow_template = attack["template_name"]
        summary_reason = "V3.2 校准后只保留进攻影子，不直接主切。"
    else:
        route_action = "prefer_champion_template"
        primary_template = champion["template_name"]
        shadow_template = attack["template_name"] if attack else None
        summary_reason = "V3.2 校准后仍偏稳态，冠军模板继续主导。"
    return {
        **base,
        "route_action": route_action,
        "primary_template": primary_template,
        "shadow_template": shadow_template,
        "direct_gap": direct_gap,
        "direct_confidence": direct_confidence,
        "attack_direct_score": round(attack_score, 4),
        "champion_direct_score": round(champion_score, 4),
        "attack_probability": calibrated_attack_probability,
        "summary_reason": summary_reason,
        "base_route_action": base["route_action"],
        "base_direct_gap": base["direct_gap"],
        "base_attack_probability": base["attack_probability"],
        "calibration_adjustments": adjustments,
    }


def build_v3_direct_route_signal(
    current_profile: dict[str, Any],
    templates: dict[str, dict[str, Any]],
    template_advantage_report: dict[str, Any],
) -> dict[str, Any]:
    state_label = str(current_profile["state_label_v2"])
    attack_score = 0.0
    champion_score = 0.0
    reasons: list[str] = []

    front_avg = float(current_profile["front_avg_pct"])
    front_gap = float(current_profile["front_mid_gap"])
    breadth = float(current_profile["broad_strength"])
    breadth_ratio = float(current_profile["breadth_to_front_ratio"])
    front_limit = float(current_profile["front_limit_share"])
    top100_limit = float(current_profile["top100_limit_share"])
    front_concentration_gap = float(current_profile["front_concentration_gap"])
    rear_avg = float(current_profile["rear_avg_pct"])
    mid_avg = float(current_profile["mid_avg_pct"])

    if state_label == "front_dominant":
        attack_score += 14
        reasons.append("当前已落在前排主导结构，进攻模板基础分上调。")
    if state_label == "broad_expansion":
        champion_score += 10
        reasons.append("当前是广度扩散结构，冠军模板更适合做稳态主路由。")
    if state_label == "front_exhaustion_watch":
        champion_score += 9
        reasons.append("出现前排透支迹象，优先防止过度追强。")
    if state_label == "rotation_broad_hot":
        champion_score += 6
        reasons.append("轮动热度上来时，先用冠军模板稳住主路由。")

    if front_avg >= 19.0:
        attack_score += 6
    if front_gap >= 8.0:
        attack_score += 5
    if front_limit >= 0.9:
        attack_score += 5
    if front_concentration_gap >= 4.8:
        attack_score += 4
    if rear_avg <= 9.8:
        attack_score += 2

    if breadth >= 10.5:
        champion_score += 4
    if breadth_ratio >= 0.55:
        champion_score += 7
    if top100_limit >= 0.09:
        champion_score += 3
    if mid_avg >= 11.2 and front_gap <= 6.0:
        champion_score += 4

    current_advantage = _advantage_row_for_state(template_advantage_report, state_label)
    if current_advantage:
        reliability = str(current_advantage.get("sample_reliability", "low"))
        preferred = str(current_advantage.get("preferred_template", "champion"))
        score_gap = float(current_advantage.get("attack_state_score_gap", 0.0))
        if reliability == "high":
            if preferred == "attack":
                attack_score += min(14.0, max(0.0, score_gap) * 0.35)
                reasons.append("当前状态的高可靠历史优势偏进攻模板。")
            else:
                champion_score += min(14.0, max(0.0, -score_gap) * 0.35)
                reasons.append("当前状态的高可靠历史优势偏冠军模板。")
        elif reliability == "medium":
            if preferred == "attack":
                attack_score += 4
            else:
                champion_score += 4

    direct_gap = round(attack_score - champion_score, 4)
    direct_confidence = round(max(0.0, min(1.0, abs(direct_gap) / 18.0 + 0.25)), 4)
    attack = templates.get("attack")
    champion = templates["champion"]

    if attack and direct_gap >= V3_DIRECT_ROUTE_GAP:
        return {
            "route_action": "prefer_attack_template",
            "primary_template": attack["template_name"],
            "shadow_template": champion["template_name"],
            "direct_gap": direct_gap,
            "direct_confidence": direct_confidence,
            "attack_direct_score": round(attack_score, 4),
            "champion_direct_score": round(champion_score, 4),
            "reasons": reasons,
        }
    if attack and direct_gap >= 1.5:
        return {
            "route_action": "champion_with_attack_shadow",
            "primary_template": champion["template_name"],
            "shadow_template": attack["template_name"],
            "direct_gap": direct_gap,
            "direct_confidence": direct_confidence,
            "attack_direct_score": round(attack_score, 4),
            "champion_direct_score": round(champion_score, 4),
            "reasons": reasons + ["进攻分有优势，但未到主切阈值，先保留为影子。"] ,
        }
    return {
        "route_action": "prefer_champion_template",
        "primary_template": champion["template_name"],
        "shadow_template": attack["template_name"] if attack else None,
        "direct_gap": direct_gap,
        "direct_confidence": direct_confidence,
        "attack_direct_score": round(attack_score, 4),
        "champion_direct_score": round(champion_score, 4),
        "reasons": reasons + ["冠军模板的稳态分更高，继续作为主路由。"] ,
    }


def build_transition_payload(state_history: list[dict[str, Any]]) -> dict[str, Any]:
    transitions: dict[str, Counter[str]] = defaultdict(Counter)
    weighted_transitions: dict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    rolling_transitions: dict[str, Counter[str]] = defaultdict(Counter)
    total_pairs = max(0, len(state_history) - 1)
    rolling_start = max(0, len(state_history) - ROLLING_TRANSITION_WINDOW)
    for offset, (prev, cur) in enumerate(zip(state_history, state_history[1:])):
        prev_state = str(prev["state_label_v2"])
        cur_state = str(cur["state_label_v2"])
        transitions[prev_state][cur_state] += 1
        distance_from_recent = total_pairs - offset - 1
        weight = RECENT_TRANSITION_DECAY ** max(0, distance_from_recent)
        weighted_transitions[prev_state][cur_state] += weight
        if offset >= rolling_start:
            rolling_transitions[prev_state][cur_state] += 1

    matrix: dict[str, dict[str, float]] = {}
    weighted_matrix: dict[str, dict[str, float]] = {}
    blended_matrix: dict[str, dict[str, float]] = {}
    rolling_matrix: dict[str, dict[str, float]] = {}
    for state, counter in transitions.items():
        total = sum(counter.values()) or 1
        matrix[state] = {key: round(value / total, 4) for key, value in counter.items()}
    for state, counter in weighted_transitions.items():
        total = sum(counter.values()) or 1.0
        weighted_matrix[state] = {key: round(value / total, 4) for key, value in counter.items()}
    for state, counter in rolling_transitions.items():
        total = sum(counter.values()) or 1
        rolling_matrix[state] = {key: round(value / total, 4) for key, value in counter.items()}
    all_states = set(matrix) | set(weighted_matrix)
    for state in all_states:
        keys = set(matrix.get(state, {})) | set(weighted_matrix.get(state, {}))
        row = {
            key: matrix.get(state, {}).get(key, 0.0) * (1.0 - RECENT_TRANSITION_BLEND)
            + weighted_matrix.get(state, {}).get(key, 0.0) * RECENT_TRANSITION_BLEND
            for key in keys
        }
        total = sum(row.values()) or 1.0
        blended_matrix[state] = {key: round(value / total, 4) for key, value in row.items()}
    recent_states = [str(item["state_label_v2"]) for item in state_history[-5:]]
    return {
        "transition_counts": {state: dict(counter) for state, counter in transitions.items()},
        "rolling_transition_counts": {state: dict(counter) for state, counter in rolling_transitions.items()},
        "transition_matrix": matrix,
        "weighted_transition_matrix": weighted_matrix,
        "rolling_transition_matrix": rolling_matrix,
        "blended_transition_matrix": blended_matrix,
        "recent_states": recent_states,
        "recent_transition_decay": RECENT_TRANSITION_DECAY,
        "recent_transition_blend": RECENT_TRANSITION_BLEND,
        "rolling_transition_window": ROLLING_TRANSITION_WINDOW,
    }


def build_transition_diagnostics(transition_payload: dict[str, Any]) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    for state, row in transition_payload.get("blended_transition_matrix", {}).items():
        if not row:
            continue
        best_next, best_prob = max(row.items(), key=lambda item: item[1])
        dispersion = round(1.0 - best_prob, 4)
        if best_prob >= 0.6:
            transition_type = "stable"
        elif best_prob >= 0.4:
            transition_type = "semi_stable"
        else:
            transition_type = "unstable"
        diagnostics[state] = {
            "best_next_state": best_next,
            "best_next_probability": best_prob,
            "dispersion": dispersion,
            "transition_type": transition_type,
            "base_row": transition_payload.get("transition_matrix", {}).get(state, {}),
            "weighted_row": transition_payload.get("weighted_transition_matrix", {}).get(state, {}),
            "rolling_row": transition_payload.get("rolling_transition_matrix", {}).get(state, {}),
        }
    return diagnostics


def _normalize_distribution(distribution: dict[str, float]) -> dict[str, float]:
    total = sum(distribution.values()) or 1.0
    return {key: round(value / total, 4) for key, value in distribution.items() if value > 0}


def _blend_rows(rows: list[tuple[dict[str, float], float]]) -> dict[str, float]:
    mixed: dict[str, float] = defaultdict(float)
    for row, weight in rows:
        if not row or weight <= 0:
            continue
        for key, value in row.items():
            mixed[key] += float(value) * weight
    return _normalize_distribution(dict(mixed))


def _apply_state_prior_penalties(
    current_state: str,
    distribution: dict[str, float],
    recent_states: list[str],
) -> tuple[dict[str, float], dict[str, Any]]:
    adjusted = dict(distribution)
    penalties: dict[str, float] = {}
    for state, value in list(adjusted.items()):
        penalty = float(STATE_PRIOR_BASE_PENALTY.get(state, 0.0))
        penalty += float(STATE_PRIOR_CONTEXT_PENALTY.get((current_state, state), 0.0))
        if state == "front_dominant":
            recent_fd_count = sum(1 for item in recent_states if item == "front_dominant")
            if recent_fd_count >= 2:
                penalty += 0.02 * (recent_fd_count - 1)
        if penalty > 0:
            adjusted[state] = max(0.0, float(value) - penalty)
            penalties[state] = round(penalty, 4)
    adjusted = _normalize_distribution(adjusted)
    return adjusted, {"penalties": penalties, "current_state": current_state}


def backtest_state_predictions(state_history: list[dict[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    correct = 0
    for idx in range(3, len(state_history) - 1):
        history_slice = state_history[: idx + 1]
        next_actual = state_history[idx + 1]
        transition_payload = build_transition_payload(history_slice)
        prediction = predict_next_state(history_slice, transition_payload)
        predicted_state = prediction["predicted_state"]
        actual_state = str(next_actual["state_label_v2"])
        is_correct = predicted_state == actual_state
        if is_correct:
            correct += 1
        records.append(
            {
                "as_of_trade_date": history_slice[-1]["trade_date"],
                "predicted_for_trade_date": next_actual["trade_date"],
                "predicted_state": predicted_state,
                "actual_state": actual_state,
                "confidence": prediction["confidence"],
                "is_correct": is_correct,
            }
        )
    total = len(records)
    avg_confidence = round(sum(float(item["confidence"]) for item in records) / total, 4) if total else 0.0
    high_conf_total = sum(1 for item in records if float(item["confidence"]) >= 0.5)
    high_conf_correct = sum(
        1 for item in records if float(item["confidence"]) >= 0.5 and bool(item["is_correct"])
    )
    return {
        "summary": {
            "prediction_count": total,
            "accuracy": round(correct / total, 4) if total else 0.0,
            "avg_confidence": avg_confidence,
            "high_confidence_count": high_conf_total,
            "high_confidence_accuracy": round(high_conf_correct / high_conf_total, 4) if high_conf_total else 0.0,
        },
        "records": records,
    }


def _select_template_label_from_route(route_action: str) -> str:
    if route_action == "prefer_attack_template":
        return "attack"
    return "champion"


def _evaluate_template_on_target(
    history_slice: list[dict[str, Any]],
    target_day: dict[str, Any],
    template_item: dict[str, Any],
) -> dict[str, Any]:
    candidates, _ = distill.select_candidates(
        history_slice,
        template_item["params"],
        veto_params=template_item.get("negative_veto"),
    )
    candidate_codes = [item["code"] for item in candidates]
    outcome = distill.summarize_candidate_outcomes(candidate_codes, target_day)
    hit_ranks = [int(target_day["row_map"][code]["rank"]) for code in candidate_codes if code in target_day["row_map"]]
    return {
        "candidate_count": int(outcome["return_count"]),
        "candidate_avg_return": round(sum(outcome["returns"]) / max(1, outcome["return_count"]), 4),
        "candidate_win_rate": round(outcome["positive_count"] / max(1, outcome["return_count"]), 4),
        "top50_hit_rate": round(
            sum(1 for code in candidate_codes if code in target_day["top50_codes"]) / max(1, outcome["return_count"]),
            4,
        ),
        "front_shift_score": round(
            sum(distill.front_bucket_points(rank) for rank in hit_ranks) / max(1, outcome["return_count"] * 4),
            4,
        ),
    }


def backtest_routing_strategy(
    dataset: list[dict[str, Any]],
    state_history: list[dict[str, Any]],
    templates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if "attack" not in templates:
        return {"summary": {"sample_days": 0}, "records": []}

    state_by_date = {item["trade_date"]: item for item in state_history}
    records: list[dict[str, Any]] = []
    route_wins_vs_champion = 0
    route_return_sum = 0.0
    champion_return_sum = 0.0
    attack_return_sum = 0.0
    correct_pred_days = 0
    incorrect_pred_days = 0
    correct_pred_route_return = 0.0
    incorrect_pred_route_return = 0.0
    correct_pred_champion_return = 0.0
    incorrect_pred_champion_return = 0.0
    wrong_attack_switch_days = 0
    wrong_attack_switch_return_gap_sum = 0.0

    for idx in range(6, len(dataset) - 1):
        history_slice = dataset[: idx + 1]
        target_day = dataset[idx + 1]
        state_slice = state_history[: idx + 1]
        state_by_date_slice = {item["trade_date"]: item for item in state_slice}
        transition_payload = build_transition_payload(state_slice)
        prediction = predict_next_state(state_slice, transition_payload)
        template_state_scores = {
            label: evaluate_template_by_state(history_slice, state_by_date_slice, item["params"], item["negative_veto"])
            for label, item in templates.items()
        }
        template_state_scorecards = build_template_state_scorecards(template_state_scores)["templates"]
        template_advantage_report = build_template_advantage_report(template_state_scorecards, templates)
        routing = build_routing_suggestion(prediction, template_state_scores, templates, template_advantage_report)
        selected_label = _select_template_label_from_route(str(routing["route_action"]))
        chosen_perf = _evaluate_template_on_target(history_slice, target_day, templates[selected_label])
        champion_perf = _evaluate_template_on_target(history_slice, target_day, templates["champion"])
        attack_perf = _evaluate_template_on_target(history_slice, target_day, templates["attack"])
        actual_state = state_by_date[target_day["trade_date"]]["state_label_v2"]
        prediction_correct = prediction["predicted_state"] == actual_state
        route_return_sum += chosen_perf["candidate_avg_return"]
        champion_return_sum += champion_perf["candidate_avg_return"]
        attack_return_sum += attack_perf["candidate_avg_return"]
        selected_vs_champion_return_gap = round(
            chosen_perf["candidate_avg_return"] - champion_perf["candidate_avg_return"],
            4,
        )
        if chosen_perf["candidate_avg_return"] >= champion_perf["candidate_avg_return"]:
            route_wins_vs_champion += 1
        if prediction_correct:
            correct_pred_days += 1
            correct_pred_route_return += chosen_perf["candidate_avg_return"]
            correct_pred_champion_return += champion_perf["candidate_avg_return"]
        else:
            incorrect_pred_days += 1
            incorrect_pred_route_return += chosen_perf["candidate_avg_return"]
            incorrect_pred_champion_return += champion_perf["candidate_avg_return"]
            if selected_label == "attack":
                wrong_attack_switch_days += 1
                wrong_attack_switch_return_gap_sum += selected_vs_champion_return_gap
        records.append(
            {
                "as_of_trade_date": history_slice[-1]["trade_date"],
                "target_trade_date": target_day["trade_date"],
                "predicted_state": prediction["predicted_state"],
                "actual_state": actual_state,
                "prediction_correct": prediction_correct,
                "route_action": routing["route_action"],
                "selected_template_label": selected_label,
                "selected_template_name": templates[selected_label]["template_name"],
                "chosen_performance": chosen_perf,
                "champion_performance": champion_perf,
                "attack_performance": attack_perf,
                "selected_vs_champion_return_gap": selected_vs_champion_return_gap,
            }
        )
    total = len(records)
    return {
        "summary": {
            "sample_days": total,
            "route_win_rate_vs_champion": round(route_wins_vs_champion / total, 4) if total else 0.0,
            "route_avg_return": round(route_return_sum / total, 4) if total else 0.0,
            "champion_avg_return": round(champion_return_sum / total, 4) if total else 0.0,
            "attack_avg_return": round(attack_return_sum / total, 4) if total else 0.0,
            "route_return_edge_vs_champion": round((route_return_sum - champion_return_sum) / total, 4) if total else 0.0,
            "correct_prediction_days": correct_pred_days,
            "correct_prediction_route_return": round(correct_pred_route_return / correct_pred_days, 4) if correct_pred_days else 0.0,
            "correct_prediction_champion_return": round(correct_pred_champion_return / correct_pred_days, 4) if correct_pred_days else 0.0,
            "incorrect_prediction_days": incorrect_pred_days,
            "incorrect_prediction_route_return": round(incorrect_pred_route_return / incorrect_pred_days, 4) if incorrect_pred_days else 0.0,
            "incorrect_prediction_champion_return": round(incorrect_pred_champion_return / incorrect_pred_days, 4) if incorrect_pred_days else 0.0,
            "wrong_attack_switch_days": wrong_attack_switch_days,
            "wrong_attack_switch_return_gap": round(wrong_attack_switch_return_gap_sum / wrong_attack_switch_days, 4) if wrong_attack_switch_days else 0.0,
        },
        "records": records,
    }


def backtest_v3_direct_routing(
    dataset: list[dict[str, Any]],
    state_history: list[dict[str, Any]],
    templates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if "attack" not in templates:
        return {"summary": {"sample_days": 0}, "records": []}

    state_by_date = {item["trade_date"]: item for item in state_history}
    records: list[dict[str, Any]] = []
    route_wins_vs_champion = 0
    route_return_sum = 0.0
    champion_return_sum = 0.0

    for idx in range(6, len(dataset) - 1):
        history_slice = dataset[: idx + 1]
        target_day = dataset[idx + 1]
        state_slice = state_history[: idx + 1]
        state_by_date_slice = {item["trade_date"]: item for item in state_slice}
        template_state_scores = {
            label: evaluate_template_by_state(history_slice, state_by_date_slice, item["params"], item["negative_veto"])
            for label, item in templates.items()
        }
        template_state_scorecards = build_template_state_scorecards(template_state_scores)["templates"]
        template_advantage_report = build_template_advantage_report(template_state_scorecards, templates)
        v3_route = build_v3_direct_route_signal(state_slice[-1], templates, template_advantage_report)
        selected_label = _select_template_label_from_route(str(v3_route["route_action"]))
        chosen_perf = _evaluate_template_on_target(history_slice, target_day, templates[selected_label])
        champion_perf = _evaluate_template_on_target(history_slice, target_day, templates["champion"])
        route_return_sum += chosen_perf["candidate_avg_return"]
        champion_return_sum += champion_perf["candidate_avg_return"]
        if chosen_perf["candidate_avg_return"] >= champion_perf["candidate_avg_return"]:
            route_wins_vs_champion += 1
        records.append(
            {
                "as_of_trade_date": history_slice[-1]["trade_date"],
                "target_trade_date": target_day["trade_date"],
                "current_state": state_slice[-1]["state_label_v2"],
                "actual_state": state_by_date[target_day["trade_date"]]["state_label_v2"],
                "route_action": v3_route["route_action"],
                "selected_template_label": selected_label,
                "direct_gap": v3_route["direct_gap"],
                "direct_confidence": v3_route["direct_confidence"],
                "selected_vs_champion_return_gap": round(
                    chosen_perf["candidate_avg_return"] - champion_perf["candidate_avg_return"],
                    4,
                ),
            }
        )
    total = len(records)
    return {
        "summary": {
            "sample_days": total,
            "route_win_rate_vs_champion": round(route_wins_vs_champion / total, 4) if total else 0.0,
            "route_avg_return": round(route_return_sum / total, 4) if total else 0.0,
            "champion_avg_return": round(champion_return_sum / total, 4) if total else 0.0,
            "route_return_edge_vs_champion": round((route_return_sum - champion_return_sum) / total, 4) if total else 0.0,
        },
        "records": records,
    }


def backtest_v31_light_routing(
    dataset: list[dict[str, Any]],
    state_history: list[dict[str, Any]],
    templates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if "attack" not in templates:
        return {"summary": {"sample_days": 0}, "records": []}

    state_by_date = {item["trade_date"]: item for item in state_history}
    records: list[dict[str, Any]] = []
    route_wins_vs_champion = 0
    route_return_sum = 0.0
    champion_return_sum = 0.0

    for idx in range(6, len(dataset) - 1):
        history_slice = dataset[: idx + 1]
        target_day = dataset[idx + 1]
        state_slice = state_history[: idx + 1]
        state_by_date_slice = {item["trade_date"]: item for item in state_slice}
        template_state_scores = {
            label: evaluate_template_by_state(history_slice, state_by_date_slice, item["params"], item["negative_veto"])
            for label, item in templates.items()
        }
        template_state_scorecards = build_template_state_scorecards(template_state_scores)["templates"]
        template_advantage_report = build_template_advantage_report(template_state_scorecards, templates)
        v31_route = build_v31_light_route_signal(state_slice[-1], templates, template_advantage_report)
        selected_label = _select_template_label_from_route(str(v31_route["route_action"]))
        chosen_perf = _evaluate_template_on_target(history_slice, target_day, templates[selected_label])
        champion_perf = _evaluate_template_on_target(history_slice, target_day, templates["champion"])
        route_return_sum += chosen_perf["candidate_avg_return"]
        champion_return_sum += champion_perf["candidate_avg_return"]
        if chosen_perf["candidate_avg_return"] >= champion_perf["candidate_avg_return"]:
            route_wins_vs_champion += 1
        records.append(
            {
                "as_of_trade_date": history_slice[-1]["trade_date"],
                "target_trade_date": target_day["trade_date"],
                "current_state": state_slice[-1]["state_label_v2"],
                "actual_state": state_by_date[target_day["trade_date"]]["state_label_v2"],
                "route_action": v31_route["route_action"],
                "selected_template_label": selected_label,
                "direct_gap": v31_route["direct_gap"],
                "direct_confidence": v31_route["direct_confidence"],
                "attack_probability": v31_route["attack_probability"],
                "selected_vs_champion_return_gap": round(
                    chosen_perf["candidate_avg_return"] - champion_perf["candidate_avg_return"],
                    4,
                ),
            }
        )
    total = len(records)
    return {
        "summary": {
            "sample_days": total,
            "route_win_rate_vs_champion": round(route_wins_vs_champion / total, 4) if total else 0.0,
            "route_avg_return": round(route_return_sum / total, 4) if total else 0.0,
            "champion_avg_return": round(champion_return_sum / total, 4) if total else 0.0,
            "route_return_edge_vs_champion": round((route_return_sum - champion_return_sum) / total, 4) if total else 0.0,
        },
        "records": records,
    }


def backtest_v32_calibrated_routing(
    dataset: list[dict[str, Any]],
    state_history: list[dict[str, Any]],
    templates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if "attack" not in templates:
        return {"summary": {"sample_days": 0}, "records": []}

    state_by_date = {item["trade_date"]: item for item in state_history}
    records: list[dict[str, Any]] = []
    route_wins_vs_champion = 0
    route_return_sum = 0.0
    champion_return_sum = 0.0

    for idx in range(6, len(dataset) - 1):
        history_slice = dataset[: idx + 1]
        target_day = dataset[idx + 1]
        state_slice = state_history[: idx + 1]
        state_by_date_slice = {item["trade_date"]: item for item in state_slice}
        template_state_scores = {
            label: evaluate_template_by_state(history_slice, state_by_date_slice, item["params"], item["negative_veto"])
            for label, item in templates.items()
        }
        template_state_scorecards = build_template_state_scorecards(template_state_scores)["templates"]
        template_advantage_report = build_template_advantage_report(template_state_scorecards, templates)
        v32_route = build_v32_calibrated_route_signal(state_slice[-1], templates, template_advantage_report)
        selected_label = _select_template_label_from_route(str(v32_route["route_action"]))
        chosen_perf = _evaluate_template_on_target(history_slice, target_day, templates[selected_label])
        champion_perf = _evaluate_template_on_target(history_slice, target_day, templates["champion"])
        route_return_sum += chosen_perf["candidate_avg_return"]
        champion_return_sum += champion_perf["candidate_avg_return"]
        if chosen_perf["candidate_avg_return"] >= champion_perf["candidate_avg_return"]:
            route_wins_vs_champion += 1
        records.append(
            {
                "as_of_trade_date": history_slice[-1]["trade_date"],
                "target_trade_date": target_day["trade_date"],
                "current_state": state_slice[-1]["state_label_v2"],
                "actual_state": state_by_date[target_day["trade_date"]]["state_label_v2"],
                "route_action": v32_route["route_action"],
                "selected_template_label": selected_label,
                "direct_gap": v32_route["direct_gap"],
                "direct_confidence": v32_route["direct_confidence"],
                "attack_probability": v32_route["attack_probability"],
                "base_route_action": v32_route["base_route_action"],
                "selected_vs_champion_return_gap": round(
                    chosen_perf["candidate_avg_return"] - champion_perf["candidate_avg_return"],
                    4,
                ),
            }
        )
    total = len(records)
    return {
        "summary": {
            "sample_days": total,
            "route_win_rate_vs_champion": round(route_wins_vs_champion / total, 4) if total else 0.0,
            "route_avg_return": round(route_return_sum / total, 4) if total else 0.0,
            "champion_avg_return": round(champion_return_sum / total, 4) if total else 0.0,
            "route_return_edge_vs_champion": round((route_return_sum - champion_return_sum) / total, 4) if total else 0.0,
        },
        "records": records,
    }


def predict_next_state(state_history: list[dict[str, Any]], transition_payload: dict[str, Any]) -> dict[str, Any]:
    current = state_history[-1]
    current_state = str(current["state_label_v2"])
    base_row = dict(transition_payload.get("transition_matrix", {}).get(current_state, {}))
    weighted_row = dict(transition_payload.get("weighted_transition_matrix", {}).get(current_state, {}))
    rolling_row = dict(transition_payload.get("rolling_transition_matrix", {}).get(current_state, {}))
    base_support = sum(transition_payload.get("transition_counts", {}).get(current_state, {}).values())
    rolling_support = sum(transition_payload.get("rolling_transition_counts", {}).get(current_state, {}).values())
    adaptive_recent_weight = 0.35 if base_support >= 8 else 0.45
    adaptive_rolling_weight = 0.35 if rolling_support >= 3 else 0.15
    adaptive_base_weight = max(0.1, 1.0 - adaptive_recent_weight - adaptive_rolling_weight)
    distribution = _blend_rows(
        [
            (base_row, adaptive_base_weight),
            (weighted_row, adaptive_recent_weight),
            (rolling_row, adaptive_rolling_weight),
        ]
    )
    if not distribution:
        counts = Counter(str(item["state_label_v2"]) for item in state_history)
        total = sum(counts.values()) or 1
        distribution = {key: round(value / total, 4) for key, value in counts.items()}
    recent_states = [str(item["state_label_v2"]) for item in state_history[-5:]]
    adjusted_distribution, prior_diagnostics = _apply_state_prior_penalties(current_state, distribution, recent_states)
    sorted_states = sorted(adjusted_distribution.items(), key=lambda item: item[1], reverse=True)
    top_state, top_prob = sorted_states[0]
    second_prob = sorted_states[1][1] if len(sorted_states) > 1 else 0.0
    recent_support_states = recent_states[-3:]
    same_cluster_count = sum(
        1 for state in recent_support_states if _state_cluster_v2(state) == _state_cluster_v2(top_state)
    )
    confidence_raw = top_prob - second_prob + top_prob * 0.5 + same_cluster_count * 0.05
    confidence = round(max(0.0, min(1.0, confidence_raw)), 4)
    return {
        "current_state": current_state,
        "distribution": adjusted_distribution,
        "raw_distribution": distribution,
        "predicted_state": top_state,
        "predicted_probability": top_prob,
        "confidence": confidence,
        "attack_bias": current["attack_score"],
        "stability_bias": current["stability_score"],
        "recent_states": recent_support_states,
        "same_cluster_support_count": same_cluster_count,
        "transition_mode": "adaptive_rolling_weighted",
        "transition_weights": {
            "base_weight": round(adaptive_base_weight, 4),
            "recent_weight": round(adaptive_recent_weight, 4),
            "rolling_weight": round(adaptive_rolling_weight, 4),
            "base_support": base_support,
            "rolling_support": rolling_support,
        },
        "prior_diagnostics": prior_diagnostics,
    }


def build_routing_suggestion(
    prediction: dict[str, Any],
    template_state_scores: dict[str, dict[str, Any]],
    templates: dict[str, dict[str, Any]],
    template_advantage_report: dict[str, Any],
) -> dict[str, Any]:
    champion = templates["champion"]
    attack = templates.get("attack")
    predicted_state = prediction["predicted_state"]
    confidence = float(prediction["confidence"])
    attack_score = float(prediction["attack_bias"])
    stability_score = float(prediction["stability_bias"])
    recent_states = list(prediction.get("recent_states", []))
    same_cluster_support_count = int(prediction.get("same_cluster_support_count", 0))
    attack_metrics = template_state_scores.get("attack", {}).get(predicted_state, {}) if attack else {}
    champion_metrics = template_state_scores.get("champion", {}).get(predicted_state, {})
    advantage_row = next(
        (item for item in template_advantage_report.get("rows", []) if item["state"] == predicted_state),
        {},
    )
    min_state_sample_days = int(ROUTING_RULES["attack_switch"]["min_state_sample_days"])
    attack_candidate_ready = bool(
        attack
        and predicted_state in {"front_dominant", "broad_expansion"}
        and attack_metrics
        and champion_metrics
        and int(advantage_row.get("attack_days", 0)) >= min_state_sample_days
        and int(advantage_row.get("champion_days", 0)) >= min_state_sample_days
        and attack_metrics.get("candidate_avg_return", 0.0)
        >= champion_metrics.get("candidate_avg_return", 0.0) + float(ROUTING_RULES["attack_switch"]["min_return_edge"])
        and attack_metrics.get("front_shift_score", 0.0)
        >= champion_metrics.get("front_shift_score", 0.0) + float(ROUTING_RULES["attack_switch"]["min_front_shift_edge"])
    )
    buffer_pass = bool(
        confidence >= float(ROUTING_RULES["attack_switch"]["min_confidence"])
        and same_cluster_support_count >= int(ROUTING_RULES["attack_switch"]["min_same_cluster_support_count"])
        and attack_score >= stability_score - float(ROUTING_RULES["attack_switch"]["max_attack_vs_stability_gap"])
    )

    if (
        attack_candidate_ready
        and buffer_pass
    ):
        return {
            "route_action": "prefer_attack_template",
            "primary_template": attack["template_name"],
            "shadow_template": champion["template_name"],
            "reason": "预测下一交易日偏强进攻结构，且进攻模板在该状态的收益侧不弱于冠军模板。",
            "buffer_diagnostics": {
                "buffer_pass": True,
                "recent_states": recent_states,
                "same_cluster_support_count": same_cluster_support_count,
                "confidence": confidence,
                "predicted_state_sample_reliability": advantage_row.get("sample_reliability"),
            },
        }

    if attack_candidate_ready:
        return {
            "route_action": "champion_with_attack_shadow",
            "primary_template": champion["template_name"],
            "shadow_template": attack["template_name"],
            "reason": "虽然预测偏进攻结构，但连续状态支持或置信度仍不足，先不直接切主模板。",
            "buffer_diagnostics": {
                "buffer_pass": False,
                "recent_states": recent_states,
                "same_cluster_support_count": same_cluster_support_count,
                "confidence": confidence,
                "predicted_state_sample_reliability": advantage_row.get("sample_reliability"),
            },
        }

    if (
        attack
        and predicted_state in set(ROUTING_RULES["balanced_shadow"]["states"])
        and confidence >= float(ROUTING_RULES["balanced_shadow"]["min_confidence"])
    ):
        return {
            "route_action": "champion_with_attack_shadow",
            "primary_template": champion["template_name"],
            "shadow_template": attack["template_name"],
            "reason": "预测处于过渡/轮动热度区，先以冠军模板稳态主导，同时保留进攻模板影子观察。",
            "buffer_diagnostics": {
                "buffer_pass": False,
                "recent_states": recent_states,
                "same_cluster_support_count": same_cluster_support_count,
                "confidence": confidence,
                "predicted_state_sample_reliability": advantage_row.get("sample_reliability"),
            },
        }

    return {
        "route_action": "prefer_champion_template",
        "primary_template": champion["template_name"],
        "shadow_template": attack["template_name"] if attack else None,
        "reason": "预测稳定性优先或置信度不足，继续以冠军模板作为主路由。",
        "buffer_diagnostics": {
            "buffer_pass": False,
            "recent_states": recent_states,
            "same_cluster_support_count": same_cluster_support_count,
            "confidence": confidence,
            "predicted_state_sample_reliability": advantage_row.get("sample_reliability"),
        },
    }


def _markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Market State V2")
    lines.append("")
    lines.append("## Current State")
    lines.append("")
    current = payload["current_state"]
    lines.append(f"- Trade date: `{current['trade_date']}`")
    lines.append(f"- State V2: `{current['state_label_v2']}`")
    lines.append(f"- Cluster: `{current['state_cluster_v2']}`")
    lines.append(f"- Attack score: `{current['attack_score']:.2f}`")
    lines.append(f"- Stability score: `{current['stability_score']:.2f}`")
    lines.append("")
    lines.append("## T+1 Prediction")
    lines.append("")
    pred = payload["prediction"]
    lines.append(f"- Predicted state: `{pred['predicted_state']}`")
    lines.append(f"- Probability: `{pred['predicted_probability']:.2%}`")
    lines.append(f"- Confidence: `{pred['confidence']:.4f}`")
    lines.append(f"- Transition mode: `{pred.get('transition_mode', 'base')}`")
    lines.append(f"- Recent states: `{', '.join(pred.get('recent_states', []))}`")
    weights = pred.get("transition_weights", {})
    if weights:
        lines.append(
            f"- Weights: `base={weights.get('base_weight', 0.0):.2f}, recent={weights.get('recent_weight', 0.0):.2f}, rolling={weights.get('rolling_weight', 0.0):.2f}`"
        )
    prior_diag = pred.get("prior_diagnostics", {})
    if prior_diag.get("penalties"):
        lines.append(f"- Prior penalties: `{prior_diag.get('penalties')}`")
    lines.append("")
    lines.append("## Routing")
    lines.append("")
    routing = payload["routing_suggestion"]
    lines.append(f"- Action: `{routing['route_action']}`")
    lines.append(f"- Primary: `{routing['primary_template']}`")
    if routing.get("shadow_template"):
        lines.append(f"- Shadow: `{routing['shadow_template']}`")
    lines.append(f"- Reason: {routing['reason']}")
    buffer_info = routing.get("buffer_diagnostics", {})
    if buffer_info:
        lines.append(f"- Buffer pass: `{buffer_info.get('buffer_pass')}`")
        lines.append(f"- Same cluster support: `{buffer_info.get('same_cluster_support_count')}`")
        lines.append(f"- Sample reliability: `{buffer_info.get('predicted_state_sample_reliability')}`")
    lines.append("")
    lines.append("## V3 Direct Routing")
    lines.append("")
    v3 = payload.get("v3_direct_routing", {})
    if v3:
        lines.append(f"- Action: `{v3.get('route_action')}`")
        lines.append(f"- Primary: `{v3.get('primary_template')}`")
        if v3.get("shadow_template"):
            lines.append(f"- Shadow: `{v3.get('shadow_template')}`")
        lines.append(f"- Direct gap: `{v3.get('direct_gap', 0.0):.2f}`")
        lines.append(f"- Direct confidence: `{v3.get('direct_confidence', 0.0):.4f}`")
        lines.append(
            f"- Scores: `attack={v3.get('attack_direct_score', 0.0):.2f}, champion={v3.get('champion_direct_score', 0.0):.2f}`"
        )
    lines.append("")
    lines.append("## V3.1 Light Routing")
    lines.append("")
    v31 = payload.get("v31_light_routing", {})
    if v31:
        lines.append(f"- Action: `{v31.get('route_action')}`")
        lines.append(f"- Primary: `{v31.get('primary_template')}`")
        if v31.get("shadow_template"):
            lines.append(f"- Shadow: `{v31.get('shadow_template')}`")
        lines.append(f"- Gap: `{v31.get('direct_gap', 0.0):.2f}`")
        lines.append(f"- Confidence: `{v31.get('direct_confidence', 0.0):.4f}`")
        lines.append(f"- Attack probability: `{v31.get('attack_probability', 0.0):.2%}`")
        lines.append(f"- Reason: {v31.get('summary_reason', '')}")
        attack_features = sorted(
            v31.get("feature_contributions", {}).get("attack", []),
            key=lambda item: float(item.get("contribution", 0.0)),
            reverse=True,
        )[:3]
        champion_features = sorted(
            v31.get("feature_contributions", {}).get("champion", []),
            key=lambda item: float(item.get("contribution", 0.0)),
            reverse=True,
        )[:3]
        if attack_features:
            lines.append(
                "- Attack drivers: "
                + ", ".join(f"`{item['name']}={item['contribution']:.2f}`" for item in attack_features)
            )
        if champion_features:
            lines.append(
                "- Champion drivers: "
                + ", ".join(f"`{item['name']}={item['contribution']:.2f}`" for item in champion_features)
            )
    lines.append("")
    lines.append("## V3.2 Calibrated Routing")
    lines.append("")
    v32 = payload.get("v32_calibrated_routing", {})
    if v32:
        lines.append(f"- Action: `{v32.get('route_action')}`")
        lines.append(f"- Primary: `{v32.get('primary_template')}`")
        if v32.get("shadow_template"):
            lines.append(f"- Shadow: `{v32.get('shadow_template')}`")
        lines.append(f"- Gap: `{v32.get('direct_gap', 0.0):.2f}`")
        lines.append(f"- Confidence: `{v32.get('direct_confidence', 0.0):.4f}`")
        lines.append(f"- Attack probability: `{v32.get('attack_probability', 0.0):.2%}`")
        lines.append(f"- Base action: `{v32.get('base_route_action', '')}`")
        lines.append(f"- Reason: {v32.get('summary_reason', '')}")
        adjustments = v32.get("calibration_adjustments", [])
        if adjustments:
            lines.append(
                "- Adjustments: "
                + ", ".join(f"`{item['side']}:{item['delta']:.2f}` {item['reason']}" for item in adjustments)
            )
    lines.append("")
    lines.append("## Transition Distribution")
    lines.append("")
    for state, prob in sorted(pred["distribution"].items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- `{state}`: `{prob:.2%}`")
    lines.append("")
    lines.append("## Prediction Backtest")
    lines.append("")
    backtest = payload.get("prediction_backtest", {}).get("summary", {})
    lines.append(f"- Accuracy: `{backtest.get('accuracy', 0.0):.2%}`")
    lines.append(f"- High confidence accuracy: `{backtest.get('high_confidence_accuracy', 0.0):.2%}`")
    lines.append(f"- Prediction count: `{backtest.get('prediction_count', 0)}`")
    lines.append("")
    lines.append("## Routing Backtest")
    lines.append("")
    routing_backtest = payload.get("routing_backtest", {}).get("summary", {})
    lines.append(f"- Sample days: `{routing_backtest.get('sample_days', 0)}`")
    lines.append(f"- Route win rate vs champion: `{routing_backtest.get('route_win_rate_vs_champion', 0.0):.2%}`")
    lines.append(f"- Route avg return: `{routing_backtest.get('route_avg_return', 0.0):.2f}%`")
    lines.append(f"- Champion avg return: `{routing_backtest.get('champion_avg_return', 0.0):.2f}%`")
    lines.append(f"- Route return edge: `{routing_backtest.get('route_return_edge_vs_champion', 0.0):.2f}%`")
    lines.append(f"- Correct-pred route return: `{routing_backtest.get('correct_prediction_route_return', 0.0):.2f}%`")
    lines.append(f"- Incorrect-pred route return: `{routing_backtest.get('incorrect_prediction_route_return', 0.0):.2f}%`")
    lines.append(f"- Wrong attack switch gap: `{routing_backtest.get('wrong_attack_switch_return_gap', 0.0):.2f}%`")
    lines.append("")
    lines.append("## V3 Direct Backtest")
    lines.append("")
    v3_backtest = payload.get("v3_direct_backtest", {}).get("summary", {})
    lines.append(f"- Sample days: `{v3_backtest.get('sample_days', 0)}`")
    lines.append(f"- Route win rate vs champion: `{v3_backtest.get('route_win_rate_vs_champion', 0.0):.2%}`")
    lines.append(f"- Route avg return: `{v3_backtest.get('route_avg_return', 0.0):.2f}%`")
    lines.append(f"- Champion avg return: `{v3_backtest.get('champion_avg_return', 0.0):.2f}%`")
    lines.append(f"- Route return edge: `{v3_backtest.get('route_return_edge_vs_champion', 0.0):.2f}%`")
    lines.append("")
    lines.append("## V3.1 Light Backtest")
    lines.append("")
    v31_backtest = payload.get("v31_light_backtest", {}).get("summary", {})
    lines.append(f"- Sample days: `{v31_backtest.get('sample_days', 0)}`")
    lines.append(f"- Route win rate vs champion: `{v31_backtest.get('route_win_rate_vs_champion', 0.0):.2%}`")
    lines.append(f"- Route avg return: `{v31_backtest.get('route_avg_return', 0.0):.2f}%`")
    lines.append(f"- Champion avg return: `{v31_backtest.get('champion_avg_return', 0.0):.2f}%`")
    lines.append(f"- Route return edge: `{v31_backtest.get('route_return_edge_vs_champion', 0.0):.2f}%`")
    lines.append("")
    lines.append("## V3.2 Calibrated Backtest")
    lines.append("")
    v32_backtest = payload.get("v32_calibrated_backtest", {}).get("summary", {})
    lines.append(f"- Sample days: `{v32_backtest.get('sample_days', 0)}`")
    lines.append(f"- Route win rate vs champion: `{v32_backtest.get('route_win_rate_vs_champion', 0.0):.2%}`")
    lines.append(f"- Route avg return: `{v32_backtest.get('route_avg_return', 0.0):.2f}%`")
    lines.append(f"- Champion avg return: `{v32_backtest.get('champion_avg_return', 0.0):.2f}%`")
    lines.append(f"- Route return edge: `{v32_backtest.get('route_return_edge_vs_champion', 0.0):.2f}%`")
    lines.append("")
    lines.append("## Template Scorecards")
    lines.append("")
    for label, scorecard in payload.get("template_state_scorecards", {}).items():
        lines.append(f"- `{label}`")
        for state, metrics in sorted(scorecard.items()):
            lines.append(
                f"  - `{state}` score={metrics['state_score']:.2f}, "
                f"ret={metrics['candidate_avg_return']:.2f}%, top50={metrics['top50_hit_rate']:.2%}, "
                f"front={metrics['front_shift_score']:.2%}, role={metrics['role_hint']}"
            )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build state V2 profiles and T+1 routing suggestion.")
    parser.add_argument(
        "--registry",
        type=Path,
        default=distill.TEMPLATES_ROOT / "combined_template_registry.json",
        help="冠军模板注册表路径",
    )
    parser.add_argument(
        "--challenger-artifact",
        type=Path,
        default=ARTIFACTS_ROOT / "state_aware_template_evolution_latest.json",
        help="状态感知模板进化产物路径",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset = distill.load_rankings()
    state_history = build_state_profiles_v2(dataset)
    state_by_date = {item["trade_date"]: item for item in state_history}
    transition_payload = build_transition_payload(state_history)
    transition_diagnostics = build_transition_diagnostics(transition_payload)
    prediction = predict_next_state(state_history, transition_payload)
    prediction_backtest = backtest_state_predictions(state_history)
    templates = _load_templates(args.registry, args.challenger_artifact)
    template_state_scores = {
        label: evaluate_template_by_state(dataset, state_by_date, item["params"], item["negative_veto"])
        for label, item in templates.items()
    }
    template_state_scorecard_payload = build_template_state_scorecards(template_state_scores)
    template_state_scorecards = template_state_scorecard_payload["templates"]
    template_advantage_report = build_template_advantage_report(template_state_scorecards, templates)
    routing = build_routing_suggestion(prediction, template_state_scores, templates, template_advantage_report)
    v3_direct_routing = build_v3_direct_route_signal(state_history[-1], templates, template_advantage_report)
    v31_light_routing = build_v31_light_route_signal(state_history[-1], templates, template_advantage_report)
    v32_calibrated_routing = build_v32_calibrated_route_signal(state_history[-1], templates, template_advantage_report)
    routing_backtest = backtest_routing_strategy(dataset, state_history, templates)
    v3_direct_backtest = backtest_v3_direct_routing(dataset, state_history, templates)
    v31_light_backtest = backtest_v31_light_routing(dataset, state_history, templates)
    v32_calibrated_backtest = backtest_v32_calibrated_routing(dataset, state_history, templates)
    payload = {
        "current_state": state_history[-1],
        "state_history": state_history,
        "transition": transition_payload,
        "transition_diagnostics": transition_diagnostics,
        "prediction": prediction,
        "prediction_backtest": prediction_backtest,
        "routing_backtest": routing_backtest,
        "routing_rules": ROUTING_RULES,
        "templates": templates,
        "template_state_scores": template_state_scores,
        "template_state_scorecards": template_state_scorecards,
        "template_advantage_report": template_advantage_report,
        "routing_suggestion": routing,
        "v3_direct_routing": v3_direct_routing,
        "v3_direct_backtest": v3_direct_backtest,
        "v31_light_routing": v31_light_routing,
        "v31_light_backtest": v31_light_backtest,
        "v32_calibrated_routing": v32_calibrated_routing,
        "v32_calibrated_backtest": v32_calibrated_backtest,
    }
    _write_json(STATE_V2_JSON, payload)
    _write_json(
        PREDICTION_JSON,
        {
            "prediction": prediction,
            "prediction_backtest": prediction_backtest,
            "routing_backtest": routing_backtest,
            "routing_rules": ROUTING_RULES,
            "routing_suggestion": routing,
            "v3_direct_routing": v3_direct_routing,
            "v3_direct_backtest": v3_direct_backtest,
            "v31_light_routing": v31_light_routing,
            "v31_light_backtest": v31_light_backtest,
            "v32_calibrated_routing": v32_calibrated_routing,
            "v32_calibrated_backtest": v32_calibrated_backtest,
        },
    )
    _write_json(
        SCORECARD_JSON,
        {
            "templates": templates,
            "template_state_scorecards": template_state_scorecards,
            "template_advantage_report": template_advantage_report,
            "v31_light_routing": v31_light_routing,
            "v31_light_backtest": v31_light_backtest,
            "v32_calibrated_routing": v32_calibrated_routing,
            "v32_calibrated_backtest": v32_calibrated_backtest,
        },
    )
    STATE_V2_MD.write_text(_markdown(payload), encoding="utf-8")
    print(str(STATE_V2_JSON))
    print(str(PREDICTION_JSON))
    print(str(SCORECARD_JSON))
    print(str(STATE_V2_MD))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
