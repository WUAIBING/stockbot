#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 mx-data 给收盘候选池补充权威特征。

最小版原则：
1. 只读取现有 v10_scan_full.csv，不改盘中主链；
2. 只在收盘节点运行；
3. 只输出观察产物，不直接接学习层。
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
OUTPUT_CSV = DATA_DIR / "mx_enrich_candidates_latest.csv"
OUTPUT_JSON = DATA_DIR / "mx_enrich_candidates_latest.json"
MX_DATA_SKILL = Path.home() / ".trae" / "skills" / "mx-data" / "mx_data.py"
QUERY_TEMPLATE = "{name} 最新价 市盈率 市净率 净资产收益率"
MAX_CANDIDATES = 12


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
    if not rows:
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "code",
                    "name",
                    "tier",
                    "mode",
                    "mx_query",
                    "mx_date",
                    "mx_last_price",
                    "mx_pe",
                    "mx_pb",
                    "mx_roe",
                    "mx_status",
                    "mx_error",
                ]
            )
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
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


def _fnum(value, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _inum(value, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return default


def _sanitize_slot(text: str) -> str:
    value = str(text or "").strip()
    return value.replace(":", "").replace(" ", "_") or datetime.now().strftime("%Y-%m-%d_%H%M")


def _load_scan_rows() -> list[dict]:
    if not SCAN_CSV.exists():
        return []
    with SCAN_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _rank_candidates(rows: list[dict]) -> list[dict]:
    signal_rows = [row for row in rows if _inum(row.get("tier", 0), 0) > 0]
    signal_rows.sort(
        key=lambda item: (
            _inum(item.get("tier", 0), 0),
            -_fnum(item.get("weekly_slope", 0.0), 0.0),
            -_fnum(item.get("close_vs_ma20_pct", 0.0), 0.0),
            item.get("code", ""),
        )
    )
    return signal_rows[:MAX_CANDIDATES]


def _pick_metric(flattened: dict[str, str], keywords: tuple[str, ...]) -> str:
    for key, value in flattened.items():
        if key == "date":
            continue
        if any(keyword in key for keyword in keywords):
            return str(value)
    return ""


def _flatten_tables(tables: list[dict]) -> dict[str, str]:
    flattened: dict[str, str] = {}
    for table in tables:
        rows = table.get("rows") or []
        if not rows:
            continue
        latest = rows[0]
        if "date" in latest and "date" not in flattened:
            flattened["date"] = str(latest.get("date", ""))
        for key, value in latest.items():
            if key == "date":
                continue
            flattened[str(key)] = str(value)
    return flattened


def main() -> int:
    if not os.environ.get("MX_APIKEY", "").strip():
        print("[ERROR] MX_APIKEY 未配置")
        return 1
    if not MX_DATA_SKILL.exists():
        print(f"[ERROR] mx-data skill 缺失: {MX_DATA_SKILL}")
        return 1

    scan_rows = _load_scan_rows()
    scan_meta = _read_json(SCAN_META_FILE)
    run_slot = _sanitize_slot(scan_meta.get("run_slot") or datetime.now().strftime("%Y-%m-%d_%H%M"))
    trade_date = run_slot.split("_", 1)[0]

    candidates = _rank_candidates(scan_rows)
    mx_data_mod = _load_module(MX_DATA_SKILL, "mx_data_runtime")
    client = mx_data_mod.MXData()

    raw_dir = DATA_DIR / "mx_enrich_candidates" / run_slot
    raw_dir.mkdir(parents=True, exist_ok=True)
    enriched_rows: list[dict] = []
    errors: list[dict] = []

    for row in candidates:
        code = str(row.get("code", "")).zfill(6)
        name = str(row.get("name", "")).strip()
        query = QUERY_TEMPLATE.format(name=name)
        base = {
            "code": code,
            "name": name,
            "tier": _inum(row.get("tier", 0), 0),
            "mode": str(row.get("mode", "")),
            "weekly_slope": row.get("weekly_slope", ""),
            "close_vs_ma20_pct": row.get("close_vs_ma20_pct", ""),
            "amt_ratio": row.get("amt_ratio", ""),
            "mx_query": query,
        }
        try:
            result = client.query(query)
            _write_json_atomic(raw_dir / f"{code}.json", result)
            tables, _, _, err = mx_data_mod.MXData.parse_result(result)
            if err:
                raise RuntimeError(err)
            flattened = _flatten_tables(tables)
            enriched_rows.append(
                {
                    **base,
                    "mx_date": flattened.get("date", ""),
                    "mx_last_price": _pick_metric(flattened, ("最新价", "收盘价")),
                    "mx_pe": _pick_metric(flattened, ("市盈率",)),
                    "mx_pb": _pick_metric(flattened, ("市净率",)),
                    "mx_roe": _pick_metric(flattened, ("净资产收益率", "ROE")),
                    "mx_status": "ok",
                    "mx_error": "",
                    "mx_fields": json.dumps(flattened, ensure_ascii=False),
                }
            )
        except Exception as exc:
            error_text = str(exc)
            errors.append({"code": code, "name": name, "query": query, "error": error_text})
            enriched_rows.append(
                {
                    **base,
                    "mx_date": "",
                    "mx_last_price": "",
                    "mx_pe": "",
                    "mx_pb": "",
                    "mx_roe": "",
                    "mx_status": "error",
                    "mx_error": error_text,
                    "mx_fields": "",
                }
            )

    snapshot_csv = DATA_DIR / f"mx_enrich_candidates.{run_slot}.csv"
    snapshot_json = DATA_DIR / f"mx_enrich_candidates.{run_slot}.json"
    _write_csv(OUTPUT_CSV, enriched_rows)
    _write_csv(snapshot_csv, enriched_rows)

    payload = {
        "generated_at": _now_str(),
        "trade_date": trade_date,
        "run_slot": run_slot,
        "source_scan_csv": str(SCAN_CSV),
        "candidate_count": len(candidates),
        "success_count": sum(1 for row in enriched_rows if row.get("mx_status") == "ok"),
        "error_count": len(errors),
        "records": enriched_rows,
        "errors": errors,
    }
    _write_json_atomic(OUTPUT_JSON, payload)
    _write_json_atomic(snapshot_json, payload)
    print(
        json.dumps(
            {
                "trade_date": trade_date,
                "run_slot": run_slot,
                "candidate_count": len(candidates),
                "success_count": payload["success_count"],
                "error_count": payload["error_count"],
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
