from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import median
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DISTILL_ROOT = ROOT / "workbuddy_distill"
RAW_TOP100_ROOT = DISTILL_ROOT / "raw_top100"
ARTIFACTS_ROOT = DISTILL_ROOT / "artifacts"
TEMPLATES_ROOT = DISTILL_ROOT / "templates"

CANDIDATE_SIZE = 20
CORE_WINDOW_DAYS = 20
BUFFER_WINDOW_DAYS = 5
VERSION = "workbuddy_distill_v2"

THRESHOLDS = {
    "priority": {"top100_hit_rate_min": 0.18, "top30_hit_rate_min": 0.06, "hit_day_rate_min": 0.70},
    "pass": {"top100_hit_rate_min": 0.15, "top30_hit_rate_min": 0.05, "hit_day_rate_min": 0.70},
    "prototype": {"top100_hit_rate_min": 0.10, "top30_hit_rate_min": 0.03, "hit_day_rate_min": 0.60},
}


def read_csv(file_path: Path) -> list[dict[str, str]]:
    with file_path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_rankings() -> list[dict[str, Any]]:
    dataset: list[dict[str, Any]] = []
    for date_dir in sorted(p for p in RAW_TOP100_ROOT.iterdir() if p.is_dir()):
        full_rows = read_csv(date_dir / "full_rank.csv")
        row_map = {row["code"]: row for row in full_rows}
        top100_rows = full_rows[:100]
        top50_rows = full_rows[:50]
        top30_rows = full_rows[:30]
        top10_rows = full_rows[:10]
        dataset.append(
            {
                "trade_date": date_dir.name,
                "full_rows": full_rows,
                "row_map": row_map,
                "row_count": len(full_rows),
                "top100_codes": [row["code"] for row in top100_rows],
                "top50_codes": [row["code"] for row in top50_rows],
                "top30_codes": [row["code"] for row in top30_rows],
                "top10_codes": [row["code"] for row in top10_rows],
            }
        )
    return dataset


def _segment_weight(role: str, core_index: int, core_count: int) -> float:
    if role == "buffer":
        return 1.35
    if core_count <= 0:
        return 1.0
    edge_span = min(5, max(1, core_count // 4))
    if core_index < edge_span:
        return 0.7
    if core_index >= max(0, core_count - edge_span):
        return 1.1
    return 1.0


def build_window_profile(
    dataset: list[dict[str, Any]],
    *,
    core_days: int = CORE_WINDOW_DAYS,
    buffer_days: int = BUFFER_WINDOW_DAYS,
) -> dict[str, Any]:
    if not dataset:
        return {
            "mode": "empty",
            "requested_core_days": core_days,
            "requested_buffer_days": buffer_days,
            "selected_trade_dates": [],
            "core_trade_dates": [],
            "buffer_trade_dates": [],
            "selected_trade_date_count": 0,
        }

    total_available = len(dataset)
    selected_count = min(total_available, core_days + buffer_days)
    selected = dataset[-selected_count:]
    actual_buffer_days = min(buffer_days, max(0, selected_count - core_days))
    actual_core_days = selected_count - actual_buffer_days

    core_dates = [item["trade_date"] for item in selected[:actual_core_days]]
    buffer_dates = [item["trade_date"] for item in selected[actual_core_days:]]
    date_weights: dict[str, float] = {}
    for idx, day in enumerate(selected):
        role = "core" if idx < actual_core_days else "buffer"
        core_index = idx if role == "core" else max(0, actual_core_days - 1)
        date_weights[day["trade_date"]] = _segment_weight(role, core_index, actual_core_days)

    dropped_dates = [item["trade_date"] for item in dataset[:-selected_count]]
    return {
        "mode": "core_plus_buffer_progressive",
        "requested_core_days": core_days,
        "requested_buffer_days": buffer_days,
        "selected_trade_dates": [item["trade_date"] for item in selected],
        "selected_trade_date_count": selected_count,
        "core_trade_dates": core_dates,
        "core_trade_date_count": actual_core_days,
        "buffer_trade_dates": buffer_dates,
        "buffer_trade_date_count": actual_buffer_days,
        "dropped_trade_dates": dropped_dates,
        "dropped_trade_date_count": len(dropped_dates),
        "date_weights": date_weights,
    }


def apply_window_profile(dataset: list[dict[str, Any]], window_profile: dict[str, Any]) -> list[dict[str, Any]]:
    selected_dates = set(window_profile.get("selected_trade_dates", []))
    selected = [dict(item) for item in dataset if item["trade_date"] in selected_dates]
    core_dates = set(window_profile.get("core_trade_dates", []))
    buffer_dates = set(window_profile.get("buffer_trade_dates", []))
    date_weights = window_profile.get("date_weights", {})
    for item in selected:
        trade_date = item["trade_date"]
        item["window_role"] = "buffer" if trade_date in buffer_dates else "core" if trade_date in core_dates else "selected"
        item["window_weight"] = float(date_weights.get(trade_date, 1.0))
    return selected


def classify_verdict(
    top100_hit_rate: float,
    top30_hit_rate: float,
    hit_day_rate: float,
    *,
    candidate_win_rate: float = 0.0,
    candidate_avg_return: float = 0.0,
    portfolio_positive_day_rate: float = 0.0,
    profit_priority_score: float = 0.0,
) -> tuple[str, str]:
    priority_profit_ok = (
        candidate_win_rate >= 0.58
        and candidate_avg_return >= 2.2
        and portfolio_positive_day_rate >= 0.58
    ) or profit_priority_score >= 108
    pass_profit_ok = (
        candidate_win_rate >= 0.52
        and candidate_avg_return >= 1.65
        and portfolio_positive_day_rate >= 0.52
    ) or profit_priority_score >= 92
    prototype_profit_ok = (
        candidate_win_rate >= 0.47
        and candidate_avg_return >= 1.1
    ) or profit_priority_score >= 78
    if (
        top100_hit_rate >= THRESHOLDS["priority"]["top100_hit_rate_min"]
        and top30_hit_rate >= THRESHOLDS["priority"]["top30_hit_rate_min"]
        and hit_day_rate >= THRESHOLDS["priority"]["hit_day_rate_min"]
        and priority_profit_ok
    ):
        return "priority", "promote"
    if (
        top100_hit_rate >= THRESHOLDS["pass"]["top100_hit_rate_min"]
        and top30_hit_rate >= THRESHOLDS["pass"]["top30_hit_rate_min"]
        and hit_day_rate >= THRESHOLDS["pass"]["hit_day_rate_min"]
        and pass_profit_ok
    ):
        return "pass", "promote"
    if (
        top100_hit_rate >= THRESHOLDS["prototype"]["top100_hit_rate_min"]
        and top30_hit_rate >= THRESHOLDS["prototype"]["top30_hit_rate_min"]
        and hit_day_rate >= THRESHOLDS["prototype"]["hit_day_rate_min"]
        and prototype_profit_ok
    ):
        return "prototype", "observe"
    return "fail", "downgrade"


def compute_profit_priority_score(
    *,
    candidate_win_rate: float,
    candidate_avg_return: float,
    portfolio_positive_day_rate: float,
    gain_loss_ratio: float | None = None,
) -> float:
    gain_loss_component = min(max(float(gain_loss_ratio or 0.0), 0.0), 3.0)
    return round(
        candidate_win_rate * 100
        + candidate_avg_return * 16
        + portfolio_positive_day_rate * 18
        + gain_loss_component * 6,
        4,
    )


def compute_business_score(
    *,
    quality_score: float,
    candidate_win_rate: float,
    candidate_avg_return: float,
    portfolio_positive_day_rate: float,
    candidate_retention_rate: float,
    gain_loss_ratio: float | None = None,
) -> tuple[float, float]:
    profit_priority_score = compute_profit_priority_score(
        candidate_win_rate=candidate_win_rate,
        candidate_avg_return=candidate_avg_return,
        portfolio_positive_day_rate=portfolio_positive_day_rate,
        gain_loss_ratio=gain_loss_ratio,
    )
    business_score = round(
        quality_score * 0.55
        + profit_priority_score * 0.45
        + candidate_retention_rate * 6,
        4,
    )
    return business_score, profit_priority_score


def build_template_name(params: dict[str, Any]) -> str:
    family = params.get("family", "tdx_carry")
    if family == "gap_mix":
        return (
            f"gapmix_head{params['head']}_skip{params['mid_skip']}_end{params['end_rank']}"
        )
    if family == "mix":
        return (
            f"mix_head{params['head']}_tail{params['tail_start']}_{params['tail_end']}"
        )
    return (
        f"carry_lb{params['lookback']}_cut{params['cutoff']}_dec{params['decay']}_"
        f"pctw{params['pct_weight']}_minp1_{params['min_prev1_pct']}_"
        f"minapp{params['min_appearances']}_reqp1_{int(params['require_prev1'])}_"
        f"excltop{params['exclude_prev1_top']}"
    )


def build_veto_name(params: dict[str, Any]) -> str:
    family = params.get("family", "recent_tail_veto")
    suffix = "_nost" if params.get("exclude_risk_warning") else ""
    if family == "fake_head_veto":
        return (
            f"fakehead_lb{params['lookback']}_head{params['head_cutoff']}_"
            f"minprev{params['min_prev_pct']:.1f}_failr{params['fail_rank_min']}_"
            f"failpct{abs(params['fail_pct_max']):.1f}_minf{params['min_failures']}{suffix}"
        )
    if family == "crowded_mid_veto":
        return (
            f"crowdedmid_lb{params['lookback']}_mid{params['mid_start']}_{params['mid_end']}_"
            f"minprev{params['min_prev_pct']:.1f}_failr{params['fail_rank_min']}_"
            f"failpct{abs(params['fail_pct_max']):.1f}_minf{params['min_failures']}{suffix}"
        )
    if family == "rear_hit_veto":
        return (
            f"rearhit_lb{params['lookback']}_rear{params['rear_start']}_{params['rear_end']}_"
            f"minprev{params['min_prev_pct']:.1f}_failfront{params['fail_front_rank']}_"
            f"failpct{abs(params['fail_pct_max']):.1f}_minf{params['min_failures']}{suffix}"
        )
    if family == "front_exhaustion_veto":
        return (
            f"frontex_lb{params['lookback']}_head{params['head_cutoff']}_"
            f"minprev{params['min_prev_pct']:.1f}_failr{params['fail_rank_min']}_"
            f"failpct{abs(params['fail_pct_max']):.1f}_minf{params['min_failures']}{suffix}"
        )
    return (
        f"tailveto_lb{params['lookback']}_tail{params['tail_cutoff']}_"
        f"minapp{params['min_appearances']}_maxpct{abs(params['max_tail_pct']):.1f}{suffix}"
    )


def rank_candidates(history: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    family = params.get("family", "tdx_carry")
    if family in {"gap_mix", "mix"}:
        prev_rows = history[-1]["full_rows"]
        ranked: list[dict[str, Any]] = []
        if family == "mix":
            for row in prev_rows:
                rank = int(row["rank"])
                if rank <= params["head"]:
                    ranked.append({"code": row["code"], "name": row["name"]})
                elif params["tail_start"] <= rank <= params["tail_end"]:
                    ranked.append({"code": row["code"], "name": row["name"]})
                if rank > params["tail_end"]:
                    break
        else:
            for row in prev_rows:
                rank = int(row["rank"])
                if rank <= params["head"]:
                    ranked.append({"code": row["code"], "name": row["name"]})
                elif rank > params["mid_skip"] and rank <= params["end_rank"]:
                    ranked.append({"code": row["code"], "name": row["name"]})
                if rank > params["end_rank"]:
                    break
        return ranked

    scores: dict[str, dict[str, Any]] = {}
    usable_history = history[-params["lookback"] :]
    recent_first = list(reversed(usable_history))

    for lag_idx, day in enumerate(recent_first):
        weight = params["decay"] ** lag_idx
        cutoff_rows = day["full_rows"][: params["cutoff"]]
        for row in cutoff_rows:
            code = row["code"]
            rank = int(row["rank"])
            pct_change = float(row["pct_change"])
            item = scores.setdefault(
                code,
                {
                    "code": code,
                    "name": row["name"],
                    "score": 0.0,
                    "appearances": 0,
                    "best_rank": rank,
                    "prev1_rank": None,
                    "prev1_pct": None,
                    "days": [],
                },
            )
            item["score"] += weight * ((params["cutoff"] + 1 - rank) + params["pct_weight"] * max(pct_change, 0.0))
            item["appearances"] += 1
            item["best_rank"] = min(item["best_rank"], rank)
            item["days"].append(day["trade_date"])
            if lag_idx == 0:
                item["prev1_rank"] = rank
                item["prev1_pct"] = pct_change

    filtered: list[dict[str, Any]] = []
    for item in scores.values():
        prev1_rank = item["prev1_rank"]
        prev1_pct = item["prev1_pct"]

        if item["appearances"] < params["min_appearances"]:
            continue
        if params["require_prev1"] and prev1_rank is None:
            continue
        if prev1_pct is not None and prev1_pct < params["min_prev1_pct"]:
            continue
        if prev1_rank is not None and params["exclude_prev1_top"] > 0 and prev1_rank <= params["exclude_prev1_top"]:
            continue

        recurrence_bonus = max(item["appearances"] - 1, 0) * 15.0
        prev1_bonus = 0.0
        if prev1_rank is not None:
            prev1_bonus += (params["cutoff"] + 1 - prev1_rank) * 0.8
        if prev1_pct is not None:
            prev1_bonus += max(prev1_pct, 0.0) * 0.6

        item["score"] = round(item["score"] + recurrence_bonus + prev1_bonus, 4)
        filtered.append(item)

    filtered.sort(
        key=lambda item: (
            float(item["score"]),
            int(item["appearances"]),
            -(item["best_rank"]),
            str(item["code"]),
        ),
        reverse=True,
    )
    return filtered


def _is_risk_warning_name(name: Any) -> bool:
    text = str(name or "").strip().upper()
    prefixes = ("ST", "*ST", "S*ST", "SST")
    return any(text.startswith(prefix) for prefix in prefixes)


def _is_risk_warning_row(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    if _is_risk_warning_name(row.get("name")):
        return True
    for field in ("risk_warning", "special_treatment", "is_st"):
        value = str(row.get(field, "")).strip().lower()
        if value in {"1", "true", "yes", "y"}:
            return True
    return False


def should_veto_candidate(code: str, history: list[dict[str, Any]], veto_params: dict[str, Any]) -> bool:
    family = veto_params.get("family", "recent_tail_veto")
    if family == "fake_head_veto":
        return should_veto_fake_head_candidate(code, history, veto_params)
    if family == "crowded_mid_veto":
        return should_veto_crowded_mid_candidate(code, history, veto_params)
    if family == "rear_hit_veto":
        return should_veto_rear_hit_candidate(code, history, veto_params)
    if family == "front_exhaustion_veto":
        return should_veto_front_exhaustion_candidate(code, history, veto_params)

    prior_history = history[:-1]
    usable_history = prior_history[-veto_params["lookback"] :]
    if not usable_history:
        return False

    tail_hits = 0
    worst_pct = 0.0
    for day in usable_history:
        row = day["row_map"].get(code)
        if row is None:
            continue
        rank = int(row["rank"])
        row_count = int(day["row_count"])
        if rank <= max(row_count - veto_params["tail_cutoff"], 0):
            continue
        tail_hits += 1
        worst_pct = min(worst_pct, float(row["pct_change"]))

    return tail_hits >= veto_params["min_appearances"] and worst_pct <= veto_params["max_tail_pct"]


def should_veto_fake_head_candidate(code: str, history: list[dict[str, Any]], veto_params: dict[str, Any]) -> bool:
    if len(history) < 2:
        return False

    failure_hits = 0
    transitions = list(zip(history[:-1], history[1:]))
    usable_transitions = transitions[-veto_params["lookback"] :]
    for prev_day, next_day in usable_transitions:
        prev_row = prev_day["row_map"].get(code)
        next_row = next_day["row_map"].get(code)
        if prev_row is None or next_row is None:
            continue

        prev_rank = int(prev_row["rank"])
        prev_pct = float(prev_row["pct_change"])
        next_rank = int(next_row["rank"])
        next_pct = float(next_row["pct_change"])
        if prev_rank > veto_params["head_cutoff"] or prev_pct < veto_params["min_prev_pct"]:
            continue

        failed_follow_through = next_rank >= veto_params["fail_rank_min"] or next_pct <= veto_params["fail_pct_max"]
        if failed_follow_through:
            failure_hits += 1

    return failure_hits >= veto_params["min_failures"]


def should_veto_crowded_mid_candidate(code: str, history: list[dict[str, Any]], veto_params: dict[str, Any]) -> bool:
    if len(history) < 2:
        return False

    failure_hits = 0
    transitions = list(zip(history[:-1], history[1:]))
    usable_transitions = transitions[-veto_params["lookback"] :]
    for prev_day, next_day in usable_transitions:
        prev_row = prev_day["row_map"].get(code)
        next_row = next_day["row_map"].get(code)
        if prev_row is None or next_row is None:
            continue

        prev_rank = int(prev_row["rank"])
        prev_pct = float(prev_row["pct_change"])
        next_rank = int(next_row["rank"])
        next_pct = float(next_row["pct_change"])
        if not (veto_params["mid_start"] <= prev_rank <= veto_params["mid_end"]):
            continue
        if prev_pct < veto_params["min_prev_pct"]:
            continue

        failed_follow_through = next_rank >= veto_params["fail_rank_min"] or next_pct <= veto_params["fail_pct_max"]
        if failed_follow_through:
            failure_hits += 1

    return failure_hits >= veto_params["min_failures"]


def should_veto_rear_hit_candidate(code: str, history: list[dict[str, Any]], veto_params: dict[str, Any]) -> bool:
    if len(history) < 2:
        return False

    failure_hits = 0
    transitions = list(zip(history[:-1], history[1:]))
    usable_transitions = transitions[-veto_params["lookback"] :]
    for prev_day, next_day in usable_transitions:
        prev_row = prev_day["row_map"].get(code)
        next_row = next_day["row_map"].get(code)
        if prev_row is None or next_row is None:
            continue

        prev_rank = int(prev_row["rank"])
        prev_pct = float(prev_row["pct_change"])
        next_rank = int(next_row["rank"])
        next_pct = float(next_row["pct_change"])
        if not (veto_params["rear_start"] <= prev_rank <= veto_params["rear_end"]):
            continue
        if prev_pct < veto_params["min_prev_pct"]:
            continue

        rear_failure = next_rank > veto_params["fail_front_rank"] or next_pct <= veto_params["fail_pct_max"]
        if rear_failure:
            failure_hits += 1

    return failure_hits >= veto_params["min_failures"]


def should_veto_front_exhaustion_candidate(code: str, history: list[dict[str, Any]], veto_params: dict[str, Any]) -> bool:
    if len(history) < 2:
        return False

    failure_hits = 0
    transitions = list(zip(history[:-1], history[1:]))
    usable_transitions = transitions[-veto_params["lookback"] :]
    for prev_day, next_day in usable_transitions:
        prev_row = prev_day["row_map"].get(code)
        next_row = next_day["row_map"].get(code)
        if prev_row is None or next_row is None:
            continue

        prev_rank = int(prev_row["rank"])
        prev_pct = float(prev_row["pct_change"])
        next_rank = int(next_row["rank"])
        next_pct = float(next_row["pct_change"])
        if prev_rank > veto_params["head_cutoff"] or prev_pct < veto_params["min_prev_pct"]:
            continue

        exhausted = next_rank >= veto_params["fail_rank_min"] or next_pct <= veto_params["fail_pct_max"]
        if exhausted:
            failure_hits += 1

    return failure_hits >= veto_params["min_failures"]


def select_candidates(
    history: list[dict[str, Any]],
    params: dict[str, Any],
    veto_params: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    ranked_candidates = rank_candidates(history, params)
    if veto_params is None:
        return ranked_candidates[:CANDIDATE_SIZE], []

    selected: list[dict[str, Any]] = []
    vetoed_codes: list[str] = []
    for item in ranked_candidates:
        latest_row = history[-1]["row_map"].get(item["code"]) if history else None
        if veto_params and veto_params.get("exclude_risk_warning") and _is_risk_warning_row(latest_row):
            vetoed_codes.append(item["code"])
            continue
        if should_veto_candidate(item["code"], history, veto_params):
            vetoed_codes.append(item["code"])
            continue
        selected.append(item)
        if len(selected) >= CANDIDATE_SIZE:
            break
    return selected[:CANDIDATE_SIZE], vetoed_codes


def summarize_candidate_outcomes(candidate_codes: list[str], target: dict[str, Any]) -> dict[str, Any]:
    returns: list[float] = []
    positive_count = 0
    gains: list[float] = []
    losses: list[float] = []

    for code in candidate_codes:
        row = target["row_map"].get(code)
        if row is None:
            continue
        pct_change = float(row["pct_change"])
        returns.append(pct_change)
        if pct_change > 0:
            positive_count += 1
            gains.append(pct_change)
        elif pct_change < 0:
            losses.append(pct_change)

    return_count = len(returns)
    avg_return = sum(returns) / return_count if return_count else 0.0
    return {
        "return_count": return_count,
        "positive_count": positive_count,
        "avg_return": round(avg_return, 4),
        "median_return": round(median(returns), 4) if returns else 0.0,
        "gain_loss_ratio": round((sum(gains) / len(gains)) / abs(sum(losses) / len(losses)), 4)
        if gains and losses
        else None,
        "returns": returns,
    }


def front_bucket_points(rank: int) -> int:
    if rank <= 10:
        return 4
    if rank <= 30:
        return 3
    if rank <= 50:
        return 2
    if rank <= 100:
        return 1
    return 0


def evaluate_params(
    dataset: list[dict[str, Any]],
    params: dict[str, Any],
    veto_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evaluations: list[dict[str, Any]] = []
    total_top100_hits = 0
    total_top50_hits = 0
    total_top30_hits = 0
    total_top10_hits = 0
    hit_days = 0
    total_return_count = 0
    total_positive_count = 0
    total_candidate_count = 0.0
    total_vetoed_count = 0.0
    all_returns: list[float] = []
    positive_portfolio_days = 0
    total_front_shift_points = 0
    total_hit_rank_sum = 0
    total_hit_rank_count = 0
    total_weighted_days = 0.0
    weighted_return_denominator = 0.0
    weighted_return_sum = 0.0
    weighted_positive_day_score = 0.0
    weighted_hit_day_score = 0.0

    for idx in range(1, len(dataset)):
        history = dataset[:idx]
        target = dataset[idx]
        target_weight = float(target.get("window_weight", 1.0))
        candidates, vetoed_codes = select_candidates(history, params, veto_params=veto_params)
        candidate_codes = [item["code"] for item in candidates]

        top100_hits = [code for code in candidate_codes if code in target["top100_codes"]]
        top50_hits = [code for code in candidate_codes if code in target["top50_codes"]]
        top30_hits = [code for code in candidate_codes if code in target["top30_codes"]]
        top10_hits = [code for code in candidate_codes if code in target["top10_codes"]]
        outcome = summarize_candidate_outcomes(candidate_codes, target)
        hit_ranks = [int(target["row_map"][code]["rank"]) for code in top100_hits if code in target["row_map"]]
        front_shift_points = sum(front_bucket_points(rank) for rank in hit_ranks)

        total_top100_hits += len(top100_hits) * target_weight
        total_top50_hits += len(top50_hits) * target_weight
        total_top30_hits += len(top30_hits) * target_weight
        total_top10_hits += len(top10_hits) * target_weight
        total_candidate_count += len(candidate_codes) * target_weight
        total_return_count += outcome["return_count"]
        total_positive_count += outcome["positive_count"] * target_weight
        total_vetoed_count += len(vetoed_codes) * target_weight
        all_returns.extend(outcome["returns"])
        total_front_shift_points += front_shift_points * target_weight
        total_hit_rank_sum += sum(hit_ranks) * target_weight
        total_hit_rank_count += len(hit_ranks) * target_weight
        total_weighted_days += target_weight
        weighted_return_denominator += outcome["return_count"] * target_weight
        weighted_return_sum += sum(outcome["returns"]) * target_weight
        if top100_hits:
            hit_days += 1
            weighted_hit_day_score += target_weight
        if outcome["avg_return"] > 0:
            positive_portfolio_days += 1
            weighted_positive_day_score += target_weight

        evaluations.append(
            {
                "t0_date": target["trade_date"],
                "window_role": target.get("window_role", "selected"),
                "window_weight": round(target_weight, 4),
                "candidate_count": len(candidate_codes),
                "top100_hit_count": len(top100_hits),
                "top50_hit_count": len(top50_hits),
                "top30_hit_count": len(top30_hits),
                "top10_hit_count": len(top10_hits),
                "top100_hits": top100_hits,
                "candidate_codes": candidate_codes,
                "vetoed_codes": vetoed_codes,
                "avg_hit_rank": round(sum(hit_ranks) / len(hit_ranks), 4) if hit_ranks else None,
                "front_shift_points": front_shift_points,
                "candidate_win_rate": round(outcome["positive_count"] / outcome["return_count"], 4)
                if outcome["return_count"]
                else 0.0,
                "candidate_avg_return": outcome["avg_return"],
            }
        )

    total_days = len(evaluations)
    denominator = total_weighted_days * CANDIDATE_SIZE
    top100_hit_rate = total_top100_hits / denominator if denominator else 0.0
    top50_hit_rate = total_top50_hits / denominator if denominator else 0.0
    top30_hit_rate = total_top30_hits / denominator if denominator else 0.0
    top10_hit_rate = total_top10_hits / denominator if denominator else 0.0
    hit_day_rate = weighted_hit_day_score / total_weighted_days if total_weighted_days else 0.0
    candidate_win_rate = total_positive_count / weighted_return_denominator if weighted_return_denominator else 0.0
    candidate_avg_return = weighted_return_sum / weighted_return_denominator if weighted_return_denominator else 0.0
    candidate_median_return = median(all_returns) if all_returns else 0.0
    portfolio_positive_day_rate = (
        weighted_positive_day_score / total_weighted_days if total_weighted_days else 0.0
    )
    candidate_retention_rate = total_candidate_count / denominator if denominator else 0.0
    front_shift_score = total_front_shift_points / (denominator * 4) if denominator else 0.0
    avg_hit_rank = total_hit_rank_sum / total_hit_rank_count if total_hit_rank_count else None
    rank_quality_score = ((101 - avg_hit_rank) / 100) if avg_hit_rank is not None else 0.0
    gain_loss_ratio = None
    gains = [value for value in all_returns if value > 0]
    losses = [value for value in all_returns if value < 0]
    if gains and losses:
        gain_loss_ratio = round((sum(gains) / len(gains)) / abs(sum(losses) / len(losses)), 4)

    profit_priority_score = compute_profit_priority_score(
        candidate_win_rate=candidate_win_rate,
        candidate_avg_return=candidate_avg_return,
        portfolio_positive_day_rate=portfolio_positive_day_rate,
        gain_loss_ratio=gain_loss_ratio,
    )
    verdict, recommended_action = classify_verdict(
        top100_hit_rate,
        top30_hit_rate,
        hit_day_rate,
        candidate_win_rate=candidate_win_rate,
        candidate_avg_return=candidate_avg_return,
        portfolio_positive_day_rate=portfolio_positive_day_rate,
        profit_priority_score=profit_priority_score,
    )
    quality_score = round(
        top100_hit_rate * 90
        + top50_hit_rate * 120
        + top30_hit_rate * 150
        + top10_hit_rate * 60
        + hit_day_rate * 25
        + front_shift_score * 80
        + rank_quality_score * 20,
        4,
    )
    business_score, profit_priority_score = compute_business_score(
        quality_score=quality_score,
        candidate_win_rate=candidate_win_rate,
        candidate_avg_return=candidate_avg_return,
        portfolio_positive_day_rate=portfolio_positive_day_rate,
        candidate_retention_rate=candidate_retention_rate,
        gain_loss_ratio=gain_loss_ratio,
    )
    template_name = build_template_name(params)
    if veto_params is not None:
        template_name = f"{template_name}__veto__{build_veto_name(veto_params)}"

    return {
        "template_name": template_name,
        "base_template_name": build_template_name(params),
        "family": params.get("family", "tdx_carry"),
        "params": params,
        "negative_veto": veto_params,
        "metrics": {
            "evaluation_days": total_days,
            "weighted_evaluation_days": round(total_weighted_days, 4),
            "candidate_size": CANDIDATE_SIZE,
            "avg_candidate_count": round(total_candidate_count / total_days, 4) if total_days else 0.0,
            "candidate_retention_rate": round(candidate_retention_rate, 4),
            "vetoed_candidate_count": total_vetoed_count,
            "avg_vetoed_per_day": round(total_vetoed_count / total_days, 4) if total_days else 0.0,
            "top100_hit_count": total_top100_hits,
            "top50_hit_count": total_top50_hits,
            "top30_hit_count": total_top30_hits,
            "top10_hit_count": total_top10_hits,
            "top100_hit_rate": round(top100_hit_rate, 4),
            "top50_hit_rate": round(top50_hit_rate, 4),
            "top30_hit_rate": round(top30_hit_rate, 4),
            "top10_hit_rate": round(top10_hit_rate, 4),
            "hit_day_count": hit_days,
            "hit_day_rate": round(hit_day_rate, 4),
            "front_shift_score": round(front_shift_score, 4),
            "avg_hit_rank": round(avg_hit_rank, 4) if avg_hit_rank is not None else None,
            "candidate_win_rate": round(candidate_win_rate, 4),
            "candidate_avg_return": round(candidate_avg_return, 4),
            "candidate_median_return": round(candidate_median_return, 4),
            "portfolio_positive_day_rate": round(portfolio_positive_day_rate, 4),
            "gain_loss_ratio": gain_loss_ratio,
            "quality_score": quality_score,
            "profit_priority_score": profit_priority_score,
            "business_score": business_score,
        },
        "verdict": verdict,
        "recommended_action": recommended_action,
        "daily_evaluations": evaluations,
    }


def tier_rank(verdict: str) -> int:
    return {"priority": 4, "pass": 3, "prototype": 2, "fail": 1}.get(verdict, 0)


def build_param_grid() -> list[dict[str, Any]]:
    params_list: list[dict[str, Any]] = []
    for cutoff in (10, 15, 20, 25, 30, 40):
        for pct_weight in (0.0, 0.5, 1.0, 2.0):
            for min_prev1_pct in (0.0, 3.0, 5.0, 8.0, 10.0):
                for exclude_prev1_top in (0, 1, 2, 3, 5):
                    params_list.append(
                        {
                            "family": "tdx_carry",
                            "lookback": 1,
                            "cutoff": cutoff,
                            "decay": 1.0,
                            "pct_weight": pct_weight,
                            "min_prev1_pct": min_prev1_pct,
                            "min_appearances": 1,
                            "require_prev1": True,
                            "exclude_prev1_top": exclude_prev1_top,
                        }
                    )
    for head in (5, 8, 10, 12):
        for mid_skip in (15, 20, 25, 30, 35, 40):
            for end_rank in (35, 40, 45, 50, 55, 60):
                if end_rank <= mid_skip:
                    continue
                params_list.append(
                    {
                        "family": "gap_mix",
                        "head": head,
                        "mid_skip": mid_skip,
                        "end_rank": end_rank,
                    }
                )
    for head in (5, 8, 10, 12):
        for tail_start in (11, 13, 16, 21, 26, 31):
            for tail_end in (30, 35, 40, 45, 50):
                if tail_end <= tail_start:
                    continue
                params_list.append(
                    {
                        "family": "mix",
                        "head": head,
                        "tail_start": tail_start,
                        "tail_end": tail_end,
                    }
                )
    for head, mid_skip, end_rank in (
        (10, 35, 42),
        (10, 38, 48),
        (11, 35, 45),
        (12, 33, 45),
        (12, 35, 42),
        (12, 35, 48),
        (12, 38, 45),
        (12, 38, 48),
        (12, 40, 48),
        (13, 35, 45),
        (13, 38, 48),
        (14, 35, 45),
        (14, 38, 48),
        (14, 40, 50),
    ):
        params_list.append(
            {
                "family": "gap_mix",
                "head": head,
                "mid_skip": mid_skip,
                "end_rank": end_rank,
            }
        )
    for head, tail_start, tail_end in (
        (10, 24, 30),
        (10, 24, 32),
        (12, 21, 30),
        (12, 24, 30),
        (12, 24, 32),
        (12, 25, 30),
        (12, 25, 32),
        (12, 26, 32),
        (12, 28, 35),
        (13, 24, 32),
        (14, 24, 30),
        (14, 24, 32),
    ):
        params_list.append(
            {
                "family": "mix",
                "head": head,
                "tail_start": tail_start,
                "tail_end": tail_end,
            }
        )
    unique_params: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in params_list:
        key = json.dumps(item, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique_params.append(item)
    return unique_params


def build_negative_param_grid() -> list[dict[str, Any]]:
    params_list: list[dict[str, Any]] = []
    for lookback in (2, 3, 5):
        for tail_cutoff in (50, 100, 150, 200):
            for min_appearances in (1, 2):
                for max_tail_pct in (-4.0, -6.0, -8.0):
                    if min_appearances > lookback:
                        continue
                    params_list.append(
                        {
                            "family": "recent_tail_veto",
                            "lookback": lookback,
                            "tail_cutoff": tail_cutoff,
                            "min_appearances": min_appearances,
                            "max_tail_pct": max_tail_pct,
                        }
                    )
    for lookback in (3, 5, 8):
        for head_cutoff in (10, 20):
            for min_prev_pct in (3.0, 5.0, 8.0):
                for fail_rank_min in (120, 150, 200):
                    for fail_pct_max in (0.0, -2.0):
                        for min_failures in (1, 2):
                            if min_failures > lookback:
                                continue
                            params_list.append(
                                {
                                    "family": "fake_head_veto",
                                    "lookback": lookback,
                                    "head_cutoff": head_cutoff,
                                    "min_prev_pct": min_prev_pct,
                                    "fail_rank_min": fail_rank_min,
                                    "fail_pct_max": fail_pct_max,
                                    "min_failures": min_failures,
                                }
                            )
    for lookback in (2, 3, 4, 5):
        for head_cutoff in (10, 15, 20, 25):
            for min_prev_pct in (2.0, 3.0, 4.0, 5.0):
                for fail_rank_min in (100, 120, 140, 160):
                    for fail_pct_max in (0.0, -0.5, -1.0):
                        for min_failures in (1, 2):
                            if min_failures > lookback:
                                continue
                            params_list.append(
                                {
                                    "family": "fake_head_veto",
                                    "lookback": lookback,
                                    "head_cutoff": head_cutoff,
                                    "min_prev_pct": min_prev_pct,
                                    "fail_rank_min": fail_rank_min,
                                    "fail_pct_max": fail_pct_max,
                                    "min_failures": min_failures,
                                }
                            )
    for lookback in (1, 2, 3):
        for head_cutoff in (15, 20, 25):
            for min_prev_pct in (2.0, 3.0, 4.0):
                for fail_rank_min in (80, 100, 120):
                    for fail_pct_max in (0.0, -0.5):
                        params_list.append(
                            {
                                "family": "fake_head_veto",
                                "lookback": lookback,
                                "head_cutoff": head_cutoff,
                                "min_prev_pct": min_prev_pct,
                                "fail_rank_min": fail_rank_min,
                                "fail_pct_max": fail_pct_max,
                                "min_failures": 1,
                            }
                        )
    for lookback in (3, 5, 8):
        for mid_start, mid_end in ((12, 35), (15, 40), (20, 50), (25, 60)):
            for min_prev_pct in (2.0, 3.0, 5.0):
                for fail_rank_min in (120, 150, 200):
                    for fail_pct_max in (0.0, -1.0, -2.0):
                        for min_failures in (1, 2):
                            if min_failures > lookback:
                                continue
                            params_list.append(
                                {
                                    "family": "crowded_mid_veto",
                                    "lookback": lookback,
                                    "mid_start": mid_start,
                                    "mid_end": mid_end,
                                    "min_prev_pct": min_prev_pct,
                                    "fail_rank_min": fail_rank_min,
                                    "fail_pct_max": fail_pct_max,
                                    "min_failures": min_failures,
                                }
                            )
    for lookback in (2, 3, 4, 5):
        for mid_start, mid_end in ((12, 35), (15, 40), (18, 45), (20, 45), (25, 50)):
            for min_prev_pct in (2.0, 3.0, 4.0):
                for fail_rank_min in (100, 120, 150, 180, 200):
                    for fail_pct_max in (0.0, -0.5, -1.0):
                        for min_failures in (1, 2):
                            if min_failures > lookback:
                                continue
                            params_list.append(
                                {
                                    "family": "crowded_mid_veto",
                                    "lookback": lookback,
                                    "mid_start": mid_start,
                                    "mid_end": mid_end,
                                    "min_prev_pct": min_prev_pct,
                                    "fail_rank_min": fail_rank_min,
                                    "fail_pct_max": fail_pct_max,
                                    "min_failures": min_failures,
                                }
                            )
    for lookback in (1, 2, 3):
        for mid_start, mid_end in ((12, 35), (15, 40)):
            for min_prev_pct in (2.0, 3.0):
                for fail_rank_min in (80, 100, 120):
                    params_list.append(
                        {
                            "family": "crowded_mid_veto",
                            "lookback": lookback,
                            "mid_start": mid_start,
                            "mid_end": mid_end,
                            "min_prev_pct": min_prev_pct,
                            "fail_rank_min": fail_rank_min,
                            "fail_pct_max": 0.0,
                            "min_failures": 1,
                        }
                    )
    for lookback in (3, 5, 8):
        for rear_start, rear_end in ((40, 100), (50, 100), (60, 100)):
            for min_prev_pct in (1.0, 2.0, 3.0):
                for fail_front_rank in (30, 50):
                    for fail_pct_max in (0.0, -1.0):
                        for min_failures in (1, 2):
                            if min_failures > lookback:
                                continue
                            params_list.append(
                                {
                                    "family": "rear_hit_veto",
                                    "lookback": lookback,
                                    "rear_start": rear_start,
                                    "rear_end": rear_end,
                                    "min_prev_pct": min_prev_pct,
                                    "fail_front_rank": fail_front_rank,
                                    "fail_pct_max": fail_pct_max,
                                    "min_failures": min_failures,
                                }
                            )
    for lookback in (3, 4, 5):
        for rear_start, rear_end in ((35, 100), (40, 100), (45, 100), (50, 90), (60, 100)):
            for min_prev_pct in (0.5, 1.0, 2.0, 3.0):
                for fail_front_rank in (30, 40, 50):
                    for fail_pct_max in (0.0, -0.5):
                        for min_failures in (1, 2):
                            if min_failures > lookback:
                                continue
                            params_list.append(
                                {
                                    "family": "rear_hit_veto",
                                    "lookback": lookback,
                                    "rear_start": rear_start,
                                    "rear_end": rear_end,
                                    "min_prev_pct": min_prev_pct,
                                    "fail_front_rank": fail_front_rank,
                                    "fail_pct_max": fail_pct_max,
                                    "min_failures": min_failures,
                                }
                            )
    for lookback in (1, 2, 3):
        for rear_start, rear_end in ((40, 100), (50, 100), (60, 100)):
            for min_prev_pct in (1.0, 2.0):
                for fail_front_rank in (30, 40):
                    params_list.append(
                        {
                            "family": "rear_hit_veto",
                            "lookback": lookback,
                            "rear_start": rear_start,
                            "rear_end": rear_end,
                            "min_prev_pct": min_prev_pct,
                            "fail_front_rank": fail_front_rank,
                            "fail_pct_max": 0.0,
                            "min_failures": 1,
                        }
                    )
    for lookback in (2, 3, 5):
        for head_cutoff in (5, 8, 10):
            for min_prev_pct in (3.0, 4.0, 5.0):
                for fail_rank_min in (50, 80, 100):
                    for fail_pct_max in (0.0, -0.5, -1.0):
                        for min_failures in (1, 2):
                            if min_failures > lookback:
                                continue
                            for exclude_risk_warning in (False, True):
                                params_list.append(
                                    {
                                        "family": "front_exhaustion_veto",
                                        "lookback": lookback,
                                        "head_cutoff": head_cutoff,
                                        "min_prev_pct": min_prev_pct,
                                        "fail_rank_min": fail_rank_min,
                                        "fail_pct_max": fail_pct_max,
                                        "min_failures": min_failures,
                                        "exclude_risk_warning": exclude_risk_warning,
                                    }
                                )
    unique_params: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in params_list:
        key = json.dumps(item, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique_params.append(item)
    return unique_params


def template_class(params: dict[str, Any]) -> str:
    family = params.get("family", "tdx_carry")
    if family == "gap_mix":
        if params["mid_skip"] >= 35:
            return "late_gap_mix"
        return "layered_gap_mix"
    if family == "mix":
        return "layered_mix"
    if params["exclude_prev1_top"] >= 3:
        return "expansion_after_head"
    if params["cutoff"] <= 15:
        return "tight_carry"
    return "strong_carry"


def candidate_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(tuple(day["candidate_codes"]) for day in item["daily_evaluations"])


def dedupe_trials(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in trials:
        signature = candidate_signature(item)
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(item)
    return unique


def build_combination_uplift(base_metrics: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "top100_hit_rate_delta": round(metrics["top100_hit_rate"] - base_metrics["top100_hit_rate"], 4),
        "top50_hit_rate_delta": round(metrics["top50_hit_rate"] - base_metrics["top50_hit_rate"], 4),
        "top30_hit_rate_delta": round(metrics["top30_hit_rate"] - base_metrics["top30_hit_rate"], 4),
        "hit_day_rate_delta": round(metrics["hit_day_rate"] - base_metrics["hit_day_rate"], 4),
        "front_shift_score_delta": round(metrics["front_shift_score"] - base_metrics["front_shift_score"], 4),
        "avg_hit_rank_delta": round(
            (metrics["avg_hit_rank"] or 101.0) - (base_metrics["avg_hit_rank"] or 101.0),
            4,
        ),
        "candidate_win_rate_delta": round(metrics["candidate_win_rate"] - base_metrics["candidate_win_rate"], 4),
        "candidate_avg_return_delta": round(metrics["candidate_avg_return"] - base_metrics["candidate_avg_return"], 4),
        "portfolio_positive_day_rate_delta": round(
            metrics["portfolio_positive_day_rate"] - base_metrics["portfolio_positive_day_rate"], 4
        ),
        "business_score_delta": round(metrics["business_score"] - base_metrics["business_score"], 4),
    }


def is_profit_priority_candidate(item: dict[str, Any]) -> bool:
    metrics = item["metrics"]
    return (
        item["verdict"] == "prototype"
        and metrics["candidate_win_rate"] >= 0.55
        and metrics["candidate_avg_return"] >= 2.05
        and metrics["hit_day_rate"] >= 0.84
        and metrics["front_shift_score"] >= 0.08
    )


def is_front_priority_candidate(item: dict[str, Any]) -> bool:
    metrics = item["metrics"]
    return (
        item["verdict"] == "prototype"
        and metrics["top50_hit_rate"] >= 0.12
        and metrics["top30_hit_rate"] >= 0.105
        and (metrics["avg_hit_rank"] or 999.0) <= 28.5
        and metrics["candidate_avg_return"] >= 1.7
    )


def build_negative_base_templates(unique_trials: list[dict[str, Any]], passed_templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bases = list(passed_templates)
    selected_names = {item["template_name"] for item in bases}
    profit_candidates = [item for item in unique_trials if is_profit_priority_candidate(item)]
    front_candidates = [item for item in unique_trials if is_front_priority_candidate(item)]
    profit_candidates.sort(
        key=lambda item: (
            item["metrics"]["candidate_win_rate"],
            item["metrics"]["candidate_avg_return"],
            item["metrics"]["business_score"],
            item["metrics"]["front_shift_score"],
        ),
        reverse=True,
    )
    front_candidates.sort(
        key=lambda item: (
            item["metrics"]["front_shift_score"],
            -float(item["metrics"]["avg_hit_rank"] or 999.0),
            item["metrics"]["top50_hit_rate"],
            item["metrics"]["candidate_avg_return"],
        ),
        reverse=True,
    )
    for item in profit_candidates[:6]:
        if item["template_name"] in selected_names:
            continue
        bases.append(item)
        selected_names.add(item["template_name"])
    for item in front_candidates[:6]:
        if item["template_name"] in selected_names:
            continue
        bases.append(item)
        selected_names.add(item["template_name"])
    return bases


def select_champion_combination(promoted_combinations: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not promoted_combinations:
        return None
    return max(
        promoted_combinations,
        key=lambda item: (
            item["metrics"]["candidate_win_rate"],
            item["metrics"]["candidate_avg_return"],
            item["metrics"]["front_shift_score"],
            item["metrics"]["top50_hit_rate"],
            item["metrics"]["business_score"],
            -(item["metrics"]["avg_hit_rank"] or 999.0),
        ),
    )


def classify_combination(base_metrics: dict[str, Any], metrics: dict[str, Any]) -> str:
    hit_stable = metrics["top100_hit_rate"] >= base_metrics["top100_hit_rate"] - 0.01
    top50_stable = metrics["top50_hit_rate"] >= base_metrics["top50_hit_rate"] - 0.01
    top30_stable = metrics["top30_hit_rate"] >= base_metrics["top30_hit_rate"] - 0.01
    day_stable = metrics["hit_day_rate"] >= base_metrics["hit_day_rate"] - 0.05
    front_up = metrics["front_shift_score"] >= base_metrics["front_shift_score"]
    rank_up = (metrics["avg_hit_rank"] or 101.0) <= (base_metrics["avg_hit_rank"] or 101.0) + 2.0
    profit_up = metrics["candidate_avg_return"] > base_metrics["candidate_avg_return"]
    win_up = metrics["candidate_win_rate"] >= base_metrics["candidate_win_rate"]
    retention_ok = metrics["candidate_retention_rate"] >= 0.75
    base_profit_priority = float(base_metrics.get("profit_priority_score", 0.0) or 0.0)
    current_profit_priority = float(metrics.get("profit_priority_score", 0.0) or 0.0)
    profit_priority_up = current_profit_priority >= base_profit_priority + 4.0
    strong_profit_target = (
        metrics["candidate_avg_return"] >= 2.05
        and metrics["candidate_win_rate"] >= 0.555
    ) or current_profit_priority >= 96.0
    stability_ok = sum(
        1
        for flag in (hit_stable, top50_stable, top30_stable, day_stable, front_up, rank_up)
        if flag
    ) >= 4
    if (
        retention_ok
        and stability_ok
        and strong_profit_target
        and (profit_up or profit_priority_up)
        and win_up
    ):
        return "promote"
    if (
        retention_ok
        and strong_profit_target
        and profit_priority_up
        and sum(1 for flag in (top50_stable, top30_stable, front_up, rank_up) if flag) >= 2
    ):
        return "promote"
    if (
        retention_ok
        and (profit_up or win_up or profit_priority_up)
        and (
            stability_ok
            or current_profit_priority >= max(base_profit_priority, 86.0)
        )
    ):
        return "observe"
    if retention_ok and (profit_up or win_up):
        return "observe"
    return "reject"


def write_json(file_path: Path, payload: object) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown(file_path: Path, payload: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("# WorkBuddy Distill Search")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Dataset trade dates: `{payload['summary']['trade_date_count']}`")
    lines.append(f"- Candidate size: `{payload['summary']['candidate_size']}`")
    lines.append(f"- Template trials: `{payload['summary']['template_trial_count']}`")
    lines.append(f"- Unique template behaviors: `{payload['summary']['unique_template_count']}`")
    lines.append(f"- Passed templates: `{payload['summary']['passed_template_count']}`")
    lines.append(f"- Priority templates: `{payload['summary']['priority_template_count']}`")
    lines.append(f"- Window mode: `{payload['summary'].get('window_mode', 'full')}`")
    lines.append(f"- Core dates: `{payload['summary'].get('core_trade_date_count', 0)}`")
    lines.append(f"- Buffer dates: `{payload['summary'].get('buffer_trade_date_count', 0)}`")
    lines.append("")
    lines.append("## Passing Templates")
    lines.append("")
    for item in payload["passed_templates"]:
        metrics = item["metrics"]
        lines.append(f"- `{item['template_name']}`")
        lines.append(f"  - class: `{item['template_class']}`")
        lines.append(f"  - verdict: `{item['verdict']}`")
        lines.append(f"  - top100_hit_rate: `{metrics['top100_hit_rate']}`")
        lines.append(f"  - top50_hit_rate: `{metrics['top50_hit_rate']}`")
        lines.append(f"  - top30_hit_rate: `{metrics['top30_hit_rate']}`")
        lines.append(f"  - front_shift_score: `{metrics['front_shift_score']}`")
        lines.append(f"  - avg_hit_rank: `{metrics['avg_hit_rank']}`")
        lines.append(f"  - hit_day_rate: `{metrics['hit_day_rate']}`")
        lines.append(f"  - candidate_win_rate: `{metrics['candidate_win_rate']}`")
        lines.append(f"  - candidate_avg_return: `{metrics['candidate_avg_return']}`")
        lines.append(f"  - business_score: `{metrics['business_score']}`")
    lines.append("")
    lines.append("## Prototype Templates")
    lines.append("")
    for item in payload["prototype_templates"]:
        metrics = item["metrics"]
        lines.append(f"- `{item['template_name']}`")
        lines.append(f"  - class: `{item['template_class']}`")
        lines.append(f"  - top100_hit_rate: `{metrics['top100_hit_rate']}`")
        lines.append(f"  - top50_hit_rate: `{metrics['top50_hit_rate']}`")
        lines.append(f"  - top30_hit_rate: `{metrics['top30_hit_rate']}`")
        lines.append(f"  - front_shift_score: `{metrics['front_shift_score']}`")
        lines.append(f"  - avg_hit_rank: `{metrics['avg_hit_rank']}`")
        lines.append(f"  - hit_day_rate: `{metrics['hit_day_rate']}`")
        lines.append(f"  - candidate_win_rate: `{metrics['candidate_win_rate']}`")
        lines.append(f"  - candidate_avg_return: `{metrics['candidate_avg_return']}`")
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_combined_markdown(file_path: Path, payload: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("# WorkBuddy Combined Distill Search")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Base templates searched: `{payload['summary']['base_template_count']}`")
    lines.append(f"- Negative veto trials: `{payload['summary']['negative_veto_trial_count']}`")
    lines.append(f"- Combined trials: `{payload['summary']['combination_trial_count']}`")
    lines.append(f"- Unique combined behaviors: `{payload['summary']['unique_combination_count']}`")
    lines.append(f"- Promoted combinations: `{payload['summary']['promoted_combination_count']}`")
    lines.append("")
    lines.append("## Promoted Combinations")
    lines.append("")
    for item in payload["promoted_combinations"]:
        metrics = item["metrics"]
        uplift = item["uplift"]
        lines.append(f"- `{item['template_name']}`")
        lines.append(f"  - base: `{item['base_template_name']}`")
        lines.append(f"  - base_verdict: `{item.get('base_verdict', 'unknown')}`")
        lines.append(f"  - veto: `{build_veto_name(item['negative_veto'])}`")
        lines.append(f"  - top100_hit_rate: `{metrics['top100_hit_rate']}` ({uplift['top100_hit_rate_delta']:+.4f})")
        lines.append(f"  - top50_hit_rate: `{metrics['top50_hit_rate']}` ({uplift['top50_hit_rate_delta']:+.4f})")
        lines.append(f"  - front_shift_score: `{metrics['front_shift_score']}` ({uplift['front_shift_score_delta']:+.4f})")
        lines.append(f"  - avg_hit_rank: `{metrics['avg_hit_rank']}` ({uplift['avg_hit_rank_delta']:+.4f})")
        lines.append(f"  - candidate_win_rate: `{metrics['candidate_win_rate']}` ({uplift['candidate_win_rate_delta']:+.4f})")
        lines.append(f"  - candidate_avg_return: `{metrics['candidate_avg_return']}` ({uplift['candidate_avg_return_delta']:+.4f})")
        lines.append(f"  - candidate_retention_rate: `{metrics['candidate_retention_rate']}`")
        lines.append(f"  - combined_recommendation: `{item['combined_recommendation']}`")
    lines.append("")
    lines.append("## Observed Combinations")
    lines.append("")
    for item in payload["observed_combinations"]:
        metrics = item["metrics"]
        uplift = item["uplift"]
        lines.append(f"- `{item['template_name']}`")
        lines.append(f"  - base: `{item['base_template_name']}`")
        lines.append(f"  - base_verdict: `{item.get('base_verdict', 'unknown')}`")
        lines.append(f"  - veto: `{build_veto_name(item['negative_veto'])}`")
        lines.append(f"  - top100_hit_rate: `{metrics['top100_hit_rate']}` ({uplift['top100_hit_rate_delta']:+.4f})")
        lines.append(f"  - top50_hit_rate: `{metrics['top50_hit_rate']}` ({uplift['top50_hit_rate_delta']:+.4f})")
        lines.append(f"  - front_shift_score: `{metrics['front_shift_score']}` ({uplift['front_shift_score_delta']:+.4f})")
        lines.append(f"  - candidate_avg_return: `{metrics['candidate_avg_return']}` ({uplift['candidate_avg_return_delta']:+.4f})")
        lines.append(f"  - candidate_retention_rate: `{metrics['candidate_retention_rate']}`")
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Distill local templates from TDX rankings with controlled rolling window.")
    parser.add_argument("--core-days", type=int, default=CORE_WINDOW_DAYS, help="核心学习窗交易日数量")
    parser.add_argument("--buffer-days", type=int, default=BUFFER_WINDOW_DAYS, help="缓冲观察窗交易日数量")
    return parser.parse_args()


def run_distill(*, core_days: int = CORE_WINDOW_DAYS, buffer_days: int = BUFFER_WINDOW_DAYS) -> dict[str, Any]:
    all_dataset = load_rankings()
    window_profile = build_window_profile(all_dataset, core_days=core_days, buffer_days=buffer_days)
    dataset = apply_window_profile(all_dataset, window_profile)
    if len(dataset) < 2:
        raise RuntimeError("Need at least two trade dates to distill templates.")

    trials: list[dict[str, Any]] = []
    for params in build_param_grid():
        item = evaluate_params(dataset, params)
        item["template_class"] = template_class(params)
        trials.append(item)

    trials.sort(
        key=lambda item: (
            tier_rank(item["verdict"]),
            item["metrics"]["business_score"],
            item["metrics"]["front_shift_score"],
            -(item["metrics"]["avg_hit_rank"] or 999.0),
            item["metrics"]["top50_hit_rate"],
            item["metrics"]["top30_hit_rate"],
            item["metrics"]["top100_hit_rate"],
        ),
        reverse=True,
    )

    unique_trials = dedupe_trials(trials)
    passed_templates = [item for item in unique_trials if item["verdict"] in {"pass", "priority"}][:12]
    prototype_templates = [item for item in unique_trials if item["verdict"] == "prototype"][:12]
    top_templates = unique_trials[:12]

    payload = {
        "summary": {
            "trade_date_count": len(dataset),
            "trade_dates": [item["trade_date"] for item in dataset],
            "candidate_size": CANDIDATE_SIZE,
            "template_trial_count": len(trials),
            "unique_template_count": len(unique_trials),
            "passed_template_count": len(passed_templates),
            "priority_template_count": sum(1 for item in unique_trials if item["verdict"] == "priority"),
            "window_mode": window_profile["mode"],
            "core_trade_date_count": window_profile["core_trade_date_count"],
            "buffer_trade_date_count": window_profile["buffer_trade_date_count"],
        },
        "window_profile": window_profile,
        "top_templates": top_templates,
        "passed_templates": passed_templates,
        "prototype_templates": prototype_templates,
    }

    write_json(ARTIFACTS_ROOT / "template_search_latest.json", payload)
    write_markdown(ARTIFACTS_ROOT / "template_search_latest.md", payload)
    write_json(
        TEMPLATES_ROOT / "template_registry.json",
        {
            "version": VERSION,
            "window": {
                "start_date": dataset[0]["trade_date"],
                "end_date": dataset[-1]["trade_date"],
                "trade_date_count": len(dataset),
                **window_profile,
            },
            "templates": passed_templates,
        },
    )

    negative_trials: list[dict[str, Any]] = []
    negative_params_list = build_negative_param_grid()
    negative_base_templates = build_negative_base_templates(unique_trials, passed_templates)
    for base_item in negative_base_templates:
        for veto_params in negative_params_list:
            combo_item = evaluate_params(dataset, base_item["params"], veto_params=veto_params)
            combo_item["template_class"] = base_item["template_class"]
            combo_item["base_template_class"] = base_item["template_class"]
            combo_item["base_metrics"] = base_item["metrics"]
            combo_item["base_verdict"] = base_item["verdict"]
            combo_item["uplift"] = build_combination_uplift(base_item["metrics"], combo_item["metrics"])
            combo_item["combined_recommendation"] = classify_combination(base_item["metrics"], combo_item["metrics"])
            negative_trials.append(combo_item)

    negative_trials.sort(
        key=lambda item: (
            2 if item["combined_recommendation"] == "promote" else 1 if item["combined_recommendation"] == "observe" else 0,
            item["uplift"]["front_shift_score_delta"],
            -item["uplift"]["avg_hit_rank_delta"],
            item["uplift"]["candidate_avg_return_delta"],
            item["uplift"]["candidate_win_rate_delta"],
            item["uplift"]["top50_hit_rate_delta"],
            item["uplift"]["top100_hit_rate_delta"],
            item["metrics"]["business_score"],
        ),
        reverse=True,
    )

    unique_negative_trials = dedupe_trials(negative_trials)
    promoted_combinations = [item for item in unique_negative_trials if item["combined_recommendation"] == "promote"][:12]
    observed_combinations = [item for item in unique_negative_trials if item["combined_recommendation"] == "observe"][:12]
    combined_payload = {
        "summary": {
            "base_template_count": len(negative_base_templates),
            "negative_veto_trial_count": len(negative_params_list),
            "combination_trial_count": len(negative_trials),
            "unique_combination_count": len(unique_negative_trials),
            "promoted_combination_count": len(promoted_combinations),
        },
        "promoted_combinations": promoted_combinations,
        "observed_combinations": observed_combinations,
    }
    write_json(ARTIFACTS_ROOT / "combined_template_search_latest.json", combined_payload)
    write_combined_markdown(ARTIFACTS_ROOT / "combined_template_search_latest.md", combined_payload)
    champion_combination = select_champion_combination(promoted_combinations)
    write_json(
        TEMPLATES_ROOT / "combined_template_registry.json",
        {
            "version": VERSION,
            "window": {
                "start_date": dataset[0]["trade_date"],
                "end_date": dataset[-1]["trade_date"],
                "trade_date_count": len(dataset),
                **window_profile,
            },
            "champion_template_name": champion_combination["template_name"] if champion_combination else None,
            "champion_template": champion_combination,
            "templates": promoted_combinations,
        },
    )

    write_json(ARTIFACTS_ROOT / "distill_window_profile_latest.json", window_profile)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    if passed_templates:
        print(json.dumps(passed_templates[:3], ensure_ascii=False, indent=2))
    print(json.dumps(combined_payload["summary"], ensure_ascii=False, indent=2))
    return {
        "payload": payload,
        "combined_payload": combined_payload,
        "window_profile": window_profile,
    }


def main() -> int:
    args = parse_args()
    run_distill(core_days=args.core_days, buffer_days=args.buffer_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
