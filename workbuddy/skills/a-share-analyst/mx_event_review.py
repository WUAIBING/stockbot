#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 mx-search 生成收盘事件复核摘要。

最小版原则：
1. 只围绕当前持仓 + 当日重点信号做事件复核；
2. 只输出风险标记与资讯摘要；
3. 不直接干预买卖与学习层。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path

from package_paths import DATA_DIR


TRACK_FILE = DATA_DIR / "v10_track_record.csv"
SCAN_CSV = DATA_DIR / "v10_scan_full.csv"
SCAN_META_FILE = DATA_DIR / "v10_scan_meta.json"
OUTPUT_JSON = DATA_DIR / "mx_event_review_latest.json"
OUTPUT_CSV = DATA_DIR / "mx_event_review_latest.csv"
MX_SEARCH_SKILL = Path.home() / ".trae" / "skills" / "mx-search" / "mx_search.py"
MAX_FOCUS = 8
MAX_ITEMS_PER_SECURITY = 5
NEGATIVE_KEYWORDS = (
    "减持",
    "问询",
    "处罚",
    "诉讼",
    "风险",
    "冻结",
    "违约",
    "调查",
    "下修",
    "亏损",
    "终止",
    "退市",
)
POSITIVE_KEYWORDS = (
    "回购",
    "中标",
    "增持",
    "分红",
    "预增",
    "上调",
    "突破",
    "签约",
    "增长",
)


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
        fieldnames = ["code", "name", "source", "risk_flags", "top_title"]
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


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _sanitize_slot(text: str) -> str:
    value = str(text or "").strip()
    return value.replace(":", "").replace(" ", "_") or datetime.now().strftime("%Y-%m-%d_%H%M")


def _build_focus_list() -> list[dict]:
    rows = []
    seen_codes: set[str] = set()

    for item in _load_csv(TRACK_FILE):
        if str(item.get("status", "")).strip() != "holding":
            continue
        code = str(item.get("code", "")).zfill(6)
        if code in seen_codes:
            continue
        rows.append({"code": code, "name": str(item.get("name", "")), "source": "holding"})
        seen_codes.add(code)

    scan_rows = [row for row in _load_csv(SCAN_CSV) if _inum(row.get("tier", 0), 0) > 0]
    scan_rows.sort(
        key=lambda item: (
            _inum(item.get("tier", 0), 0),
            -_fnum(item.get("weekly_slope", 0.0), 0.0),
            item.get("code", ""),
        )
    )
    for item in scan_rows:
        code = str(item.get("code", "")).zfill(6)
        if code in seen_codes:
            continue
        rows.append({"code": code, "name": str(item.get("name", "")), "source": "signal"})
        seen_codes.add(code)
        if len(rows) >= MAX_FOCUS:
            break
    return rows[:MAX_FOCUS]


def _extract_items(result: dict) -> list[dict]:
    return (
        result.get("data", {})
        .get("data", {})
        .get("llmSearchResponse", {})
        .get("data", [])
        or []
    )


def _derive_risk_flags(items: list[dict]) -> list[str]:
    negative_hits = 0
    positive_hits = 0
    for item in items:
        blob = " ".join(
            [
                str(item.get("title", "")),
                str(item.get("content", "")),
                str(item.get("informationType", "")),
            ]
        )
        if any(keyword in blob for keyword in NEGATIVE_KEYWORDS):
            negative_hits += 1
        if any(keyword in blob for keyword in POSITIVE_KEYWORDS):
            positive_hits += 1

    flags: list[str] = []
    if negative_hits > 0:
        flags.append("negative_event")
    if positive_hits > 0:
        flags.append("positive_event")
    if negative_hits >= 2:
        flags.append("event_conflict")
    return flags


def main() -> int:
    if not os.environ.get("MX_APIKEY", "").strip():
        print("[ERROR] MX_APIKEY 未配置")
        return 1
    if not MX_SEARCH_SKILL.exists():
        print(f"[ERROR] mx-search skill 缺失: {MX_SEARCH_SKILL}")
        return 1

    scan_meta = _read_json(SCAN_META_FILE)
    run_slot = _sanitize_slot(scan_meta.get("run_slot") or datetime.now().strftime("%Y-%m-%d_%H%M"))
    trade_date = run_slot.split("_", 1)[0]
    raw_dir = DATA_DIR / "mx_event_review" / run_slot
    raw_dir.mkdir(parents=True, exist_ok=True)

    mx_search_mod = _load_module(MX_SEARCH_SKILL, "mx_search_runtime")
    client = mx_search_mod.MXSearch()

    focus_list = _build_focus_list()
    summary_rows: list[dict] = []
    reviewed_items: list[dict] = []
    errors: list[dict] = []

    for focus in focus_list:
        code = str(focus.get("code", "")).zfill(6)
        name = str(focus.get("name", "")).strip()
        query = f"{name} 最新公告"
        try:
            result = client.search(query)
            _write_json_atomic(raw_dir / f"{code}.json", result)
            items = _extract_items(result)[:MAX_ITEMS_PER_SECURITY]
            flags = _derive_risk_flags(items)
            top_title = str(items[0].get("title", "")) if items else ""
            reviewed_items.append(
                {
                    "code": code,
                    "name": name,
                    "source": focus.get("source", ""),
                    "query": query,
                    "risk_flags": flags,
                    "items": [
                        {
                            "title": item.get("title", ""),
                            "date": item.get("date", ""),
                            "type": item.get("informationType", ""),
                            "institution": item.get("insName", ""),
                        }
                        for item in items
                    ],
                }
            )
            summary_rows.append(
                {
                    "code": code,
                    "name": name,
                    "source": focus.get("source", ""),
                    "query": query,
                    "result_count": len(items),
                    "risk_flags": ",".join(flags),
                    "top_title": top_title,
                }
            )
        except Exception as exc:
            error_text = str(exc)
            errors.append({"code": code, "name": name, "query": query, "error": error_text})
            summary_rows.append(
                {
                    "code": code,
                    "name": name,
                    "source": focus.get("source", ""),
                    "query": query,
                    "result_count": 0,
                    "risk_flags": "query_error",
                    "top_title": "",
                    "error": error_text,
                }
            )

    snapshot_json = DATA_DIR / f"mx_event_review.{run_slot}.json"
    snapshot_csv = DATA_DIR / f"mx_event_review.{run_slot}.csv"
    _write_csv(OUTPUT_CSV, summary_rows)
    _write_csv(snapshot_csv, summary_rows)
    payload = {
        "generated_at": _now_str(),
        "trade_date": trade_date,
        "run_slot": run_slot,
        "focus_count": len(focus_list),
        "reviewed_count": len(reviewed_items),
        "error_count": len(errors),
        "summary": summary_rows,
        "details": reviewed_items,
        "errors": errors,
    }
    _write_json_atomic(OUTPUT_JSON, payload)
    _write_json_atomic(snapshot_json, payload)
    print(
        json.dumps(
            {
                "trade_date": trade_date,
                "run_slot": run_slot,
                "focus_count": len(focus_list),
                "reviewed_count": len(reviewed_items),
                "error_count": len(errors),
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
