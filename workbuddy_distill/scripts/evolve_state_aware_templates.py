from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any

import distill_local_templates as distill


STATE_ARTIFACT = distill.ARTIFACTS_ROOT / "market_state_profile_latest.json"
EVOLUTION_ARTIFACT = distill.ARTIFACTS_ROOT / "state_aware_template_evolution_latest.json"


def _write_json(file_path: Path, payload: object) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _avg_pct(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(float(row["pct_change"]) for row in rows) / len(rows)


def _share_limit(rows: list[dict[str, Any]], threshold: float = 9.9) -> float:
    if not rows:
        return 0.0
    hits = sum(1 for row in rows if float(row["pct_change"]) >= threshold)
    return hits / len(rows)


def _share_st(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    hits = sum(1 for row in rows if "ST" in str(row.get("name", "")).upper())
    return hits / len(rows)


def _market_state_label(profile: dict[str, Any]) -> str:
    front_avg = float(profile["front_avg_pct"])
    front_limit = float(profile["front_limit_share"])
    top100_limit = float(profile["top100_limit_share"])
    front_gap = float(profile["front_mid_gap"])
    mid_avg = float(profile["mid_avg_pct"])
    tail_avg = float(profile["tail_avg_pct"])
    breadth = float(profile["broad_strength"])

    if front_avg >= 15.0 and front_limit >= 0.45 and top100_limit >= 0.3 and front_gap >= 4.0:
        return "trend_expansion"
    if front_avg >= 14.0 and front_limit >= 0.55 and front_gap >= 5.0:
        return "front_holding"
    if mid_avg >= 9.5 and top100_limit >= 0.4 and front_gap <= 3.5:
        return "mid_crowded"
    if front_avg <= 9.0 and breadth <= 6.5 and tail_avg <= 5.5:
        return "risk_off"
    return "rotation_mixed"


def _state_cluster(label: str) -> str:
    if label in {"trend_expansion", "front_holding", "mid_crowded"}:
        return "risk_on"
    if label == "risk_off":
        return "risk_off"
    return "neutral"


def build_market_state_history(dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for day in dataset:
        rows = day["full_rows"]
        top10 = rows[:10]
        top30 = rows[:30]
        top100 = rows[:100]
        middle = rows[19:60]
        tail = rows[60:100]
        profile = {
            "trade_date": day["trade_date"],
            "front_avg_pct": round(_avg_pct(top10), 4),
            "top30_avg_pct": round(_avg_pct(top30), 4),
            "top100_avg_pct": round(_avg_pct(top100), 4),
            "mid_avg_pct": round(_avg_pct(middle), 4),
            "tail_avg_pct": round(_avg_pct(tail), 4),
            "front_limit_share": round(_share_limit(top10), 4),
            "top30_limit_share": round(_share_limit(top30), 4),
            "top100_limit_share": round(_share_limit(top100), 4),
            "st_share_top100": round(_share_st(top100), 4),
            "front_mid_gap": round(_avg_pct(top10) - _avg_pct(middle), 4),
            "mid_tail_gap": round(_avg_pct(middle) - _avg_pct(tail), 4),
            "broad_strength": round(_avg_pct(top100[30:100]), 4),
        }
        profile["state_label"] = _market_state_label(profile)
        profile["state_cluster"] = _state_cluster(profile["state_label"])
        profiles.append(profile)
    return profiles


def _state_match_weight(day_label: str, target_label: str) -> float:
    if day_label == target_label:
        return 1.7
    day_cluster = _state_cluster(day_label)
    target_cluster = _state_cluster(target_label)
    if day_cluster == target_cluster == "risk_on":
        return 1.15
    if day_cluster == target_cluster:
        return 1.0
    if "risk_off" in {day_cluster, target_cluster}:
        return 0.72
    return 0.9


def _state_metrics(evaluations: list[dict[str, Any]], target_state: str) -> dict[str, float]:
    target_days = [item for item in evaluations if item.get("state_label") == target_state]
    if not target_days:
        return {
            "match_day_count": 0.0,
            "candidate_win_rate": 0.0,
            "candidate_avg_return": 0.0,
            "top50_hit_rate": 0.0,
            "front_shift_score": 0.0,
            "hit_day_rate": 0.0,
        }
    total_days = len(target_days)
    total_candidate = sum(float(item["candidate_count"]) for item in target_days)
    total_positive = sum(float(item["candidate_win_rate"]) * float(item["candidate_count"]) for item in target_days)
    total_return = sum(float(item["candidate_avg_return"]) * float(item["candidate_count"]) for item in target_days)
    total_top50 = sum(float(item["top50_hit_count"]) for item in target_days)
    total_front = sum(float(item["front_shift_points"]) for item in target_days)
    hit_days = sum(1.0 for item in target_days if int(item["top100_hit_count"]) > 0)
    return {
        "match_day_count": float(total_days),
        "candidate_win_rate": round(total_positive / total_candidate, 4) if total_candidate else 0.0,
        "candidate_avg_return": round(total_return / total_candidate, 4) if total_candidate else 0.0,
        "top50_hit_rate": round(total_top50 / total_candidate, 4) if total_candidate else 0.0,
        "front_shift_score": round(total_front / (total_candidate * 4), 4) if total_candidate else 0.0,
        "hit_day_rate": round(hit_days / total_days, 4) if total_days else 0.0,
    }


def evaluate_params_state_aware(
    dataset: list[dict[str, Any]],
    state_by_date: dict[str, dict[str, Any]],
    target_state: str,
    params: dict[str, Any],
    veto_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evaluations: list[dict[str, Any]] = []
    total_top100_hits = 0.0
    total_top50_hits = 0.0
    total_top30_hits = 0.0
    total_top10_hits = 0.0
    hit_days = 0
    total_return_count = 0
    total_positive_count = 0.0
    total_candidate_count = 0.0
    total_vetoed_count = 0.0
    all_returns: list[float] = []
    positive_portfolio_days = 0
    total_front_shift_points = 0.0
    total_hit_rank_sum = 0.0
    total_hit_rank_count = 0.0
    total_weighted_days = 0.0
    weighted_return_denominator = 0.0
    weighted_return_sum = 0.0
    weighted_positive_day_score = 0.0
    weighted_hit_day_score = 0.0

    for idx in range(1, len(dataset)):
        history = dataset[:idx]
        target = dataset[idx]
        state_profile = state_by_date[target["trade_date"]]
        state_weight = _state_match_weight(str(state_profile["state_label"]), target_state)
        target_weight = float(target.get("window_weight", 1.0)) * state_weight
        candidates, vetoed_codes = distill.select_candidates(history, params, veto_params=veto_params)
        candidate_codes = [item["code"] for item in candidates]

        top100_hits = [code for code in candidate_codes if code in target["top100_codes"]]
        top50_hits = [code for code in candidate_codes if code in target["top50_codes"]]
        top30_hits = [code for code in candidate_codes if code in target["top30_codes"]]
        top10_hits = [code for code in candidate_codes if code in target["top10_codes"]]
        outcome = distill.summarize_candidate_outcomes(candidate_codes, target)
        hit_ranks = [int(target["row_map"][code]["rank"]) for code in top100_hits if code in target["row_map"]]
        front_shift_points = sum(distill.front_bucket_points(rank) for rank in hit_ranks)

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
                "state_label": state_profile["state_label"],
                "state_cluster": state_profile["state_cluster"],
                "state_weight": round(state_weight, 4),
                "window_role": target.get("window_role", "selected"),
                "window_weight": round(float(target.get("window_weight", 1.0)), 4),
                "effective_weight": round(target_weight, 4),
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
                "candidate_avg_return": round(outcome["avg_return"], 4),
            }
        )

    denominator = total_candidate_count or 1.0
    weighted_day_denominator = total_weighted_days or 1.0
    candidate_win_rate = total_positive_count / denominator if denominator else 0.0
    candidate_avg_return = weighted_return_sum / weighted_return_denominator if weighted_return_denominator else 0.0
    candidate_median_return = mean(sorted(all_returns)[len(all_returns) // 2 : len(all_returns) // 2 + 1]) if all_returns else 0.0
    hit_day_rate = weighted_hit_day_score / weighted_day_denominator if weighted_day_denominator else 0.0
    portfolio_positive_day_rate = weighted_positive_day_score / weighted_day_denominator if weighted_day_denominator else 0.0
    top100_hit_rate = total_top100_hits / denominator if denominator else 0.0
    top50_hit_rate = total_top50_hits / denominator if denominator else 0.0
    top30_hit_rate = total_top30_hits / denominator if denominator else 0.0
    top10_hit_rate = total_top10_hits / denominator if denominator else 0.0
    candidate_retention_rate = total_candidate_count / denominator if denominator else 0.0
    front_shift_score = total_front_shift_points / (denominator * 4) if denominator else 0.0
    avg_hit_rank = total_hit_rank_sum / total_hit_rank_count if total_hit_rank_count else None
    rank_quality_score = ((101 - avg_hit_rank) / 100) if avg_hit_rank is not None else 0.0

    gain_loss_ratio = None
    gains = [value for value in all_returns if value > 0]
    losses = [value for value in all_returns if value < 0]
    if gains and losses:
        gain_loss_ratio = round((sum(gains) / len(gains)) / abs(sum(losses) / len(losses)), 4)

    verdict, recommended_action = distill.classify_verdict(top100_hit_rate, top30_hit_rate, hit_day_rate)
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
    business_score = round(
        quality_score
        + candidate_win_rate * 20
        + candidate_avg_return * 4
        + portfolio_positive_day_rate * 12,
        4,
    )
    state_focus = _state_metrics(evaluations, target_state)
    state_fit_score = round(
        state_focus["top50_hit_rate"] * 140
        + state_focus["front_shift_score"] * 100
        + state_focus["candidate_win_rate"] * 25
        + state_focus["candidate_avg_return"] * 5
        + state_focus["hit_day_rate"] * 20,
        4,
    )
    state_aware_score = round(business_score * 0.65 + state_fit_score * 0.35, 4)

    return {
        "template_name": distill.build_template_name(params)
        + (f"__veto__{distill.build_veto_name(veto_params)}" if veto_params else ""),
        "base_template_name": distill.build_template_name(params),
        "negative_veto": veto_params,
        "family": params.get("family", "tdx_carry"),
        "params": params,
        "metrics": {
            "evaluation_days": len(evaluations),
            "weighted_evaluation_days": round(weighted_day_denominator, 4),
            "candidate_size": distill.CANDIDATE_SIZE,
            "avg_candidate_count": round(total_candidate_count / weighted_day_denominator, 4) if weighted_day_denominator else 0.0,
            "candidate_retention_rate": round(candidate_retention_rate, 4),
            "vetoed_candidate_count": round(total_vetoed_count, 4),
            "avg_vetoed_per_day": round(total_vetoed_count / weighted_day_denominator, 4) if weighted_day_denominator else 0.0,
            "top100_hit_count": round(total_top100_hits, 4),
            "top50_hit_count": round(total_top50_hits, 4),
            "top30_hit_count": round(total_top30_hits, 4),
            "top10_hit_count": round(total_top10_hits, 4),
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
            "business_score": business_score,
            "state_fit_score": state_fit_score,
            "state_aware_score": state_aware_score,
        },
        "state_focus": state_focus,
        "verdict": verdict,
        "recommended_action": recommended_action,
        "daily_evaluations": evaluations,
    }


def _state_trial_rank(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        distill.tier_rank(item["verdict"]),
        float(item["metrics"]["state_aware_score"]),
        float(item["metrics"]["candidate_win_rate"]),
        float(item["metrics"]["candidate_avg_return"]),
        float(item["metrics"]["front_shift_score"]),
        -float(item["metrics"]["avg_hit_rank"] or 999.0),
    )


def _state_combo_rank(item: dict[str, Any]) -> tuple[Any, ...]:
    recommendation_rank = {
        "promote": 3,
        "observe": 2,
        "reject": 1,
    }.get(str(item.get("combined_recommendation", "")), 0)
    return (
        recommendation_rank,
        float(item["metrics"]["state_aware_score"]),
        float(item["uplift"]["state_aware_score_delta"]),
        float(item["uplift"]["candidate_avg_return_delta"]),
        float(item["uplift"]["front_shift_score_delta"]),
        float(item["uplift"]["top50_hit_rate_delta"]),
    )


def _build_state_uplift(base_metrics: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    uplift = distill.build_combination_uplift(base_metrics, metrics)
    uplift["state_fit_score_delta"] = round(metrics["state_fit_score"] - base_metrics["state_fit_score"], 4)
    uplift["state_aware_score_delta"] = round(metrics["state_aware_score"] - base_metrics["state_aware_score"], 4)
    return uplift


def _pick_state_champion(promoted: list[dict[str, Any]], observed: list[dict[str, Any]], incumbent_name: str) -> dict[str, Any] | None:
    for pool in (promoted, observed):
        for item in pool:
            if item["template_name"] != incumbent_name:
                return item
    for pool in (promoted, observed):
        if pool:
            return pool[0]
    return None


def _markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# State Aware Template Evolution")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Target state: `{payload['target_state']['state_label']}`")
    lines.append(f"- Target cluster: `{payload['target_state']['state_cluster']}`")
    lines.append(f"- Base trials: `{payload['summary']['base_trial_count']}`")
    lines.append(f"- Combo trials: `{payload['summary']['combo_trial_count']}`")
    lines.append(f"- Promoted combos: `{payload['summary']['promoted_combo_count']}`")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"- Action: `{payload['suggestion']['action']}`")
    lines.append(f"- Champion: `{payload['suggestion']['champion_template']}`")
    if payload["suggestion"].get("incumbent_template"):
        lines.append(f"- Incumbent: `{payload['suggestion']['incumbent_template']}`")
    lines.append(f"- Reason: {payload['suggestion']['reason']}")
    lines.append("")
    lines.append("## Top Candidates")
    lines.append("")
    lines.append("| Template | Recommend | State Score | Win Rate | Avg Return | Top50 Hit | Front Shift |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for item in payload["top_candidates"][:8]:
        metrics = item["metrics"]
        lines.append(
            "| "
            f"{item['template_name']} | "
            f"{item.get('combined_recommendation', item.get('verdict', '-'))} | "
            f"{metrics['state_aware_score']:.2f} | "
            f"{metrics['candidate_win_rate'] * 100:.2f}% | "
            f"{metrics['candidate_avg_return']:.2f}% | "
            f"{metrics['top50_hit_rate'] * 100:.2f}% | "
            f"{metrics['front_shift_score'] * 100:.2f}% |"
        )
    lines.append("")
    lines.append("## Market States")
    lines.append("")
    for item in payload["market_states"][-8:]:
        lines.append(
            f"- `{item['trade_date']}`: `{item['state_label']}` "
            f"(front_avg={item['front_avg_pct']:.2f}, mid_avg={item['mid_avg_pct']:.2f}, "
            f"top100_limit={item['top100_limit_share']:.2f})"
        )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evolve state-aware distill templates against the current champion.")
    parser.add_argument("--core-days", type=int, default=20, help="核心窗口交易日数量")
    parser.add_argument("--buffer-days", type=int, default=2, help="缓冲窗口交易日数量")
    parser.add_argument(
        "--registry",
        type=Path,
        default=distill.TEMPLATES_ROOT / "combined_template_registry.json",
        help="现冠军注册表路径",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=EVOLUTION_ARTIFACT,
        help="输出 JSON 路径",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    all_dataset = distill.load_rankings()
    state_history = build_market_state_history(all_dataset)
    _write_json(STATE_ARTIFACT, state_history)
    state_by_date = {item["trade_date"]: item for item in state_history}

    window_profile = distill.build_window_profile(all_dataset, core_days=args.core_days, buffer_days=args.buffer_days)
    dataset = distill.apply_window_profile(all_dataset, window_profile)
    target_state = state_by_date[dataset[-1]["trade_date"]]

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    incumbent_raw = registry["champion_template"]
    incumbent = evaluate_params_state_aware(
        dataset,
        state_by_date,
        target_state["state_label"],
        incumbent_raw["params"],
        incumbent_raw["negative_veto"],
    )

    base_trials = [
        evaluate_params_state_aware(dataset, state_by_date, target_state["state_label"], params)
        for params in distill.build_param_grid()
    ]
    base_trials.sort(key=_state_trial_rank, reverse=True)
    unique_base_trials = distill.dedupe_trials(base_trials)
    passed_templates = [item for item in unique_base_trials if item["verdict"] in {"pass", "priority"}][:8]
    negative_bases = distill.build_negative_base_templates(unique_base_trials, passed_templates)[:8]

    negative_params = [
        item
        for item in distill.build_negative_param_grid()
        if item["family"] in {"crowded_mid_veto", "fake_head_veto", "rear_hit_veto"}
    ]

    combo_trials: list[dict[str, Any]] = []
    for base_item in negative_bases:
        for veto_params in negative_params:
            combo_item = evaluate_params_state_aware(
                dataset,
                state_by_date,
                target_state["state_label"],
                base_item["params"],
                veto_params,
            )
            combo_item["template_class"] = distill.template_class(base_item["params"])
            combo_item["base_verdict"] = base_item["verdict"]
            combo_item["combined_recommendation"] = distill.classify_combination(
                incumbent["metrics"],
                combo_item["metrics"],
            )
            combo_item["uplift"] = _build_state_uplift(incumbent["metrics"], combo_item["metrics"])
            combo_trials.append(combo_item)

    combo_trials.sort(key=_state_combo_rank, reverse=True)
    unique_combo_trials = distill.dedupe_trials(combo_trials)
    promoted = [item for item in unique_combo_trials if item["combined_recommendation"] == "promote"][:12]
    observed = [item for item in unique_combo_trials if item["combined_recommendation"] == "observe"][:12]
    challenger = _pick_state_champion(promoted, observed, incumbent["template_name"])

    if challenger and challenger["template_name"] != incumbent["template_name"] and challenger in promoted:
        suggestion = {
            "action": "replace_incumbent",
            "champion_template": challenger["template_name"],
            "incumbent_template": incumbent["template_name"],
            "reason": "状态感知评分和晋级守门同时通过，新模板可以打败现冠军。",
        }
    elif challenger and challenger["template_name"] != incumbent["template_name"]:
        suggestion = {
            "action": "keep_incumbent_add_shadow",
            "champion_template": incumbent["template_name"],
            "incumbent_template": None,
            "reason": "状态感知下出现更强观察者，但还没有强到可以直接推翻现冠军。",
        }
    else:
        suggestion = {
            "action": "keep_incumbent",
            "champion_template": incumbent["template_name"],
            "incumbent_template": None,
            "reason": "状态感知二轮进化后，仍没有新模板打败现冠军。",
        }

    top_candidates = []
    if challenger:
        top_candidates.append(challenger)
    top_candidates.append(incumbent)
    top_candidates.extend(promoted[:4])
    top_candidates.extend(observed[:4])
    deduped_top: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for item in top_candidates:
        name = item["template_name"]
        if name in seen_names:
            continue
        seen_names.add(name)
        deduped_top.append(item)

    payload = {
        "window_profile": window_profile,
        "target_state": target_state,
        "summary": {
            "base_trial_count": len(base_trials),
            "unique_base_trial_count": len(unique_base_trials),
            "negative_base_count": len(negative_bases),
            "combo_trial_count": len(combo_trials),
            "unique_combo_count": len(unique_combo_trials),
            "promoted_combo_count": len(promoted),
            "observed_combo_count": len(observed),
        },
        "incumbent": incumbent,
        "challenger": challenger,
        "suggestion": suggestion,
        "market_states": state_history,
        "top_candidates": deduped_top[:10],
        "top_promoted": promoted[:6],
        "top_observed": observed[:6],
    }
    _write_json(args.output, payload)
    args.output.with_suffix(".md").write_text(_markdown(payload), encoding="utf-8")
    print(str(STATE_ARTIFACT))
    print(str(args.output))
    print(str(args.output.with_suffix(".md")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
