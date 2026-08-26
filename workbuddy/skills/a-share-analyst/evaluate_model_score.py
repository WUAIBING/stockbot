#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Does the model score predict realised returns?

The score picks roughly 4 stocks from the ~950 that match a profile rule, so it
does virtually all of the stock selection - and until now it was never written
to the trade record, which made it the one component of the system that could
not be evaluated after the fact.

This reads the trade ledger and asks the only question that matters: do
higher-scored entries actually earn more?

Run it once a few dozen closed trades have accumulated:

    python evaluate_model_score.py
    python evaluate_model_score.py --min-trades 40 --buckets 3

A caution on reading the output. With n closed trades and a ~10pp spread on a
single A-share hold, the standard error on a bucket mean is about 10/sqrt(n).
At 30 trades per bucket that is +/-1.8pp, so a gradient smaller than roughly
4pp between top and bottom bucket is not distinguishable from luck. The report
prints that threshold rather than leaving it to be guessed at.
"""

from __future__ import annotations

import argparse
import csv
import statistics as st
import sys
from pathlib import Path

try:
    from package_paths import DATA_DIR
except ImportError:  # running outside the skill directory
    DATA_DIR = Path(__file__).resolve().parent

TRACK_FILE = Path(DATA_DIR) / "v10_track_record.csv"
SCORE_FIELDS = ("model_score", "score_market", "score_sector", "score_stock", "score_flow")


def _f(value, default=None):
    try:
        text = str(value).strip()
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


def load_closed(path):
    """Closed trades that carry both a score and a realised return."""
    if not path.exists():
        print(f"ledger not found: {path}")
        return [], 0
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    closed, missing_score = [], 0
    for r in rows:
        if str(r.get("status", "")).strip() == "holding":
            continue
        pnl_pct = _f(r.get("pnl_pct"))
        if pnl_pct is None:
            continue
        score = _f(r.get("model_score"))
        if score is None or score <= 0:
            missing_score += 1
            continue
        closed.append({
            "code": str(r.get("code", "")).strip(),
            "tier": str(r.get("tier", "")).strip(),
            "industry": str(r.get("industry", "")).strip(),
            "pnl_pct": pnl_pct,
            "hold_days": _f(r.get("hold_days"), 0.0),
            **{k: _f(r.get(k)) for k in SCORE_FIELDS},
        })
    return closed, missing_score


def report_buckets(trades, key, nb, label):
    vals = [t for t in trades if t.get(key) is not None]
    if len(vals) < nb * 5:
        print(f"  {label:<16} only {len(vals)} trades - need {nb*5} for {nb} buckets")
        return None
    vals.sort(key=lambda t: t[key])
    size = len(vals) // nb
    print(f"  {label:<16}", end="")
    means = []
    for i in range(nb):
        chunk = vals[i*size:(i+1)*size] if i < nb - 1 else vals[i*size:]
        m = st.mean(x["pnl_pct"] for x in chunk)
        means.append(m)
        print(f"{m:>+9.2f}", end="")
    spread = means[-1] - means[0]
    print(f"{spread:>+10.2f}")
    return spread, len(vals)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(TRACK_FILE))
    ap.add_argument("--buckets", type=int, default=3)
    ap.add_argument("--min-trades", type=int, default=20)
    args = ap.parse_args(argv)

    trades, missing = load_closed(Path(args.file))
    print(f"closed trades with a score : {len(trades)}")
    if missing:
        print(f"closed trades without one  : {missing}  (entered before score logging)")
    if len(trades) < args.min_trades:
        print(f"\nnot enough yet - need {args.min_trades}. Nothing to conclude.")
        return 0

    overall = st.mean(t["pnl_pct"] for t in trades)
    sd = st.pstdev([t["pnl_pct"] for t in trades]) or 1.0
    per_bucket = len(trades) // args.buckets
    noise = 2 * sd / max(per_bucket, 1) ** 0.5
    print(f"mean realised pnl          : {overall:+.2f}%")
    print(f"spread of a single trade   : {sd:.1f}pp")
    print(f"noise floor on a bucket    : +/-{noise:.2f}pp "
          f"({per_bucket} trades per bucket)")
    print(f"-> a top-vs-bottom gradient under {2*noise:.1f}pp is not "
          f"distinguishable from luck\n")

    print(f"MEAN REALISED PNL BY BUCKET  (B1 = lowest score)")
    print("=" * 74)
    header = "".join(f"{'B'+str(i+1):>9}" for i in range(args.buckets))
    print(f"  {'ranked by':<16}{header}{'B_top-B1':>10}")
    verdicts = []
    for key, label in ((f, f.replace("score_", "")) for f in SCORE_FIELDS):
        out = report_buckets(trades, key, args.buckets, label)
        if out:
            verdicts.append((label, out[0]))

    print()
    if verdicts:
        best = max(verdicts, key=lambda v: v[1])
        print(f"strongest gradient: {best[0]} at {best[1]:+.2f}pp", end="")
        print("  -> REAL" if best[1] > 2 * noise else "  -> within noise")
    print()
    print("by tier:")
    tiers = {}
    for t in trades:
        tiers.setdefault(t["tier"] or "?", []).append(t["pnl_pct"])
    for tier in sorted(tiers):
        v = tiers[tier]
        print(f"  T{tier:<4} n={len(v):<4} mean {st.mean(v):+7.2f}%  "
              f"win {100*sum(1 for x in v if x > 0)/len(v):5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
