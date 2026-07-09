from __future__ import annotations

import csv
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
VALIDATION_ROOT = ROOT / "mx_100_validation"
HISTORY_FILE = VALIDATION_ROOT / "mx_100_validation_history.jsonl"
OUTPUT_ROOT = ROOT / "workbuddy_pool"

BUCKET_WEIGHTS = {
    "01_core_templates": 5.0,
    "02_expansion_templates": 3.0,
    "03_alternative_templates": 2.0,
    "04_historical_backcheck": 4.0,
}

PRIMARY_THEMES = (
    "AI算力",
    "ai算力",
    "数据中心",
    "光通信",
    "CPO",
    "PCB",
    "MLCC",
    "存储芯片",
    "存储模组",
    "液冷",
    "服务器PCB",
)

CHAIN_KEYWORDS = (
    "光通信",
    "CPO",
    "PCB",
    "服务器PCB",
    "MLCC",
    "被动元件",
    "存储芯片",
    "存储模组",
    "光芯片",
    "数据中心",
    "液冷",
    "半导体",
)

QUALITY_INDUSTRIES = (
    "半导体",
    "印制电路板",
    "被动元件",
    "消费电子",
    "光学光电子",
    "其他电子",
)

USER_EXCLUDE_INDUSTRY_PREFIXES = (
    "有色金属",
    "建筑材料",
)


def _load_latest_history() -> dict[int, dict[str, Any]]:
    latest: dict[int, dict[str, Any]] = {}
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        latest[int(item["index"])] = item
    return latest


def _extract_csv_path(stdout: str) -> Path | None:
    match = re.search(r"CSV:\s*(.+?\.csv)", stdout)
    if not match:
        return None
    return Path(match.group(1).strip())


def _parse_float(text: str) -> float | None:
    if text is None:
        return None
    value = str(text).strip()
    if not value:
        return None
    first = value.split("|", 1)[0].replace("%", "").replace("倍", "").replace("元", "").strip()
    try:
        return float(first)
    except ValueError:
        return None


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _score_row(bucket: str, row: dict[str, str], query: str) -> tuple[float, list[str]]:
    score = BUCKET_WEIGHTS[bucket]
    reasons = [f"bucket:{bucket}"]

    row_text = " ".join(str(v) for v in row.values())
    if _contains_any(row_text + " " + query, PRIMARY_THEMES):
        score += 1.5
        reasons.append("primary_theme")

    roe = _parse_float(row.get("净资产收益率ROE(加权)(%) 截至2026.06.20最新"))
    if roe is None:
        roe = _parse_float(row.get("净资产收益率ROE(加权)(%) 2026.03.31"))
    if roe is not None and roe >= 10:
        score += 1.0
        reasons.append("roe>=10")

    pe = _parse_float(row.get("市盈率(TTM)(倍) 2026.06.18"))
    if pe is not None:
        if 0 < pe <= 80:
            score += 1.0
            reasons.append("0<pe<=80")
        elif pe <= 0:
            score -= 2.0
            reasons.append("pe<=0")
        elif pe > 120:
            score -= 1.0
            reasons.append("pe>120")

    strong_flag = row.get("阶段强势股 2026.06.18", "")
    if strong_flag == "符合":
        score += 1.0
        reasons.append("stage_strong")

    industry = row.get("东财行业总分类", "")
    if _contains_any(industry, QUALITY_INDUSTRIES):
        score += 0.5
        reasons.append("quality_industry")

    return score, reasons


def _compute_chain_fit(record: dict[str, Any]) -> int:
    row_text = " ".join(
        [
            str(record.get("industry", "")),
            str(record.get("themes", "")),
            str(record.get("topics", "")),
            " ".join(record.get("queries", [])),
        ]
    )
    return sum(1 for keyword in CHAIN_KEYWORDS if keyword in row_text)


def _classify_record(item: dict[str, Any]) -> str:
    pe = item.get("pe_ttm")
    if pe is not None and pe <= 0:
        return "observe_only"

    chain_fit = item.get("chain_fit", 0)
    quality_positive = (
        (pe is not None and 0 < pe <= 120)
        or (item.get("roe") is not None and item["roe"] >= 10)
    )

    if chain_fit >= 3 and item["historical_hits"] >= 3 and item["core_hits"] >= 1:
        return "primary"
    if chain_fit >= 3 and item["core_hits"] >= 1 and quality_positive:
        return "primary"
    if chain_fit >= 2 and item["historical_hits"] >= 2 and quality_positive:
        return "primary"
    if chain_fit >= 2 and (item["expansion_hits"] >= 1 or item["alternative_hits"] >= 1):
        return "rotation"
    if chain_fit >= 1 and item["core_hits"] >= 1:
        return "rotation"
    if item["expansion_hits"] >= 1 or item["alternative_hits"] >= 1 or item["historical_hits"] >= 1:
        return "rotation"
    return "observe_only"


def _is_user_focus_candidate(item: dict[str, Any]) -> bool:
    pe = item.get("pe_ttm")
    if pe is not None and pe <= 0:
        return False

    industry = str(item.get("industry", ""))
    if industry.startswith(USER_EXCLUDE_INDUSTRY_PREFIXES):
        return False
    if item.get("classification") == "observe_only":
        return False

    quality_industry = _contains_any(industry, QUALITY_INDUSTRIES)
    return quality_industry or item.get("chain_fit", 0) >= 5


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    latest = _load_latest_history()

    items: dict[str, dict[str, Any]] = {}
    failed_templates: list[dict[str, Any]] = []
    template_registry: list[dict[str, Any]] = []

    for idx, item in sorted(latest.items()):
        bucket = item.get("bucket", "")
        exit_code = int(item.get("exit_code", 1))
        if bucket not in BUCKET_WEIGHTS or item.get("skill") != "mx-xuangu":
            continue

        if exit_code != 0:
            failed_templates.append(
                {
                    "index": idx,
                    "tag": item.get("tag"),
                    "bucket": bucket,
                    "query": item.get("query"),
                    "exit_code": exit_code,
                }
            )
            continue

        csv_path = _extract_csv_path(item.get("stdout", ""))
        if csv_path is None or not csv_path.exists():
            failed_templates.append(
                {
                    "index": idx,
                    "tag": item.get("tag"),
                    "bucket": bucket,
                    "query": item.get("query"),
                    "exit_code": exit_code,
                    "error": "csv_not_found_in_stdout",
                }
            )
            continue

        template_registry.append(
            {
                "index": idx,
                "tag": item.get("tag"),
                "bucket": bucket,
                "query": item.get("query"),
                "csv_path": str(csv_path),
            }
        )

        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                code = (row.get("代码") or "").strip()
                name = (row.get("名称") or "").strip()
                if not code or not name:
                    continue

                key = f"{code}:{name}"
                record = items.setdefault(
                    key,
                    {
                        "code": code,
                        "name": name,
                        "score": 0.0,
                        "appearances": 0,
                        "core_hits": 0,
                        "expansion_hits": 0,
                        "alternative_hits": 0,
                        "historical_hits": 0,
                        "buckets": set(),
                        "queries": [],
                        "reasons": [],
                        "market": row.get("市场代码简称", ""),
                        "industry": row.get("东财行业总分类", ""),
                        "price": _parse_float(row.get("最新价(元) 2026.06.18")),
                        "change_pct": _parse_float(row.get("涨跌幅(%) 2026.06.18")),
                        "roe": _parse_float(row.get("净资产收益率ROE(加权)(%) 截至2026.06.20最新"))
                        or _parse_float(row.get("净资产收益率ROE(加权)(%) 2026.03.31")),
                        "pe_ttm": _parse_float(row.get("市盈率(TTM)(倍) 2026.06.18")),
                        "themes": row.get("概念", ""),
                        "topics": row.get("个股题材", ""),
                    },
                )

                row_score, row_reasons = _score_row(bucket, row, str(item.get("query", "")))
                record["score"] += row_score
                record["appearances"] += 1
                record["buckets"].add(bucket)
                record["queries"].append(str(item.get("query", "")))
                record["reasons"].extend(row_reasons)

                if bucket == "01_core_templates":
                    record["core_hits"] += 1
                elif bucket == "02_expansion_templates":
                    record["expansion_hits"] += 1
                elif bucket == "03_alternative_templates":
                    record["alternative_hits"] += 1
                elif bucket == "04_historical_backcheck":
                    record["historical_hits"] += 1

    ranked = []
    for record in items.values():
        record["buckets"] = sorted(record["buckets"])
        record["queries"] = record["queries"][:5]
        record["reasons"] = sorted(set(record["reasons"]))
        record["chain_fit"] = _compute_chain_fit(record)
        record["classification"] = _classify_record(record)
        ranked.append(record)

    ranked.sort(
        key=lambda item: (
            item["classification"] != "primary",
            -item["score"],
            -item["chain_fit"],
            -item["historical_hits"],
            -item["core_hits"],
            -item["appearances"],
            item["code"],
        )
    )

    primary_pool = [item for item in ranked if item["classification"] == "primary"][:10]
    rotation_pool = [item for item in ranked if item["classification"] == "rotation"][:8]
    observe_only = [item for item in ranked if item["classification"] == "observe_only"][:8]
    user_focus_pool = [item for item in ranked if _is_user_focus_candidate(item)][:8]

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scoring_version": "workbuddy_pool_v1",
        "source_validation": {
            "latest_index_count": 100,
            "successful_latest_indexes": 85,
            "failed_latest_indexes": 15,
            "successful_template_count": len(template_registry),
            "failed_template_count": len(failed_templates),
        },
        "user_focus_pool": user_focus_pool,
        "primary_pool": primary_pool,
        "rotation_pool": rotation_pool,
        "observe_only": observe_only,
        "failed_templates": failed_templates,
        "template_registry": template_registry,
    }

    (OUTPUT_ROOT / "workbuddy_candidate_pool_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        f"# WorkBuddy Candidate Pool",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- scoring_version: {payload['scoring_version']}",
        f"- latest_indexes: 100",
        f"- successful_latest_indexes: 85",
        f"- failed_latest_indexes: 15",
        "",
        "## User Focus Pool",
        "",
    ]
    for item in user_focus_pool:
        lines.append(
            f"- {item['code']} {item['name']} | score={item['score']:.1f} | "
            f"hits={item['appearances']} | core={item['core_hits']} | history={item['historical_hits']} | "
            f"PE={item['pe_ttm']} | ROE={item['roe']} | industry={item['industry']}"
        )

    lines.extend(
        [
            "",
            "## Primary Pool",
            "",
        ]
    )
    for item in primary_pool:
        lines.append(
            f"- {item['code']} {item['name']} | score={item['score']:.1f} | "
            f"hits={item['appearances']} | core={item['core_hits']} | history={item['historical_hits']} | "
            f"PE={item['pe_ttm']} | ROE={item['roe']} | industry={item['industry']}"
        )

    lines.extend(["", "## Rotation Pool", ""])
    for item in rotation_pool:
        lines.append(
            f"- {item['code']} {item['name']} | score={item['score']:.1f} | "
            f"hits={item['appearances']} | expansion={item['expansion_hits']} | alternative={item['alternative_hits']} | "
            f"PE={item['pe_ttm']} | ROE={item['roe']}"
        )

    lines.extend(["", "## Observe Only", ""])
    for item in observe_only:
        lines.append(
            f"- {item['code']} {item['name']} | score={item['score']:.1f} | "
            f"hits={item['appearances']} | PE={item['pe_ttm']} | reason={','.join(item['reasons'])}"
        )

    (OUTPUT_ROOT / "workbuddy_candidate_pool_latest.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
