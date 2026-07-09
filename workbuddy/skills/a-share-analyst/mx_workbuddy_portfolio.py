#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 mx-xuangu challenger 预备军接入本地 shadow 组合 `workbuddy`。

设计原则：
1. 不碰现有主组合，不和 v10_track_record.csv 串账；
2. 以 mx-xuangu challenger 为核心来源，形成独立预备军组合；
3. 尽量复用现有 mx-data / mx-search 收盘产物，给 challenger 增加解释信息；
4. 可在收盘节点或 `13:35 workbuddy-refresh` 节点刷新；
5. 输出进入复核层观察入口，便于后续判断是否值得晋升到学习候选或形成当日 challenger 下单对象。
"""

from __future__ import annotations

import csv
import json
import math
import re
from datetime import datetime
from pathlib import Path

from package_paths import DATA_DIR


PORTFOLIO_NAME = "workbuddy"
PORTFOLIO_TYPE = "shadow_challenger"
MAX_POSITIONS = 5

CHALLENGER_FILE = DATA_DIR / "mx_challenger_pool_latest.json"
ENRICH_FILE = DATA_DIR / "mx_enrich_candidates_latest.json"
EVENT_FILE = DATA_DIR / "mx_event_review_latest.json"
CLOSE_NODE_FILE = DATA_DIR / "v10_close_node_latest.json"

OUTPUT_JSON = DATA_DIR / "mx_workbuddy_portfolio_latest.json"
OUTPUT_CSV = DATA_DIR / "mx_workbuddy_portfolio_latest.csv"
HISTORY_CSV = DATA_DIR / "mx_workbuddy_portfolio_history.csv"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            with path.open("r", encoding=encoding) as f:
                payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
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
        fieldnames = [
            "portfolio_name",
            "trade_date",
            "selection_rank",
            "code",
            "name",
            "selection_score",
            "role",
            "target_weight_pct",
        ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _append_history(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _sanitize_slot(text: str) -> str:
    value = str(text or "").strip()
    return value.replace(":", "").replace(" ", "_") or datetime.now().strftime("%Y-%m-%d_%H%M")


def _first_float(value, default: float = 0.0) -> float:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return default
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def _scaled_amount(value) -> float:
    text = str(value or "").replace(",", "").strip()
    base = _first_float(text, 0.0)
    if not text:
        return 0.0
    if "万亿" in text:
        return base * 1_0000_0000_0000
    if "亿" in text:
        return base * 100_000_000
    if "万" in text:
        return base * 10_000
    return base


def _pick_key(row: dict, keyword: str) -> str:
    for key in row.keys():
        if keyword in key:
            return key
    return ""


def _pick_keys(row: dict, keyword: str) -> list[str]:
    return [key for key in row.keys() if keyword in key]


def _build_enrich_map(payload: dict) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for item in payload.get("records", []):
        code = str(item.get("code", "")).zfill(6)
        if code:
            mapping[code] = dict(item)
    return mapping


def _build_event_map(payload: dict) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for item in payload.get("summary", []):
        code = str(item.get("code", "")).zfill(6)
        if not code:
            continue
        flags = [token.strip() for token in str(item.get("risk_flags", "")).split(",") if token.strip()]
        mapping[code] = {
            "risk_flags": flags,
            "top_title": str(item.get("top_title", "")).strip(),
            "source": str(item.get("source", "")).strip(),
        }
    return mapping


def _role_of(row: dict) -> str:
    in_scan = str(row.get("在当前扫描池", "")).strip() == "是"
    scanner_tier = int(_first_float(row.get("scanner_tier", ""), 0.0))
    if in_scan and scanner_tier > 0:
        return "double_confirm"
    if in_scan:
        return "scanner_weak_overlap"
    return "blind_spot"


def _score_candidate(
    *,
    role: str,
    roe: float,
    avg_growth_yoy: float,
    latest_chg_pct: float,
    turnover_pct: float,
    pe: float,
    pb: float,
    amount_yuan: float,
    scanner_tier: int,
    risk_flags: list[str],
    enrich_supported: bool,
) -> float:
    score = 50.0
    if role == "blind_spot":
        score += 12.0
    elif role == "double_confirm":
        score += 10.0
    else:
        score += 5.0

    score += min(max(roe - 15.0, 0.0), 18.0)
    score += min(max(avg_growth_yoy, 0.0) / 12.0, 18.0)
    score += min(max(latest_chg_pct, 0.0) * 1.2, 8.0)
    score += min(max(turnover_pct, 0.0), 12.0) / 3.0
    if amount_yuan > 0:
        score += min(math.log10(amount_yuan / 100_000_000 + 1.0) * 4.0, 6.0)

    if pe <= 0:
        score -= 6.0
    elif pe <= 25:
        score += 6.0
    elif pe <= 50:
        score += 3.0
    elif pe >= 120:
        score -= 6.0

    if 0 < pb <= 8:
        score += 3.0
    elif pb >= 20:
        score -= 6.0

    if scanner_tier > 0:
        score += min(scanner_tier * 4.0, 8.0)
    if enrich_supported:
        score += 3.0
    if "negative_event" in risk_flags:
        score -= 8.0
    if "event_conflict" in risk_flags:
        score -= 4.0
    elif "positive_event" in risk_flags:
        score += 2.0
    return round(score, 2)


def _selection_reasons(item: dict) -> list[str]:
    reasons = []
    role = item["role"]
    if role == "blind_spot":
        reasons.append("属于 scanner 未覆盖的 challenger 盲区补充")
    elif role == "double_confirm":
        reasons.append("属于 scanner 与 challenger 双确认")
    else:
        reasons.append("已进入 scanner 视野，但当前更适合作为预备军观察")

    if item["roe"] >= 18:
        reasons.append(f"ROE {item['roe']:.2f}% 较高")
    if item["avg_growth_yoy"] >= 30:
        reasons.append(f"近三期净利润同比均值 {item['avg_growth_yoy']:.2f}%")
    if item["latest_chg_pct"] > 0:
        reasons.append(f"最近交易日涨跌幅 {item['latest_chg_pct']:.2f}%")
    if item["risk_flags"]:
        reasons.append("已有事件标签，进入复核层持续跟踪")
    if item["enrich_supported"]:
        reasons.append("可与 mx-data 当前增强结果交叉印证")
    return reasons[:4]


def _merge_close_node_summary(payload: dict) -> None:
    close_payload = _read_json(CLOSE_NODE_FILE)
    if not close_payload:
        return
    summary = {
        "portfolio_name": payload.get("portfolio_name", PORTFOLIO_NAME),
        "portfolio_type": payload.get("portfolio_type", PORTFOLIO_TYPE),
        "status": payload.get("status", ""),
        "trade_date": payload.get("trade_date", ""),
        "selected_count": payload.get("selected_count", 0),
        "blind_spot_count": payload.get("blind_spot_count", 0),
        "double_confirm_count": payload.get("double_confirm_count", 0),
        "scanner_weak_overlap_count": payload.get("scanner_weak_overlap_count", 0),
        "learning_candidate_status": payload.get("learning_candidate_status", "observe"),
        "top_codes": [item.get("code", "") for item in payload.get("selected_records", [])],
        "detail_file": str(OUTPUT_JSON),
        "history_file": str(HISTORY_CSV),
        "note": "workbuddy challenger 组合属于本地 shadow 预备军，不直接参与当前主组合交易。",
    }
    close_payload["workbuddy_challenger"] = summary
    notes = close_payload.get("notes", [])
    if not isinstance(notes, list):
        notes = []
    note = "workbuddy challenger shadow 组合摘要已并入收盘节点总入口，用于复核层观察预备军质量。"
    if note not in notes:
        notes.append(note)
    close_payload["notes"] = notes
    _write_json_atomic(CLOSE_NODE_FILE, close_payload)


def main() -> int:
    challenger_payload = _read_json(CHALLENGER_FILE)
    if challenger_payload.get("status") != "ok":
        print("[ERROR] mx_challenger_pool_latest.json 不可用，无法生成 workbuddy 预备军组合")
        return 3

    records = challenger_payload.get("records", [])
    if not isinstance(records, list) or not records:
        print("[ERROR] challenger 记录为空，无法生成 workbuddy 预备军组合")
        return 3

    enrich_map = _build_enrich_map(_read_json(ENRICH_FILE))
    event_map = _build_event_map(_read_json(EVENT_FILE))
    sample = records[0]
    roe_key = _pick_key(sample, "净资产收益率ROE")
    pe_key = _pick_key(sample, "市盈率")
    pb_key = _pick_key(sample, "市净率")
    latest_chg_key = _pick_key(sample, "涨跌幅")
    turnover_key = _pick_key(sample, "换手率")
    amount_key = _pick_key(sample, "成交额")
    growth_keys = _pick_keys(sample, "归属母公司股东的净利润同比增长率")

    ranked: list[dict] = []
    for row in records:
        item = dict(row)
        code = str(item.get("股票代码") or item.get("代码") or "").zfill(6)
        name = str(item.get("股票简称") or item.get("名称") or "").strip()
        if not code or not name:
            continue
        role = _role_of(item)
        scanner_tier = int(_first_float(item.get("scanner_tier", ""), 0.0))
        risk_info = event_map.get(code, {})
        risk_flags = list(risk_info.get("risk_flags", []))

        growth_values = [_first_float(item.get(key, ""), 0.0) for key in growth_keys]
        growth_values = [value for value in growth_values if value != 0.0]
        avg_growth_yoy = round(sum(growth_values) / len(growth_values), 2) if growth_values else 0.0
        roe = _first_float(item.get(roe_key, ""), 0.0)
        pe = _first_float(item.get(pe_key, ""), 0.0)
        pb = _first_float(item.get(pb_key, ""), 0.0)
        latest_chg_pct = _first_float(item.get(latest_chg_key, ""), 0.0)
        turnover_pct = _first_float(item.get(turnover_key, ""), 0.0)
        amount_yuan = _scaled_amount(item.get(amount_key, ""))
        enrich_supported = code in enrich_map

        selection_score = _score_candidate(
            role=role,
            roe=roe,
            avg_growth_yoy=avg_growth_yoy,
            latest_chg_pct=latest_chg_pct,
            turnover_pct=turnover_pct,
            pe=pe,
            pb=pb,
            amount_yuan=amount_yuan,
            scanner_tier=scanner_tier,
            risk_flags=risk_flags,
            enrich_supported=enrich_supported,
        )
        ranked.append(
            {
                "code": code,
                "name": name,
                "role": role,
                "selection_score": selection_score,
                "in_scanner": str(item.get("在当前扫描池", "")).strip() == "是",
                "scanner_tier": scanner_tier,
                "scanner_mode": str(item.get("scanner_mode", "")).strip(),
                "scanner_weekly_slope": _first_float(item.get("scanner_weekly_slope", ""), 0.0),
                "roe": roe,
                "avg_growth_yoy": avg_growth_yoy,
                "latest_chg_pct": latest_chg_pct,
                "turnover_pct": turnover_pct,
                "pe": pe,
                "pb": pb,
                "amount_yuan": round(amount_yuan, 2),
                "risk_flags": risk_flags,
                "event_top_title": str(risk_info.get("top_title", "")).strip(),
                "event_source": str(risk_info.get("source", "")).strip(),
                "enrich_supported": enrich_supported,
                "challenger_query": challenger_payload.get("query_used", ""),
                "learning_candidate_status": "observe",
            }
        )

    if not ranked:
        print("[ERROR] challenger 记录缺少可用代码，无法生成 workbuddy 预备军组合")
        return 3

    ranked.sort(
        key=lambda item: (
            item["selection_score"],
            item["roe"],
            item["avg_growth_yoy"],
            item["latest_chg_pct"],
            item["code"],
        ),
        reverse=True,
    )

    selected = ranked[:MAX_POSITIONS]
    weight = round(100.0 / len(selected), 2) if selected else 0.0
    history_rows: list[dict] = []
    for idx, item in enumerate(selected, start=1):
        item["selection_rank"] = idx
        item["portfolio_name"] = PORTFOLIO_NAME
        item["portfolio_type"] = PORTFOLIO_TYPE
        item["target_weight_pct"] = weight
        item["selection_reasons"] = _selection_reasons(item)
        history_rows.append(
            {
                "generated_at": _now_str(),
                "trade_date": challenger_payload.get("trade_date", ""),
                "run_slot": challenger_payload.get("run_slot", ""),
                "portfolio_name": PORTFOLIO_NAME,
                "selection_rank": idx,
                "code": item["code"],
                "name": item["name"],
                "role": item["role"],
                "selection_score": item["selection_score"],
                "target_weight_pct": weight,
                "in_scanner": "是" if item["in_scanner"] else "否",
                "scanner_tier": item["scanner_tier"],
                "scanner_mode": item["scanner_mode"],
                "roe": item["roe"],
                "avg_growth_yoy": item["avg_growth_yoy"],
                "latest_chg_pct": item["latest_chg_pct"],
                "pe": item["pe"],
                "pb": item["pb"],
                "event_flags": ",".join(item["risk_flags"]),
                "learning_candidate_status": item["learning_candidate_status"],
            }
        )

    run_slot = _sanitize_slot(challenger_payload.get("run_slot", ""))
    blind_spot_count = len([item for item in selected if item["role"] == "blind_spot"])
    double_confirm_count = len([item for item in selected if item["role"] == "double_confirm"])
    weak_overlap_count = len([item for item in selected if item["role"] == "scanner_weak_overlap"])
    payload = {
        "generated_at": _now_str(),
        "trade_date": challenger_payload.get("trade_date", ""),
        "run_slot": run_slot,
        "portfolio_name": PORTFOLIO_NAME,
        "portfolio_type": PORTFOLIO_TYPE,
        "status": "ok",
        "query_used": challenger_payload.get("query_used", ""),
        "source_status": {
            "mx_challenger_pool": challenger_payload.get("status", "unknown"),
            "mx_enrich_candidates": "ok" if enrich_map else "missing_or_not_used",
            "mx_event_review": "ok" if event_map else "missing_or_not_used",
        },
        "selected_count": len(selected),
        "candidate_count": len(ranked),
        "blind_spot_count": blind_spot_count,
        "double_confirm_count": double_confirm_count,
        "scanner_weak_overlap_count": weak_overlap_count,
        "learning_candidate_status": "hold",
        "notes": [
            "workbuddy 组合是 challenger 预备军的本地 shadow 组合，不直接占用当前主组合仓位。",
            "mx-xuangu 负责提供预备军来源，mx-data 与 mx-search 在当前版本作为收盘交叉解释信号。",
            "只有经过复核层连续验证稳定有效的 challenger，才应考虑晋升到学习候选或主策略辅助因子。",
        ],
        "selected_records": selected,
    }

    _write_json_atomic(OUTPUT_JSON, payload)
    _write_json_atomic(DATA_DIR / f"mx_workbuddy_portfolio.{run_slot}.json", payload)
    _write_csv(OUTPUT_CSV, selected)
    _write_csv(DATA_DIR / f"mx_workbuddy_portfolio.{run_slot}.csv", selected)
    _append_history(HISTORY_CSV, history_rows)
    _merge_close_node_summary(payload)

    print(
        json.dumps(
            {
                "portfolio_name": PORTFOLIO_NAME,
                "trade_date": payload["trade_date"],
                "run_slot": run_slot,
                "selected_count": len(selected),
                "blind_spot_count": blind_spot_count,
                "double_confirm_count": double_confirm_count,
                "output_json": str(OUTPUT_JSON),
                "output_csv": str(OUTPUT_CSV),
                "history_csv": str(HISTORY_CSV),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
