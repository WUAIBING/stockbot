#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch the forward disclosure calendar and emit ranked candidates.

disclosure_calendar.py holds the measured logic and touches nothing. This is the
part that reaches out: it queries the 妙想 screener, works out where each name
sits inside its own sector, and writes a dated candidate file. It still does not
trade, and a test asserts it holds no execution path.

TWO QUERIES ARE THE WHOLE PIPELINE

  the calendar   定期报告预计披露日期在A到B之间的A股，显示所属东财行业和成交额
                 returns code, name, scheduled date, sector and turnover in one
                 call, which is why no separate industry map is shipped

  a sector       所属行业为X的A股按成交额从大到小排列
                 returns that sector ordered by turnover, so rank is just row
                 position. 半导体 came back as 186 rows, inside the 200 cap that
                 every 妙想 screener query is subject to

Ranking a stock only against the handful of its sector neighbours that happen to
report the same day would call almost everyone a leader, so the sector query
covers the WHOLE sector and the calendar is matched into it.

WHY THE SESSION COUNT IS FUSSY

The window is measured in SESSIONS, and A-share holidays are long: the 2026
National Day break runs 1-7 October, squarely inside Q3 reporting season, and
中秋 closes 25-27 September right before it. Counting calendar days across that
would enter a position more than a week early, in the part of the window where
the measurement says there is no edge.

An INCOMPLETE holiday table is worse than none, because it fails silently and it
fails hardest in April, when 清明 sits in the middle of annual-report season. So
the table below is explicit about the years it covers and the runner REFUSES to
score anything outside that coverage rather than guessing a weekday calendar.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import glob
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import disclosure_calendar as dc  # noqa: E402

# Published by the exchanges, and every weekday claim in the source was checked
# against the actual calendar before being written down (21 of 21 matched):
#
#   元旦    1 Jan (Thu) - 3 Jan (Sat)      reopen 5 Jan
#   春节    15 Feb (Sun) - 23 Feb (Mon)    reopen 24 Feb
#   清明    4 Apr (Sat) - 6 Apr (Mon)      reopen 7 Apr
#   劳动节  1 May (Fri) - 5 May (Tue)      reopen 6 May
#   端午    19 Jun (Fri) - 21 Jun (Sun)    reopen 22 Jun
#   中秋    25 Sep (Fri) - 27 Sep (Sun)    reopen 28 Sep
#   国庆    1 Oct (Thu) - 7 Oct (Wed)      reopen 8 Oct
_HOLIDAY_RANGES_2026 = (
    ("2026-01-01", "2026-01-03"),
    ("2026-02-15", "2026-02-23"),
    ("2026-04-04", "2026-04-06"),
    ("2026-05-01", "2026-05-05"),
    ("2026-06-19", "2026-06-21"),
    ("2026-09-25", "2026-09-27"),
    ("2026-10-01", "2026-10-07"),
)

# Years whose holiday table has been entered and checked. Outside these the
# runner declines: a wrong session count puts the entry in the wrong window,
# and the whole edge is which window you are in.
HOLIDAY_COVERAGE_YEARS = (2026,)

DEFAULT_HORIZON_DAYS = 12         # calendar days of forward calendar to request
SCREENER_TIMEOUT_SEC = 240


def _expand(ranges) -> set:
    out = set()
    for start, end in ranges:
        a = _dt.date(*map(int, start.split("-")))
        b = _dt.date(*map(int, end.split("-")))
        while a <= b:
            out.add(a)
            a += _dt.timedelta(days=1)
    return out


MARKET_HOLIDAYS = _expand(_HOLIDAY_RANGES_2026)


def _to_date(d: int) -> _dt.date:
    return _dt.date(d // 10000, (d // 100) % 100, d % 100)


def _to_int(d: _dt.date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def trading_dates(start: int, end: int) -> list[int]:
    """Sessions between two dates: weekdays that are not exchange holidays."""
    out = []
    cur, last = _to_date(start), _to_date(end)
    while cur <= last:
        if cur.weekday() < 5 and cur not in MARKET_HOLIDAYS:
            out.append(_to_int(cur))
        cur += _dt.timedelta(days=1)
    return out


def coverage_ok(start: int, end: int) -> bool:
    """Both ends must fall in a year whose holiday table was actually entered."""
    return (start // 10000 in HOLIDAY_COVERAGE_YEARS
            and end // 10000 in HOLIDAY_COVERAGE_YEARS)


def _shift_days(d: int, days: int) -> int:
    return _to_int(_to_date(d) + _dt.timedelta(days=days))


def run_screener(query: str, skill_dir: str, python_exe: str,
                 timeout: int = SCREENER_TIMEOUT_SEC) -> list[dict]:
    """One screener call, into a private directory so the CSV is unambiguous.

    mx_xuangu names its output after the query, so a shared directory turns
    "which file did this call produce" into a guess. A fresh temp directory per
    call makes it the only file there.
    """
    script = os.path.join(skill_dir, "mx_xuangu.py")
    if not os.path.exists(script):
        raise RuntimeError("screener not found at %s" % script)
    with tempfile.TemporaryDirectory(prefix="disc_cal_") as tmp:
        try:
            subprocess.run([python_exe, script, query, "--output-dir", tmp],
                           cwd=skill_dir, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           check=False)
        except subprocess.TimeoutExpired:
            return []
        files = [f for f in glob.glob(os.path.join(tmp, "*.csv"))]
        if not files:
            return []
        with open(files[0], encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))


def _col(row, *fragments):
    for key in row:
        for frag in fragments:
            if frag in str(key):
                return row[key]
    return None


def sector_of(row) -> str | None:
    s = _col(row, "东财行业分类二级", "行业分类二级", "申万行业分类", "所属行业")
    s = str(s or "").strip()
    return s or None


def fetch_calendar(start: int, end: int, skill_dir: str, python_exe: str):
    """The forward calendar, carrying sector and turnover in the same call."""
    def fmt(d):
        return "%d年%d月%d日" % (d // 10000, (d // 100) % 100, d % 100)
    q = ("定期报告预计披露日期在%s到%s之间的A股，显示所属东财行业和成交额"
         % (fmt(start), fmt(end)))
    rows = run_screener(q, skill_dir, python_exe)
    # Match sector to entry BY CODE, never by position. parse_calendar_rows drops
    # rows it cannot use - a bad code, a missing date - so zipping its output
    # against the raw rows shifts every sector after the first dropped row and
    # silently files a name under someone else industry.
    sectors = {}
    for raw in rows:
        code = str(_col(raw, "代码") or "").strip().zfill(6)
        if code.isdigit() and len(code) == 6:
            sectors.setdefault(code, sector_of(raw))
    entries = []
    for parsed in dc.parse_calendar_rows(rows):
        parsed["sector"] = sectors.get(parsed["code"])
        entries.append(parsed)
    return entries


def fetch_sector_ranking(sector: str, skill_dir: str, python_exe: str) -> dict:
    """{code: 1-based turnover rank} for a whole sector."""
    q = "所属行业为%s的A股按成交额从大到小排列" % sector
    rows = run_screener(q, skill_dir, python_exe)
    ranks = {}
    for i, r in enumerate(rows, start=1):
        code = str(_col(r, "代码") or "").strip().zfill(6)
        if code.isdigit() and len(code) == 6 and code not in ranks:
            ranks[code] = i
    return ranks


def sectors_worth_ranking(entries) -> set:
    """Sectors holding at least one name that could survive the liquidity floor.

    One screener call per sector, and a day's calendar spans ~83 of them. A
    sector whose every calendar name is already below the floor will have all of
    those names rejected whatever their rank, so fetching its ranking buys
    nothing.

    Unknown turnover does NOT count as illiquid - the calendar query need not
    carry the column, and treating a missing value as a rejection would silently
    empty the book on the day the screener changes its columns.
    """
    out = set()
    for e in entries:
        sector = e.get("sector")
        if not sector:
            continue
        turnover = e.get("turnover")
        if turnover is None or turnover >= dc.MIN_TURNOVER_YUAN:
            out.add(sector)
    return out


def build_candidates(today: int, entries, sector_ranks, horizon_days: int):
    """Score every calendar entry, keeping the rejections and their reasons.

    A silent empty result is indistinguishable from a broken one, which is how a
    candidate pool in this system stayed frozen for 37 days without anyone
    noticing. Everything scored is written out, selected or not.
    """
    cal = trading_dates(_shift_days(today, -30), _shift_days(today, horizon_days + 20))
    scored = []
    for e in entries:
        entry = dict(e)
        ranks = sector_ranks.get(e.get("sector") or "", {})
        entry["sector_rank"] = ranks.get(e["code"])
        scored.append(dc.evaluate(entry, today, cal))
    return scored


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--today", type=int, default=None, help="YYYYMMDD")
    ap.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    ap.add_argument("--skill-dir",
                    default=os.environ.get("MX_XUANGU_DIR", ""),
                    help="directory holding mx_xuangu.py")
    ap.add_argument("--python", dest="python_exe", default=sys.executable)
    ap.add_argument("--out", default="disclosure_candidates_latest.json")
    args = ap.parse_args(argv)

    today = args.today or _to_int(_dt.date.today())
    horizon_end = _shift_days(today, args.horizon_days)

    if not coverage_ok(today, horizon_end):
        print("[ERROR] no verified holiday table for %d-%d; refusing to count "
              "sessions rather than guess a weekday calendar"
              % (today // 10000, horizon_end // 10000), file=sys.stderr)
        return 2
    if not args.skill_dir:
        print("[ERROR] --skill-dir (or MX_XUANGU_DIR) must point at mx_xuangu.py",
              file=sys.stderr)
        return 2

    entries = fetch_calendar(today, horizon_end, args.skill_dir, args.python_exe)
    if not entries:
        print("[WARN] the calendar query returned nothing - either the season is "
              "over or the screener failed; not writing an empty candidate file",
              file=sys.stderr)
        return 1

    sectors = sorted(sectors_worth_ranking(entries))
    skipped = len({e["sector"] for e in entries if e.get("sector")}) - len(sectors)
    sector_ranks = {}
    for s in sectors:
        sector_ranks[s] = fetch_sector_ranking(s, args.skill_dir, args.python_exe)

    scored = build_candidates(today, entries, sector_ranks, args.horizon_days)
    selected = dc.rank(scored)

    payload = {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "today": today,
        "horizon_end": horizon_end,
        "entries_scored": len(scored),
        "sectors_ranked": len(sectors),
        "sectors_skipped_illiquid": skipped,
        "unranked": sum(1 for c in scored if c.get("sector_rank") is None),
        "selected": selected,
        "rejected": [c for c in scored if not c.get("selected")],
    }
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("scored %d over %d sectors (%d skipped as illiquid); %d selected -> %s"
          % (len(scored), len(sectors), skipped, len(selected), args.out))
    for c in selected[:15]:
        print("  %-8s %-10s rank %-4s in %-10s reports in %s sessions"
              % (c["code"], (c.get("name") or "")[:9],
                 c.get("sector_rank"), (c.get("sector") or "?")[:10],
                 c.get("sessions_until")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
