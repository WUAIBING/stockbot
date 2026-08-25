#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evaluate the live strategy profile against backtest signal records.

The live entry rules live in ``strategy_profiles/v10.0.0.json``. The backtest
used to carry its own copy of the same thresholds inside
``backtest_t5_v10.compute_signal_tier``, so the two could drift apart silently.
This module reads the profile directly: whatever the live scanner trades on is
what gets measured here, and there is only one place to change a threshold.

Input is the signal CSV written by ``backtest_framework.BacktestEngine.run`` -
one row per (stock, trading day) carrying the feature values plus ``ret_5d``,
the forward 5-session return. That matches the profile's ``holding_sessions``
of 5, so a rule's mean ``ret_5d`` is the return it would have produced over its
own intended horizon.

## The split is mandatory, and that is the point

These rules were very likely derived from this same history - eleven
``backtest_t5_v*.py`` scripts exist. Re-scoring them on the data they were
fitted to produces flattering numbers that confirm nothing. ``split_date`` is a
required argument for that reason: pick it before looking at any output, and
read the holdout column. If train and holdout disagree, believe the holdout.

Usage:
    python backtest_profile_eval.py backtest_t5_v10_signals.csv --split 2026-01-01
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable

from package_paths import DATA_DIR

SKILL_ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILE = SKILL_ROOT / "strategy_profiles" / "v10.0.0.json"

# profile condition -> (record field, comparison)
# Anything not listed here is a rule the evaluator does not understand, and is
# reported rather than silently ignored.
CONDITION_MAP: dict[str, tuple[str, str]] = {
    "bz_lt": ("bz_direction", "lt"),
    "bz_min": ("bz_direction", "ge"),
    "bz_rt_min": ("bz_rt_direction", "ge"),
    "ma20_min": ("close_vs_ma20", "ge"),
    "ma20_max": ("close_vs_ma20", "le"),
    "weekly_slope_gt": ("weekly_slope", "gt"),
    "weekly_slope_min": ("weekly_slope", "ge"),
    "weekly_slope_max": ("weekly_slope", "le"),
    "rsi_lt": ("rsi14", "lt"),
    "amt_ratio_lt": ("amt_ratio", "lt"),
    "amt_ratio_min": ("amt_ratio", "ge"),
    "amt_ratio_max": ("amt_ratio", "le"),
    "weekly_align": ("weekly_align", "bool"),
    "vol_expand": ("vol_expand", "bool"),
    "is_green": ("is_green", "bool"),
}

IGNORED_KEYS = {"tier", "mode", "position"}


def load_profile(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else DEFAULT_PROFILE
    with open(target, encoding="utf-8") as handle:
        return json.load(handle)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(result) else result


def _opt_float(value: Any) -> float | None:
    """None when the value is missing - a NaN horizon must not read as 0.0."""
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "y"}


def unsupported_conditions(rule: dict[str, Any]) -> list[str]:
    """Conditions in a rule this evaluator cannot express."""
    return sorted(k for k in rule if k not in IGNORED_KEYS and k not in CONDITION_MAP)


def rule_matches(rule: dict[str, Any], record: dict[str, Any]) -> bool:
    """True when one (stock, day) record satisfies every condition in a rule."""
    for key, threshold in rule.items():
        if key in IGNORED_KEYS:
            continue
        mapped = CONDITION_MAP.get(key)
        if mapped is None:
            # Unknown condition: refuse to match rather than pass silently.
            return False
        field, op = mapped
        if op == "bool":
            if _as_bool(record.get(field)) != bool(threshold):
                return False
            continue
        value = _as_float(record.get(field))
        limit = _as_float(threshold)
        if op == "lt" and not value < limit:
            return False
        if op == "le" and not value <= limit:
            return False
        if op == "gt" and not value > limit:
            return False
        if op == "ge" and not value >= limit:
            return False
    return True


def summarize(returns: list[float]) -> dict[str, Any]:
    n = len(returns)
    if n == 0:
        return {"n": 0, "win_rate_pct": None, "avg_return_pct": None, "median_return_pct": None}
    wins = sum(1 for r in returns if r > 0)
    ordered = sorted(returns)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "n": n,
        "win_rate_pct": round(wins / n * 100, 2),
        "avg_return_pct": round(sum(returns) / n, 4),
        "median_return_pct": round(median, 4),
    }


def evaluate(records: Iterable[dict[str, Any]], *, split_date: str,
             profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score every profile rule on data before and after ``split_date``.

    ``split_date`` is required: records dated before it are 'train' (likely the
    data the rules were fitted on), on/after it are 'holdout'. Only the holdout
    column carries evidential weight.
    """
    if not split_date:
        raise ValueError("split_date is required - see the module docstring")
    profile = profile or load_profile()
    rules = profile.get("rules", {})

    buckets: dict[str, dict[str, list[float]]] = {
        name: {"train": [], "holdout": []} for name in rules
    }
    exits: dict[str, list[tuple[Any, Any, Any]]] = {name: [] for name in rules}
    baseline_exits: list[tuple[Any, Any, Any]] = []
    baseline: dict[str, list[float]] = {"train": [], "holdout": []}

    total = 0
    for record in records:
        total += 1
        date = str(record.get("date", ""))[:10]
        period = "holdout" if date >= split_date else "train"
        ret = _as_float(record.get("ret_5d"))
        baseline[period].append(ret)
        excursion = (_opt_float(record.get("ret_5d")),
                     _opt_float(record.get("mfe_5d")),
                     _opt_float(record.get("mae_5d")))
        baseline_exits.append(excursion)
        for name, rule in rules.items():
            if rule_matches(rule, record):
                buckets[name][period].append(ret)
                exits[name].append(excursion)

    per_rule = {}
    for name, rule in rules.items():
        per_rule[name] = {
            "mode": rule.get("mode", name),
            "tier": rule.get("tier"),
            "train": summarize(buckets[name]["train"]),
            "holdout": summarize(buckets[name]["holdout"]),
            "unsupported_conditions": unsupported_conditions(rule),
            "exit_study": exit_study(exits[name]),
        }

    return {
        "profile_id": profile.get("profile_id", ""),
        "split_date": split_date,
        "records_scored": total,
        "baseline": {
            "train": summarize(baseline["train"]),
            "holdout": summarize(baseline["holdout"]),
        },
        "rules": per_rule,
        "baseline_exit_study": exit_study(baseline_exits),
    }



# Live exit policy, from v10_moni_trader:
#   HIGH_PROFIT_TAKE_PROFIT_PCT   = 15.0
#   MEDIUM_PROFIT_TAKE_PROFIT_PCT = 8.0
#   holding_sessions              = 5   (time exit)
#   stop loss                     = none defined
LIVE_TAKE_PROFIT_PCT = 8.0
LIVE_HOLD_SESSIONS = 5


def exit_study(returns_and_excursions: list[tuple[float, float, float]],
               *, take_profit_pct: float = LIVE_TAKE_PROFIT_PCT) -> dict[str, Any]:
    """How much the take-profit cap left on the table, and what a stop would cost.

    Each item is (ret_5d, mfe_5d, mae_5d) for one entry: what holding to the
    horizon returned, the best unrealised gain along the way, and the worst.

    truncation_gap is the honest version of the profit_truncation label - the
    average distance between the peak available and what holding actually
    returned. A large gap means the caps are leaving money behind; a small one
    means "let winners run" is wrong and the peak was never holdable.
    """
    rows = [(r, f, a) for r, f, a in returns_and_excursions
            if r is not None and f is not None and a is not None]
    if not rows:
        return {"n": 0}
    n = len(rows)
    rets = [r for r, _, _ in rows]
    mfes = [f for _, f, _ in rows]
    maes = [a for _, _, a in rows]

    # what capping at take_profit_pct would have produced: if the peak reached
    # the cap the trade exits there, otherwise it rides to the horizon
    capped = [take_profit_pct if f >= take_profit_pct else r for r, f, _ in rows]
    reached_cap = sum(1 for _, f, _ in rows if f >= take_profit_pct)

    return {
        "n": n,
        "avg_return_pct": round(sum(rets) / n, 3),
        "avg_mfe_pct": round(sum(mfes) / n, 3),
        "avg_mae_pct": round(sum(maes) / n, 3),
        "truncation_gap_pct": round((sum(mfes) - sum(rets)) / n, 3),
        "reached_take_profit_pct": round(reached_cap / n * 100, 1),
        "avg_return_if_capped_pct": round(sum(capped) / n, 3),
        "cap_vs_hold_pct": round((sum(capped) - sum(rets)) / n, 3),
    }

def load_signal_records(path: str | Path) -> list[dict[str, Any]]:
    import csv

    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def format_report(result: dict[str, Any]) -> str:
    lines = []
    lines.append(f"profile {result['profile_id']}   split {result['split_date']}   "
                 f"{result['records_scored']:,} records")
    lines.append("")
    base = result["baseline"]
    lines.append("{:<24}{:>26}{:>26}".format("", "TRAIN (pre-split)", "HOLDOUT (post-split)"))
    lines.append("{:<24}{:>8}{:>9}{:>9}{:>8}{:>9}{:>9}".format(
        "rule", "n", "win%", "avg%", "n", "win%", "avg%"))
    lines.append("-" * 84)

    def row(label: str, tr: dict[str, Any], ho: dict[str, Any]) -> str:
        def cell(stat: dict[str, Any], key: str, width: int, fmt: str) -> str:
            value = stat.get(key)
            return format("-", ">%d" % width) if value is None else format(value, fmt)
        return "{:<24}{:>8}{}{}{:>8}{}{}".format(
            label[:24],
            tr["n"], cell(tr, "win_rate_pct", 9, ">9.1f"), cell(tr, "avg_return_pct", 9, ">9.2f"),
            ho["n"], cell(ho, "win_rate_pct", 9, ">9.1f"), cell(ho, "avg_return_pct", 9, ">9.2f"))

    lines.append(row("ALL RECORDS (baseline)", base["train"], base["holdout"]))
    lines.append("-" * 84)
    ordered = sorted(result["rules"].items(),
                     key=lambda kv: (kv[1]["holdout"]["avg_return_pct"] is None,
                                     -(kv[1]["holdout"]["avg_return_pct"] or 0)))
    for name, stats in ordered:
        lines.append(row(stats["mode"], stats["train"], stats["holdout"]))
        if stats["unsupported_conditions"]:
            lines.append("    ! unsupported conditions, rule never matches: "
                         + ", ".join(stats["unsupported_conditions"]))
    lines.append("-" * 84)
    lines.append("A rule only beats the baseline if its holdout avg% exceeds the baseline "
                 "holdout avg%.")
    lines.append("Train columns are shown to expose overfit, not to be believed.")
    lines.append("")
    lines.append("EXIT STUDY  (5-session window, all records)")
    lines.append("{:<24}{:>7}{:>9}{:>9}{:>9}{:>9}{:>10}".format(
        "rule", "n", "hold%", "peak%", "worst%", "gap%", "cap8-hold"))
    lines.append("-" * 84)

    def exit_row(label, study):
        if not study or not study.get("n"):
            return "{:<24}{:>7}".format(label[:24], 0)
        return "{:<24}{:>7}{:>9.2f}{:>9.2f}{:>9.2f}{:>9.2f}{:>10.2f}".format(
            label[:24], study["n"], study["avg_return_pct"], study["avg_mfe_pct"],
            study["avg_mae_pct"], study["truncation_gap_pct"], study["cap_vs_hold_pct"])

    lines.append(exit_row("ALL RECORDS (baseline)", result.get("baseline_exit_study", {})))
    lines.append("-" * 84)
    for name, stats in ordered:
        lines.append(exit_row(stats["mode"], stats.get("exit_study", {})))
    lines.append("-" * 84)
    lines.append("gap% = peak available minus what holding to the horizon returned.")
    lines.append("       Large gap => the 8/15% take-profit caps leave money behind.")
    lines.append("       Small gap => the peak was never holdable; let-winners-run is wrong.")
    lines.append("cap8-hold = what capping at +8% adds (+) or costs (-) versus holding.")
    lines.append("worst% = deepest drawdown before the horizon; what a stop would have hit.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("signals_csv", help="signal CSV from backtest_framework")
    parser.add_argument("--split", required=True,
                        help="YYYY-MM-DD; records on/after this date are the holdout")
    parser.add_argument("--profile", default=None, help="path to a strategy profile JSON")
    parser.add_argument("--json-out", default=None, help="also write the full result as JSON")
    args = parser.parse_args()

    records = load_signal_records(args.signals_csv)
    result = evaluate(records, split_date=args.split,
                      profile=load_profile(args.profile))
    print(format_report(result))

    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
