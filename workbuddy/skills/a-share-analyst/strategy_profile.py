"""Versioned, deterministic strategy profiles shared by scanning and evaluation.

A profile defines only signal classification and ranking.  It deliberately has no
market-data, account, or order-execution dependencies so offline evaluation can
replay the same rules the scanner uses.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


PROFILE_DIR = Path(__file__).with_name("strategy_profiles")
DEFAULT_PROFILE_PATH = PROFILE_DIR / "v10.0.0.json"
REQUIRED_PROFILE_FIELDS = {
    "schema_version",
    "profile_id",
    "base_strategy",
    "decision_cutoff",
    "holding_sessions",
    "candidate_limit",
    "ranking",
    "rules",
}
REQUIRED_RULE_FIELDS = {"tier", "mode", "position"}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip().lower() in {"", "nan", "none", "null", "<na>"}:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _optional_bool(value: Any) -> bool | None:
    if value is None or str(value).strip().lower() in {"", "nan", "none", "null", "<na>"}:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"nan", "none", "null", "<na>"} else text


def _canonical_json(value: Mapping[str, Any]) -> str:
    """Canonicalize a profile while preserving ordered-rule semantics.

    Rules are evaluated in declaration order, so their order is deliberately
    represented as a list in the fingerprint rather than being sorted away.
    """
    normalized = {key: item for key, item in value.items() if key != "rules"}
    rules = value.get("rules", {})
    normalized["rules_in_priority_order"] = [
        {"name": str(name), "rule": rule}
        for name, rule in rules.items()
    ]
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def profile_fingerprint(profile: Mapping[str, Any]) -> str:
    """Return a stable hash that identifies the exact profile contents."""
    validate_strategy_profile(profile)
    return "sha256:" + hashlib.sha256(_canonical_json(profile).encode("utf-8")).hexdigest()


def validate_strategy_profile(profile: Mapping[str, Any]) -> None:
    """Reject incomplete or unsafe profile definitions before they reach a run."""
    if not isinstance(profile, Mapping):
        raise ValueError("strategy profile must be an object")
    missing = sorted(REQUIRED_PROFILE_FIELDS - set(profile))
    if missing:
        raise ValueError(f"strategy profile missing fields: {', '.join(missing)}")
    if int(profile["schema_version"]) != 1:
        raise ValueError("unsupported strategy profile schema_version")
    if not str(profile["profile_id"]).strip():
        raise ValueError("strategy profile profile_id is required")
    if str(profile["decision_cutoff"]).strip() != "14:50":
        raise ValueError("strategy profile decision_cutoff must be 14:50")
    if int(profile["holding_sessions"]) <= 0 or int(profile["candidate_limit"]) <= 0:
        raise ValueError("strategy profile holding_sessions and candidate_limit must be positive")
    if not isinstance(profile["ranking"], list) or not profile["ranking"]:
        raise ValueError("strategy profile ranking must be a non-empty list")
    rules = profile["rules"]
    if not isinstance(rules, Mapping) or not rules:
        raise ValueError("strategy profile rules must be a non-empty object")
    for name, rule in rules.items():
        if not str(name).strip() or not isinstance(rule, Mapping):
            raise ValueError("strategy profile rules must have named object entries")
        rule_missing = REQUIRED_RULE_FIELDS - set(rule)
        if rule_missing:
            raise ValueError(f"strategy profile rule {name} missing fields: {', '.join(sorted(rule_missing))}")
        if int(rule["tier"]) < 1 or _float(rule["position"]) <= 0:
            raise ValueError(f"strategy profile rule {name} has invalid tier or position")
    constraints = profile.get("portfolio_constraints")
    if constraints is not None:
        if not isinstance(constraints, Mapping):
            raise ValueError("strategy profile portfolio_constraints must be an object")
        for field in ("max_new_positions", "max_per_industry_l2", "max_per_correlation_cluster"):
            if int(constraints.get(field, 0)) <= 0:
                raise ValueError(f"strategy profile portfolio constraint {field} must be positive")
        exposure_groups = constraints.get("exposure_groups", {})
        if not isinstance(exposure_groups, Mapping):
            raise ValueError("strategy profile exposure_groups must be an object")
        for name, group in exposure_groups.items():
            if not str(name).strip() or not isinstance(group, Mapping):
                raise ValueError("strategy profile exposure groups must have named object entries")
            if int(group.get("max_new_positions", 0)) <= 0:
                raise ValueError(f"strategy profile exposure group {name} must have a positive limit")


def load_strategy_profile(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate an immutable profile definition from disk."""
    profile_path = Path(path) if path is not None else DEFAULT_PROFILE_PATH
    with profile_path.open("r", encoding="utf-8") as handle:
        profile = json.load(handle)
    validate_strategy_profile(profile)
    return profile


def _rule_matches(rule: Mapping[str, Any], features: Mapping[str, Any]) -> bool:
    bz = _optional_float(features.get("bz_dir"))
    bz_rt = _optional_float(features.get("bz_rt"))
    values = {
        "bz": bz,
        "bz_rt": bz if bz_rt is None else bz_rt,
        "weekly_slope": _optional_float(features.get("weekly_slope")),
        "ma20": _optional_float(features.get("ma20_off")),
        "amt_ratio": _optional_float(features.get("amt_ratio")),
        "rsi": _optional_float(features.get("rsi")),
        "weekly_align": _optional_bool(features.get("weekly_align")),
        "vol_expand": _optional_bool(features.get("vol_expand")),
        "is_green": _optional_bool(features.get("is_green")),
    }
    for key, expected in rule.items():
        if key in REQUIRED_RULE_FIELDS:
            continue
        if key in {"weekly_align", "vol_expand", "is_green"}:
            if values[key] is None or values[key] is not bool(expected):
                return False
            continue
        if key.endswith("_lt"):
            if values[key[:-3]] is None or not values[key[:-3]] < _float(expected):
                return False
            continue
        if key.endswith("_gt"):
            if values[key[:-3]] is None or not values[key[:-3]] > _float(expected):
                return False
            continue
        if key.endswith("_min"):
            if values[key[:-4]] is None or not values[key[:-4]] >= _float(expected):
                return False
            continue
        if key.endswith("_max"):
            if values[key[:-4]] is None or not values[key[:-4]] <= _float(expected):
                return False
            continue
        raise ValueError(f"unsupported strategy profile rule field: {key}")
    return True


def _description(mode: str, features: Mapping[str, Any]) -> str:
    bz = _float(features.get("bz_dir"))
    slope = _float(features.get("weekly_slope"))
    if mode == "V9_full":
        return f"bz={bz:+.2f}%+weekly+MA20"
    if mode == "kill+weekly+nearMA20":
        return f"bz={bz:+.2f}%+weekly+MA20_near"
    if mode == "kill+MA20_pull":
        return f"bz={bz:+.2f}%+MA20"
    if mode == "trend_ride+vol":
        return f"slope={slope:.1f}%+MA20+vol_expand"
    if mode == "trend_ride+green":
        return f"slope={slope:.1f}%+MA20+green"
    if mode == "near_kill+weekly+MA20":
        return f"bz_mild={bz:+.2f}%+weekly+MA20+stabilizing"
    if mode == "vol_breakout":
        return f"vol*{_float(features.get('amt_ratio')):.1f}+green+weekly"
    if mode == "pre_breakout+":
        return f"缩量贴MA20 slope={slope:.1f}%"
    if mode == "pre_breakout":
        return f"slope={slope:.1f}%+MA20+缩量企稳"
    if mode == "kill_only":
        return f"bz={bz:+.2f}%"
    if mode == "trend_only":
        return f"slope={slope:.1f}%+MA20_near"
    return ""


def classify_signal(features: Mapping[str, Any], profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Classify one as-of feature row using the ordered rules in ``profile``."""
    active_profile = dict(profile) if profile is not None else load_strategy_profile()
    validate_strategy_profile(active_profile)
    fingerprint = profile_fingerprint(active_profile)
    for rule in active_profile["rules"].values():
        if _rule_matches(rule, features):
            mode = str(rule["mode"])
            return {
                "tier": int(rule["tier"]),
                "mode": mode,
                "position": _float(rule["position"]),
                "signal_desc": _description(mode, features),
                "profile_id": str(active_profile["profile_id"]),
                "profile_hash": fingerprint,
            }
    return {
        "tier": 0,
        "mode": "no_signal",
        "position": 0.0,
        "signal_desc": "",
        "profile_id": str(active_profile["profile_id"]),
        "profile_hash": fingerprint,
    }


def _text_items(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
            if isinstance(decoded, list):
                return [str(item).strip() for item in decoded if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [item.strip() for item in text.replace("|", ",").split(",") if item.strip()]


def _exposure_groups(row: Mapping[str, Any], constraints: Mapping[str, Any]) -> list[str]:
    industry = str(row.get("industry_l2", "")).strip()
    searchable = "|".join([industry, *_text_items(row.get("themes"))])
    matches = []
    for name, group in constraints.get("exposure_groups", {}).items():
        keywords = [
            *_text_items(group.get("industry_keywords")),
            *_text_items(group.get("theme_keywords")),
        ]
        if any(keyword and keyword in searchable for keyword in keywords):
            matches.append(str(name))
    return matches


def select_candidate_decisions(
    rows: list[Mapping[str, Any]], profile: Mapping[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Classify, rank, and apply auditable portfolio-level constraints."""
    active_profile = dict(profile) if profile is not None else load_strategy_profile()
    validate_strategy_profile(active_profile)
    ranked: list[dict[str, Any]] = []
    for source in rows:
        decision = classify_signal(source, active_profile)
        if decision["tier"] <= 0:
            continue
        row = dict(source)
        row.update(decision)
        ranked.append(row)
    ranked.sort(key=lambda row: (int(row["tier"]), -_float(row.get("weekly_slope")), str(row.get("code", "")).zfill(6)))

    constraints = active_profile.get("portfolio_constraints", {})
    hard_limit = min(
        int(active_profile["candidate_limit"]),
        int(constraints.get("max_new_positions", active_profile["candidate_limit"])),
    )
    industry_counts: dict[str, int] = {}
    cluster_counts: dict[str, int] = {}
    exposure_counts: dict[str, int] = {}
    seen_codes: set[str] = set()
    selected: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for row in ranked:
        code = str(row.get("code", "")).strip().zfill(6)
        industry = _clean_text(row.get("industry_l2", ""))
        cluster = _clean_text(row.get("correlation_cluster", ""))
        groups = _exposure_groups(row, constraints)
        reasons: list[str] = []
        if code in seen_codes:
            reasons.append(f"duplicate_code:{code}")
        else:
            seen_codes.add(code)
        if constraints.get("require_industry_l2") and not industry:
            reasons.append("missing_industry_l2")
        if constraints.get("require_correlation_cluster") and not cluster:
            reasons.append("missing_correlation_cluster")
        if industry and industry_counts.get(industry, 0) >= int(constraints.get("max_per_industry_l2", 10**9)):
            reasons.append(f"industry_l2_limit:{industry}")
        if cluster and cluster_counts.get(cluster, 0) >= int(constraints.get("max_per_correlation_cluster", 10**9)):
            reasons.append(f"correlation_cluster_limit:{cluster}")
        for group_name in groups:
            group = constraints.get("exposure_groups", {}).get(group_name, {})
            if exposure_counts.get(group_name, 0) >= int(group.get("max_new_positions", 10**9)):
                reasons.append(f"exposure_group_limit:{group_name}")
        if len(selected) >= hard_limit:
            reasons.append("max_new_positions")

        candidate = dict(row)
        candidate["industry_exposure_groups"] = groups
        if reasons:
            candidate["constraint_status"] = "blocked"
            candidate["constraint_reasons"] = reasons
            blocked.append(candidate)
            continue

        candidate["constraint_status"] = "selected"
        candidate["constraint_reasons"] = []
        candidate["rank"] = len(selected) + 1
        selected.append(candidate)
        if industry:
            industry_counts[industry] = industry_counts.get(industry, 0) + 1
        if cluster:
            cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        for group_name in groups:
            exposure_counts[group_name] = exposure_counts.get(group_name, 0) + 1

    return {"selected": selected, "blocked": blocked}


def rank_candidates(rows: list[Mapping[str, Any]], profile: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return selected candidates; use ``select_candidate_decisions`` for rejections."""
    return select_candidate_decisions(rows, profile)["selected"]
