#!/usr/bin/env python3
"""JSON Schema validation for candidate pool at the pool->trader pipeline boundary.

Validates every selected_record against required fields/types/ranges,
rejecting the entire batch on first violation (fail-fast).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CANDIDATE_ENTRY_REQUIRED = {
    "code",
    "name",
    "tier",
    "selection_rank",
    "selection_score",
}

CANDIDATE_ENTRY_NUMERIC = {
    "selection_rank": (1, 9999),
    "selection_score": (0.0, 100.0),
    "avg_profitability_priority": (0.0, 200.0),
    "avg_candidate_win_rate": (0.0, 1.0),
    "avg_candidate_avg_return": (-50.0, 200.0),
    "target_weight_pct": (0.0, 100.0),
    "score": (0.0, 100.0),
    "volatility": (0.0, 500.0),
}

POOL_TOP_REQUIRED = {
    "generated_at",
    "trade_date",
    "status",
    "selected_count",
    "candidate_count",
    "selected_records",
}


class PipelineValidationError(RuntimeError):
    pass


@dataclass
class SchemaViolation:
    field: str
    issue: str
    entry_index: int
    entry_code: str


def _is_finite(value: Any) -> bool:
    try:
        f = float(value)
        return math.isfinite(f)
    except (TypeError, ValueError):
        return False


def _validate_entry(entry: dict[str, Any], index: int) -> list[SchemaViolation]:
    if not isinstance(entry, dict):
        return [SchemaViolation("entry", "not a dict", index, "?")]
    code = str(entry.get("code", "") or "").strip()
    violations: list[SchemaViolation] = []

    for field in CANDIDATE_ENTRY_REQUIRED:
        if field not in entry:
            violations.append(SchemaViolation(field, "missing required field", index, code))

    for field, (lo, hi) in CANDIDATE_ENTRY_NUMERIC.items():
        value = entry.get(field)
        if value is None:
            violations.append(SchemaViolation(field, f"null; expected number in [{lo}, {hi}]", index, code))
            continue
        try:
            f = float(value)
            if not math.isfinite(f):
                violations.append(SchemaViolation(field, f"NaN/Inf value={value!r}", index, code))
            elif f < lo or f > hi:
                violations.append(SchemaViolation(field, f"value={f} outside [{lo}, {hi}]", index, code))
        except (TypeError, ValueError):
            violations.append(SchemaViolation(field, f"non-numeric value={value!r}", index, code))

    if code:
        if "." in code:
            violations.append(SchemaViolation("code", f"contains market suffix: {code}", index, code))
        cleaned = code.split(".", 1)[0].zfill(6)
        if len(cleaned) != 6 or not cleaned.isdigit():
            violations.append(SchemaViolation("code", f"invalid stock code: {code}", index, code))

    name = str(entry.get("name", "") or "").strip()
    if not name:
        violations.append(SchemaViolation("name", "empty name", index, code))

    tier_raw = entry.get("tier")
    try:
        tier = int(float(str(tier_raw)))
        if tier not in {1, 2, 3}:
            violations.append(SchemaViolation("tier", f"unexpected tier={tier}", index, code))
    except (TypeError, ValueError):
        violations.append(SchemaViolation("tier", f"non-integer tier={tier_raw!r}", index, code))

    return violations


def validate_candidate_pool(payload: dict[str, Any], *, path_hint: str = "") -> list[SchemaViolation]:
    if not isinstance(payload, dict):
        raise PipelineValidationError(f"pool payload is not a dict: {type(payload).__name__}")

    all_violations: list[SchemaViolation] = []

    for field in POOL_TOP_REQUIRED:
        if field not in payload:
            all_violations.append(SchemaViolation(field, f"missing top-level field in pool: {path_hint or '?'}", -1, ""))

    status = str(payload.get("status", "") or "").strip()
    if status != "ok":
        all_violations.append(SchemaViolation("status", f"pool status={status!r}, expected 'ok': {path_hint or '?'}", -1, ""))

    selected_count = int(payload.get("selected_count", 0) or 0)
    candidate_count = int(payload.get("candidate_count", 0) or 0)
    selected_records = payload.get("selected_records", [])
    if not isinstance(selected_records, list):
        raise PipelineValidationError(f"selected_records is not a list: {type(selected_records).__name__}")
    if selected_count != len(selected_records):
        all_violations.append(
            SchemaViolation(
                "selected_count",
                f"count={selected_count} != len(selected_records)={len(selected_records)}",
                -1,
                "",
            )
        )
    if candidate_count < selected_count:
        all_violations.append(
            SchemaViolation(
                "candidate_count",
                f"candidate_count={candidate_count} < selected_count={selected_count}",
                -1,
                "",
            )
        )

    for i, entry in enumerate(selected_records):
        entry_violations = _validate_entry(entry, i)
        if entry_violations:
            all_violations.extend(entry_violations)
            if len(all_violations) >= 20:
                break

    if all_violations:
        detail_parts = [f"{v.entry_index}:{v.entry_code}:{v.field}:{v.issue}" for v in all_violations[:10]]
        raise PipelineValidationError(
            f"pool schema violations ({len(all_violations)} total): {'; '.join(detail_parts)}"
        )

    return all_violations


def validate_pool_file(path: Path) -> list[SchemaViolation]:
    if not path.exists():
        raise PipelineValidationError(f"pool file not found: {path}")
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            with path.open("r", encoding=encoding) as f:
                payload = json.load(f)
            break
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    else:
        raise PipelineValidationError(f"cannot parse pool file: {path}")
    if not isinstance(payload, dict):
        raise PipelineValidationError(f"pool file is not a JSON object: {path}")
    return validate_candidate_pool(payload, path_hint=str(path))
