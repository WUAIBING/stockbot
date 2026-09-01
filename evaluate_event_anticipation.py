# -*- coding: utf-8 -*-
"""Do theme stocks run up before a scheduled industry event?

ANSWER: CANNOT BE ANSWERED WITH THIS DATA, and the naive version fails.

Four World Robot Conferences (2023-08-16, 2024-08-21, 2025-08-08, 2026-08-19,
dates from the organiser own past-editions table) against the 人形机器 and
智能机器 concept blocks:

    pre-event 10 sessions   +0.63%   t=0.95
    placebo shift -60       +2.16%   t=3.16   <- larger than the true anchor
    placebo shift -40       -0.84%   t=-1.67

Per event: 2023 -1.09%, 2024 -0.66%, 2025 +3.62%, 2026 +3.28%. That is not
anticipation, it is the humanoid theme becoming a market obsession in 2025.

The obstacle is structural, not fixable by a better window. A company reports
four times a year, which gave the disclosure study 24,773 events over 272 dates.
A conference happens ONCE a year. Twelve conferences over eight years is under a
hundred observations, and two biases sit on top of them: concept membership is a
snapshot taken today, so a stock joins 人形机器 only after it visibly is one; and
the themes are picked by someone who already knows which ones mattered.

For events this sparse the only honest validation is forward - record what a live
book does around them and accumulate observations. Kept for that purpose.

THE TWO BIASES IN DETAIL, because they bound anything this file ever reports:

  Membership is measured today. block_gn.dat is a snapshot. A stock joins the
  humanoid-robot concept once it visibly IS a humanoid-robot stock, so testing a
  2023 event against 2026 membership selects the names that went on to qualify.
  There is no historical membership file to fix this with.

  Events are chosen by me, knowing which themes turned out to matter. Nobody was
  running a humanoid-robot book in 2019.

So this is a look, not a proof. It answers whether the effect is large enough to
be worth building against - nothing more.
"""
from __future__ import annotations
import sys, math, collections
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent /
                       "workbuddy" / "skills" / "a-share-analyst"))
from tdx_blocks import parse

IMPLAUSIBLE = 0.21
MIN_UNIVERSE = 200
MIN_AMOUNT = 2.0e7


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
    s = b / a - 1.0
    if abs(s) > 3.0:
        return None
    return (s - (idx[j] / idx[i] - 1.0)) * 100.0


def basket_excess(dates, C, A, row, idx, codes, d, K, offset=0):
    """Equal-weighted theme basket, K sessions ending the day before the event."""
    j = int(np.searchsorted(dates, d, side="left"))
    if j <= 0 or j >= C.shape[1]:
        return None, 0
    j += offset
    entry, exit_ = j - 1 - K, j - 1
    if entry < 0 or exit_ >= C.shape[1]:
        return None, 0
    vals = []
    for c in codes:
        r = row.get(c)
        if r is None:
            continue
        if not np.isfinite(A[r, entry]) or A[r, entry] < MIN_AMOUNT:
            continue
        e = excess(C, idx, r, entry, exit_)
        if e is not None:
            vals.append(e)
    if len(vals) < 8:
        return None, len(vals)
    return sum(vals) / len(vals), len(vals)


def stats(vals):
    vals = [v for v in vals if v is not None and np.isfinite(v)]
    n = len(vals)
    if n < 2:
        return n, float("nan"), float("nan")
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    return n, m, (m / (sd / math.sqrt(n)) if sd else float("nan"))


def main():
    px, blockfile, evfile = sys.argv[1], sys.argv[2], sys.argv[3]
    z = np.load(px, allow_pickle=True)
    dates = z["dates"]
    codes_all = [str(c) for c in z["codes"]]
    C = z["close"].astype(np.float64)
    A = z["amount"].astype(np.float64)
    row = {c: i for i, c in enumerate(codes_all)}
    idx = universe_index(C)

    themes = {name: cs for name, cs in parse(blockfile)}

    events = []
    for ln in open(evfile, encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 3:
            continue
        events.append((parts[0], int(parts[1].replace("-", "")), parts[2]))

    print("themes in block file: %d | events: %d" % (len(themes), len(events)))
    print()

    by_ev = collections.defaultdict(list)
    print("%-22s %-10s %-12s %6s %9s" % ("event", "date", "theme", "names", "pre-10%"))
    for name, d, theme in events:
        cs = themes.get(theme)
        if not cs:
            print("  %-20s %-10d %-12s   THEME NOT FOUND" % (name[:20], d, theme))
            continue
        v, n = basket_excess(dates, C, A, row, idx, cs, d, 10)
        print("%-22s %-10d %-12s %6d %9s"
              % (name[:22], d, theme, n, ("%+.2f" % v) if v is not None else "n/a"))
        if v is not None:
            by_ev[theme].append(v)

    allv = [v for vs in by_ev.values() for v in vs]
    n, m, t = stats(allv)
    print()
    print("pre-event 10 sessions, all events: n=%d  mean %+.2f%%  t %+.2f" % (n, m, t))

    for K in (3, 5, 20):
        vals = []
        for name, d, theme in events:
            cs = themes.get(theme)
            if not cs:
                continue
            v, _ = basket_excess(dates, C, A, row, idx, cs, d, K)
            if v is not None:
                vals.append(v)
        n, m, t = stats(vals)
        print("pre-event %-2d sessions            : n=%d  mean %+.2f%%  t %+.2f" % (K, n, m, t))

    print()
    print("PLACEBO - same theme, same window, anchor moved off the event")
    for off in (-60, -40, 40, 60):
        vals = []
        for name, d, theme in events:
            cs = themes.get(theme)
            if not cs:
                continue
            v, _ = basket_excess(dates, C, A, row, idx, cs, d, 10, offset=off)
            if v is not None:
                vals.append(v)
        n, m, t = stats(vals)
        print("  shift %+4d : n=%d  mean %+.2f%%  t %+.2f" % (off, n, m, t))


if __name__ == "__main__":
    main()
