# -*- coding: utf-8 -*-
"""Does the disclosure calendar predict return, and is the edge above the noise?

Two hypotheses, both tradeable from information published BEFORE the fact:

  DRIFT   stocks drift up in the sessions before their own scheduled disclosure
  TIMING  companies that schedule early outperform ones that schedule late,
          the folk claim being that good news gets reported eagerly

Both are measured as excess over the SAME sessions equal-weighted universe, so a
season that simply floats everything cannot masquerade as skill.

THE UNIT OF OBSERVATION IS THE DATE, NOT THE STOCK. Two hundred stocks
disclosing on one day share that day market, sector and news flow; counting them
as 200 independent draws overstates the sample by more than an order of
magnitude and would manufacture significance out of a handful of days.
"""
from __future__ import annotations
import sys, math, collections
import numpy as np

IMPLAUSIBLE = 0.21        # daily limits cap A-shares; beyond this is a data error
MIN_UNIVERSE = 200        # sessions with fewer live names are not a cross-section
MIN_AMOUNT = 2.0e7        # 2000万 turnover: below this you cannot get filled


def load(px_path, pairs_path):
    z = np.load(px_path, allow_pickle=True)
    dates, codes = z["dates"], [str(c) for c in z["codes"]]
    C, A = z["close"].astype(np.float64), z["amount"].astype(np.float64)
    row = {c: i for i, c in enumerate(codes)}

    pairs = []
    for ln in open(pairs_path, encoding="utf-8"):
        ln = ln.strip()
        if "," not in ln:
            continue
        code, d = ln.split(",")[:2]
        code, d = code.strip(), d.strip().replace("-", "")
        if code in row and len(d) == 8 and d.isdigit():
            pairs.append((code, int(d)))
    return dates, C, A, row, sorted(set(pairs))


def universe_index(C):
    """Equal-weighted index level from the cross-sectional mean daily return."""
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


def session_at_or_after(dates, d):
    i = int(np.searchsorted(dates, d, side="left"))
    return i if i < len(dates) else -1


def excess(C, idx, r, i, j):
    """Stock return from session i to j, minus the universe over the same span."""
    if i < 0 or j < 0 or i >= C.shape[1] or j >= C.shape[1] or i >= j:
        return None
    a, b = C[r, i], C[r, j]
    if not (np.isfinite(a) and np.isfinite(b)) or a <= 0:
        return None
    stock = b / a - 1.0
    if abs(stock) > 3.0:
        return None
    return (stock - (idx[j] / idx[i] - 1.0)) * 100.0


def by_date_stats(per_date):
    """Mean and t across DATES. Each date is one draw, whatever its cohort size."""
    vals = [v for v in per_date.values() if v is not None]
    n = len(vals)
    if n < 2:
        return dict(n=n, stocks=0, mean=float("nan"), sd=float("nan"),
                    se=float("nan"), t=float("nan"))
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    se = sd / math.sqrt(n)
    return dict(n=n, stocks=0, mean=m, sd=sd, se=se,
                t=(m / se if se else float("nan")))


def drift_test(dates, C, A, row, pairs, idx, K, offset=0):
    """Buy K sessions before the disclosure, sell the session before it.

    offset shifts the anchor by that many sessions, holding the STOCK and the
    rough calendar period fixed. That is the control this study needs: reporting
    season is a specific stretch of the year and the liquidity filter selects
    tradeable names, so a window measured against nothing at all would credit
    both to the calendar. If the true anchor does not beat its own shifted twin,
    there is no event effect here.
    """
    buckets = collections.defaultdict(list)
    for code, d in pairs:
        j = session_at_or_after(dates, d)
        if j <= 0:
            continue
        j += offset
        if j <= 0 or j >= C.shape[1]:
            continue
        r = row[code]
        entry, exit_ = j - 1 - K, j - 1
        if entry < 0:
            continue
        if not np.isfinite(A[r, entry]) or A[r, entry] < MIN_AMOUNT:
            continue
        e = excess(C, idx, r, entry, exit_)
        if e is not None:
            buckets[int(dates[j])].append(e)
    st = by_date_stats({d: sum(v) / len(v) for d, v in buckets.items() if v})
    st["stocks"] = sum(len(v) for v in buckets.values())
    return st


def event_and_after(dates, C, A, row, pairs, idx, K):
    """The disclosure session itself, and the K sessions after it."""
    ev, af = collections.defaultdict(list), collections.defaultdict(list)
    for code, d in pairs:
        j = session_at_or_after(dates, d)
        if j <= 0 or j + K >= C.shape[1]:
            continue
        r = row[code]
        if not np.isfinite(A[r, j - 1]) or A[r, j - 1] < MIN_AMOUNT:
            continue
        a = excess(C, idx, r, j - 1, j)
        b = excess(C, idx, r, j, j + K)
        if a is not None:
            ev[int(dates[j])].append(a)
        if b is not None:
            af[int(dates[j])].append(b)
    return (by_date_stats({d: sum(v) / len(v) for d, v in ev.items() if v}),
            by_date_stats({d: sum(v) / len(v) for d, v in af.items() if v}))


def board_of(code):
    """STAR and ChiNext are where the tech names list, and where the daily band
    is +/-20% rather than +/-10% - twice the room for an anticipated result to
    move the price before it is published."""
    if code.startswith("688"):
        return "STAR 688"
    if code.startswith(("300", "301")):
        return "ChiNext"
    if code.startswith(("8", "4")):
        return "BJ"
    if code.startswith("6"):
        return "SH main"
    return "SZ main"


def season_of(d):
    y, m = d // 10000, (d // 100) % 100
    if 7 <= m <= 9:
        return (y, "H1")
    if m in (10, 11):
        return (y, "Q3")
    if 3 <= m <= 5:
        return (y, "FY")
    return None


def timing_test(dates, C, A, row, pairs, idx):
    """Early schedulers against late ones, held over the SAME sessions.

    Both legs run over one identical window per season, so this is a clean
    spread: no market exposure and no calendar mismatch between the legs.
    """
    window = {"H1": (7, 9), "Q3": (10, 11), "FY": (3, 5)}
    seasons = collections.defaultdict(list)
    for code, d in pairs:
        s = season_of(d)
        if s:
            seasons[s].append((code, d))
    out = {}
    for s, members in sorted(seasons.items()):
        if len(members) < 40:
            continue
        m0, m1 = window[s[1]]
        i = session_at_or_after(dates, s[0] * 10000 + m0 * 100 + 1)
        j = session_at_or_after(dates, s[0] * 10000 + m1 * 100 + 1)
        if i <= 0 or j <= 0 or j <= i:
            continue
        ds = sorted(d for _, d in members)
        cut_lo, cut_hi = ds[len(ds) // 3], ds[2 * len(ds) // 3]
        early, mid, late = [], [], []
        for code, d in members:
            r = row[code]
            if not np.isfinite(A[r, i]) or A[r, i] < MIN_AMOUNT:
                continue
            e = excess(C, idx, r, i, j)
            if e is None:
                continue
            if d <= cut_lo:
                early.append(e)
            elif d >= cut_hi:
                late.append(e)
            else:
                mid.append(e)
        if len(early) >= 15 and len(late) >= 15:
            out[s] = dict(early=sum(early) / len(early), late=sum(late) / len(late),
                          n_early=len(early), n_late=len(late))
            out[s]["spread"] = out[s]["early"] - out[s]["late"]
            # The late tercile is where deferrals pile up, and a company defers
            # because the numbers are bad - which nobody knew at the season open.
            # Comparing early against the MIDDLE tercile drops most of that
            # hindsight: if the spread survives here, it is not just deferral.
            out[s]["mid"] = (sum(mid) / len(mid)) if len(mid) >= 15 else None
            out[s]["spread_mid"] = (out[s]["early"] - out[s]["mid"]
                                    if out[s]["mid"] is not None else None)
    return out


def main():
    px, pairs_path = sys.argv[1], sys.argv[2]
    dates, C, A, row, pairs = load(px, pairs_path)
    idx = universe_index(C)
    print("universe: %d stocks x %d sessions (%d-%d)"
          % (C.shape[0], C.shape[1], dates[0], dates[-1]))
    print("pairs: %d over %d distinct dates"
          % (len(pairs), len(set(d for _, d in pairs))))
    print()

    print("TEST 1  pre-disclosure drift (excess over universe, per-date mean)")
    print("  %-7s %6s %7s %9s %8s %7s"
          % ("window", "dates", "stocks", "mean%", "sd", "t"))
    for K in (3, 5, 10, 20):
        st = drift_test(dates, C, A, row, pairs, idx, K)
        print("  -%-6d %6d %7d %+9.3f %8.3f %+7.2f"
              % (K, st["n"], st["stocks"], st["mean"], st["sd"], st["t"]))

    print()
    print("  PLACEBO: same stocks, same window length, anchor moved off the event.")
    print("  Run at every window, because the window that survives is the claim.")
    print("  %-7s %8s %8s %8s %8s" % ("shift", "K=3", "K=5", "K=10", "K=20"))
    for off in (0, -60, -40, 40, 60):
        cells = []
        for K in (3, 5, 10, 20):
            st = drift_test(dates, C, A, row, pairs, idx, K, offset=off)
            cells.append("%+.2f/%+.1f" % (st["mean"], st["t"]))
        print("  %-7s %8s %8s %8s %8s"
              % ("true" if off == 0 else "%+d" % off, *cells))

    ev, af = event_and_after(dates, C, A, row, pairs, idx, 10)
    print()
    print("  event day   %5d dates  mean %+.3f%%  t %+.2f" % (ev["n"], ev["mean"], ev["t"]))
    print("  +10 after   %5d dates  mean %+.3f%%  t %+.2f" % (af["n"], af["mean"], af["t"]))

    print()
    print("BY BOARD, -10 window (tech boards carry a +/-20%% daily band)")
    print("  %-10s %6s %7s %9s %7s" % ("board", "dates", "stocks", "mean%", "t"))
    boards = collections.defaultdict(list)
    for code, d in pairs:
        boards[board_of(code)].append((code, d))
    for b, sub in sorted(boards.items(), key=lambda kv: -len(kv[1])):
        if len(sub) < 60:
            continue
        st = drift_test(dates, C, A, row, sub, idx, 10)
        print("  %-10s %6d %7d %+9.3f %+7.2f"
              % (b, st["n"], st["stocks"], st["mean"], st["t"]))

    print()
    print("TEST 2  early schedulers vs late, held over the same sessions")
    res = timing_test(dates, C, A, row, pairs, idx)
    if not res:
        print("  not enough seasons with >=40 members")
        return
    print("  %-10s %7s %7s %9s %9s %9s"
          % ("season", "n_earl", "n_late", "early%", "late%", "spread"))
    for s, v in sorted(res.items()):
        print("  %-10s %7d %7d %+9.2f %+9.2f %+9.2f"
              % ("%d-%s" % s, v["n_early"], v["n_late"], v["early"], v["late"], v["spread"]))
    for label, key in (("early - late  ", "spread"), ("early - middle", "spread_mid")):
        sp = [v[key] for v in res.values() if v.get(key) is not None]
        if len(sp) < 2:
            continue
        m = sum(sp) / len(sp)
        sd = math.sqrt(sum((x - m) ** 2 for x in sp) / (len(sp) - 1))
        t = m / (sd / math.sqrt(len(sp))) if sd else float("nan")
        pos = sum(1 for x in sp if x > 0)
        print("  %s  %+.2fpp  sd %.2f  t %+.2f  positive %d/%d seasons"
              % (label, m, sd, t, pos, len(sp)))


if __name__ == "__main__":
    main()
