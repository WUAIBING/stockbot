#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 mx-xuangu 生成 challenger 候选池。

当前边界：
1. 可在收盘节点或盘中 `workbuddy-refresh` 节点生成 challenger 候选池；
2. 不接主交易下单主链；
3. 主要用于和当前 scanner 候选做交叉验证，并服务 workbuddy 预备军刷新。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path

from package_paths import DATA_DIR


SCAN_CSV = DATA_DIR / "v10_scan_full.csv"
SCAN_META_FILE = DATA_DIR / "v10_scan_meta.json"
OUTPUT_CSV = DATA_DIR / "mx_challenger_pool_latest.csv"
OUTPUT_JSON = DATA_DIR / "mx_challenger_pool_latest.json"
MX_XUANGU_SKILL = Path.home() / ".trae" / "skills" / "mx-xuangu" / "mx_xuangu.py"
QUERY_CANDIDATES = [
    "ROE大于15% 净利润连续三年增长",
    "净资产收益率大于15%的公司",
]


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            with path.open("r", encoding=encoding) as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["股票代码", "股票简称", "在当前扫描池", "scanner_tier", "scanner_mode"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_scan_rows() -> list[dict]:
    if not SCAN_CSV.exists():
        return []
    with SCAN_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _sanitize_slot(text: str) -> str:
    value = str(text or "").strip()
    return value.replace(":", "").replace(" ", "_") or datetime.now().strftime("%Y-%m-%d_%H%M")


def _pick_key(row: dict, keywords: tuple[str, ...], *, exclude: tuple[str, ...] = ()) -> str:
    for key in row.keys():
        if any(token in key for token in exclude):
            continue
        if all(keyword in key for keyword in keywords):
            return key
    for key in row.keys():
        if any(token in key for token in exclude):
            continue
        if any(keyword in key for keyword in keywords):
            return key
    return ""


def _build_scan_lookup(rows: list[dict]) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("code", "")).zfill(6)
        if not code:
            continue
        lookup[code] = {
            "scanner_tier": row.get("tier", ""),
            "scanner_mode": row.get("mode", ""),
            "scanner_weekly_slope": row.get("weekly_slope", ""),
        }
    return lookup


def main() -> int:
    if not os.environ.get("MX_APIKEY", "").strip():
        print("[ERROR] MX_APIKEY 未配置")
        return 1
    if not MX_XUANGU_SKILL.exists():
        print(f"[ERROR] mx-xuangu skill 缺失: {MX_XUANGU_SKILL}")
        return 1

    scan_rows = _load_scan_rows()
    scan_lookup = _build_scan_lookup(scan_rows)
    scan_meta = _read_json(SCAN_META_FILE)
    run_slot = _sanitize_slot(scan_meta.get("run_slot") or datetime.now().strftime("%Y-%m-%d_%H%M"))
    trade_date = run_slot.split("_", 1)[0]
    raw_dir = DATA_DIR / "mx_challenger_pool" / run_slot
    raw_dir.mkdir(parents=True, exist_ok=True)

    mx_xuangu_mod = _load_module(MX_XUANGU_SKILL, "mx_xuangu_runtime")
    client = mx_xuangu_mod.MXSelectStock()

    rows: list[dict] = []
    used_query = ""
    last_error = ""
    raw_result: dict = {}
    for query in QUERY_CANDIDATES:
        try:
            raw_result = client.search(query)
            rows, _, err = mx_xuangu_mod.MXSelectStock.extract_data(raw_result)
            if err:
                last_error = err
                continue
            if rows:
                used_query = query
                break
        except Exception as exc:
            last_error = str(exc)
            continue

    if not rows:
        payload = {
            "generated_at": _now_str(),
            "trade_date": trade_date,
            "run_slot": run_slot,
            "status": "error",
            "query_candidates": QUERY_CANDIDATES,
            "error": last_error or "mx-xuangu 无返回结果",
            "records": [],
        }
        _write_json_atomic(OUTPUT_JSON, payload)
        _write_json_atomic(DATA_DIR / f"mx_challenger_pool.{run_slot}.json", payload)
        _write_csv(OUTPUT_CSV, [])
        _write_csv(DATA_DIR / f"mx_challenger_pool.{run_slot}.csv", [])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 3

    _write_json_atomic(raw_dir / "raw.json", raw_result)
    code_key = _pick_key(rows[0], ("代码",))
    name_key = _pick_key(rows[0], ("名称",)) or _pick_key(rows[0], ("简称",), exclude=("市场",))

    annotated_rows: list[dict] = []
    overlap_count = 0
    for row in rows:
        item = dict(row)
        code = str(item.get(code_key, "")).zfill(6) if code_key else ""
        name = str(item.get(name_key, "")) if name_key else ""
        matched = scan_lookup.get(code, {})
        if matched:
            overlap_count += 1
        item["股票代码"] = code or item.get(code_key, "")
        item["股票简称"] = name or item.get(name_key, "")
        item["在当前扫描池"] = "是" if matched else "否"
        item["scanner_tier"] = matched.get("scanner_tier", "")
        item["scanner_mode"] = matched.get("scanner_mode", "")
        item["scanner_weekly_slope"] = matched.get("scanner_weekly_slope", "")
        annotated_rows.append(item)

    snapshot_csv = DATA_DIR / f"mx_challenger_pool.{run_slot}.csv"
    snapshot_json = DATA_DIR / f"mx_challenger_pool.{run_slot}.json"
    _write_csv(OUTPUT_CSV, annotated_rows)
    _write_csv(snapshot_csv, annotated_rows)
    payload = {
        "generated_at": _now_str(),
        "trade_date": trade_date,
        "run_slot": run_slot,
        "status": "ok",
        "query_used": used_query,
        "query_candidates": QUERY_CANDIDATES,
        "row_count": len(annotated_rows),
        "overlap_with_scan_count": overlap_count,
        "records": annotated_rows,
    }
    _write_json_atomic(OUTPUT_JSON, payload)
    _write_json_atomic(snapshot_json, payload)
    print(
        json.dumps(
            {
                "trade_date": trade_date,
                "run_slot": run_slot,
                "query_used": used_query,
                "row_count": len(annotated_rows),
                "overlap_with_scan_count": overlap_count,
                "output_csv": str(OUTPUT_CSV),
                "output_json": str(OUTPUT_JSON),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
