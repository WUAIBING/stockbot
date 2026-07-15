from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from package_paths import DATA_DIR, PACKAGE_ROOT


def _path_from_env(env_name: str, default: Path) -> Path:
    raw = os.environ.get(env_name, "").strip()
    return Path(raw) if raw else default


WORKBUDDY_ROOT = _path_from_env("TLFZ_WORKBUDDY_ROOT", PACKAGE_ROOT)
WORKBUDDY_SKILL_ROOT = _path_from_env(
    "TLFZ_WORKBUDDY_SKILL_ROOT",
    WORKBUDDY_ROOT / "skills" / "a-share-analyst",
)
REPO_ROOT = WORKBUDDY_ROOT.parent
ARKCLAW_ROOT = _path_from_env(
    "TLFZ_ARKCLAW_ROOT",
    REPO_ROOT,
)
WORKBUDDY_POOL_DIR = _path_from_env(
    "TLFZ_WORKBUDDY_POOL_DIR",
    ARKCLAW_ROOT / "workbuddy_pool",
)
WORKBUDDY_CANDIDATE_POOL_FILE = _path_from_env(
    "TLFZ_WORKBUDDY_CANDIDATE_POOL_FILE",
    WORKBUDDY_POOL_DIR / "workbuddy_candidate_pool_latest.json",
)
REFRESH_DISTILL_PIPELINE_SCRIPT = _path_from_env(
    "TLFZ_REFRESH_DISTILL_PIPELINE_SCRIPT",
    ARKCLAW_ROOT / "refresh_distill_pipeline.py",
)
BUILD_WORKBUDDY_DISTILL_DAILY_REVIEW_SCRIPT = _path_from_env(
    "TLFZ_BUILD_WORKBUDDY_DISTILL_DAILY_REVIEW_SCRIPT",
    ARKCLAW_ROOT / "build_workbuddy_distill_daily_review.py",
)
WORKBUDDY_DISTILL_DAILY_REVIEW_FILE = _path_from_env(
    "TLFZ_WORKBUDDY_DISTILL_DAILY_REVIEW_FILE",
    WORKBUDDY_POOL_DIR / "workbuddy_distill_daily_review_latest.json",
)
OPENING_TRADABILITY_FILE = DATA_DIR / "opening_tradability_latest.json"
WORKBUDDY_LOCAL_TRACK_RECORD_FILE = DATA_DIR / "workbuddy_local_track_record.csv"
WORKBUDDY_LOCAL_EXECUTION_STATE_FILE = DATA_DIR / "workbuddy_local_execution_state_latest.json"


class RuntimeValidationError(RuntimeError):
    """Raised when runtime paths or artifacts are inconsistent."""


@dataclass(slots=True)
class ValidationReport:
    name: str
    path: Path
    details: dict[str, Any]


def _read_json(path: Path) -> dict[str, Any]:
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


def _normalize_trade_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:10]


def _require_path(path: Path, *, label: str, kind: str = "file") -> None:
    if kind == "dir":
        if not path.exists() or not path.is_dir():
            raise RuntimeValidationError(f"{label} 不存在或不是目录: {path}")
        return
    if not path.exists() or not path.is_file():
        raise RuntimeValidationError(f"{label} 不存在或不是文件: {path}")


def _ensure_required_fields(payload: dict[str, Any], required_fields: list[str], *, artifact_name: str) -> None:
    missing = [field for field in required_fields if field not in payload]
    if missing:
        raise RuntimeValidationError(f"{artifact_name} 缺少关键字段: {', '.join(missing)}")


def validate_candidate_pool_artifact(
    *,
    path: Path | None = None,
    expected_trade_date: str = "",
) -> ValidationReport:
    target_path = path or WORKBUDDY_CANDIDATE_POOL_FILE
    _require_path(target_path, label="workbuddy 候选池文件")
    payload = _read_json(target_path)
    if not payload:
        raise RuntimeValidationError(f"workbuddy 候选池为空壳或无法解析: {target_path}")
    _ensure_required_fields(
        payload,
        ["generated_at", "trade_date", "status", "selected_count", "candidate_count", "selected_records"],
        artifact_name="workbuddy_candidate_pool_latest.json",
    )
    trade_date = _normalize_trade_date(payload.get("trade_date"))
    if expected_trade_date and trade_date < _normalize_trade_date(expected_trade_date):
        raise RuntimeValidationError(
            "workbuddy 候选池交易日过旧: "
            f"expected>={_normalize_trade_date(expected_trade_date)}, actual={trade_date}"
        )
    if str(payload.get("status", "")).strip() != "ok":
        raise RuntimeValidationError(f"workbuddy 候选池状态异常: {payload.get('status')!r}")
    selected_records = payload.get("selected_records", [])
    if not isinstance(selected_records, list):
        raise RuntimeValidationError("workbuddy 候选池 selected_records 不是 list")
    selected_count = int(payload.get("selected_count", 0) or 0)
    candidate_count = int(payload.get("candidate_count", 0) or 0)
    if selected_count != len(selected_records):
        raise RuntimeValidationError(
            "workbuddy 候选池 selected_count 与 selected_records 数量不一致: "
            f"{selected_count} != {len(selected_records)}"
        )
    if candidate_count < selected_count:
        raise RuntimeValidationError(
            "workbuddy 候选池 candidate_count 小于 selected_count: "
            f"{candidate_count} < {selected_count}"
        )

    # Per-entry schema validation (fail-fast on bad data at pool->trader boundary)
    from .pipeline_schema import validate_candidate_pool as _validate_pool_schema
    _validate_pool_schema(payload, path_hint=str(target_path))

    return ValidationReport(
        name="workbuddy_candidate_pool_latest.json",
        path=target_path,
        details={
            "trade_date": trade_date,
            "selected_count": selected_count,
            "candidate_count": candidate_count,
        },
    )


def validate_opening_tradability_artifact(
    *,
    path: Path | None = None,
    expected_trade_date: str = "",
) -> ValidationReport:
    target_path = path or OPENING_TRADABILITY_FILE
    _require_path(target_path, label="opening_tradability 文件")
    payload = _read_json(target_path)
    if not payload:
        raise RuntimeValidationError(f"opening_tradability 为空壳或无法解析: {target_path}")
    _ensure_required_fields(
        payload,
        ["generated_at", "trade_date", "status", "record_count", "records"],
        artifact_name="opening_tradability_latest.json",
    )
    trade_date = _normalize_trade_date(payload.get("trade_date"))
    if expected_trade_date and trade_date != _normalize_trade_date(expected_trade_date):
        raise RuntimeValidationError(
            "opening_tradability 交易日不匹配: "
            f"expected={_normalize_trade_date(expected_trade_date)}, actual={trade_date}"
        )
    if str(payload.get("status", "")).strip() != "ok":
        raise RuntimeValidationError(f"opening_tradability 状态异常: {payload.get('status')!r}")
    records = payload.get("records", [])
    if not isinstance(records, list):
        raise RuntimeValidationError("opening_tradability records 不是 list")
    record_count = int(payload.get("record_count", 0) or 0)
    if record_count != len(records):
        raise RuntimeValidationError(
            "opening_tradability record_count 与 records 数量不一致: "
            f"{record_count} != {len(records)}"
        )
    return ValidationReport(
        name="opening_tradability_latest.json",
        path=target_path,
        details={
            "trade_date": trade_date,
            "record_count": record_count,
            "excluded_today_count": int(payload.get("excluded_today_count", 0) or 0),
        },
    )


def preflight_phase(phase: str, *, expected_trade_date: str = "") -> list[ValidationReport]:
    reports: list[ValidationReport] = []
    _require_path(WORKBUDDY_ROOT, label="workbuddy 根目录", kind="dir")
    _require_path(WORKBUDDY_SKILL_ROOT, label="workbuddy 技能目录", kind="dir")
    _require_path(DATA_DIR, label="workbuddy DATA_DIR", kind="dir")
    if phase in {"workbuddy-refresh", "workbuddy-buy"}:
        _require_path(ARKCLAW_ROOT, label="arkclaw 根目录", kind="dir")
        _require_path(REFRESH_DISTILL_PIPELINE_SCRIPT, label="refresh_distill_pipeline.py")
    if phase == "close-node":
        _require_path(ARKCLAW_ROOT, label="arkclaw 根目录", kind="dir")
        _require_path(BUILD_WORKBUDDY_DISTILL_DAILY_REVIEW_SCRIPT, label="build_workbuddy_distill_daily_review.py")
    if phase in {"workbuddy-buy", "workbuddy-smart-sell", "workbuddy-sell"}:
        reports.append(validate_opening_tradability_artifact(expected_trade_date=expected_trade_date))
    return reports


def validate_challenger_execution_consistency(
    records: list[dict[str, Any]],
    execution_state: dict[str, Any],
) -> dict[str, Any]:
    positions = execution_state.get("positions", {}) if isinstance(execution_state, dict) else {}
    if not isinstance(positions, dict):
        positions = {}
    holding_map: dict[str, dict[str, Any]] = {}
    for row in records:
        code = str(row.get("code", "")).strip().split(".", 1)[0].zfill(6)
        if not code or str(row.get("status", "")).strip() != "holding":
            continue
        holding_map[code] = row
    state_codes = {
        str(code).strip().split(".", 1)[0].zfill(6)
        for code, value in positions.items()
        if isinstance(value, dict) and str(code).strip()
    }
    holding_codes = set(holding_map.keys())
    missing_in_state = sorted(code for code in holding_codes if code not in state_codes)
    stale_in_state = sorted(code for code in state_codes if code not in holding_codes)
    quantity_mismatches: list[dict[str, Any]] = []
    for code in sorted(holding_codes & state_codes):
        state_entry = positions.get(code, {})
        if not isinstance(state_entry, dict):
            continue
        state_qty = int(state_entry.get("last_known_quantity", 0) or 0)
        record_qty = int(float(holding_map[code].get("quantity", 0) or 0))
        if state_qty != record_qty:
            quantity_mismatches.append(
                {
                    "code": code,
                    "state_quantity": state_qty,
                    "track_quantity": record_qty,
                }
            )
    ok = not missing_in_state and not stale_in_state and not quantity_mismatches
    return {
        "ok": ok,
        "holding_count": len(holding_codes),
        "state_position_count": len(state_codes),
        "missing_in_state": missing_in_state,
        "stale_in_state": stale_in_state,
        "quantity_mismatches": quantity_mismatches,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def raise_on_inconsistent_challenger_state(
    records: list[dict[str, Any]],
    execution_state: dict[str, Any],
    *,
    context: str,
) -> dict[str, Any]:
    report = validate_challenger_execution_consistency(records, execution_state)
    if report["ok"]:
        return report
    details: list[str] = []
    if report["missing_in_state"]:
        details.append(f"missing_in_state={','.join(report['missing_in_state'])}")
    if report["stale_in_state"]:
        details.append(f"stale_in_state={','.join(report['stale_in_state'])}")
    if report["quantity_mismatches"]:
        mismatch_codes = ",".join(item["code"] for item in report["quantity_mismatches"])
        details.append(f"quantity_mismatches={mismatch_codes}")
    raise RuntimeValidationError(f"challenger 执行状态与账本不一致[{context}]: {' | '.join(details)}")
