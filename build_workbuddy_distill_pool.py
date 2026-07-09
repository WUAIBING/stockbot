from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from workbuddy_distill.scripts.distill_local_templates import (  # noqa: E402
    build_veto_name,
    load_rankings,
    select_candidates,
)


OUTPUT_ROOT = ROOT / "workbuddy_pool"
REGISTRY_FILE = ROOT / "workbuddy_distill" / "templates" / "combined_template_registry.json"
DAILY_REVIEW_FILE = OUTPUT_ROOT / "workbuddy_distill_daily_review_latest.json"
CHAMPION_EVOLUTION_FILE = ROOT / "workbuddy_distill" / "artifacts" / "distill_champion_evolution_latest.json"
STATE_AWARE_EVOLUTION_FILE = ROOT / "workbuddy_distill" / "artifacts" / "state_aware_template_evolution_latest.json"
OUTPUT_JSON = OUTPUT_ROOT / "workbuddy_distill_candidate_pool_latest.json"
OUTPUT_MD = OUTPUT_ROOT / "workbuddy_distill_candidate_pool_latest.md"
MAIN_OUTPUT_JSON = OUTPUT_ROOT / "workbuddy_candidate_pool_latest.json"
MAIN_OUTPUT_MD = OUTPUT_ROOT / "workbuddy_candidate_pool_latest.md"

MAX_USER_FOCUS = 8
MAX_PRIMARY = 10
MAX_ROTATION = 8
MAX_SELECTED = 5
CHAMPION_TEMPLATE_BONUS = 48.0
CHAMPION_POSITION_BONUS = 3.0
SCORING_VERSION = "workbuddy_distill_pool_v3"

DEFAULT_SCORING_PROFILE = {
    "profile_name": "guardrail_v1",
    "hot_pct_threshold": 19.5,
    "hot_rank_cutoff": 8,
    "head_rank_cutoff": 3,
    "extreme_pct_threshold": 20.0,
    "extreme_rank_cutoff": 3,
    "heat_pct_penalty_per_pct": 0.35,
    "heat_rank_penalty_step": 0.6,
    "champion_heat_bonus": 0.8,
    "hot_cluster_safe_count": 4,
    "hot_cluster_penalty_step": 1.5,
    "rotation_penalty_threshold": 999.0,
    "observe_penalty_threshold": 999.0,
    "enable_execution_gap_penalty": True,
    "execution_gap_penalty_max": 2.0,
}

LEGACY_SCORING_PROFILE = {
    "profile_name": "legacy_v2",
    "hot_pct_threshold": 999.0,
    "hot_rank_cutoff": 0,
    "head_rank_cutoff": 0,
    "extreme_pct_threshold": 999.0,
    "extreme_rank_cutoff": 0,
    "heat_pct_penalty_per_pct": 0.0,
    "heat_rank_penalty_step": 0.0,
    "champion_heat_bonus": 0.0,
    "hot_cluster_safe_count": 99,
    "hot_cluster_penalty_step": 0.0,
    "rotation_penalty_threshold": 999.0,
    "observe_penalty_threshold": 999.0,
    "enable_execution_gap_penalty": False,
    "execution_gap_penalty_max": 0.0,
}


def load_registry() -> dict[str, Any]:
    payload = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    templates = payload.get("templates", [])
    if not isinstance(templates, list) or not templates:
        fallback = _fallback_registry_from_artifacts()
        if fallback:
            return fallback
        raise RuntimeError("combined_template_registry.json 缺少可用模板")
    return payload


def default_scoring_profile() -> dict[str, Any]:
    return dict(DEFAULT_SCORING_PROFILE)


def legacy_scoring_profile() -> dict[str, Any]:
    return dict(LEGACY_SCORING_PROFILE)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _profitability_priority_score(
    *,
    candidate_win_rate: float,
    candidate_avg_return: float,
    business_score: float = 0.0,
) -> float:
    return round(
        candidate_win_rate * 100
        + candidate_avg_return * 16
        + business_score * 0.18,
        4,
    )


def _compute_template_weight(metrics: dict[str, Any]) -> float:
    profit_priority_score = _safe_float(metrics.get("profit_priority_score"), 0.0)
    if profit_priority_score <= 0:
        profit_priority_score = _profitability_priority_score(
            candidate_win_rate=_safe_float(metrics.get("candidate_win_rate"), 0.0),
            candidate_avg_return=_safe_float(metrics.get("candidate_avg_return"), 0.0),
            business_score=_safe_float(metrics.get("business_score"), 0.0),
        )
    return round(
        profit_priority_score * 0.75
        + _safe_float(metrics.get("business_score"), 0.0) * 0.25
        + _safe_float(metrics.get("top100_hit_rate"), 0.0) * 8.0,
        4,
    )


def _rank_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item["classification"] != "primary",
        -item.get("avg_profitability_priority", 0.0),
        -item["selection_score"],
        -item["avg_candidate_avg_return"],
        -item["avg_candidate_win_rate"],
        -item["champion_hits"],
        -item["raw_selection_score"],
        -item["template_hits"],
        -(item["avg_business_score"]),
        item["latest_rank"] or 999999,
        item["code"],
    )


def _hot_crowding_priority_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -item.get("avg_profitability_priority", 0.0),
        -item.get("avg_candidate_avg_return", 0.0),
        -item.get("avg_candidate_win_rate", 0.0),
        -item.get("raw_selection_score", 0.0),
        -item.get("champion_hits", 0),
        item.get("latest_rank") or 999999,
        item["code"],
    )


def _read_json_if_exists(file_path: Path) -> dict[str, Any]:
    if not file_path.exists():
        return {}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _normalize_template_entry(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    params = item.get("params")
    metrics = item.get("metrics")
    if not isinstance(params, dict) or not isinstance(metrics, dict):
        return None
    return {
        "template_name": item.get("template_name"),
        "base_template_name": item.get("base_template_name"),
        "params": params,
        "negative_veto": item.get("negative_veto"),
        "metrics": metrics,
    }


def _fallback_registry_from_artifacts() -> dict[str, Any] | None:
    sources = [
        _read_json_if_exists(CHAMPION_EVOLUTION_FILE),
        _read_json_if_exists(STATE_AWARE_EVOLUTION_FILE),
    ]
    for payload in sources:
        incumbent = _normalize_template_entry(payload.get("incumbent"))
        if not incumbent:
            continue
        candidates = [incumbent]
        for key in ("best_overall",):
            candidate = _normalize_template_entry(payload.get(key))
            if candidate and candidate["template_name"] != incumbent["template_name"]:
                candidates.append(candidate)
        for item in payload.get("top_observed", []):
            candidate = _normalize_template_entry(item)
            if not candidate:
                continue
            if any(existing["template_name"] == candidate["template_name"] for existing in candidates):
                continue
            candidates.append(candidate)
            if len(candidates) >= 2:
                break
        window = payload.get("window", {})
        return {
            "version": payload.get("version", "workbuddy_distill_fallback_v1"),
            "window": window if isinstance(window, dict) else {},
            "champion_template_name": incumbent["template_name"],
            "champion_template": incumbent,
            "templates": candidates,
        }
    return None


def _normalize_security_name(name: Any) -> str:
    return str(name or "").strip().replace(" ", "").upper()


def _is_risk_warning_name(name: Any) -> bool:
    text = _normalize_security_name(name)
    if not text:
        return False
    prefixes = ("ST", "*ST", "S*ST", "SST")
    return any(text.startswith(prefix) for prefix in prefixes)


def _is_risk_warning_candidate(candidate: dict[str, Any], latest_row: dict[str, Any] | None = None) -> bool:
    latest_row = latest_row or {}
    if _is_risk_warning_name(candidate.get("name")) or _is_risk_warning_name(latest_row.get("name")):
        return True
    truthy_values = {"1", "true", "yes", "y"}
    for field in ("risk_warning", "special_treatment", "is_st"):
        candidate_value = str(candidate.get(field, "")).strip().lower()
        latest_value = str(latest_row.get(field, "")).strip().lower()
        if candidate_value in truthy_values or latest_value in truthy_values:
            return True
    return False


def _champion_bonus(position: int) -> float:
    return CHAMPION_TEMPLATE_BONUS + max(0, MAX_SELECTED + 1 - position) * CHAMPION_POSITION_BONUS


def _resolve_scoring_profile(scoring_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = default_scoring_profile()
    if scoring_profile:
        merged.update(scoring_profile)
    return merged


def _load_execution_gap_context(file_path: Path = DAILY_REVIEW_FILE) -> dict[str, Any]:
    context = {
        "available": False,
        "forward_win_rate_pct": 0.0,
        "actual_win_rate_pct": 0.0,
        "forward_avg_return_pct": 0.0,
        "actual_avg_return_pct": 0.0,
        "win_rate_gap_pct": 0.0,
        "avg_return_gap_pct": 0.0,
        "penalty_scale": 0.0,
    }
    if not file_path.exists():
        return context
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return context
    forward = payload.get("forward_validation", {})
    actual = payload.get("actual_execution", {})
    forward_win = _safe_float(forward.get("win_rate_pct"), 0.0)
    actual_win = _safe_float(
        actual.get("closed_trade_win_rate_pct", actual.get("win_rate_pct")),
        0.0,
    )
    forward_ret = _safe_float(forward.get("avg_return_pct"), 0.0)
    actual_ret = _safe_float(
        actual.get("avg_closed_return_pct", actual.get("avg_return_pct")),
        0.0,
    )
    win_gap = max(forward_win - actual_win, 0.0)
    return_gap = max(forward_ret - actual_ret, 0.0)
    penalty_scale = _clamp(win_gap / 20.0 + return_gap / 3.0, 0.0, 1.0)
    return {
        "available": True,
        "forward_win_rate_pct": round(forward_win, 4),
        "actual_win_rate_pct": round(actual_win, 4),
        "forward_avg_return_pct": round(forward_ret, 4),
        "actual_avg_return_pct": round(actual_ret, 4),
        "win_rate_gap_pct": round(win_gap, 4),
        "avg_return_gap_pct": round(return_gap, 4),
        "penalty_scale": round(penalty_scale, 4),
    }


def _build_heat_profile(item: dict[str, Any], scoring_profile: dict[str, Any]) -> dict[str, Any]:
    latest_rank = item.get("latest_rank")
    latest_pct = _safe_float(item.get("latest_pct_change"), 0.0)
    is_hot = bool(
        latest_rank
        and latest_rank <= scoring_profile["hot_rank_cutoff"]
        and latest_pct >= scoring_profile["hot_pct_threshold"]
    )
    pct_penalty = 0.0
    rank_penalty = 0.0
    if is_hot:
        pct_penalty = (latest_pct - scoring_profile["hot_pct_threshold"]) * scoring_profile["heat_pct_penalty_per_pct"]
        if latest_rank <= scoring_profile["head_rank_cutoff"]:
            rank_penalty = (scoring_profile["head_rank_cutoff"] + 1 - latest_rank) * scoring_profile["heat_rank_penalty_step"]
    champion_heat_penalty = scoring_profile["champion_heat_bonus"] if is_hot and item["champion_hits"] else 0.0
    penalty = round(pct_penalty + rank_penalty + champion_heat_penalty, 4)
    level = "normal"
    if is_hot:
        level = "extreme" if latest_pct >= scoring_profile["extreme_pct_threshold"] and latest_rank <= scoring_profile["extreme_rank_cutoff"] else "warm"
    return {
        "is_hot": is_hot,
        "level": level,
        "pct_penalty": round(pct_penalty, 4),
        "rank_penalty": round(rank_penalty, 4),
        "champion_heat_penalty": round(champion_heat_penalty, 4),
        "penalty": penalty,
    }


def _execution_gap_penalty(
    item: dict[str, Any],
    execution_context: dict[str, Any],
    scoring_profile: dict[str, Any],
) -> float:
    if not scoring_profile.get("enable_execution_gap_penalty"):
        return 0.0
    if not execution_context.get("available") or not execution_context.get("penalty_scale"):
        return 0.0
    if item["champion_hits"] < 1 or not item["heat_profile"]["is_hot"]:
        return 0.0
    multiplier = 1.0 + (0.25 if item["heat_profile"]["level"] == "extreme" else 0.0)
    return round(scoring_profile["execution_gap_penalty_max"] * execution_context["penalty_scale"] * multiplier, 4)


def _apply_guardrails(
    items: list[dict[str, Any]],
    scoring_profile: dict[str, Any],
    execution_context: dict[str, Any],
) -> None:
    hot_items = [item for item in items if item["heat_profile"]["is_hot"]]
    hot_items.sort(key=_hot_crowding_priority_key)
    crowd_map: dict[str, tuple[int, float]] = {}
    for idx, item in enumerate(hot_items, start=1):
        excess = max(0, idx - scoring_profile["hot_cluster_safe_count"])
        crowd_penalty = round(excess * scoring_profile["hot_cluster_penalty_step"], 4)
        crowd_map[item["code"]] = (idx, crowd_penalty)

    for item in items:
        crowd_position, crowd_penalty = crowd_map.get(item["code"], (None, 0.0))
        exec_penalty = _execution_gap_penalty(item, execution_context, scoring_profile)
        total_penalty = round(item["heat_profile"]["penalty"] + crowd_penalty + exec_penalty, 4)
        item["crowding_position"] = crowd_position
        item["crowding_penalty"] = crowd_penalty
        item["execution_gap_penalty"] = exec_penalty
        item["guardrail_penalty_total"] = total_penalty
        item["selection_score"] = round(item["raw_selection_score"] - total_penalty, 4)
        if total_penalty >= scoring_profile["observe_penalty_threshold"]:
            status = "observe_only"
        elif total_penalty >= scoring_profile["rotation_penalty_threshold"]:
            status = "cooled_rotation"
        elif item["heat_profile"]["is_hot"]:
            status = "hot_primary"
        else:
            status = "normal"
        item["guardrail_status"] = status


def _classify_record(item: dict[str, Any], template_count: int, scoring_profile: dict[str, Any]) -> str:
    if item.get("guardrail_penalty_total", 0.0) >= scoring_profile["observe_penalty_threshold"]:
        return "observe_only"
    if item.get("guardrail_penalty_total", 0.0) >= scoring_profile["rotation_penalty_threshold"]:
        return "rotation"
    strong_consensus = item["template_hits"] >= max(3, min(5, template_count // 3))
    decent_consensus = item["template_hits"] >= 2
    strong_rank = item["latest_rank"] and item["latest_rank"] <= 80
    business_ok = item["avg_business_score"] >= 95
    profitability_ok = (
        item.get("avg_profitability_priority", 0.0) >= 90
        or item["avg_candidate_win_rate"] >= 0.55
        or item["avg_candidate_avg_return"] >= 2.0
    )
    if item["champion_hits"] >= 1 and (strong_rank or business_ok or profitability_ok):
        return "primary"
    if strong_consensus and (strong_rank or business_ok or profitability_ok):
        return "primary"
    if decent_consensus or business_ok or profitability_ok:
        return "rotation"
    return "observe_only"


def _is_user_focus_candidate(item: dict[str, Any]) -> bool:
    if item["classification"] == "observe_only":
        return False
    if item["champion_hits"] >= 1:
        return True
    if item["template_hits"] >= 3:
        return True
    latest_rank = item.get("latest_rank")
    return bool(
        latest_rank
        and latest_rank <= 100
        and (
            item["avg_candidate_win_rate"] >= 0.5
            or item["avg_candidate_avg_return"] >= 1.8
            or item.get("avg_profitability_priority", 0.0) >= 85
        )
    )


def _selection_reasons(item: dict[str, Any], template_count: int) -> list[str]:
    reasons = [
        f"入选 {item['template_hits']}/{template_count} 个 promoted 组合模板",
        f"平均 business_score {item['avg_business_score']:.2f}",
        f"平均候选池胜率 {item['avg_candidate_win_rate']:.4f}",
        f"平均候选池收益 {item['avg_candidate_avg_return']:.4f}",
    ]
    if item["champion_hits"]:
        reasons.insert(0, f"命中冠军组合 {item['champion_hits']} 次")
    if item.get("latest_rank"):
        reasons.append(f"最近一日 TDX 排名 {item['latest_rank']}")
    if item.get("latest_pct_change") is not None:
        reasons.append(f"最近一日涨跌幅 {item['latest_pct_change']:.2f}%")
    if item["negative_veto_count"]:
        reasons.append(f"来源组合覆盖 {item['negative_veto_count']} 类反向 veto")
    if item["heat_profile"]["penalty"] > 0:
        reasons.append(
            f"过热降温 heat={item['heat_profile']['penalty']:.2f} ({item['heat_profile']['level']})"
        )
    if item.get("crowding_penalty", 0.0) > 0:
        reasons.append(
            f"拥挤降温 crowd={item['crowding_penalty']:.2f} (hot_cluster_pos={item['crowding_position']})"
        )
    if item.get("execution_gap_penalty", 0.0) > 0:
        reasons.append(f"近期实盘衰减修正 exec_gap={item['execution_gap_penalty']:.2f}")
    if item.get("guardrail_penalty_total", 0.0) > 0:
        reasons.append(
            f"降温后分数 {item['selection_score']:.2f} / 原始 {item['raw_selection_score']:.2f}"
        )
    return reasons


def _record_role(item: dict[str, Any], template_count: int) -> str:
    if item["champion_hits"] >= 1:
        if item["classification"] == "primary":
            return "distill_champion_core"
        if item["classification"] == "rotation":
            return "distill_champion_cooling"
        return "distill_champion_observe"
    if item["template_hits"] >= max(4, template_count // 3):
        return "distill_consensus"
    if item["template_hits"] >= 2:
        return "distill_support"
    return "distill_observe"


def build_payload(
    *,
    scoring_profile: dict[str, Any] | None = None,
    dataset: list[dict[str, Any]] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    registry = registry or load_registry()
    dataset = dataset or load_rankings()
    latest_day = dataset[-1]
    latest_rows = latest_day["row_map"]
    templates = registry["templates"]
    champion_template_name = str(registry.get("champion_template_name") or "")
    champion_template = registry.get("champion_template") or {}
    scoring_profile = _resolve_scoring_profile(scoring_profile)
    execution_context = _load_execution_gap_context()

    items: dict[str, dict[str, Any]] = {}
    veto_watch: dict[str, dict[str, Any]] = {}
    risk_warning_filtered: dict[str, dict[str, Any]] = {}

    for template in templates:
        metrics = template.get("metrics", {})
        candidates, vetoed_codes = select_candidates(dataset, template["params"], template.get("negative_veto"))
        veto_name = build_veto_name(template["negative_veto"])
        is_champion_template = bool(champion_template_name and template["template_name"] == champion_template_name)
        template_weight = _compute_template_weight(metrics)

        for position, candidate in enumerate(candidates, start=1):
            code = candidate.get("code", "")
            name = candidate.get("name", "")
            if not code or not name:
                continue
            row = latest_rows.get(code, {})
            if _is_risk_warning_candidate(candidate, row):
                risk_warning_filtered.setdefault(
                    code,
                    {
                        "code": code,
                        "name": str(row.get("name") or name),
                        "reason": "st_risk_warning_filtered",
                    },
                )
                continue
            latest_rank = _safe_int(row.get("rank"), 0) or None
            latest_pct = _safe_float(row.get("pct_change"), 0.0) if row else None
            latest_close = _safe_float(row.get("close"), 0.0) if row else None
            contribution = round(template_weight + (MAX_SELECTED * 4 - position) * 0.9, 4)

            item = items.setdefault(
                code,
                {
                    "code": code,
                    "name": name,
                    "raw_selection_score": 0.0,
                    "selection_score": 0.0,
                    "template_hits": 0,
                    "source_templates": [],
                    "base_templates": set(),
                    "negative_vetoes": set(),
                    "avg_business_score_values": [],
                    "avg_top100_hit_rate_values": [],
                    "avg_candidate_avg_return_values": [],
                    "avg_candidate_win_rate_values": [],
                    "avg_profitability_priority_values": [],
                    "champion_hits": 0,
                    "champion_score": 0.0,
                    "latest_rank": latest_rank,
                    "latest_pct_change": latest_pct,
                    "latest_close": latest_close,
                },
            )
            item["raw_selection_score"] += contribution
            item["template_hits"] += 1
            item["source_templates"].append(template["template_name"])
            item["base_templates"].add(template["base_template_name"])
            item["negative_vetoes"].add(veto_name)
            item["avg_business_score_values"].append(_safe_float(metrics.get("business_score")))
            item["avg_top100_hit_rate_values"].append(_safe_float(metrics.get("top100_hit_rate")))
            item["avg_candidate_avg_return_values"].append(_safe_float(metrics.get("candidate_avg_return")))
            item["avg_candidate_win_rate_values"].append(_safe_float(metrics.get("candidate_win_rate")))
            item["avg_profitability_priority_values"].append(
                _safe_float(metrics.get("profit_priority_score"), 0.0)
                or _profitability_priority_score(
                    candidate_win_rate=_safe_float(metrics.get("candidate_win_rate"), 0.0),
                    candidate_avg_return=_safe_float(metrics.get("candidate_avg_return"), 0.0),
                    business_score=_safe_float(metrics.get("business_score"), 0.0),
                )
            )
            if is_champion_template:
                item["champion_hits"] += 1
                champion_score = _champion_bonus(position)
                item["champion_score"] += champion_score
                item["raw_selection_score"] += champion_score

        for code in vetoed_codes:
            row = latest_rows.get(code, {})
            if _is_risk_warning_candidate({"code": code, "name": row.get("name", code)}, row):
                risk_warning_filtered.setdefault(
                    code,
                    {
                        "code": code,
                        "name": str(row.get("name", code)),
                        "reason": "st_risk_warning_filtered",
                    },
                )
                continue
            watch = veto_watch.setdefault(
                code,
                {
                    "code": code,
                    "name": str(row.get("name", code)),
                    "veto_hits": 0,
                    "veto_templates": [],
                    "latest_rank": _safe_int(row.get("rank"), 0) or None,
                    "latest_pct_change": _safe_float(row.get("pct_change"), 0.0) if row else None,
                },
            )
            watch["veto_hits"] += 1
            watch["veto_templates"].append(template["template_name"])

    ranked: list[dict[str, Any]] = []
    template_count = len(templates)
    for item in items.values():
        item["raw_selection_score"] = round(item["raw_selection_score"], 4)
        item["champion_score"] = round(item["champion_score"], 4)
        item["base_templates"] = sorted(item["base_templates"])
        item["negative_vetoes"] = sorted(item["negative_vetoes"])
        item["negative_veto_count"] = len(item["negative_vetoes"])
        item["avg_business_score"] = round(_avg(item.pop("avg_business_score_values")), 4)
        item["avg_top100_hit_rate"] = round(_avg(item.pop("avg_top100_hit_rate_values")), 4)
        item["avg_candidate_avg_return"] = round(_avg(item.pop("avg_candidate_avg_return_values")), 4)
        item["avg_candidate_win_rate"] = round(_avg(item.pop("avg_candidate_win_rate_values")), 4)
        item["avg_profitability_priority"] = round(_avg(item.pop("avg_profitability_priority_values")), 4)
        item["template_hit_ratio"] = round(item["template_hits"] / template_count, 4)
        item["heat_profile"] = _build_heat_profile(item, scoring_profile)
        ranked.append(item)

    _apply_guardrails(ranked, scoring_profile, execution_context)
    for item in ranked:
        item["classification"] = _classify_record(item, template_count, scoring_profile)

    ranked.sort(key=_rank_sort_key)

    primary_pool = [item for item in ranked if item["classification"] == "primary"][:MAX_PRIMARY]
    rotation_pool = [item for item in ranked if item["classification"] == "rotation"][:MAX_ROTATION]
    observe_only = [item for item in ranked if item["classification"] == "observe_only"][:MAX_ROTATION]
    user_focus_pool = [item for item in ranked if _is_user_focus_candidate(item)][:MAX_USER_FOCUS]
    selected = user_focus_pool[:MAX_SELECTED] if len(user_focus_pool) >= MAX_SELECTED else ranked[:MAX_SELECTED]
    weight = round(100.0 / len(selected), 2) if selected else 0.0

    selected_records: list[dict[str, Any]] = []
    for idx, item in enumerate(selected, start=1):
        selected_records.append(
            {
                "code": item["code"],
                "name": item["name"],
                "role": _record_role(item, template_count),
                "selection_score": item["selection_score"],
                "raw_selection_score": item["raw_selection_score"],
                "template_hits": item["template_hits"],
                "template_hit_ratio": item["template_hit_ratio"],
                "latest_rank": item["latest_rank"],
                "latest_chg_pct": item["latest_pct_change"],
                "avg_business_score": item["avg_business_score"],
                "avg_top100_hit_rate": item["avg_top100_hit_rate"],
                "avg_candidate_win_rate": item["avg_candidate_win_rate"],
                "avg_candidate_avg_return": item["avg_candidate_avg_return"],
                "avg_profitability_priority": item["avg_profitability_priority"],
                "champion_hits": item["champion_hits"],
                "champion_score": item["champion_score"],
                "negative_veto_count": item["negative_veto_count"],
                "guardrail_status": item["guardrail_status"],
                "guardrail_penalty_total": item["guardrail_penalty_total"],
                "heat_level": item["heat_profile"]["level"],
                "heat_penalty": item["heat_profile"]["penalty"],
                "crowding_penalty": item["crowding_penalty"],
                "crowding_position": item["crowding_position"],
                "execution_gap_penalty": item["execution_gap_penalty"],
                "source_templates": item["source_templates"][:5],
                "base_templates": item["base_templates"],
                "negative_vetoes": item["negative_vetoes"],
                "selection_rank": idx,
                "portfolio_name": "workbuddy",
                "portfolio_type": "distill_challenger",
                "target_weight_pct": weight,
                "learning_candidate_status": "hold",
                "selection_reasons": _selection_reasons(item, template_count),
            }
        )

    veto_watch_pool = sorted(
        veto_watch.values(),
        key=lambda item: (-item["veto_hits"], item["latest_rank"] or 999999, item["code"]),
    )[:10]

    template_registry = [
        {
            "template_name": item["template_name"],
            "base_template_name": item["base_template_name"],
            "negative_veto": build_veto_name(item["negative_veto"]),
            "business_score": item.get("metrics", {}).get("business_score", 0.0),
            "profit_priority_score": item.get("metrics", {}).get("profit_priority_score", 0.0),
            "top100_hit_rate": item.get("metrics", {}).get("top100_hit_rate", 0.0),
            "candidate_win_rate": item.get("metrics", {}).get("candidate_win_rate", 0.0),
            "candidate_avg_return": item.get("metrics", {}).get("candidate_avg_return", 0.0),
        }
        for item in templates
    ]

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": latest_day["trade_date"],
        "portfolio_name": "workbuddy",
        "portfolio_type": "distill_challenger",
        "status": "ok",
        "scoring_version": SCORING_VERSION,
        "scoring_profile": scoring_profile,
        "execution_gap_context": execution_context,
        "source_distill_registry": {
            "file": str(REGISTRY_FILE),
            "window": registry.get("window", {}),
            "promoted_template_count": template_count,
            "champion_template_name": champion_template_name,
        },
        "champion_template": {
            "template_name": champion_template_name,
            "base_template_name": champion_template.get("base_template_name"),
            "negative_veto": build_veto_name(champion_template.get("negative_veto")) if champion_template else None,
            "candidate_win_rate": champion_template.get("metrics", {}).get("candidate_win_rate"),
            "candidate_avg_return": champion_template.get("metrics", {}).get("candidate_avg_return"),
            "top50_hit_rate": champion_template.get("metrics", {}).get("top50_hit_rate"),
            "front_shift_score": champion_template.get("metrics", {}).get("front_shift_score"),
        },
        "selected_count": len(selected_records),
        "candidate_count": len(ranked),
        "risk_warning_filtered_count": len(risk_warning_filtered),
        "notes": [
            "本产物由 promoted 正反向蒸馏组合模板直接投票生成，冠军组合已固化为主链优先模板。",
            "正向模板负责选入，negative veto 负责剔除失败结构；冠军组合命中的候选会获得更高基础权重。",
            "本文件已同步覆盖 workbuddy_candidate_pool_latest.json，作为当前 workbuddy 候选池主输出。",
            "已在候选池构建层过滤 ST/*ST/风险警示证券，避免进入 challenger 买入名单。",
            "v3 新增过热降温、拥挤降温与近期实盘衰减修正，避免冠军模板在 live 环境下过度追逐末端强票。",
        ],
        "selected_records": selected_records,
        "risk_warning_filtered": sorted(risk_warning_filtered.values(), key=lambda item: item["code"]),
        "user_focus_pool": user_focus_pool,
        "primary_pool": primary_pool,
        "rotation_pool": rotation_pool,
        "observe_only": observe_only,
        "veto_watch_pool": veto_watch_pool,
        "template_registry": template_registry,
    }


def _write_markdown(payload: dict[str, Any]) -> None:
    lines = [
        "# WorkBuddy Distill Candidate Pool",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- trade_date: {payload['trade_date']}",
        f"- scoring_version: {payload['scoring_version']}",
        f"- promoted_template_count: {payload['source_distill_registry']['promoted_template_count']}",
        f"- champion_template_name: {payload['source_distill_registry']['champion_template_name']}",
        f"- selected_count: {payload['selected_count']}",
        "",
        "## Selected Records",
        "",
    ]
    for item in payload["selected_records"]:
        lines.append(
            f"- {item['code']} {item['name']} | score={item['selection_score']:.2f} "
            f"(raw={item['raw_selection_score']:.2f}, guardrail={item['guardrail_penalty_total']:.2f}) | "
            f"template_hits={item['template_hits']} | champion_hits={item['champion_hits']} | rank={item['latest_rank']} | "
            f"chg={item['latest_chg_pct']} | guardrail={item['guardrail_status']} | weight={item['target_weight_pct']}"
        )

    lines.extend(["", "## User Focus Pool", ""])
    for item in payload["user_focus_pool"]:
        lines.append(
            f"- {item['code']} {item['name']} | score={item['selection_score']:.2f} | "
            f"hits={item['template_hits']} | avg_return={item['avg_candidate_avg_return']} | "
            f"win_rate={item['avg_candidate_win_rate']} | rank={item['latest_rank']}"
        )

    lines.extend(["", "## Veto Watch Pool", ""])
    for item in payload["veto_watch_pool"]:
        lines.append(
            f"- {item['code']} {item['name']} | veto_hits={item['veto_hits']} | "
            f"latest_rank={item['latest_rank']} | latest_chg_pct={item['latest_pct_change']}"
        )

    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    payload = build_payload()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    MAIN_OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(payload)
    MAIN_OUTPUT_MD.write_text(OUTPUT_MD.read_text(encoding="utf-8"), encoding="utf-8")
    print(
        json.dumps(
            {
                "trade_date": payload["trade_date"],
                "selected_count": payload["selected_count"],
                "candidate_count": payload["candidate_count"],
                "output_json": str(OUTPUT_JSON),
                "main_output_json": str(MAIN_OUTPUT_JSON),
                "output_md": str(OUTPUT_MD),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
