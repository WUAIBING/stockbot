#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Did selling actually help? The counterfactual for every exit rule.

An exit rule can only be judged by what the stock did AFTER it fired. If the
price kept rising once we were out, the rule cost money no matter how sensible
its reasoning looked.

    post-exit return > 0   ->  selling was premature, the rule cost you
    post-exit return < 0   ->  the rule saved you

Sell prices come from the trade API log (successful sells only). Forward prices
come from workbuddy_distill/raw_top100/<date>/full_rank.csv.

A caution that is not theoretical. Both of those sources are produced through
pytdx, which silently corrupts data when a socket read fails mid-run: 688131 was
logged sold at 85.58 on a day it closed 30.74, and the 2026-08-25 ranking file
carries 002396 at 7.03 against a real 30.84. So this tool cross-checks the
logged sell price against the ranking close for the same session and drops the
pair when they disagree, reports the MEDIAN rather than the mean, and warns when
the spread is too wide to be real. Until build_tdx_rankings.py stops swallowing
socket errors, treat every number here as provisional.

Because forward prices must exist, a sell can only be scored once the horizon
has passed. Recent exits show as pending, not as zero.

    python evaluate_exit_rules.py
    python evaluate_exit_rules.py --horizon 5 --min-per-rule 5

A single A-share hold has a ~10pp spread, so the standard error on a rule's mean
is about 10/sqrt(n) - at 10 exits that is +/-3.2pp. The report prints the
threshold rather than leaving it to be guessed at.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics as st
import sys
from pathlib import Path


def _root(env_name, fallback):
    """Env first, matching how the rest of the runtime resolves its paths."""
    value = os.environ.get(env_name, "").strip()
    return Path(value) if value else Path(fallback)


try:
    from package_paths import ARKCLAW_ROOT as _AR, DATA_DIR as _DD
except ImportError:
    _DD = Path(__file__).resolve().parent
    _AR = _DD.parents[2] if len(_DD.parents) > 2 else _DD
DATA_DIR = _root("TLFZ_WORKBUDDY_DATA_DIR", _DD)
ARKCLAW_ROOT = _root("TLFZ_ARKCLAW_ROOT", _AR)

TRADE_LOG = Path(DATA_DIR) / "v10_trade_api_log.jsonl"
RANK_ROOT = Path(ARKCLAW_ROOT) / "workbuddy_distill" / "raw_top100"

# A 10-session A-share move spreads about 10pp. Much beyond that means bad
# prices survived the cross-check rather than a real move.
IMPLAUSIBLE_SPREAD_PP = 40.0


def load_sells(path):
    """Successful sells, newest last."""
    if not path.exists():
        print(f"trade log not found: {path}")
        return []
    out = []
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(r.get("action", "")).strip() != "sell":
            continue
        if str(r.get("final_outcome", "")).strip() != "success":
            continue
        try:
            price = float(r.get("ref_price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            continue
        out.append({
            "code": str(r.get("code", "")).strip().zfill(6),
            "date": str(r.get("logged_at", "")).strip()[:10],
            "price": price,
            "reason": str(r.get("exit_reason", "")).strip(),
            "strategy_action": str(r.get("strategy_action", "")).strip(),
        })
    return out


def load_rank_dates(root):
    """trade_date -> {code: close}, only dates that exist on disk."""
    if not root.is_dir():
        print(f"ranking root not found: {root}")
        return {}, []
    prices, dates = {}, []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        f = d / "full_rank.csv"
        if not f.is_file():
            continue
        m = {}
        for row in csv.DictReader(f.open(encoding="utf-8-sig")):
            code = str(row.get("code", "")).strip().zfill(6)
            try:
                m[code] = float(row.get("close") or 0)
            except (TypeError, ValueError):
                continue
        if m:
            prices[d.name] = m
            dates.append(d.name)
    return prices, dates


def bucket_label(reason, strategy_action):
    """Collapse a free-text reason into the rule that produced it."""
    r = reason or ""
    # Most specific first: "中高盈利+9.2%且信号转弱" contains both a profit tier
    # and the generic decay phrase, and "中高盈利" contains "高盈利".
    for key in ("冲高回落", "连跌", "T+5到期", "落袋为安",
                "中高盈利", "高盈利", "risk_trim", "信号转弱"):
        if key in r:
            return key
    if not r:
        return f"(unlogged: {strategy_action or 'unknown'})"
    return r[:24]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", type=int, default=10,
                    help="trading sessions after the sell (default 10)")
    ap.add_argument("--min-per-rule", type=int, default=3)
    ap.add_argument("--max-price-dev", type=float, default=25.0,
                    help="drop a sell whose logged price disagrees with the "
                         "ranking close by more than this percent (default 25)")
    ap.add_argument("--trade-log", default=str(TRADE_LOG))
    ap.add_argument("--rank-root", default=str(RANK_ROOT))
    args = ap.parse_args(argv)

    sells = load_sells(Path(args.trade_log))
    prices, dates = load_rank_dates(Path(args.rank_root))
    print(f"successful sells in log : {len(sells)}")
    print(f"ranking dates available : {len(dates)}"
          + (f"  ({dates[0]} .. {dates[-1]})" if dates else ""))
    if not sells or not dates:
        return 0

    idx = {d: i for i, d in enumerate(dates)}
    scored, pending, nodata, rejected = [], 0, 0, 0
    for s in sells:
        i = idx.get(s["date"])
        if i is None:
            nodata += 1
            continue
        # Both prices arrive through pytdx, which corrupts silently. Drop the
        # pair rather than publish a return built on a price that never existed.
        same_day = prices[dates[i]].get(s["code"])
        if same_day and same_day > 0:
            if abs(s["price"] - same_day) / same_day * 100.0 > args.max_price_dev:
                rejected += 1
                continue
        j = i + args.horizon
        if j >= len(dates):
            pending += 1
            continue
        later = prices[dates[j]].get(s["code"])
        if not later or later <= 0:
            nodata += 1
            continue
        s = dict(s)
        s["post"] = (later / s["price"] - 1.0) * 100.0
        scored.append(s)

    print(f"scored                  : {len(scored)}")
    print(f"waiting for +{args.horizon} sessions : {pending}")
    if nodata:
        print(f"no price match          : {nodata}")
    if rejected:
        print(f"rejected, price mismatch: {rejected}  (logged sell price "
              f"disagreed with the ranking by >{args.max_price_dev:.0f}%)")
    if not scored:
        print("\nnothing scoreable yet. Re-run once more sessions have passed.")
        return 0

    allp = [s["post"] for s in scored]
    sd = st.pstdev(allp) if len(allp) > 1 else 0.0
    print(f"\npost-exit move over {args.horizon} sessions: "
          f"median {st.median(allp):+.2f}%   mean {st.mean(allp):+.2f}%")
    print("  positive => the stock kept rising after we sold => exits were early")
    if sd > IMPLAUSIBLE_SPREAD_PP:
        print(f"  [WARN] spread is {sd:.0f}pp against ~10pp expected. Bad prices are")
        print( "         still getting through, so the mean is unusable and even the")
        print( "         median is suspect. Fix build_tdx_rankings.py first.")
    print()

    groups = {}
    for s in scored:
        groups.setdefault(bucket_label(s["reason"], s["strategy_action"]), []).append(s["post"])

    print("BY EXIT RULE")
    print("=" * 80)
    print(f"  {'rule':<26}{'median after':>14}{'mean':>10}{'n':>5}"
          f"{'kept rising':>13}{'verdict':>12}")
    for name, v in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(v) < args.min_per_rule:
            print(f"  {name:<26}{'':>14}{'':>10}{len(v):>5}{'':>13}{'too few':>12}")
            continue
        med = st.median(v)          # robust; bad prices are still getting through
        noise = 2 * (sd or 1.0) / len(v) ** 0.5
        if med > noise:
            verdict = "COST YOU"
        elif med < -noise:
            verdict = "saved you"
        else:
            verdict = "noise"
        print(f"  {name:<26}{med:>+14.2f}{st.mean(v):>+10.2f}{len(v):>5}"
              f"{100*sum(1 for x in v if x > 0)/len(v):>12.0f}%{verdict:>12}")

    print()
    print(f"  spread of one post-exit move: {sd:.1f}pp")
    print(f"  a rule needs |median| > 2*{sd:.1f}/sqrt(n) to beat luck")
    print(f"  e.g. at n=10 that is +/-{2*sd/10**0.5:.1f}pp, "
          f"at n=30 +/-{2*sd/30**0.5:.1f}pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
