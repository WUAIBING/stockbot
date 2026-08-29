# -*- coding: utf-8 -*-
"""What happened to the candidates the mainline turned down?

VERDICT: THE FILTERS ARE RIGHT. DO NOT LOOSEN THEM.

Measured over 2,449 decisions on 21 dates, 2026-07-15 to 08-28, deduplicated to
1,241 unique candidates carrying a signal - 78 bought, 1,163 refused.

Selection beats rejection at every horizon:

    hold    selected   rejected      gap       t
    3         +0.02%     -0.98%   +1.00%    0.92
    5         +1.41%     -0.65%   +2.07%    1.67
    10        +3.45%     +1.22%   +2.22%    1.30

And flow is the filter doing most of the work. It separates the two groups
harder than any other component - selected mean 58.26 against rejected 38.18,
a gap of +20.07, against +12.62 for the stock score and +10.26 for the total.

THE MISTAKE THIS FILE EXISTS TO RECORD

A first pass measured LOW-FLOW REJECTS IN GENERAL - 613 names - and found them
performing identically to what was bought, gap 0.08% with t=0.03 at ten
sessions. Read alone that says the flow gate costs breadth and buys nothing,
and the obvious move is to loosen it.

That is the wrong population. Most rejects are simply lower-scoring, which is an
ordinary ranking cut. The set a relaxation would actually ADMIT is the 103 names
that OUTSCORED a pick on the same day and were held back anyway - and they are a
different animal entirely:

    hold    picks    those rejects       gap       t
    3      +0.02%          -3.04%    -3.07%   -1.80
    5      +2.03%          -3.95%    -5.98%   -2.91
    10     +3.40%          -0.67%    -4.07%   -1.30

They lose three to six points against what was bought, and t=-2.91 at five
sessions is the strongest number in the study. The gate is catching names the
score model likes that have no money behind them: 正帆科技 scored 80.0 on
2026-08-28 with a flow of 39.8 and was correctly refused.

Measuring the convenient population instead of the decision-relevant one would
have loosened the most valuable filter in the system.

WHERE THE DEPLOYMENT PROBLEM ACTUALLY IS

Not entries. Those run about five a day and are well chosen. The book holds 10
of 25 slots because positions do not survive - 23 of 26 closed trades died
within four days, 10 of them within one, at a 10% win rate. Five entries a day
against a ten-session hold would settle near 50 positions.

--- method ---


The book is built for 25 slots and ran 1.6 positions through July, with 4% of
the money working. Friday's scan produced 52 signals and bought 6. No daily cap
does that - max_new_positions is 0 and the gate is inactive - so the filters are
what hold the book small.

That is only a problem if the rejected names would have made money. This asks
them.

NO BENCHMARK IS NEEDED. Selected and rejected candidates are scored on the SAME
DATE, so comparing their forward returns to each other holds the market, the
regime and the season fixed by construction. The paired difference per date is
the whole answer, and averaging it across dates - rather than across candidates
- keeps one busy day with 200 rejects from outvoting twenty quiet ones.
"""
from __future__ import annotations
import json, os, sys, math, collections, io
import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
REC = np.dtype([('date','<u4'),('o','<u4'),('h','<u4'),('l','<u4'),
                ('c','<u4'),('a','<f4'),('v','<u4'),('r','<u4')])
VIPDOC = r"D:\new_tdx\vipdoc"
_cache: dict = {}


def bars(code):
    if code in _cache:
        return _cache[code]
    out = None
    for mk in ("sh", "sz", "bj"):
        p = os.path.join(VIPDOC, mk, "lday", "%s%s.day" % (mk, code))
        if os.path.exists(p):
            a = np.fromfile(p, dtype=REC)
            a = a[a['date'] >= 20260101]
            if a.size:
                out = ([int(x) for x in a['date']], a['c'] / 100.0)
            break
    _cache[code] = out
    return out


def fwd(code, date_int, k):
    b = bars(code)
    if not b:
        return None
    dates, close = b
    try:
        i = dates.index(date_int)
    except ValueError:
        return None
    if i + k >= len(dates) or close[i] <= 0:
        return None
    r = (close[i + k] / close[i] - 1.0) * 100.0
    return None if abs(r) > 60 else r


def stats(vals):
    vals = [v for v in vals if v is not None and np.isfinite(v)]
    n = len(vals)
    if n < 2:
        return n, float("nan"), float("nan")
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    return n, m, (m / (sd / math.sqrt(n)) if sd else float("nan"))


def main(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    for r in rows:
        r["_d"] = int(str(r.get("trade_date", "")).replace("-", "") or 0)
        r["_sel"] = bool(r.get("selected"))
        c = r.get("components") or {}
        r["_flow"] = c.get("flow")
        r["_score"] = r.get("score")

    print("decisions %d over %d dates" % (rows and len(rows), len(set(r["_d"] for r in rows))))
    print()
    print("SELECTED vs REJECTED, paired within each decision date")
    print("  %-6s %6s %9s %9s %9s %7s" % ("hold", "dates", "selected", "rejected",
                                          "gap", "t"))
    for k in (3, 5, 10):
        per_date = {}
        for d in sorted(set(r["_d"] for r in rows)):
            day = [r for r in rows if r["_d"] == d]
            s = [fwd(r["code"], d, k) for r in day if r["_sel"]]
            j = [fwd(r["code"], d, k) for r in day if not r["_sel"]]
            s = [x for x in s if x is not None]
            j = [x for x in j if x is not None]
            if len(s) < 1 or len(j) < 5:
                continue
            per_date[d] = (sum(s) / len(s), sum(j) / len(j))
        if len(per_date) < 2:
            print("  %-6d  insufficient forward data" % k)
            continue
        sel = [v[0] for v in per_date.values()]
        rej = [v[1] for v in per_date.values()]
        gaps = [a - b for a, b in per_date.values()]
        n, m, t = stats(gaps)
        print("  %-6d %6d %+8.2f%% %+8.2f%% %+8.2f%% %+7.2f"
              % (k, n, sum(sel) / len(sel), sum(rej) / len(rej), m, t))

    # The flow component is the prime suspect: it declined 正帆科技 at 79.97,
    # the highest score on 2026-08-28, for a flow of 39.75.
    print()
    print("REJECTED ONLY, split by the flow score that did the rejecting")
    print("  %-14s %6s %7s %9s %9s %7s" % ("flow band", "dates", "names",
                                           "fwd10", "vs sel", "t"))
    bands = (("flow < 40", lambda f: f is not None and f < 40),
             ("flow 40-60", lambda f: f is not None and 40 <= f < 60),
             ("flow 60-75", lambda f: f is not None and 60 <= f < 75),
             ("flow >= 75", lambda f: f is not None and f >= 75))
    for label, pred in bands:
        per_date, names = {}, 0
        for d in sorted(set(r["_d"] for r in rows)):
            day = [r for r in rows if r["_d"] == d]
            s = [fwd(r["code"], d, 10) for r in day if r["_sel"]]
            j = [fwd(r["code"], d, 10) for r in day
                 if not r["_sel"] and pred(r["_flow"])]
            s = [x for x in s if x is not None]
            j = [x for x in j if x is not None]
            if len(s) < 1 or len(j) < 3:
                continue
            names += len(j)
            per_date[d] = (sum(j) / len(j), sum(s) / len(s))
        if len(per_date) < 2:
            print("  %-14s   too few" % label)
            continue
        gaps = [a - b for a, b in per_date.values()]
        n, m, t = stats(gaps)
        print("  %-14s %6d %7d %+8.2f%% %+8.2f%% %+7.2f"
              % (label, n, names,
                 sum(v[0] for v in per_date.values()) / len(per_date), m, t))

    print()
    print("THE TOP REJECTS: highest-scoring names the system turned down")
    print("  %-6s %-7s %-9s %7s %7s %9s" % ("date", "code", "name", "score",
                                            "flow", "fwd10"))
    top = sorted((r for r in rows if not r["_sel"] and r["_score"]),
                 key=lambda r: -float(r["_score"] or 0))[:15]
    for r in top:
        f = fwd(r["code"], r["_d"], 10)
        print("  %-6d %-7s %-9s %7.1f %7s %9s"
              % (r["_d"] % 10000, r["code"], (r.get("name") or "")[:8],
                 float(r["_score"] or 0),
                 ("%.1f" % r["_flow"]) if r["_flow"] is not None else "-",
                 ("%+.2f%%" % f) if f is not None else "n/a"))


if __name__ == "__main__":
    main(sys.argv[1])
