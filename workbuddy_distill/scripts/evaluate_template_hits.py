from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DISTILL_ROOT = ROOT / "workbuddy_distill"
RAW_TOP100_ROOT = DISTILL_ROOT / "raw_top100"
EVALUATIONS_ROOT = DISTILL_ROOT / "evaluations"

DEFAULT_THRESHOLDS = {
    "prototype": {
        "top100_hit_rate_min": 0.10,
        "top30_hit_rate_min": 0.03,
    },
    "pass": {
        "top100_hit_rate_min": 0.15,
        "top30_hit_rate_min": 0.05,
    },
    "priority": {
        "top100_hit_rate_min": 0.18,
        "top30_hit_rate_min": 0.06,
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate a T-1 template candidate pool against T0 TDX top rankings.")
    parser.add_argument("--template-name", required=True, help="Human-readable template name.")
    parser.add_argument("--candidate-file", required=True, help="CSV or JSON file that contains the candidate pool.")
    parser.add_argument("--t0-date", required=True, help="T0 trade date in YYYY-MM-DD format.")
    parser.add_argument(
        "--candidate-size",
        type=int,
        default=20,
        help="Evaluation denominator. If the file has fewer names, it will use actual row count instead.",
    )
    return parser.parse_args()


def normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    return digits.zfill(6) if digits else ""


def extract_candidate_rows(file_path: Path) -> list[dict[str, Any]]:
    if file_path.suffix.lower() == ".json":
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            for key in (
                "candidates",
                "items",
                "rows",
                "data",
                "user_focus_pool",
                "selected_records",
                "portfolio",
            ):
                value = payload.get(key)
                if isinstance(value, list):
                    payload = value
                    break
        if not isinstance(payload, list):
            raise ValueError("JSON candidate file must be a list or contain a list-like field.")
        rows = []
        for item in payload:
            if isinstance(item, dict):
                rows.append(item)
        return rows

    if file_path.suffix.lower() != ".csv":
        raise ValueError("Only CSV or JSON candidate files are supported.")

    with file_path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def extract_codes(rows: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for row in rows:
        code = ""
        for key in ("code", "代码", "证券代码", "股票代码", "SECURITY_CODE"):
            if key in row:
                code = normalize_code(row.get(key))
                if code:
                    break
        if not code:
            continue
        if code in seen:
            continue
        seen.add(code)
        candidates.append(code)
    return candidates


def load_top_rows(t0_date: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    date_dir = RAW_TOP100_ROOT / t0_date
    top100_file = date_dir / "top100.csv"
    summary_file = date_dir / "summary.json"
    if not top100_file.exists():
        raise FileNotFoundError(f"Top100 file not found: {top100_file}")
    if not summary_file.exists():
        raise FileNotFoundError(f"Summary file not found: {summary_file}")

    with top100_file.open("r", encoding="utf-8-sig", newline="") as fh:
        top_rows = list(csv.DictReader(fh))
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    return top_rows, summary


def compute_verdict(top100_hit_rate: float, top30_hit_rate: float) -> tuple[str, str]:
    if (
        top100_hit_rate >= DEFAULT_THRESHOLDS["priority"]["top100_hit_rate_min"]
        and top30_hit_rate >= DEFAULT_THRESHOLDS["priority"]["top30_hit_rate_min"]
    ):
        return "priority", "promote"
    if (
        top100_hit_rate >= DEFAULT_THRESHOLDS["pass"]["top100_hit_rate_min"]
        and top30_hit_rate >= DEFAULT_THRESHOLDS["pass"]["top30_hit_rate_min"]
    ):
        return "pass", "promote"
    if (
        top100_hit_rate >= DEFAULT_THRESHOLDS["prototype"]["top100_hit_rate_min"]
        and top30_hit_rate >= DEFAULT_THRESHOLDS["prototype"]["top30_hit_rate_min"]
    ):
        return "prototype", "observe"
    return "fail", "downgrade"


def main() -> int:
    args = parse_args()
    candidate_file = Path(args.candidate_file)
    rows = extract_candidate_rows(candidate_file)
    candidate_codes = extract_codes(rows)
    if not candidate_codes:
        raise RuntimeError("No candidate codes found in candidate file.")

    evaluation_size = min(args.candidate_size, len(candidate_codes))
    evaluated_codes = candidate_codes[:evaluation_size]

    top_rows, summary = load_top_rows(args.t0_date)
    top100_codes = [normalize_code(row.get("code")) for row in top_rows[:100]]
    top30_codes = [normalize_code(row.get("code")) for row in top_rows[:30]]
    top10_codes = [normalize_code(row.get("code")) for row in top_rows[:10]]

    top100_hits = [code for code in evaluated_codes if code in top100_codes]
    top30_hits = [code for code in evaluated_codes if code in top30_codes]
    top10_hits = [code for code in evaluated_codes if code in top10_codes]

    top100_hit_rate = len(top100_hits) / evaluation_size
    top30_hit_rate = len(top30_hits) / evaluation_size
    top10_hit_rate = len(top10_hits) / evaluation_size

    universe_count = int(summary.get("universe_count") or 0)
    random_top100_rate = (100 / universe_count) if universe_count else math.nan
    random_top30_rate = (30 / universe_count) if universe_count else math.nan

    verdict, recommended_action = compute_verdict(top100_hit_rate, top30_hit_rate)

    payload = {
        "template_name": args.template_name,
        "candidate_file": str(candidate_file),
        "t0_date": args.t0_date,
        "candidate_size": evaluation_size,
        "evaluated_codes": evaluated_codes,
        "metrics": {
            "top100_hits": top100_hits,
            "top30_hits": top30_hits,
            "top10_hits": top10_hits,
            "top100_hit_count": len(top100_hits),
            "top30_hit_count": len(top30_hits),
            "top10_hit_count": len(top10_hits),
            "top100_hit_rate": round(top100_hit_rate, 4),
            "top30_hit_rate": round(top30_hit_rate, 4),
            "top10_hit_rate": round(top10_hit_rate, 4),
            "random_top100_rate": round(random_top100_rate, 4) if not math.isnan(random_top100_rate) else None,
            "random_top30_rate": round(random_top30_rate, 4) if not math.isnan(random_top30_rate) else None,
            "top100_lift": round(top100_hit_rate / random_top100_rate, 4) if random_top100_rate and not math.isnan(random_top100_rate) else None,
            "top30_lift": round(top30_hit_rate / random_top30_rate, 4) if random_top30_rate and not math.isnan(random_top30_rate) else None,
        },
        "thresholds": DEFAULT_THRESHOLDS,
        "verdict": verdict,
        "recommended_action": recommended_action,
    }

    EVALUATIONS_ROOT.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^0-9A-Za-z_\-一-龥]+", "_", args.template_name).strip("_") or "template"
    output_path = EVALUATIONS_ROOT / f"{safe_name}_{args.t0_date}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
