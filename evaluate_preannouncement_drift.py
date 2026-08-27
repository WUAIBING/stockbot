# -*- coding: utf-8 -*-
"""Does an A-share earnings pre-announcement predict the sessions that follow?

ANSWER: NO, IN EITHER DIRECTION. Kept so the idea is not rebuilt from scratch.

12,803 announcements over 1,124 dates, 2017-2026. At face value it looks like a
signal - 预减 runs -1.34% over five sessions with t=-4.14, 首亏 -0.79% with
t=-2.00 - and that is exactly the trap. The placebo, which re-runs the same
cohort with the anchor moved off the event, says both sides were already moving:

    cohort                       true      -60      -40      +40      +60
    POSITIVE 预增+扭亏  n=7753   +0.31    +1.07    +0.56    +0.22    -0.29
                          t      +0.99    +3.86    +2.67    +0.77    -1.17
    NEGATIVE 预减+首亏  n=4676   -0.77    -1.87    -0.38    -0.34    +0.17
                          t      -2.35    -5.93    -1.35    -1.05    +0.44

The shifted anchor BEATS the true one on both sides. Companies that pre-announce
a profit jump had already been drifting up for months; companies that pre-announce
a loss had already been falling harder sixty sessions earlier. The announcement
adds nothing you could not read off the prior trend.

Year by year it is a coin flip - the positive cohort is positive in 6 of 10 years
and the negative in 5 of 10, with 2024 at -5.51% and 2026 at +2.45%. Net of a
0.15% round trip the positive cohort returns +0.158% per cycle, which is not a
business.

Contrast disclosure_calendar.py, measured the same day on the same machinery: its
true anchor beat all four placebos at every window. That is the difference
between a signal and a cohort that was already moving.

--- method, kept because the placebo is the part worth reusing ---


业绩预告 is the mandatory warning a listed company files when profit will swing
hard. It lands WEEKS before the report itself and carries a direction and a
number, which makes it the rare signal that is public, dated, and forward.

ENTRY IS THE SESSION AFTER THE ANNOUNCEMENT. Pre-announcements are filed after
the close or before the open, so entering at the announcement day close would
book part of a move that was not reachable. Entering at the NEXT close is
always reachable, and it costs the study whatever the first day was worth -
which is the honest direction to be wrong in.

Every figure is excess over the same sessions equal-weighted universe, and the
unit of observation is the ANNOUNCEMENT DATE. A-share pre-announcements cluster
brutally on filing deadlines (94 of 200 names in one 2025 cohort landed on a
single day), so counting stocks as independent draws would inflate the sample by
more than an order of magnitude against a handful of real days.
"""
from __future__ import annotations
import sys, math, collections
import numpy as np

IMPLAUSIBLE = 0.21
MIN_UNIVERSE = 200
MIN_AMOUNT = 2.0e7
ROUND_TRIP_PCT = 0.15     # the cost a real book pays; quoted net where it matters


def load_px(px_path):
    z = np.load(px_path, allow_pickle=True)
    dates = z["dates"]
    codes = [str(c) for c in z["codes"]]
    C = z["close"].astype(np.float64)
    A = z["amount"].astype(np.float64)
    return dates, C, A, {c: i for i, c in enumerate(codes)}


def load_events(path, row):
    """code,date,type,magnitude - dropping anything not priceable."""
    out = []
    for ln in open(path, encoding="utf-8"):
        parts = ln.strip().split(",")
        if len(parts) < 3:
            continue
        code, d, typ = parts[0].strip(), parts[1].strip().replace("-", ""), parts[2].strip()
        try:
            mag = float(parts[3])
        except (IndexError, ValueError):
            mag = float("nan")
        if code in row and len(d) == 8 and d.isdigit() and typ:
            out.append((code, int(d), typ, mag))
    return sorted(set(out))


def universe_index(C):
    prev, cur = C[:, :-1], C[:, 1:]
    with np.errstate(invalid="ignore", divide="ignore"):
        R = cur / prev - 1.0
    R[~np.isfinite(R)] = np.nan
    R[np.abs(R) > IMPLAUSIBLE] = np.nan
    live = np.sum(np.isfinite(R), axis=0)
    with np.errstate(invalid="ignore"):
        mean = np.nanmean(R, axis=0)
    mean[live < MIN_UNIVERSE] = 0.0
    mean[~np.isfinite(mean)] = 0.0
    idx = np.empty(C.shape[1])
    idx[0] = 1.0
    idx[1:] = np.cumprod(1.0 + mean)
    return idx


def excess(C, idx, r, i, j):
    if i < 0 or j < 0 or i >= C.shape[1] or j >= C.shape[1] or i >= j:
        return None
    a, b = C[r, i], C[r, j]
    if not (np.isfinite(a) and np.isfinite(b)) or a <= 0:
        return None
    stock = b / a - 1.0
    if abs(stock) > 3.0:
        return None
    return (stock - (idx[j] / idx[i] - 1.0)) * 100.0


def stats(per_date):
    vals = [v for v in per_date.values() if v is not None and np.isfinite(v)]
    n = len(vals)
    if n < 2:
        return dict(n=n, mean=float("nan"), sd=float("nan"), t=float("nan"))
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    se = sd / math.sqrt(n)
    return dict(n=n, mean=m, sd=sd, se=se, t=(m / se if se else float("nan")))


def window_test(dates, C, A, row, events, idx, K, offset=0, entry_lag=1):
    """Enter entry_lag sessions after the announcement, hold K sessions."""
    buckets = collections.defaultdict(list)
    for code, d, typ, mag in events:
        j = int(np.searchsorted(dates, d, side="left"))
        if j <= 0 or j >= C.shape[1]:
            continue
        entry = j + entry_lag + offset
        exit_ = entry + K
        if entry <= 0 or exit_ >= C.shape[1]:
            continue
        r = row[code]
        if not np.isfinite(A[r, entry]) or A[r, entry] < MIN_AMOUNT:
            continue
        e = excess(C, idx, r, entry, exit_)
        if e is not None:
            buckets[int(dates[j])].append(e)
    st = stats({d: sum(v) / len(v) for d, v in buckets.items() if v})
    st["stocks"] = sum(len(v) for v in buckets.values())
    return st, buckets


def run(px, ev_path):
    dates, C, A, row = load_px(px)
    idx = universe_index(C)
    events = load_events(ev_path, row)
    by_type = collections.defaultdict(list)
    for e in events:
        by_type[e[2]].append(e)

    print("universe: %d stocks x %d sessions (%d-%d)"
          % (C.shape[0], C.shape[1], dates[0], dates[-1]))
    print("events: %d over %d dates | types: %s"
          % (len(events), len(set(e[1] for e in events)),
             ", ".join("%s %d" % (k, len(v)) for k, v in sorted(
                 by_type.items(), key=lambda kv: -len(kv[1])))))
    print()

    print("BY TYPE  entry = close of the session after the announcement")
    print("  %-6s %-4s %6s %7s %9s %8s %7s"
          % ("type", "hold", "dates", "stocks", "mean%", "sd", "t"))
    for typ, evs in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        if len(evs) < 100:
            continue
        for K in (5, 10, 20):
            st, _ = window_test(dates, C, A, row, evs, idx, K)
            print("  %-6s %-4d %6d %7d %+9.3f %8.3f %+7.2f"
                  % (typ, K, st["n"], st["stocks"], st["mean"], st["sd"], st["t"]))

    cohorts = (
        ("POSITIVE (预增 + 扭亏)", by_type.get("预增", []) + by_type.get("扭亏", [])),
        ("NEGATIVE (预减 + 首亏)", by_type.get("预减", []) + by_type.get("首亏", [])),
    )
    for label, evs in cohorts:
        if len(evs) < 100:
            continue
        print()
        print("=" * 66)
        print("%s  n=%d" % (label, len(evs)))
        print()
        print("PLACEBO, hold 10, anchor moved off the event.")
        print("A shifted anchor that matches the true one means the cohort was")
        print("already drifting: that is selection, not a response to the news.")
        print("  %-7s %6s %7s %9s %7s" % ("shift", "dates", "stocks", "mean%", "t"))
        for off in (0, -60, -40, 40, 60):
            st, _ = window_test(dates, C, A, row, evs, idx, 10, offset=off)
            print("  %-7s %6d %7d %+9.3f %+7.2f"
                  % ("true" if off == 0 else "%+d" % off,
                     st["n"], st["stocks"], st["mean"], st["t"]))

        print()
        print("RUN-UP before the announcement (was it already leaked?)")
        for K in (5, 10):
            st, _ = window_test(dates, C, A, row, evs, idx, K,
                                offset=-(K + 1), entry_lag=0)
            print("  -%-3d sessions  %5d dates  mean %+.3f%%  t %+.2f"
                  % (K, st["n"], st["mean"], st["t"]))

        print()
        print("BY YEAR, hold 10 - an edge that died is not an edge")
        print("  %-6s %6s %7s %9s %7s" % ("year", "dates", "stocks", "mean%", "t"))
        pos_years = 0
        tot_years = 0
        for y in range(2017, 2027):
            sub = [e for e in evs if e[1] // 10000 == y]
            if len(sub) < 40:
                continue
            st, _ = window_test(dates, C, A, row, sub, idx, 10)
            if np.isfinite(st["mean"]):
                tot_years += 1
                pos_years += 1 if st["mean"] > 0 else 0
            print("  %-6d %6d %7d %+9.3f %+7.2f"
                  % (y, st["n"], st["stocks"], st["mean"], st["t"]))
        print("  positive in %d of %d years" % (pos_years, tot_years))

        st, _ = window_test(dates, C, A, row, evs, idx, 10)
        print()
        print("  hold 10 net of a %.2f%% round trip: %+.3f%% per cycle"
              % (ROUND_TRIP_PCT, st["mean"] - ROUND_TRIP_PCT))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
