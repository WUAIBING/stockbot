from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import distill_local_templates as distill


EVOLUTION_ARTIFACT = distill.ARTIFACTS_ROOT / "distill_champion_evolution_latest.json"
ROOT = Path(__file__).resolve().parents[2]
LIVE_PROXY_WINDOWS = (10, 21)
LIVE_PROXY_TOP_N = 5


def _read_json(file_path: Path) -> dict[str, Any]:
    return json.loads(file_path.read_text(encoding="utf-8"))


def _write_json(file_path: Path, payload: object) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _metric(item: dict[str, Any], key: str, default: float = 0.0) -> float:
    metrics = item.get("metrics", {})
    if not isinstance(metrics, dict):
        return default
    try:
        return float(metrics.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _live_proxy_metric(item: dict[str, Any], window: int, key: str, default: float = 0.0) -> float:
    live_proxy = item.get("live_proxy", {})
    if not isinstance(live_proxy, dict):
        return default
    window_payload = live_proxy.get(f"{window}d", {})
    if not isinstance(window_payload, dict):
        return default
    try:
        return float(window_payload.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _live_proxy_total_advantage(item: dict[str, Any]) -> float:
    total = 0.0
    for window in LIVE_PROXY_WINDOWS:
        total += _live_proxy_metric(item, window, "shadow_minus_prod_candidate_avg_return_pct", 0.0)
        total += _live_proxy_metric(item, window, "shadow_minus_prod_candidate_win_rate_pct", 0.0) / 10.0
        total += _live_proxy_metric(item, window, "shadow_minus_prod_cumulative_day_return_pct", 0.0) / 5.0
    return round(total, 4)


def _candidate_stats(returns: list[float]) -> dict[str, Any]:
    positive = [value for value in returns if value > 0]
    return {
        "return_count": len(returns),
        "positive_count": len(positive),
        "win_rate_pct": round((len(positive) / len(returns) * 100.0), 4) if returns else 0.0,
        "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else 0.0,
    }


def _run_live_proxy(
    dataset: list[dict[str, Any]],
    template: dict[str, Any],
    *,
    recent_days: int,
    top_n: int,
) -> dict[str, Any]:
    recent_window = max(1, min(recent_days, len(dataset) - 1))
    start_idx = max(1, len(dataset) - 1 - recent_window)
    day_rows: list[dict[str, Any]] = []
    all_returns: list[float] = []

    for selection_idx in range(start_idx, len(dataset) - 1):
        selection_day = dataset[selection_idx]
        target_day = dataset[selection_idx + 1]
        history = dataset[: selection_idx + 1]
        candidates, vetoed_codes = distill.select_candidates(
            history,
            template["params"],
            veto_params=template["negative_veto"],
        )
        selected_codes = [entry["code"] for entry in candidates[:top_n]]
        outcome = distill.summarize_candidate_outcomes(selected_codes, target_day)
        all_returns.extend(outcome["returns"])
        day_rows.append(
            {
                "selection_trade_date": selection_day["trade_date"],
                "evaluation_trade_date": target_day["trade_date"],
                "selected_codes": selected_codes,
                "selected_count": len(selected_codes),
                "vetoed_count": len(vetoed_codes),
                "avg_return_pct": outcome["avg_return"],
                "win_rate_pct": round((outcome["positive_count"] / outcome["return_count"] * 100.0), 4)
                if outcome["return_count"]
                else 0.0,
            }
        )

    candidate_stats = _candidate_stats(all_returns)
    positive_days = [row for row in day_rows if row["avg_return_pct"] > 0]
    return {
        "recent_days": recent_window,
        "evaluation_day_count": len(day_rows),
        "candidate_win_rate_pct": candidate_stats["win_rate_pct"],
        "candidate_avg_return_pct": candidate_stats["avg_return_pct"],
        "positive_day_rate_pct": round((len(positive_days) / len(day_rows) * 100.0), 4) if day_rows else 0.0,
        "avg_day_return_pct": round(sum(row["avg_return_pct"] for row in day_rows) / len(day_rows), 4) if day_rows else 0.0,
        "cumulative_day_return_pct": round(sum(row["avg_return_pct"] for row in day_rows), 4) if day_rows else 0.0,
    }


def _build_live_proxy_summary(production: dict[str, Any], challenger: dict[str, Any]) -> dict[str, Any]:
    return {
        "shadow_minus_prod_candidate_win_rate_pct": round(
            challenger["candidate_win_rate_pct"] - production["candidate_win_rate_pct"],
            4,
        ),
        "shadow_minus_prod_candidate_avg_return_pct": round(
            challenger["candidate_avg_return_pct"] - production["candidate_avg_return_pct"],
            4,
        ),
        "shadow_minus_prod_avg_day_return_pct": round(
            challenger["avg_day_return_pct"] - production["avg_day_return_pct"],
            4,
        ),
        "shadow_minus_prod_cumulative_day_return_pct": round(
            challenger["cumulative_day_return_pct"] - production["cumulative_day_return_pct"],
            4,
        ),
        "shadow_minus_prod_positive_day_rate_pct": round(
            challenger["positive_day_rate_pct"] - production["positive_day_rate_pct"],
            4,
        ),
    }


def _attach_live_proxy(item: dict[str, Any], incumbent: dict[str, Any], live_dataset: list[dict[str, Any]]) -> None:
    template = {
        "template_name": item["template_name"],
        "base_template_name": item["base_template_name"],
        "params": item["params"],
        "negative_veto": item["negative_veto"],
    }
    incumbent_template = {
        "template_name": incumbent["template_name"],
        "base_template_name": incumbent["base_template_name"],
        "params": incumbent["params"],
        "negative_veto": incumbent["negative_veto"],
    }
    live_proxy: dict[str, Any] = {}
    for window in LIVE_PROXY_WINDOWS:
        production_payload = _run_live_proxy(live_dataset, incumbent_template, recent_days=window, top_n=LIVE_PROXY_TOP_N)
        challenger_payload = _run_live_proxy(live_dataset, template, recent_days=window, top_n=LIVE_PROXY_TOP_N)
        summary = _build_live_proxy_summary(production_payload, challenger_payload)
        live_proxy[f"{window}d"] = {
            **summary,
            "production": production_payload,
            "candidate": challenger_payload,
        }
    item["live_proxy"] = live_proxy
    item["live_proxy_advantage"] = _live_proxy_total_advantage(item)
    item["live_proxy_replace_ready"] = all(
        _live_proxy_metric(item, window, "shadow_minus_prod_candidate_avg_return_pct", -999.0) >= 0.0
        and _live_proxy_metric(item, window, "shadow_minus_prod_cumulative_day_return_pct", -999.0) >= 0.0
        for window in LIVE_PROXY_WINDOWS
    )


def _stage_rank(item: dict[str, Any]) -> tuple[Any, ...]:
    recommendation_rank = {
        "promote": 3,
        "observe": 2,
        "baseline": 1,
        "reject": 0,
        "downgrade": -1,
    }.get(str(item.get("evolution_recommendation", "")), -1)
    return (
        recommendation_rank,
        1 if bool(item.get("live_proxy_replace_ready")) else 0,
        _live_proxy_total_advantage(item),
        _metric(item, "candidate_win_rate"),
        _metric(item, "candidate_avg_return"),
        _metric(item, "front_shift_score"),
        _metric(item, "top50_hit_rate"),
        _metric(item, "business_score"),
        -_metric(item, "avg_hit_rank", 999.0),
    )


def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _evaluate_candidate(
    dataset: list[dict[str, Any]],
    live_dataset: list[dict[str, Any]],
    incumbent: dict[str, Any],
    params: dict[str, Any],
    veto_params: dict[str, Any],
    *,
    stage: str,
) -> dict[str, Any]:
    item = distill.evaluate_params(dataset, params, veto_params=veto_params)
    item["template_class"] = distill.template_class(params)
    item["base_template_name"] = distill.build_template_name(params)
    item["negative_veto_name"] = distill.build_veto_name(veto_params)
    item["evolution_stage"] = stage
    item["uplift_vs_incumbent"] = distill.build_combination_uplift(incumbent["metrics"], item["metrics"])
    item["is_incumbent"] = (
        json.dumps(params, sort_keys=True) == json.dumps(incumbent["params"], sort_keys=True)
        and json.dumps(veto_params, sort_keys=True) == json.dumps(incumbent["negative_veto"], sort_keys=True)
    )
    item["evolution_recommendation"] = (
        "baseline"
        if item["is_incumbent"]
        else distill.classify_combination(incumbent["metrics"], item["metrics"])
    )
    _attach_live_proxy(item, incumbent, live_dataset)
    if not item["is_incumbent"]:
        if item["evolution_recommendation"] == "promote" and not item.get("live_proxy_replace_ready"):
            item["evolution_recommendation"] = "observe"
        elif item["evolution_recommendation"] == "observe" and item.get("live_proxy_replace_ready"):
            item["evolution_recommendation"] = "promote"
    return item


def _sort_and_dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items.sort(key=_stage_rank, reverse=True)
    return distill.dedupe_trials(items)


def _int_variants(center: int, deltas: tuple[int, ...], *, lower: int, upper: int) -> list[int]:
    values = {max(lower, min(upper, center + delta)) for delta in deltas}
    return sorted(values)


def _float_variants(center: float, deltas: tuple[float, ...], *, lower: float, upper: float) -> list[float]:
    values = {round(max(lower, min(upper, center + delta)), 1) for delta in deltas}
    return sorted(values)


def _build_base_neighbors(params: dict[str, Any]) -> list[dict[str, Any]]:
    family = params.get("family")
    if family != "gap_mix":
        return [params]

    head = int(params["head"])
    mid_skip = int(params["mid_skip"])
    end_rank = int(params["end_rank"])
    candidates: list[dict[str, Any]] = [dict(params)]

    for value in _int_variants(head, (-2, 2), lower=6, upper=20):
        candidates.append({"family": family, "head": value, "mid_skip": mid_skip, "end_rank": end_rank})
    for value in _int_variants(mid_skip, (-2, 2), lower=20, upper=60):
        candidates.append({"family": family, "head": head, "mid_skip": value, "end_rank": end_rank})
    for value in _int_variants(end_rank, (-2, 2), lower=30, upper=70):
        candidates.append({"family": family, "head": head, "mid_skip": mid_skip, "end_rank": value})

    for head_value, skip_value, end_value in (
        (head - 2, mid_skip - 2, end_rank - 2),
        (head + 2, mid_skip + 2, end_rank + 2),
        (head, mid_skip - 2, end_rank + 2),
        (head, mid_skip + 2, end_rank + 2),
    ):
        if skip_value < 20 or end_value > 70 or head_value < 6:
            continue
        candidates.append(
            {
                "family": family,
                "head": head_value,
                "mid_skip": skip_value,
                "end_rank": end_value,
            }
        )

    unique = []
    for item in _dedupe_dicts(candidates):
        if int(item["end_rank"]) <= int(item["mid_skip"]):
            continue
        unique.append(item)
    return unique


def _build_veto_neighbors(veto_params: dict[str, Any]) -> list[dict[str, Any]]:
    family = veto_params.get("family")
    candidates: list[dict[str, Any]] = [dict(veto_params)]
    if family == "crowded_mid_veto":
        lookback = int(veto_params["lookback"])
        mid_start = int(veto_params["mid_start"])
        mid_end = int(veto_params["mid_end"])
        min_prev_pct = float(veto_params["min_prev_pct"])
        fail_rank_min = int(veto_params["fail_rank_min"])
        fail_pct_max = float(veto_params["fail_pct_max"])
        min_failures = int(veto_params["min_failures"])

        for value in _int_variants(lookback, (-2, 3), lower=1, upper=8):
            candidates.append({**veto_params, "lookback": value})
        for value in _int_variants(mid_start, (-5, 5), lower=10, upper=40):
            candidates.append({**veto_params, "mid_start": value})
        for value in _int_variants(mid_end, (-10, 10), lower=35, upper=80):
            candidates.append({**veto_params, "mid_end": value})
        for value in _float_variants(min_prev_pct, (-0.5, 0.5, 1.0), lower=1.0, upper=5.0):
            candidates.append({**veto_params, "min_prev_pct": value})
        for value in _int_variants(fail_rank_min, (-20, 20), lower=80, upper=180):
            candidates.append({**veto_params, "fail_rank_min": value})
        for value in sorted({round(fail_pct_max, 1), -0.5, -1.0}):
            if value > 0:
                continue
            candidates.append({**veto_params, "fail_pct_max": value})
        for value in sorted({1, min_failures, min_failures + 1}):
            if value < 1:
                continue
            candidates.append({**veto_params, "min_failures": value})

        for start_value, end_value in (
            (mid_start - 5, mid_end - 10),
            (mid_start - 5, mid_end),
            (mid_start + 5, mid_end),
            (mid_start + 5, mid_end + 10),
        ):
            candidates.append({**veto_params, "mid_start": start_value, "mid_end": end_value})

        for alt in (
            {
                "family": "fake_head_veto",
                "lookback": max(3, lookback),
                "head_cutoff": 20,
                "min_prev_pct": max(2.0, min_prev_pct),
                "fail_rank_min": fail_rank_min,
                "fail_pct_max": 0.0,
                "min_failures": min(2, max(1, min_failures)),
            },
            {
                "family": "fake_head_veto",
                "lookback": max(3, lookback),
                "head_cutoff": 25,
                "min_prev_pct": max(2.0, min_prev_pct + 1.0),
                "fail_rank_min": fail_rank_min + 20,
                "fail_pct_max": -0.5,
                "min_failures": min(2, max(1, min_failures)),
            },
            {
                "family": "rear_hit_veto",
                "lookback": max(3, lookback),
                "rear_start": 70,
                "rear_end": 120,
                "min_prev_pct": max(2.0, min_prev_pct),
                "fail_front_rank": 30,
                "fail_pct_max": 0.0,
                "min_failures": min(2, max(1, min_failures)),
            },
            {
                "family": "rear_hit_veto",
                "lookback": max(3, lookback),
                "rear_start": 80,
                "rear_end": 140,
                "min_prev_pct": max(2.0, min_prev_pct + 1.0),
                "fail_front_rank": 40,
                "fail_pct_max": 0.0,
                "min_failures": min(2, max(1, min_failures)),
            },
            {
                "family": "front_exhaustion_veto",
                "lookback": max(2, min(5, lookback)),
                "head_cutoff": 8,
                "min_prev_pct": max(3.0, min_prev_pct + 1.0),
                "fail_rank_min": 80,
                "fail_pct_max": 0.0,
                "min_failures": 1,
                "exclude_risk_warning": True,
            },
            {
                "family": "front_exhaustion_veto",
                "lookback": max(2, min(5, lookback)),
                "head_cutoff": 8,
                "min_prev_pct": max(3.0, min_prev_pct + 1.0),
                "fail_rank_min": 80,
                "fail_pct_max": 0.0,
                "min_failures": 1,
                "exclude_risk_warning": False,
            },
            {
                "family": "front_exhaustion_veto",
                "lookback": max(2, min(5, lookback)),
                "head_cutoff": 10,
                "min_prev_pct": max(4.0, min_prev_pct + 1.0),
                "fail_rank_min": 100,
                "fail_pct_max": -0.5,
                "min_failures": min(2, max(1, min_failures)),
                "exclude_risk_warning": True,
            },
            {
                "family": "front_exhaustion_veto",
                "lookback": max(2, min(5, lookback)),
                "head_cutoff": 10,
                "min_prev_pct": max(4.0, min_prev_pct + 1.0),
                "fail_rank_min": 100,
                "fail_pct_max": -0.5,
                "min_failures": min(2, max(1, min_failures)),
                "exclude_risk_warning": False,
            },
        ):
            candidates.append(alt)
    elif family == "front_exhaustion_veto":
        lookback = int(veto_params["lookback"])
        head_cutoff = int(veto_params["head_cutoff"])
        min_prev_pct = float(veto_params["min_prev_pct"])
        fail_rank_min = int(veto_params["fail_rank_min"])
        fail_pct_max = float(veto_params["fail_pct_max"])
        min_failures = int(veto_params["min_failures"])

        for value in _int_variants(lookback, (-1, 1, 2), lower=1, upper=8):
            candidates.append({**veto_params, "lookback": value})
        for value in _int_variants(head_cutoff, (-2, 2), lower=3, upper=15):
            candidates.append({**veto_params, "head_cutoff": value})
        for value in _float_variants(min_prev_pct, (-1.0, -0.5, 0.5, 1.0), lower=2.0, upper=8.0):
            candidates.append({**veto_params, "min_prev_pct": value})
        for value in _int_variants(fail_rank_min, (-20, 20), lower=30, upper=140):
            candidates.append({**veto_params, "fail_rank_min": value})
        for value in sorted({round(fail_pct_max, 1), 0.0, -0.5, -1.0}):
            if value > 0:
                continue
            candidates.append({**veto_params, "fail_pct_max": value})
        for value in sorted({1, min_failures, min_failures + 1}):
            if value < 1:
                continue
            candidates.append({**veto_params, "min_failures": value})
        candidates.append({**veto_params, "exclude_risk_warning": True})
        candidates.append({**veto_params, "exclude_risk_warning": False})
        candidates.append(
            {
                "family": "crowded_mid_veto",
                "lookback": max(3, lookback),
                "mid_start": 25,
                "mid_end": 60,
                "min_prev_pct": max(2.0, min_prev_pct - 1.0),
                "fail_rank_min": max(100, fail_rank_min),
                "fail_pct_max": min(0.0, fail_pct_max),
                "min_failures": min(2, max(1, min_failures)),
                "exclude_risk_warning": True,
            }
        )

    unique: list[dict[str, Any]] = []
    for item in _dedupe_dicts(candidates):
        item = dict(item)
        if item["family"] == "crowded_mid_veto":
            if int(item["mid_end"]) <= int(item["mid_start"]) + 10:
                continue
            if int(item["min_failures"]) > int(item["lookback"]):
                item["min_failures"] = int(item["lookback"])
        elif item["family"] == "fake_head_veto":
            if int(item["min_failures"]) > int(item["lookback"]):
                item["min_failures"] = int(item["lookback"])
        elif item["family"] == "rear_hit_veto":
            if int(item["rear_end"]) <= int(item["rear_start"]) + 10:
                continue
            if int(item["min_failures"]) > int(item["lookback"]):
                item["min_failures"] = int(item["lookback"])
        elif item["family"] == "front_exhaustion_veto":
            if int(item["min_failures"]) > int(item["lookback"]):
                item["min_failures"] = int(item["lookback"])
        unique.append(item)
    return _dedupe_dicts(unique)


def _load_incumbent(args: argparse.Namespace) -> tuple[dict[str, Any], int, int]:
    if args.registry.exists():
        registry = _read_json(args.registry)
        incumbent = registry.get("champion_template")
        if isinstance(incumbent, dict):
            window = registry.get("window", {})
            core_days = int(args.core_days or window.get("requested_core_days") or distill.CORE_WINDOW_DAYS)
            buffer_days = int(args.buffer_days or window.get("requested_buffer_days") or distill.BUFFER_WINDOW_DAYS)
            return incumbent, core_days, buffer_days

    if EVOLUTION_ARTIFACT.exists():
        payload = _read_json(EVOLUTION_ARTIFACT)
        incumbent = payload.get("incumbent")
        if isinstance(incumbent, dict):
            window = payload.get("window", {})
            core_days = int(args.core_days or window.get("core_days") or distill.CORE_WINDOW_DAYS)
            buffer_days = int(args.buffer_days or window.get("buffer_days") or distill.BUFFER_WINDOW_DAYS)
            return incumbent, core_days, buffer_days

    raise RuntimeError("未找到可用冠军模板，请检查 registry 或 distill_champion_evolution_latest.json")


def _take_top_params(items: list[dict[str, Any]], key: str, limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        value = item.get(key)
        if not isinstance(value, dict):
            continue
        signature = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _summarize_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "template_name": item.get("template_name"),
        "base_template_name": item.get("base_template_name"),
        "negative_veto_name": item.get("negative_veto_name")
        or (
            distill.build_veto_name(item["negative_veto"])
            if isinstance(item.get("negative_veto"), dict)
            else None
        ),
        "evolution_stage": item.get("evolution_stage"),
        "evolution_recommendation": item.get("evolution_recommendation"),
        "is_incumbent": bool(item.get("is_incumbent") or item.get("evolution_recommendation") == "baseline"),
        "params": item.get("params"),
        "negative_veto": item.get("negative_veto"),
        "metrics": item.get("metrics"),
        "uplift_vs_incumbent": item.get("uplift_vs_incumbent"),
        "live_proxy": item.get("live_proxy"),
        "live_proxy_advantage": item.get("live_proxy_advantage"),
        "live_proxy_replace_ready": item.get("live_proxy_replace_ready"),
    }


def _format_pct(value: float, *, scale_100: bool = True) -> str:
    shown = value * 100 if scale_100 else value
    return f"{shown:.2f}%"


def _format_signed_pct(value: float, *, scale_100: bool = True) -> str:
    shown = value * 100 if scale_100 else value
    sign = "+" if shown >= 0 else ""
    return f"{sign}{shown:.2f}%"


def _markdown_report(payload: dict[str, Any]) -> str:
    incumbent = payload["incumbent"]
    suggestion = payload["suggestion"]
    lines: list[str] = []
    lines.append("# Distill Champion Evolution")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Window: `20+{payload['window']['buffer_days']}`")
    lines.append(f"- Incumbent: `{incumbent['template_name']}`")
    lines.append(f"- Base neighbors tried: `{payload['summary']['base_trial_count']}`")
    lines.append(f"- Veto neighbors tried: `{payload['summary']['veto_trial_count']}`")
    lines.append(f"- Hybrid neighbors tried: `{payload['summary']['hybrid_trial_count']}`")
    lines.append(f"- Unique candidates: `{payload['summary']['unique_candidate_count']}`")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append(f"- Action: `{suggestion['action']}`")
    lines.append(f"- Production: `{suggestion['production_template']}`")
    if suggestion.get("shadow_template"):
        lines.append(f"- Shadow: `{suggestion['shadow_template']}`")
    lines.append(f"- Reason: {suggestion['reason']}")
    lines.append("")
    lines.append("## Top Candidates")
    lines.append("")
    lines.append("| Stage | Template | Recommend | Win Rate | Avg Return | Top50 Hit | Front Shift | 10D Ret | 21D Ret |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for item in payload["top_candidates"][:10]:
        metrics = item["metrics"]
        lines.append(
            "| "
            f"{item['evolution_stage']} | "
            f"{item['template_name']} | "
            f"{item['evolution_recommendation']} | "
            f"{_format_pct(float(metrics['candidate_win_rate']))} | "
            f"{_format_pct(float(metrics['candidate_avg_return']), scale_100=False)} | "
            f"{_format_pct(float(metrics['top50_hit_rate']))} | "
            f"{_format_pct(float(metrics['front_shift_score']))} | "
            f"{_format_signed_pct(float(item.get('live_proxy', {}).get('10d', {}).get('shadow_minus_prod_candidate_avg_return_pct', 0.0)), scale_100=False)} | "
            f"{_format_signed_pct(float(item.get('live_proxy', {}).get('21d', {}).get('shadow_minus_prod_candidate_avg_return_pct', 0.0)), scale_100=False)} |"
        )
    lines.append("")
    lines.append("## Stage Notes")
    lines.append("")
    lines.append(
        f"- Base best: `{payload['stage_best']['base']['template_name']}` -> `{payload['stage_best']['base']['evolution_recommendation']}`"
    )
    lines.append(
        f"- Veto best: `{payload['stage_best']['veto']['template_name']}` -> `{payload['stage_best']['veto']['evolution_recommendation']}`"
    )
    lines.append(
        f"- Hybrid best: `{payload['stage_best']['hybrid']['template_name']}` -> `{payload['stage_best']['hybrid']['evolution_recommendation']}`"
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evolve the current distill champion with a local neighborhood search.")
    parser.add_argument(
        "--registry",
        type=Path,
        default=distill.TEMPLATES_ROOT / "combined_template_registry.json",
        help="冠军模板注册表路径",
    )
    parser.add_argument("--core-days", type=int, default=None, help="覆盖冠军注册表中的核心窗口")
    parser.add_argument("--buffer-days", type=int, default=None, help="覆盖冠军注册表中的缓冲窗口")
    parser.add_argument(
        "--output",
        type=Path,
        default=distill.ARTIFACTS_ROOT / "distill_champion_evolution_latest.json",
        help="输出 JSON 路径",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=None,
        help="输出 Markdown 路径，默认与 JSON 同名 .md",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    incumbent, core_days, buffer_days = _load_incumbent(args)
    full_dataset = distill.load_rankings()

    dataset = distill.apply_window_profile(
        full_dataset,
        distill.build_window_profile(full_dataset, core_days=core_days, buffer_days=buffer_days),
    )

    base_neighbors = _build_base_neighbors(incumbent["params"])
    veto_neighbors = _build_veto_neighbors(incumbent["negative_veto"])

    base_trials = [
        _evaluate_candidate(dataset, full_dataset, incumbent, params, incumbent["negative_veto"], stage="base")
        for params in base_neighbors
    ]
    veto_trials = [
        _evaluate_candidate(dataset, full_dataset, incumbent, incumbent["params"], veto_params, stage="veto")
        for veto_params in veto_neighbors
    ]
    base_trials = _sort_and_dedupe(base_trials)
    veto_trials = _sort_and_dedupe(veto_trials)

    top_base_params = _take_top_params(base_trials, "params", limit=5)
    top_veto_params = _take_top_params(veto_trials, "negative_veto", limit=5)
    hybrid_trials: list[dict[str, Any]] = []
    for params in top_base_params:
        for veto_params in top_veto_params:
            hybrid_trials.append(_evaluate_candidate(dataset, full_dataset, incumbent, params, veto_params, stage="hybrid"))
    hybrid_trials = _sort_and_dedupe(hybrid_trials)

    all_trials = _sort_and_dedupe(base_trials + veto_trials + hybrid_trials)
    promoted = [item for item in all_trials if item.get("evolution_recommendation") == "promote"]
    observed = [item for item in all_trials if item.get("evolution_recommendation") == "observe"]

    replacement = next(
        (
            item
            for item in promoted
            if not item.get("is_incumbent") and bool(item.get("live_proxy_replace_ready"))
        ),
        None,
    )
    shadow = next((item for item in observed if not item.get("is_incumbent")), None)
    best_overall = replacement or shadow or next((item for item in all_trials if not item.get("is_incumbent")), all_trials[0])

    if replacement:
        suggestion = {
            "action": "replace",
            "production_template": replacement["template_name"],
            "shadow_template": incumbent["template_name"],
            "reason": "邻域进化中出现了满足晋级守门条件的替代冠军，建议升级生产模板并把旧冠军降为影子观察。",
        }
    elif shadow:
        suggestion = {
            "action": "keep_prod_add_shadow",
            "production_template": incumbent["template_name"],
            "shadow_template": shadow["template_name"],
            "reason": "没有出现足够稳健的替换者，但观察候选可作为下一周收益/前排侧对照。",
        }
    else:
        suggestion = {
            "action": "keep_prod",
            "production_template": incumbent["template_name"],
            "shadow_template": None,
            "reason": "当前邻域内没有跑出比现冠军更稳的替换者，先维持生产模板不变。",
        }

    payload = {
        "window": {"core_days": core_days, "buffer_days": buffer_days},
        "summary": {
            "base_trial_count": len(base_trials),
            "veto_trial_count": len(veto_trials),
            "hybrid_trial_count": len(hybrid_trials),
            "unique_candidate_count": len(all_trials),
            "promote_count": len(promoted),
            "observe_count": len(observed),
        },
        "incumbent": _summarize_item(incumbent),
        "suggestion": suggestion,
        "best_overall": _summarize_item(best_overall),
        "stage_best": {
            "base": _summarize_item(base_trials[0]),
            "veto": _summarize_item(veto_trials[0]),
            "hybrid": _summarize_item(hybrid_trials[0]) if hybrid_trials else _summarize_item(base_trials[0]),
        },
        "top_candidates": [_summarize_item(item) for item in all_trials[:12]],
        "top_promoted": [_summarize_item(item) for item in promoted[:6]],
        "top_observed": [_summarize_item(item) for item in observed[:6]],
    }

    _write_json(args.output, payload)
    markdown_output = args.markdown_output or args.output.with_suffix(".md")
    markdown_output.write_text(_markdown_report(payload), encoding="utf-8")
    print(str(args.output))
    print(str(markdown_output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
