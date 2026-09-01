#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily: join what the account actually did to how each entry was chosen.

Run after the close. Reads broker fills (ground truth for outcome) and the
decision log (ground truth for why the entry was made), joins them, and appends
one snapshot line so the estimates can be watched converging.

Reads only. Writes one JSONL line. Places no orders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import entry_tuner as et  # noqa: E402
import mx_moni_ledger as ml  # noqa: E402

try:
    from package_paths import DATA_DIR
    _DATA = str(DATA_DIR)
except Exception:                                    # pragma: no cover
    _DATA = "/opt/stockbot/workbuddy/a-share-analyst"

ORDERS_FILE = os.path.join(_DATA, "mx_orders_archive", "mx_moni_orders_merged.json")
DECISIONS_FILE = os.path.join(_DATA, "v10_model_decisions.jsonl")
HISTORY_FILE = os.path.join(_DATA, "v10_entry_tuner_history.jsonl")


def _day(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def load_orders(path=ORDERS_FILE):
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload.get("orders") if isinstance(payload, dict) else payload


def load_entry_attributes(path=DECISIONS_FILE, smap=None):
    """(trade_date, code) -> how that entry was chosen.

    sector_same_day_buys is read when present and RECONSTRUCTED otherwise: the
    field only exists on records written after it was added, and the history
    that matters most is the history written before it. Reconstructing it from
    the same sector map keeps old and new rows on one definition.
    """
    picked = {}
    per_day_buys = defaultdict(list)
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return {}
    for r in rows:
        if not r.get("selected"):
            continue
        day = str(r.get("trade_date") or "")
        code = str(r.get("code") or "").zfill(6)
        if not day or not code:
            continue
        picked[(day, code)] = r
        per_day_buys[day].append(code)

    out = {}
    for (day, code), r in picked.items():
        same = r.get("sector_same_day_buys")
        if same is None and smap is not None:
            sec = smap.sector_code(code)
            if sec:
                same = sum(1 for c in per_day_buys[day]
                           if smap.sector_code(c) == sec)
        out[(day, code)] = {
            "tier": r.get("tier"),
            "mode": r.get("mode"),
            "entry_score": r.get("score"),
            "sector": r.get("sector"),
            "sector_same_day_buys": same,
        }
    return out


def join(episodes, attrs):
    """Attach entry attributes to each realised round trip.

    Matched on (buy date, code). An episode with no decision record keeps its
    outcome and carries no attributes - it still counts in the overall figure,
    which is why the overall trip count can exceed any single attribute's n.
    """
    trips = []
    matched = 0
    for ep in episodes:
        code = str(ep.get("code") or "").zfill(6)
        day = _day(ep.get("buy_time"))
        rec = dict(ep)
        a = attrs.get((day, code))
        if a:
            matched += 1
            rec.update(a)
        trips.append(rec)
    return trips, matched


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--orders", default=ORDERS_FILE)
    ap.add_argument("--decisions", default=DECISIONS_FILE)
    ap.add_argument("--history", default=HISTORY_FILE)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the snapshot without appending it")
    args = ap.parse_args(argv)

    try:
        import sector_map
        smap = sector_map.SectorMap()
        if not len(smap):
            smap = None
    except Exception:
        smap = None

    orders = load_orders(args.orders)
    episodes = ml.build_episodes(orders)["episodes"]
    attrs = load_entry_attributes(args.decisions, smap=smap)
    trips, matched = join(episodes, attrs)
    snap = et.build_snapshot(trips)
    snap["episodes"] = len(episodes)
    snap["matched_to_decisions"] = matched

    print("round trips %d, matched to a decision record %d"
          % (len(episodes), matched))
    print("overall %+.2f%% over %d trips (t%+.2f)"
          % (snap["overall_mean_pct"], snap["trips"], snap["overall_t"]))
    print()
    print("  %-16s %-12s %5s %9s %8s %8s %10s %s"
          % ("attribute", "value", "n", "mean", "effect", "t", "shrunk", "eligible"))
    for attr, entries in sorted(snap["effects"].items()):
        for lab, e in sorted(entries.items(), key=lambda kv: kv[1]["shrunk_pp"]):
            print("  %-16s %-12.12s %5d %+8.2f%% %+7.2f %+8.2f %+9.2f  %s"
                  % (attr, lab, e["n"], e["mean_pct"], e["effect_pp"],
                     e["t"], e["shrunk_pp"], "yes" if e["eligible"] else "-"))
    print()
    print("  shrink weight = t^2/(t^2+%g); applied only when eligible and "
          "TLFZ_ENTRY_TUNER=1 (now: %s)"
          % (et.SHRINK_K, "on" if et.TUNER_ENABLED else "off"))

    if args.dry_run:
        print()
        print("dry run - nothing written")
        return 0
    et.append_snapshot(snap, args.history)
    print()
    print("appended snapshot to %s" % args.history)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
