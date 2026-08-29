#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Snapshot the broker order history before the window rolls past it.

The 妙想 orders endpoint returns a rolling window. On 2026-08-29 it held 293
records reaching back to 2026-06-01 while the account had run 163 days, and it
takes no date range and no pagination - fltOrderDrt and fltOrderStatus are its
only parameters. So the first ten weeks are simply gone, and about 12,090 of
realised P&L with them.

Nothing can recover that. What this stops is the same loss happening again: run
daily, and the archive grows even as the window slides forward.

WHY IT WRITES TWO FILES

A dated snapshot per day is the archive and is never rewritten. The merged file
is what everything else reads, and it is the union of every snapshot ever taken,
keyed by order id - so an order that has aged out of the API survives in the
merge for as long as the archive does.

THE FAILURE WORTH GUARDING IS NOT A SHORT RESPONSE

The merge is a union, so a truncated fetch cannot delete anything - yesterday's
orders survive on their own. The real way to lose the archive is subtler: read
the merged file, fail to parse it, treat that as "no history yet", and write one
window over the top. That is everything gone, silently, in a single run.

So an unreadable merged file is refused rather than replaced, and the operator
is told to move it aside deliberately. Absent and unreadable are different
claims and get different answers.

READ ONLY. It queries orders and writes files; it holds no trade or cancel path,
and a test asserts it.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ORDERS_ENDPOINT = "/api/claw/mockTrading/orders"
ORDERS_PAYLOAD = {"fltOrderDrt": 0, "fltOrderStatus": 0}
DEFAULT_TIMEOUT = 180


def extract_orders(payload) -> list:
    """Find the orders list wherever the response nests it.

    The skill has wrapped its result at data.orders and at data.data.orders in
    different versions, so this walks rather than assuming a shape.
    """
    if not isinstance(payload, (dict, list)):
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict) and "secCode" in r]
    for key in ("orders",):
        val = payload.get(key)
        if isinstance(val, list) and all(isinstance(r, dict) for r in val):
            return val
    for val in payload.values():
        found = extract_orders(val)
        if found:
            return found
    return []


def merge_orders(existing, incoming) -> list:
    """Union by order id, newest field values winning.

    An order can legitimately change - reported, then filled - so a later
    snapshot of the same id replaces the earlier one. Ids never seen again are
    kept forever, which is the entire point.
    """
    merged = {}
    for row in list(existing or []) + list(incoming or []):
        if not isinstance(row, dict):
            continue
        oid = str(row.get("id") or "").strip()
        key = oid or "%s|%s|%s" % (row.get("secCode"), row.get("time"),
                                   row.get("tradeCount"))
        merged[key] = row
    return sorted(merged.values(),
                  key=lambda r: (int(r.get("time") or 0), str(r.get("id") or "")))


def fetch_orders(skill_dir: str, python_exe: str,
                 timeout: int = DEFAULT_TIMEOUT) -> list:
    """One read-only call, via the skill's own api_request."""
    script = os.path.join(skill_dir, "mx_moni.py")
    if not os.path.exists(script):
        raise RuntimeError("mx_moni.py not found at %s" % script)
    code = (
        "import importlib.util,json,sys;"
        "spec=importlib.util.spec_from_file_location('m',%r);"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
        "print(json.dumps(m.api_request(%r,%r)))"
        % (script, ORDERS_ENDPOINT, ORDERS_PAYLOAD)
    )
    proc = subprocess.run([python_exe, "-c", code], cwd=skill_dir,
                          timeout=timeout, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)
    out = (proc.stdout or b"").decode("utf-8", "ignore").strip()
    if not out:
        raise RuntimeError("empty response from the orders endpoint")
    return extract_orders(json.loads(out.splitlines()[-1]))


def write_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def archive(orders, out_dir: Path, today: str | None = None,
            allow_shrink: bool = False) -> dict:
    """Write today's snapshot and refresh the merge. Returns what changed."""
    today = today or _dt.date.today().strftime("%Y-%m-%d")
    out_dir = Path(out_dir)
    merged_path = out_dir / "mx_moni_orders_merged.json"

    previous = []
    if merged_path.exists():
        try:
            previous = extract_orders(json.loads(
                merged_path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            # An unreadable archive must NOT be treated as an empty one. The
            # merge is a union, so it can only ever grow - which means the only
            # way to lose history here is to start from a blank slate and write
            # over what could not be parsed. That is the whole archive gone, and
            # silently, which is the failure this file exists to prevent.
            if not allow_shrink:
                raise RuntimeError(
                    "%s exists but could not be read (%s); refusing to overwrite "
                    "it with a fresh window - move it aside to start over"
                    % (merged_path, exc))
            previous = []

    merged = merge_orders(previous, orders)
    if len(merged) < len(previous) and not allow_shrink:
        raise RuntimeError(
            "merge would drop %d orders (%d -> %d); refusing to overwrite the "
            "archive with a smaller history"
            % (len(previous) - len(merged), len(previous), len(merged)))

    write_atomic(out_dir / ("mx_moni_orders_%s.json" % today),
                 {"captured_at": _dt.datetime.now().isoformat(timespec="seconds"),
                  "trade_date": today, "order_count": len(orders),
                  "orders": list(orders)})
    write_atomic(merged_path,
                 {"updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
                  "order_count": len(merged), "orders": merged})
    return {
        "fetched": len(orders),
        "known_before": len(previous),
        "known_after": len(merged),
        "new_orders": len(merged) - len(previous),
        "merged_path": str(merged_path),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skill-dir", default=os.environ.get("MX_MONI_DIR", ""),
                    help="directory holding mx_moni.py")
    ap.add_argument("--python", dest="python_exe", default=sys.executable)
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="permit the archive to lose orders; for repair only")
    args = ap.parse_args(argv)

    if not args.skill_dir:
        print("[ERROR] --skill-dir (or MX_MONI_DIR) must point at mx_moni.py",
              file=sys.stderr)
        return 2
    try:
        orders = fetch_orders(args.skill_dir, args.python_exe)
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        print("[ERROR] could not read orders: %s" % exc, file=sys.stderr)
        return 1
    if not orders:
        print("[ERROR] the endpoint returned no orders; not touching the archive",
              file=sys.stderr)
        return 1
    try:
        res = archive(orders, Path(args.out_dir), allow_shrink=args.allow_shrink)
    except RuntimeError as exc:
        print("[ERROR] %s" % exc, file=sys.stderr)
        return 1
    print("fetched %d, archive %d -> %d (+%d new) -> %s"
          % (res["fetched"], res["known_before"], res["known_after"],
             res["new_orders"], res["merged_path"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
