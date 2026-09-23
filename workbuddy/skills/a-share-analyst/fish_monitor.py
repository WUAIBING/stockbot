#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Watch named stocks and say where they stand. It never trades.

WHY A WATCHLIST AND NOT A SCANNER

The obvious version of this - scan the market for "a runner pulling back into
the MA5-MA20 band" - was measured first, on CSI 1000 daily bars over ~2 years:

    any liquid stock-day            n=337,076   touch +20% in 20d 20.0%   fwd20 +1.09%
    ran +20% in 20d, in MA20..MA5   n= 28,238   touch +20%        26.1%   fwd20 +0.11%
    ran +30% in 20d, in MA20..MA5   n= 15,678   touch +20%        29.3%   fwd20 -0.36%
    (a volume-contraction filter changed nothing)

It finds more volatile names - 6 points more reach +20% - and its average
outcome is WORSE than picking any liquid stock. A volatility filter, not an
edge. So there is no market-wide candidate list here: it would be a daily page
of names no better than random. What this does is watch the names a human
decided to watch, and report where they are against their own bands.

The band defaults to MA20..MA5, which is where a pullback in an uptrend usually
sits, and it is reported, never acted on. Entering the band is not a buy
signal; the numbers above say plainly that it is not.

Reads market data and its own files. No order function is imported or called.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from package_paths import DATA_DIR  # noqa: E402
import tdx_hosts  # noqa: E402

WATCHLIST_FILE = DATA_DIR / "fish_watchlist.json"
LATEST_FILE = DATA_DIR / "fish_monitor_latest.json"
HISTORY_FILE = DATA_DIR / "fish_monitor_history.jsonl"

BELOW, IN_BAND, ABOVE = "below_band", "in_band", "above_band"

# The droplet runs UTC. Every timestamp here is market time, or the log says
# 08:13 for a session that ended at 15:00 - a mistake already made twice in
# this codebase.
CHINA = timezone(timedelta(hours=8))


def now_china():
    return datetime.now(CHINA)


def _market(code):
    return 1 if str(code).startswith(("6", "9")) else 0


def load_watchlist():
    try:
        payload = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = payload.get("watch") if isinstance(payload, dict) else payload
    return [i for i in (items or []) if str(i.get("code", "")).strip()]


def save_watchlist(items):
    WATCHLIST_FILE.write_text(json.dumps(
        {"updated_at": now_china().strftime("%Y-%m-%d %H:%M:%S CST"), "watch": items},
        ensure_ascii=False, indent=1), encoding="utf-8")


def measure(bars, low_ma=20, high_ma=5):
    """Where the stock stands against its own band, from completed daily bars."""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    amts = [b["amount"] for b in bars]
    if len(closes) < max(low_ma, 60):
        return None
    last = closes[-1]

    def ma(n):
        return sum(closes[-n:]) / n

    band_low, band_high = ma(low_ma), ma(high_ma)
    if band_low > band_high:
        band_low, band_high = band_high, band_low
    state = IN_BAND if band_low <= last <= band_high else (BELOW if last < band_low else ABOVE)
    run_high = max(highs[-20:])
    return {
        "date": bars[-1]["date"], "close": round(last, 3),
        "ma5": round(ma(5), 3), "ma10": round(ma(10), 3),
        "ma20": round(ma(20), 3), "ma60": round(ma(60), 3),
        "band_low": round(band_low, 3), "band_high": round(band_high, 3),
        "state": state,
        "to_band_top_pct": round((band_high / last - 1) * 100, 2),
        "to_band_bottom_pct": round((band_low / last - 1) * 100, 2),
        "run20_pct": round((run_high / min(closes[-20:]) - 1) * 100, 2),
        "off_high20_pct": round((last / run_high - 1) * 100, 2),
        "high20": round(run_high, 3), "low20": round(min(lows[-20:]), 3),
        "amount_yi": round(amts[-1] / 1e8, 2),
        "amount_vs_5d": round(amts[-1] / (sum(amts[-5:]) / 5), 2) if sum(amts[-5:]) else None,
        "range5_pct": round(sum((highs[i] / lows[i] - 1) * 100 for i in range(-5, 0)) / 5, 2),
    }


def previous_states():
    """The last state seen per code, for transition alerts."""
    out = {}
    try:
        with open(HISTORY_FILE, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                for item in row.get("items", []):
                    if item.get("state"):
                        out[item["code"]] = item["state"]
    except OSError:
        pass
    return out


def fetch(api, code, count=90):
    rows = api.get_security_bars(9, _market(code), str(code).zfill(6), 0, count) or []
    return [{"date": str(b["datetime"])[:10], "open": float(b["open"]), "high": float(b["high"]),
             "low": float(b["low"]), "close": float(b["close"]), "amount": float(b["amount"])}
            for b in rows]


def run(write=True):
    watch = load_watchlist()
    if not watch:
        print("watchlist is empty: %s" % WATCHLIST_FILE)
        return 0
    api, where = tdx_hosts.connect_verified(log=None, heartbeat=False)
    before = previous_states()
    items, alerts = [], []
    try:
        for entry in watch:
            code = str(entry["code"]).zfill(6)
            bars = fetch(api, code)
            m = measure(bars, int(entry.get("low_ma", 20)), int(entry.get("high_ma", 5)))
            if not m:
                items.append({"code": code, "name": entry.get("name", ""), "error": "no bars"})
                continue
            m.update(code=code, name=entry.get("name", ""), note=entry.get("note", ""))
            was = before.get(code)
            m["entered_band"] = bool(was and was != IN_BAND and m["state"] == IN_BAND)
            if m["entered_band"]:
                alerts.append(m)
            items.append(m)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    payload = {"generated_at": now_china().strftime("%Y-%m-%d %H:%M:%S CST"),
               "host": where, "items": items}
    print(" fish monitor %s | host %s" % (payload["generated_at"], where))
    print(" %-7s %-9s %8s %8s %8s %8s %-10s %7s %7s %8s" % (
        "code", "name", "close", "MA5", "MA20", "MA60", "state", "to top", "run20", "amt亿"))
    for m in items:
        if m.get("error"):
            print(" %-7s %-9s  %s" % (m["code"], m.get("name", "")[:9], m["error"]))
            continue
        print(" %-7s %-9s %8.2f %8.2f %8.2f %8.2f %-10s %+6.1f%% %+6.1f%% %8.2f%s" % (
            m["code"], str(m["name"])[:9], m["close"], m["ma5"], m["ma20"], m["ma60"],
            m["state"], m["to_band_top_pct"], m["run20_pct"], m["amount_yi"],
            "  <= ENTERED BAND" if m["entered_band"] else ""))
    for m in alerts:
        print(" [ALERT] %s %s entered %.2f-%.2f (close %.2f). A band is not a signal: "
              "the pattern tested flat - see the module docstring." % (
                  m["code"], m["name"], m["band_low"], m["band_high"], m["close"]))
    if write:
        LATEST_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return len(alerts)


def main(argv=None):
    p = argparse.ArgumentParser(description="watch named stocks against their MA bands; never trades")
    p.add_argument("--add", metavar="CODE")
    p.add_argument("--name", default="")
    p.add_argument("--note", default="")
    p.add_argument("--low-ma", type=int, default=20)
    p.add_argument("--high-ma", type=int, default=5)
    p.add_argument("--remove", metavar="CODE")
    p.add_argument("--list", action="store_true")
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args(argv)

    if args.add:
        items = [i for i in load_watchlist() if str(i["code"]).zfill(6) != args.add.zfill(6)]
        items.append({"code": args.add.zfill(6), "name": args.name, "note": args.note,
                      "low_ma": args.low_ma, "high_ma": args.high_ma,
                      "added_at": now_china().strftime("%Y-%m-%d")})
        save_watchlist(items)
        print("watching %s (%d on the list)" % (args.add.zfill(6), len(items)))
        return 0
    if args.remove:
        items = [i for i in load_watchlist() if str(i["code"]).zfill(6) != args.remove.zfill(6)]
        save_watchlist(items)
        print("removed %s (%d left)" % (args.remove.zfill(6), len(items)))
        return 0
    if args.list:
        for i in load_watchlist():
            print(" %s %s %s" % (str(i["code"]).zfill(6), i.get("name", ""), i.get("note", "")))
        return 0
    run(write=not args.no_write)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
