from __future__ import annotations

import json
import hashlib
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from package_paths import CSI1000_SKILLS_DIR, DATA_DIR, SKILLS_DIR


MODEL_STATE_FILE = DATA_DIR / "v10_evolving_model_state.json"
MODEL_DECISIONS_FILE = DATA_DIR / "v10_model_decisions.jsonl"
MODEL_BASELINE_FILE = DATA_DIR / "v10_evolving_model_baseline.json"
MODEL_CHANGELOG_FILE = DATA_DIR / "v10_evolving_model_changelog.jsonl"
CLOSE_NODE_FILE = DATA_DIR / "v10_close_node_latest.json"
ENGINEERING_REVIEW_FILE = DATA_DIR / "v10_engineering_review_latest.json"

MIN_MATCHED_TRADES = 12
MIN_RECENT_CLOSED_TRADES = 10
MODEL_UPDATE_COOLDOWN_HOURS = 24
MAX_WEIGHT_STEP = 0.03
MAX_MODE_BIAS_STEP = 1.0
MAX_TIER_BIAS_STEP = 0.8
MIN_MODE_SAMPLES = 4
MIN_TIER_SAMPLES = 5

TRADE_API_LOG_FILE = DATA_DIR / "v10_trade_api_log.jsonl"

EXECUTION_BLOCKING_RESULT_CODES = {"112", "501"}
NON_LEARNING_MODES = {"external_sync"}
AUTO_IMPORTED_MARKERS = ("[AUTO_IMPORTED]",)

DEFAULT_WEIGHTS = {
    "market": 0.24,
    "sector": 0.18,
    "stock": 0.38,
    "flow": 0.20,
}

DEFAULT_MODE_PRIOR = {
    "V9_full": 6.0,
    "trend_ride+vol": 3.0,
    "trend_ride+green": 2.0,
    "vol_breakout": 1.5,
    "trend_only": 0.5,
    "kill_only": -1.0,
}

DEFAULT_STATE = {
    "version": 1,
    "updated_at": "",
    "weights": DEFAULT_WEIGHTS,
    "tier_bias": {"1": 6.0, "2": 2.0, "3": 0.0},
    "mode_bias": {},
    "learning": {
        "matched_trades": 0,
        "recent_closed_trades": 0,
        "component_corr": {},
        "last_retrain_at": "",
        "notes": "bootstrap",
        "last_status": "bootstrap",
        "window_metrics": {
            "matched_win_rate_pct": 0.0,
            "matched_avg_return_pct": 0.0,
            "gross_matched_trades": 0,
            "eligible_matched_trades": 0,
        },
        "sample_filter": {
            "gross_matched_trades": 0,
            "eligible_matched_trades": 0,
            "blocked_trades": 0,
            "eligible_rate_pct": 0.0,
            "reason_counts": {},
            "reason_counts_top": [],
        },
        "judgment_calibration": {
            "available": False,
            "trade_date": "",
            "verdict": "",
            "score": 0,
            "risk_bias": "",
            "rebound_bias": "",
            "opening_liquidity_verdict": "",
            "opening_window_confirmed": False,
            "external_risk_level": "",
            "external_window_tag": "",
            "external_negative_sectors": [],
            "opening_anchor_pressure_level": "",
            "broken_anchor_names": [],
            "weekend_digest_active": False,
            "weekend_digest_bias": "",
        },
        "engineering_evolution": {
            "available": False,
            "trade_date": "",
            "verdict": "",
            "incident_count": 0,
            "recurring_incident_count": 0,
            "high_severity_count": 0,
            "category_counts": {},
            "incident_codes": [],
            "hardening_actions": [],
            "summary": "",
        },
        "guardrails": {
            "min_matched_trades": MIN_MATCHED_TRADES,
            "min_recent_closed_trades": MIN_RECENT_CLOSED_TRADES,
            "update_cooldown_hours": MODEL_UPDATE_COOLDOWN_HOURS,
            "max_weight_step": MAX_WEIGHT_STEP,
            "max_mode_bias_step": MAX_MODE_BIAS_STEP,
            "max_tier_bias_step": MAX_TIER_BIAS_STEP,
            "min_mode_samples": MIN_MODE_SAMPLES,
            "min_tier_samples": MIN_TIER_SAMPLES,
        },
    },
}

INDUSTRY_FILE_CANDIDATES = [
    CSI1000_SKILLS_DIR / "tdxhy.cfg",
    SKILLS_DIR / "csi1000-skills" / "tdxhy.cfg",
    Path.home() / ".workbuddy" / "skills" / "csi1000-skills" / "tdxhy.cfg",
]


def _fnum(value, default=0.0):
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _inum(value, default=0):
    try:
        if value in (None, ""):
            return int(default)
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _score_linear(value, low, high):
    if high <= low:
        return 50.0
    scaled = (value - low) / (high - low) * 100.0
    return _clamp(scaled, 0.0, 100.0)


def _score_band(value, preferred_low, preferred_high, hard_low, hard_high):
    if value <= hard_low or value >= hard_high:
        return 5.0
    if preferred_low <= value <= preferred_high:
        return 90.0
    if value < preferred_low:
        return _score_linear(value, hard_low, preferred_low) * 0.85
    distance = _score_linear(hard_high - value, 0.0, hard_high - preferred_high)
    return distance * 0.85


def _read_json(path: Path, default):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _write_json_atomic(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append_jsonl(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _parse_dt(text):
    value = str(text or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _hours_since(text):
    dt = _parse_dt(text)
    if dt is None:
        return None
    return (datetime.now() - dt).total_seconds() / 3600.0


def _extract_trade_date_from_run_slot(run_slot):
    text = str(run_slot or "").strip()
    candidate = text[:10]
    if len(candidate) == 10:
        try:
            datetime.strptime(candidate, "%Y-%m-%d")
            return candidate
        except ValueError:
            return ""
    return ""


def _normalize_trade_date(value="", run_slot=""):
    slot_date = _extract_trade_date_from_run_slot(run_slot)
    if slot_date:
        return slot_date
    text = str(value or "").strip()
    if len(text) >= 10:
        candidate = text[:10]
        try:
            datetime.strptime(candidate, "%Y-%m-%d")
            return candidate
        except ValueError:
            return text
    return text


def _split_order_ids(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return []
    for sep in ["|", ";", "/"]:
        text = text.replace(sep, ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def load_model_state():
    state = _read_json(MODEL_STATE_FILE, {})
    merged = json.loads(json.dumps(DEFAULT_STATE))
    if isinstance(state, dict):
        merged.update({k: v for k, v in state.items() if k in merged and v is not None})
        merged["weights"].update(state.get("weights", {}) if isinstance(state.get("weights"), dict) else {})
        merged["tier_bias"].update(state.get("tier_bias", {}) if isinstance(state.get("tier_bias"), dict) else {})
        merged["mode_bias"].update(state.get("mode_bias", {}) if isinstance(state.get("mode_bias"), dict) else {})
        learning = state.get("learning", {})
        if isinstance(learning, dict):
            merged["learning"].update(learning)
    return merged


def save_model_state(state):
    payload = dict(state or {})
    payload["updated_at"] = _now_str()
    _write_json_atomic(MODEL_STATE_FILE, payload)


def ensure_model_baseline(state=None):
    if MODEL_BASELINE_FILE.exists():
        return _read_json(MODEL_BASELINE_FILE, {})
    payload = dict(state or load_model_state())
    payload["snapshot_role"] = "baseline"
    payload["snapshot_at"] = _now_str()
    _write_json_atomic(MODEL_BASELINE_FILE, payload)
    return payload


def append_model_change_log(event_type, payload):
    entry = {
        "recorded_at": _now_str(),
        "event_type": str(event_type).strip(),
        "payload": payload,
    }
    _append_jsonl(MODEL_CHANGELOG_FILE, entry)


def _bounded_step(current, target, step_limit, low=None, high=None):
    value = _fnum(current, 0.0) + _clamp(_fnum(target, 0.0) - _fnum(current, 0.0), -step_limit, step_limit)
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _bounded_weight_update(current_weights, target_weights):
    stepped = {}
    for component in DEFAULT_WEIGHTS:
        current = _fnum((current_weights or {}).get(component), DEFAULT_WEIGHTS[component])
        target = _fnum((target_weights or {}).get(component), DEFAULT_WEIGHTS[component])
        stepped[component] = _bounded_step(current, target, MAX_WEIGHT_STEP, low=0.05, high=0.70)
    total = sum(stepped.values()) or 1.0
    return {k: round(v / total, 4) for k, v in stepped.items()}


def _bounded_bias_update(current_map, target_map, *, step_limit, low, high):
    merged = dict(current_map or {})
    for key, value in (target_map or {}).items():
        current = _fnum(merged.get(key, 0.0), 0.0)
        merged[key] = round(_bounded_step(current, value, step_limit, low=low, high=high), 2)
    return merged


def load_industry_mapping():
    mapping = {}
    for candidate in INDUSTRY_FILE_CANDIDATES:
        try:
            if not candidate.exists():
                continue
            with candidate.open("r", encoding="gbk") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("|")
                    if len(parts) >= 3:
                        mapping[parts[0].strip()] = parts[2].strip() or "unknown"
            if mapping:
                break
        except Exception:
            continue
    return mapping


def _flatten_signals(signals):
    rows = []
    for tier, items in (signals or {}).items():
        for row in items or []:
            item = dict(row)
            item["tier"] = _inum(item.get("tier", tier), _inum(tier, 0))
            item["code"] = str(item.get("code", "")).zfill(6)
            rows.append(item)
    return rows


def _compute_market_score(scan_context, rows):
    meta = {}
    if isinstance(scan_context, dict):
        meta = scan_context.get("meta") if isinstance(scan_context.get("meta"), dict) else scan_context
    total_signals = len(rows)
    stocks_scanned = max(_inum(meta.get("stocks_scanned"), 0), total_signals, 1)
    regime = str(meta.get("market_regime", "")).strip()
    total_amt_yi = _fnum(meta.get("total_csi1000_amt_yi"), 0.0)
    sigs = meta.get("signals_by_tier", {}) if isinstance(meta.get("signals_by_tier"), dict) else {}
    t1 = _inum(sigs.get("T1", 0), 0)
    t2 = _inum(sigs.get("T2", 0), 0)
    t3 = _inum(sigs.get("T3", 0), 0)

    regime_score = {
        "活跃市": 82.0,
        "正常市": 62.0,
        "清淡市": 38.0,
    }.get(regime, 58.0)
    breadth_score = _score_linear(total_signals / stocks_scanned, 0.05, 0.32)
    tier_mix_raw = (t1 * 3.0 + t2 * 2.0 + t3 * 1.0) / max(total_signals, 1)
    tier_mix_score = _score_linear(tier_mix_raw, 1.0, 2.4)
    liquidity_score = _score_linear(total_amt_yi, 1800.0, 6500.0)
    total = (
        regime_score * 0.40
        + breadth_score * 0.25
        + tier_mix_score * 0.20
        + liquidity_score * 0.15
    )
    return {
        "score": round(total, 2),
        "regime": regime or "unknown",
        "breakdown": {
            "regime": round(regime_score, 2),
            "breadth": round(breadth_score, 2),
            "tier_mix": round(tier_mix_score, 2),
            "liquidity": round(liquidity_score, 2),
        },
        "stats": {
            "total_signals": total_signals,
            "stocks_scanned": stocks_scanned,
            "t1": t1,
            "t2": t2,
            "t3": t3,
            "total_amt_yi": round(total_amt_yi, 2),
        },
    }


def _compute_sector_stats(rows, industry_map):
    sector_stats = {}
    grouped = defaultdict(list)
    for row in rows:
        code = str(row.get("code", "")).zfill(6)
        industry = industry_map.get(code, "unknown")
        grouped[industry].append(row)

    for industry, items in grouped.items():
        weighted_count = 0.0
        weighted_slope = []
        weighted_amt = []
        for row in items:
            tier = _inum(row.get("tier", 0), 0)
            weight = {1: 3.0, 2: 2.0, 3: 1.0}.get(tier, 0.5)
            weighted_count += weight
            weighted_slope.extend([_fnum(row.get("weekly_slope", 0.0), 0.0)] * int(weight))
            weighted_amt.extend([_fnum(row.get("amt_ratio", 1.0), 1.0)] * int(weight))
        avg_slope = float(np.mean(weighted_slope)) if weighted_slope else 0.0
        avg_amt_ratio = float(np.mean(weighted_amt)) if weighted_amt else 1.0
        count_score = _score_linear(weighted_count, 1.0, 12.0)
        slope_score = _score_linear(avg_slope, 0.0, 18.0)
        liquidity_score = _score_linear(avg_amt_ratio, 0.9, 2.2)
        total = count_score * 0.45 + slope_score * 0.35 + liquidity_score * 0.20
        sector_stats[industry] = {
            "name": industry,
            "weighted_signal_count": round(weighted_count, 2),
            "avg_weekly_slope": round(avg_slope, 4),
            "avg_amt_ratio": round(avg_amt_ratio, 4),
            "score": round(total, 2),
            "breakdown": {
                "count": round(count_score, 2),
                "slope": round(slope_score, 2),
                "liquidity": round(liquidity_score, 2),
            },
        }
    return sector_stats


def prepare_context(signals, scan_context=None, model_state=None):
    state = model_state or load_model_state()
    rows = _flatten_signals(signals)
    industry_map = load_industry_mapping()
    market = _compute_market_score(scan_context or {}, rows)
    sector_stats = _compute_sector_stats(rows, industry_map)
    return {
        "state": state,
        "rows": rows,
        "industry_map": industry_map,
        "market": market,
        "sector_stats": sector_stats,
        "scan_context": scan_context or {},
    }


def compute_min_trade_score(context):
    market_score = _fnum(((context or {}).get("market") or {}).get("score"), 58.0)
    if market_score >= 75:
        return 52.0
    if market_score >= 60:
        return 58.0
    return 64.0


def _compute_stock_score(row):
    tier = _inum(row.get("tier", 0), 0)
    slope = _fnum(row.get("weekly_slope", 0.0), 0.0)
    ma20_off = _fnum(row.get("close_vs_ma20_pct", 0.0), 0.0)
    amt_ratio = _fnum(row.get("amt_ratio", 1.0), 1.0)
    rsi = _fnum(row.get("rsi14", 50.0), 50.0)
    is_green = str(row.get("is_green", "")).lower() in {"true", "1"} or bool(row.get("is_green"))

    tier_score = {1: 94.0, 2: 74.0, 3: 56.0}.get(tier, 40.0)
    slope_score = _score_linear(slope, 0.0, 20.0)
    ma_score = _score_band(ma20_off, -1.5, 7.0, -9.0, 18.0)
    amt_score = _score_band(amt_ratio, 1.1, 2.2, 0.5, 4.0)
    rsi_score = _score_band(rsi, 48.0, 72.0, 20.0, 92.0)
    candle_score = 74.0 if is_green else 46.0
    total = (
        tier_score * 0.35
        + slope_score * 0.28
        + ma_score * 0.14
        + amt_score * 0.11
        + rsi_score * 0.07
        + candle_score * 0.05
    )
    return round(total, 2), {
        "tier": round(tier_score, 2),
        "weekly_slope": round(slope_score, 2),
        "ma20": round(ma_score, 2),
        "amt_ratio": round(amt_score, 2),
        "rsi": round(rsi_score, 2),
        "candle": round(candle_score, 2),
    }


def _compute_flow_score(row):
    bz_dir = _fnum(row.get("bz_direction", 0.0), 0.0)
    bz_rt = _fnum(row.get("bz_rt_direction", 0.0), 0.0)
    vol_expand = str(row.get("vol_expand", "")).lower() in {"true", "1"} or bool(row.get("vol_expand"))
    is_green = str(row.get("is_green", "")).lower() in {"true", "1"} or bool(row.get("is_green"))

    tail_score = _score_linear(bz_dir, -1.5, 1.5)
    realtime_score = _score_linear(bz_rt, -1.0, 1.0)
    vol_score = 78.0 if vol_expand else 52.0
    candle_score = 72.0 if is_green else 45.0
    total = (
        tail_score * 0.36
        + realtime_score * 0.34
        + vol_score * 0.18
        + candle_score * 0.12
    )
    return round(total, 2), {
        "tail": round(tail_score, 2),
        "realtime": round(realtime_score, 2),
        "volume": round(vol_score, 2),
        "candle": round(candle_score, 2),
    }


def _lookup_mode_bias(state, mode):
    mode_bias = {}
    if isinstance(state, dict):
        raw = state.get("mode_bias", {})
        if isinstance(raw, dict):
            mode_bias = raw
    value = mode_bias.get(str(mode or "").strip())
    if value is None:
        return DEFAULT_MODE_PRIOR.get(str(mode or "").strip(), 0.0)
    return _fnum(value, 0.0)


def score_row(row, context):
    state = (context or {}).get("state") or load_model_state()
    weights = state.get("weights", DEFAULT_WEIGHTS)
    market = (context or {}).get("market", {})
    industry_map = (context or {}).get("industry_map", {})
    sector_stats = (context or {}).get("sector_stats", {})

    code = str(row.get("code", "")).zfill(6)
    industry = industry_map.get(code, "unknown")
    sector = sector_stats.get(industry)
    sector_score = _fnum((sector or {}).get("score"), 52.0)
    stock_score, stock_detail = _compute_stock_score(row)
    flow_score, flow_detail = _compute_flow_score(row)
    market_score = _fnum(market.get("score"), 58.0)
    tier = _inum(row.get("tier", 0), 0)
    mode = str(row.get("mode", "")).strip()
    tier_bias = _fnum((state.get("tier_bias", {}) or {}).get(str(tier), 0.0), 0.0)
    mode_bias = _lookup_mode_bias(state, mode)

    total = (
        market_score * _fnum(weights.get("market"), DEFAULT_WEIGHTS["market"])
        + sector_score * _fnum(weights.get("sector"), DEFAULT_WEIGHTS["sector"])
        + stock_score * _fnum(weights.get("stock"), DEFAULT_WEIGHTS["stock"])
        + flow_score * _fnum(weights.get("flow"), DEFAULT_WEIGHTS["flow"])
    )
    total += tier_bias + mode_bias
    total = _clamp(total, 0.0, 100.0)

    return {
        "score": round(total, 2),
        "industry": industry,
        "component_scores": {
            "market": round(market_score, 2),
            "sector": round(sector_score, 2),
            "stock": round(stock_score, 2),
            "flow": round(flow_score, 2),
        },
        "component_detail": {
            "market": market.get("breakdown", {}),
            "sector": (sector or {}).get("breakdown", {}),
            "stock": stock_detail,
            "flow": flow_detail,
        },
        "bias": {
            "tier": round(tier_bias, 2),
            "mode": round(mode_bias, 2),
        },
    }


def rank_signals(signals, *, scan_context=None):
    context = prepare_context(signals, scan_context=scan_context)
    ranked = {1: [], 2: [], 3: []}
    flat = []
    min_trade_score = compute_min_trade_score(context)
    for tier, items in (signals or {}).items():
        enriched_rows = []
        for row in items or []:
            scored = score_row(row, context)
            enriched = dict(row)
            enriched["code"] = str(enriched.get("code", "")).zfill(6)
            enriched["model_score"] = scored["score"]
            enriched["model_industry"] = scored["industry"]
            enriched["model_market_score"] = scored["component_scores"]["market"]
            enriched["model_sector_score"] = scored["component_scores"]["sector"]
            enriched["model_stock_score"] = scored["component_scores"]["stock"]
            enriched["model_flow_score"] = scored["component_scores"]["flow"]
            enriched["model_bias_tier"] = scored["bias"]["tier"]
            enriched["model_bias_mode"] = scored["bias"]["mode"]
            enriched["model_components_json"] = json.dumps(scored["component_detail"], ensure_ascii=False, sort_keys=True)
            enriched["model_pass"] = scored["score"] >= min_trade_score
            enriched_rows.append(enriched)
            flat.append(enriched)
        enriched_rows.sort(
            key=lambda r: (
                -_fnum(r.get("model_score"), 0.0),
                -_fnum(r.get("weekly_slope"), 0.0),
                -_fnum(r.get("amt_ratio"), 0.0),
            )
        )
        ranked[_inum(tier, 0)] = enriched_rows

    return {
        "context": context,
        "ranked_signals": ranked,
        "all_ranked": sorted(flat, key=lambda r: -_fnum(r.get("model_score"), 0.0)),
        "min_trade_score": min_trade_score,
    }


def record_decisions(run_slot, candidates, *, selected_codes=None, scan_context=None):
    selected_codes = {str(code).zfill(6) for code in (selected_codes or set())}
    MODEL_DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    trade_date = _normalize_trade_date(datetime.now().strftime("%Y-%m-%d"), run_slot)
    meta = {}
    if isinstance(scan_context, dict):
        meta = scan_context.get("meta") if isinstance(scan_context.get("meta"), dict) else scan_context
    with MODEL_DECISIONS_FILE.open("a", encoding="utf-8") as f:
        for item in candidates or []:
            code = str(item.get("code", "")).zfill(6)
            payload = {
                "recorded_at": _now_str(),
                "trade_date": trade_date,
                "run_slot": run_slot,
                "code": code,
                "decision_id": str(item.get("decision_id") or f"{trade_date}|{run_slot}|{code}").strip(),
                "decision_run_slot": str(item.get("decision_run_slot") or run_slot).strip(),
                "selected_reason_hash": str(
                    item.get("selected_reason_hash")
                    or hashlib.sha1(
                        json.dumps(
                            {
                                "code": code,
                                "tier": _inum(item.get("tier", 0), 0),
                                "mode": item.get("mode", ""),
                                "score": round(_fnum(item.get("model_score", 0.0), 0.0), 4),
                                "market": round(_fnum(item.get("model_market_score", 0.0), 0.0), 4),
                                "sector": round(_fnum(item.get("model_sector_score", 0.0), 0.0), 4),
                                "stock": round(_fnum(item.get("model_stock_score", 0.0), 0.0), 4),
                                "flow": round(_fnum(item.get("model_flow_score", 0.0), 0.0), 4),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ).encode("utf-8")
                    ).hexdigest()[:16]
                ).strip(),
                "name": item.get("name", ""),
                "tier": _inum(item.get("tier", 0), 0),
                "mode": item.get("mode", ""),
                "industry": item.get("model_industry", "unknown"),
                "score": round(_fnum(item.get("model_score", 0.0), 0.0), 2),
                "selected": code in selected_codes,
                "selection_rank": _inum(item.get("selection_rank", 0), 0),
                "target_amount": round(_fnum(item.get("target_amount", 0.0), 0.0), 2),
                "components": {
                    "market": round(_fnum(item.get("model_market_score", 0.0), 0.0), 2),
                    "sector": round(_fnum(item.get("model_sector_score", 0.0), 0.0), 2),
                    "stock": round(_fnum(item.get("model_stock_score", 0.0), 0.0), 2),
                    "flow": round(_fnum(item.get("model_flow_score", 0.0), 0.0), 2),
                },
                "market_regime": meta.get("market_regime", ""),
                "signals_by_tier": meta.get("signals_by_tier", {}),
            }
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _load_decision_records(limit=5000):
    if not MODEL_DECISIONS_FILE.exists():
        return []
    rows = []
    try:
        with MODEL_DECISIONS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    return rows[-limit:]


def _load_trade_api_logs(limit=10000):
    if not TRADE_API_LOG_FILE.exists():
        return []
    rows = []
    try:
        with TRADE_API_LOG_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    return rows[-limit:]


def _build_trade_log_index(rows):
    index = {}
    for row in rows or []:
        trade_date = _normalize_trade_date(str(row.get("logged_at", ""))[:10])
        action = str(row.get("action", "")).strip().lower()
        code = str(row.get("code", "")).zfill(6)
        if not trade_date or not action or not code:
            continue
        key = (trade_date, action, code)
        bucket = index.setdefault(
            key,
            {
                "has_success": False,
                "success_count": 0,
                "failure_codes": defaultdict(int),
                "ok_order_ids": set(),
                "event_count": 0,
            },
        )
        bucket["event_count"] += 1
        result_code = str(
            row.get("result_code")
            or ((row.get("raw") or {}).get("code"))
            or ((row.get("raw") or {}).get("status"))
            or ""
        ).strip()
        if bool(row.get("ok")):
            bucket["has_success"] = True
            bucket["success_count"] += 1
            order_id = str(row.get("order_id") or ((row.get("raw") or {}).get("data") or {}).get("orderID") or "").strip()
            if order_id:
                bucket["ok_order_ids"].add(order_id)
        elif result_code:
            bucket["failure_codes"][result_code] += 1
    return index


def _get_trade_log_summary(index, trade_date, action, code):
    bucket = index.get((trade_date, action, code))
    if not bucket:
        return {
            "has_success": False,
            "success_count": 0,
            "failure_codes": {},
            "ok_order_ids": set(),
            "event_count": 0,
        }
    return {
        "has_success": bool(bucket.get("has_success")),
        "success_count": _inum(bucket.get("success_count", 0), 0),
        "failure_codes": dict(bucket.get("failure_codes", {})),
        "ok_order_ids": set(bucket.get("ok_order_ids", set())),
        "event_count": _inum(bucket.get("event_count", 0), 0),
    }


def _choose_decision_match(record, candidates):
    if not candidates:
        return None
    record_mode = str(record.get("mode", "")).strip()
    record_tier = _inum(record.get("tier", 0), 0)
    ranked = list(candidates)

    if record_mode and record_mode not in NON_LEARNING_MODES:
        exact_mode = [row for row in ranked if str(row.get("mode", "")).strip() == record_mode]
        if exact_mode:
            ranked = exact_mode
    if record_tier > 0:
        exact_tier = [row for row in ranked if _inum(row.get("tier", 0), 0) == record_tier]
        if exact_tier:
            ranked = exact_tier

    ranked.sort(
        key=lambda row: (
            1 if bool(row.get("selected")) else 0,
            _fnum(row.get("score", 0.0), 0.0),
            str(row.get("recorded_at", "")),
        ),
        reverse=True,
    )
    return ranked[0] if ranked else None


def _top_reason_counts(reason_counts, limit=8):
    ordered = sorted((reason_counts or {}).items(), key=lambda item: (-_inum(item[1], 0), item[0]))
    return [{"reason": key, "count": _inum(value, 0)} for key, value in ordered[:limit]]


def _load_intraday_judgment_calibration():
    close_node = _read_json(CLOSE_NODE_FILE, {})
    review = close_node.get("intraday_judgment_review", {}) if isinstance(close_node, dict) else {}
    if not isinstance(review, dict) or not review:
        return {
            "available": False,
            "trade_date": "",
            "verdict": "",
            "score": 0,
            "risk_bias": "",
            "rebound_bias": "",
            "opening_liquidity_verdict": "",
            "opening_window_confirmed": False,
            "external_risk_level": "",
            "external_window_tag": "",
            "external_negative_sectors": [],
            "opening_anchor_pressure_level": "",
            "broken_anchor_names": [],
            "weekend_digest_active": False,
            "weekend_digest_bias": "",
        }
    opening_liquidity = review.get("opening_liquidity", {}) if isinstance(review.get("opening_liquidity", {}), dict) else {}
    external_market = review.get("external_market", {}) if isinstance(review.get("external_market", {}), dict) else {}
    opening_anchor_monitor = external_market.get("opening_anchor_break_monitor", {}) if isinstance(external_market.get("opening_anchor_break_monitor", {}), dict) else {}
    weekend_digest_monitor = external_market.get("weekend_digest_monitor", {}) if isinstance(external_market.get("weekend_digest_monitor", {}), dict) else {}
    return {
        "available": bool(review.get("available")),
        "trade_date": str(review.get("trade_date", "")).strip(),
        "verdict": str(review.get("verdict", "")).strip(),
        "score": _inum(review.get("score", 0), 0),
        "risk_bias": str(review.get("risk_bias", "")).strip(),
        "rebound_bias": str(review.get("rebound_bias", "")).strip(),
        "confidence": _fnum(review.get("confidence", 0.0), 0.0),
        "opening_liquidity_verdict": str(opening_liquidity.get("verdict", "")).strip(),
        "opening_window_confirmed": bool(opening_liquidity.get("in_0931_window")),
        "external_risk_level": str(external_market.get("risk_level", "")).strip(),
        "external_window_tag": str(external_market.get("window_tag", "")).strip(),
        "external_negative_sectors": [
            str(item).strip()
            for item in (external_market.get("negative_sectors", []) or [])
            if str(item).strip()
        ][:8],
        "opening_anchor_pressure_level": str(opening_anchor_monitor.get("pressure_level", "")).strip(),
        "broken_anchor_names": [
            str(item).strip()
            for item in (opening_anchor_monitor.get("broken_anchor_names", []) or [])
            if str(item).strip()
        ][:8],
        "weekend_digest_active": bool(weekend_digest_monitor.get("active")),
        "weekend_digest_bias": str(weekend_digest_monitor.get("bias", "")).strip(),
    }


def _load_engineering_evolution():
    review = _read_json(ENGINEERING_REVIEW_FILE, {})
    if not isinstance(review, dict) or not review:
        close_node = _read_json(CLOSE_NODE_FILE, {})
        review = close_node.get("engineering_review", {}) if isinstance(close_node, dict) else {}
    if not isinstance(review, dict) or not review:
        return {
            "available": False,
            "trade_date": "",
            "verdict": "",
            "incident_count": 0,
            "recurring_incident_count": 0,
            "high_severity_count": 0,
            "category_counts": {},
            "incident_codes": [],
            "hardening_actions": [],
            "summary": "",
        }
    return {
        "available": bool(review.get("available")),
        "trade_date": str(review.get("trade_date", "")).strip(),
        "verdict": str(review.get("verdict", "")).strip(),
        "incident_count": _inum(review.get("incident_count", 0), 0),
        "recurring_incident_count": _inum(review.get("recurring_incident_count", 0), 0),
        "high_severity_count": _inum(review.get("high_severity_count", 0), 0),
        "category_counts": dict(review.get("category_counts", {}) if isinstance(review.get("category_counts", {}), dict) else {}),
        "incident_codes": [
            str(item).strip()
            for item in (review.get("incident_codes", []) or [])
            if str(item).strip()
        ][:12],
        "hardening_actions": [
            str(item).strip()
            for item in (review.get("hardening_actions", []) or [])
            if str(item).strip()
        ][:8],
        "summary": str(review.get("summary", "")).strip(),
    }


def _classify_learning_sample(record, decision_candidates, trade_log_index):
    trade_date = _normalize_trade_date(record.get("date", ""))
    code = str(record.get("code", "")).zfill(6)
    build_note = str(record.get("build_note", "")).strip()
    mode = str(record.get("mode", "")).strip()
    status = str(record.get("status", "")).strip()
    sell_date = _normalize_trade_date(record.get("sell_date", ""))
    buy_order_ids = set(_split_order_ids(record.get("buy_order_ids", "")))
    sell_order_id = str(record.get("sell_order_id", "")).strip()
    candidates = decision_candidates.get((trade_date, code), [])
    decision = _choose_decision_match(record, candidates)

    result = {
        "trade_date": trade_date,
        "code": code,
        "eligible": False,
        "reason": "",
        "flags": [],
        "decision": decision,
        "buy_evidence": {},
        "sell_evidence": {},
    }

    if status != "closed":
        result["reason"] = "not_closed"
        return result
    if mode in NON_LEARNING_MODES or any(marker in build_note for marker in AUTO_IMPORTED_MARKERS):
        result["reason"] = "external_sync_record"
        return result
    if not decision:
        result["reason"] = "missing_decision_match"
        return result

    buy_summary = _get_trade_log_summary(trade_log_index, trade_date, "buy", code)
    sell_summary = _get_trade_log_summary(trade_log_index, sell_date, "sell", code) if sell_date else _get_trade_log_summary({}, "", "", "")
    result["buy_evidence"] = {
        "has_success": bool(buy_summary.get("has_success")) or bool(buy_order_ids),
        "success_count": _inum(buy_summary.get("success_count", 0), 0),
        "failure_codes": buy_summary.get("failure_codes", {}),
        "buy_order_ids": sorted(buy_order_ids),
    }
    result["sell_evidence"] = {
        "has_success": bool(sell_summary.get("has_success")) or bool(sell_order_id),
        "success_count": _inum(sell_summary.get("success_count", 0), 0),
        "failure_codes": sell_summary.get("failure_codes", {}),
        "sell_order_id": sell_order_id,
    }

    if not result["buy_evidence"]["has_success"]:
        result["reason"] = "missing_buy_fill_evidence"
        return result
    if not result["sell_evidence"]["has_success"]:
        result["reason"] = "missing_sell_fill_evidence"
        return result

    for fail_code, count in sorted((buy_summary.get("failure_codes") or {}).items()):
        if fail_code in EXECUTION_BLOCKING_RESULT_CODES and _inum(count, 0) > 0:
            result["flags"].append(f"buy_noise_{fail_code}")
    for fail_code, count in sorted((sell_summary.get("failure_codes") or {}).items()):
        if fail_code in EXECUTION_BLOCKING_RESULT_CODES and _inum(count, 0) > 0:
            result["flags"].append(f"sell_noise_{fail_code}")

    if result["flags"]:
        result["reason"] = "execution_noise"
        return result

    result["eligible"] = True
    result["reason"] = "eligible"
    return result


def refresh_model_state(records):
    state = load_model_state()
    ensure_model_baseline(state)
    decisions = _load_decision_records()
    decision_candidates = defaultdict(list)
    for row in decisions:
        normalized_row = dict(row)
        normalized_row["intended_trade_date"] = _normalize_trade_date(
            row.get("trade_date", ""),
            row.get("run_slot", ""),
        )
        key = (normalized_row["intended_trade_date"], str(row.get("code", "")).zfill(6))
        decision_candidates[key].append(normalized_row)

    trade_log_index = _build_trade_log_index(_load_trade_api_logs())

    closed = [r for r in (records or []) if str(r.get("status", "")).strip() == "closed"]
    matched = []
    gross_matched = 0
    reason_counts = defaultdict(int)
    for record in closed:
        classification = _classify_learning_sample(record, decision_candidates, trade_log_index)
        reason_counts[classification["reason"]] += 1
        decision = classification.get("decision")
        if decision:
            gross_matched += 1
        if not classification.get("eligible") or not decision:
            continue
        matched.append(
            {
                "pnl_pct": _fnum(record.get("pnl_pct", 0.0), 0.0),
                "pnl": _fnum(record.get("pnl", 0.0), 0.0),
                "mode": str(decision.get("mode", "") or record.get("mode", "")).strip(),
                "tier": _inum(decision.get("tier", record.get("tier", 0)), 0),
                "components": decision.get("components", {}),
                "decision_selected": bool(decision.get("selected")),
                "decision_score": _fnum(decision.get("score", 0.0), 0.0),
                "execution_flags": list(classification.get("flags") or []),
            }
        )

    learning = state.get("learning", {})
    learning["recent_closed_trades"] = len(closed)
    learning["matched_trades"] = len(matched)
    learning["guardrails"] = {
        "min_matched_trades": MIN_MATCHED_TRADES,
        "min_recent_closed_trades": MIN_RECENT_CLOSED_TRADES,
        "update_cooldown_hours": MODEL_UPDATE_COOLDOWN_HOURS,
        "max_weight_step": MAX_WEIGHT_STEP,
        "max_mode_bias_step": MAX_MODE_BIAS_STEP,
        "max_tier_bias_step": MAX_TIER_BIAS_STEP,
        "min_mode_samples": MIN_MODE_SAMPLES,
        "min_tier_samples": MIN_TIER_SAMPLES,
    }
    matched_wr = (
        sum(1 for item in matched if _fnum(item.get("pnl_pct", 0.0), 0.0) > 0) / len(matched) * 100.0
        if matched else 0.0
    )
    matched_avg = float(np.mean([_fnum(item.get("pnl_pct", 0.0), 0.0) for item in matched])) if matched else 0.0
    learning["sample_filter"] = {
        "gross_matched_trades": gross_matched,
        "eligible_matched_trades": len(matched),
        "blocked_trades": max(len(closed) - len(matched), 0),
        "eligible_rate_pct": round(len(matched) / max(len(closed), 1) * 100.0, 2) if closed else 0.0,
        "reason_counts": dict(reason_counts),
        "reason_counts_top": _top_reason_counts(reason_counts),
    }
    learning["judgment_calibration"] = _load_intraday_judgment_calibration()
    learning["engineering_evolution"] = _load_engineering_evolution()
    learning["window_metrics"] = {
        "matched_win_rate_pct": round(matched_wr, 2),
        "matched_avg_return_pct": round(matched_avg, 4),
        "gross_matched_trades": gross_matched,
        "eligible_matched_trades": len(matched),
    }

    if len(closed) < MIN_RECENT_CLOSED_TRADES or len(matched) < MIN_MATCHED_TRADES:
        filtered_too_much = len(closed) >= MIN_RECENT_CLOSED_TRADES and gross_matched >= MIN_MATCHED_TRADES and len(matched) < MIN_MATCHED_TRADES
        learning["notes"] = (
            "waiting_for_clean_learning_samples"
            if filtered_too_much else
            "waiting_for_more_closed_trades"
        )
        learning["last_status"] = (
            "guardrail_waiting_clean_samples"
            if filtered_too_much else
            "guardrail_waiting_samples"
        )
        state["learning"] = learning
        save_model_state(state)
        append_model_change_log(
            "skip_update",
            {
                "reason": "insufficient_eligible_samples" if filtered_too_much else "insufficient_samples",
                "recent_closed_trades": len(closed),
                "gross_matched_trades": gross_matched,
                "eligible_matched_trades": len(matched),
                "required_recent_closed_trades": MIN_RECENT_CLOSED_TRADES,
                "required_matched_trades": MIN_MATCHED_TRADES,
                "sample_filter": learning.get("sample_filter", {}),
                "judgment_calibration": learning.get("judgment_calibration", {}),
                "engineering_evolution": learning.get("engineering_evolution", {}),
            },
        )
        return model_summary(state)

    hours_since_retrain = _hours_since(learning.get("last_retrain_at", ""))
    if hours_since_retrain is not None and hours_since_retrain < MODEL_UPDATE_COOLDOWN_HOURS:
        learning["notes"] = "cooldown_active"
        learning["last_status"] = "guardrail_cooldown"
        state["learning"] = learning
        save_model_state(state)
        append_model_change_log(
            "skip_update",
            {
                "reason": "cooldown_active",
                "hours_since_retrain": round(hours_since_retrain, 2),
                "required_cooldown_hours": MODEL_UPDATE_COOLDOWN_HOURS,
                "judgment_calibration": learning.get("judgment_calibration", {}),
                "engineering_evolution": learning.get("engineering_evolution", {}),
            },
        )
        return model_summary(state)

    component_corr = {}
    adjusted_weights = {}
    for component in ["market", "sector", "stock", "flow"]:
        x = np.array([_fnum(item["components"].get(component), 0.0) for item in matched], dtype=float)
        y = np.array([_fnum(item.get("pnl_pct", 0.0), 0.0) for item in matched], dtype=float)
        corr = 0.0
        if len(x) >= 3 and np.std(x) > 1e-6 and np.std(y) > 1e-6:
            corr = float(np.corrcoef(x, y)[0, 1])
            if not math.isfinite(corr):
                corr = 0.0
        component_corr[component] = round(corr, 4)
        base = _fnum(DEFAULT_WEIGHTS.get(component), 0.25)
        adjusted_weights[component] = base * _clamp(1.0 + corr * 0.45, 0.7, 1.35)

    total_weight = sum(adjusted_weights.values()) or 1.0
    normalized_target = {k: round(v / total_weight, 4) for k, v in adjusted_weights.items()}
    next_weights = _bounded_weight_update(state.get("weights", {}), normalized_target)

    mode_groups = defaultdict(list)
    tier_groups = defaultdict(list)
    for item in matched:
        mode_groups[item["mode"]].append(item["pnl_pct"])
        tier_groups[str(item["tier"])].append(item["pnl_pct"])

    new_mode_bias = {}
    for mode, pnl_list in mode_groups.items():
        if not mode or len(pnl_list) < MIN_MODE_SAMPLES:
            continue
        avg_ret = float(np.mean(pnl_list))
        win_rate = sum(1 for v in pnl_list if v > 0) / len(pnl_list) * 100.0
        bias = avg_ret * 0.7 + (win_rate - 50.0) / 10.0
        new_mode_bias[mode] = round(_clamp(bias, -8.0, 8.0), 2)

    tier_bias = {}
    for tier, pnl_list in tier_groups.items():
        if len(pnl_list) < MIN_TIER_SAMPLES:
            continue
        avg_ret = float(np.mean(pnl_list))
        win_rate = sum(1 for v in pnl_list if v > 0) / len(pnl_list) * 100.0
        bias = avg_ret * 0.55 + (win_rate - 50.0) / 14.0
        tier_bias[tier] = round(_clamp(bias, -6.0, 8.0), 2)

    old_weights = dict(state.get("weights", {}))
    old_mode_bias = dict(state.get("mode_bias", {}))
    old_tier_bias = dict(state.get("tier_bias", {}))
    state["weights"] = next_weights
    if new_mode_bias:
        state["mode_bias"] = _bounded_bias_update(
            old_mode_bias,
            new_mode_bias,
            step_limit=MAX_MODE_BIAS_STEP,
            low=-8.0,
            high=8.0,
        )
    if tier_bias:
        state["tier_bias"] = _bounded_bias_update(
            old_tier_bias,
            tier_bias,
            step_limit=MAX_TIER_BIAS_STEP,
            low=-6.0,
            high=8.0,
        )

    learning["component_corr"] = component_corr
    learning["notes"] = "weights_refreshed_from_closed_trades"
    learning["last_status"] = "updated_with_guardrails"
    learning["last_retrain_at"] = _now_str()
    state["learning"] = learning
    save_model_state(state)
    append_model_change_log(
        "apply_update",
        {
            "recent_closed_trades": len(closed),
            "gross_matched_trades": gross_matched,
            "matched_trades": len(matched),
            "window_metrics": learning.get("window_metrics", {}),
            "weights": {"before": old_weights, "after": state.get("weights", {})},
            "mode_bias": {"before": old_mode_bias, "after": state.get("mode_bias", {})},
            "tier_bias": {"before": old_tier_bias, "after": state.get("tier_bias", {})},
            "component_corr": component_corr,
            "guardrails": learning.get("guardrails", {}),
            "sample_filter": learning.get("sample_filter", {}),
            "judgment_calibration": learning.get("judgment_calibration", {}),
            "engineering_evolution": learning.get("engineering_evolution", {}),
        },
    )
    return model_summary(state)


def model_summary(state=None):
    payload = state or load_model_state()
    weights = payload.get("weights", {})
    learning = payload.get("learning", {})
    mode_bias = payload.get("mode_bias", {})
    baseline = _read_json(MODEL_BASELINE_FILE, {})
    top_modes = sorted(mode_bias.items(), key=lambda item: abs(_fnum(item[1], 0.0)), reverse=True)[:5]
    return {
        "updated_at": payload.get("updated_at", ""),
        "weights": {
            "market": round(_fnum(weights.get("market"), 0.0), 4),
            "sector": round(_fnum(weights.get("sector"), 0.0), 4),
            "stock": round(_fnum(weights.get("stock"), 0.0), 4),
            "flow": round(_fnum(weights.get("flow"), 0.0), 4),
        },
        "tier_bias": payload.get("tier_bias", {}),
        "mode_bias_top": [{"mode": k, "bias": _fnum(v, 0.0)} for k, v in top_modes],
        "learning": learning,
        "guardrails": learning.get("guardrails", {}),
        "baseline": {
            "snapshot_at": baseline.get("snapshot_at", ""),
            "weights": (baseline or {}).get("weights", DEFAULT_WEIGHTS),
            "tier_bias": (baseline or {}).get("tier_bias", {}),
        },
        "files": {
            "state": str(MODEL_STATE_FILE),
            "baseline": str(MODEL_BASELINE_FILE),
            "decisions": str(MODEL_DECISIONS_FILE),
            "changelog": str(MODEL_CHANGELOG_FILE),
        },
    }
