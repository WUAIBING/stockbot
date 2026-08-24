#!/usr/bin/env python3
"""Kelly-based position allocator for workbuddy challenger.

Replaces equal-weight allocation with conviction-weighted sizing that respects:
- Signal conviction (score / profitability_priority)
- Volatility (inverse-vol scaling)
- Correlation group (concentration limits)
- Drawdown remaining (throttle)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SizerConfig:
    max_single_pct: float = 12.0
    min_single_pct: float = 1.0
    max_correlation_group_pct: float = 25.0
    max_positions: int = 8
    kelly_half: bool = True
    base_conviction: float = 0.25
    drawdown_threshold_pct: float = 5.0
    max_drawdown_pct: float = 15.0
    vol_floor: float = 15.0
    vol_ceiling: float = 60.0


@dataclass
class PositionAllocation:
    code: str
    name: str
    weight_pct: float
    conviction_score: float
    vol_scale: float
    correlation_penalty: float
    drawdown_scale: float
    target_amount: float
    entry_cap_ratio: float


def _safe_conviction(candidate: dict[str, Any]) -> float:
    score = float(candidate.get("score", 0) or 0)
    avg_win = float(candidate.get("avg_candidate_win_rate", 0) or 0)
    avg_ret = float(candidate.get("avg_candidate_avg_return", 0) or 0)
    profit = float(
        candidate.get("avg_profitability_priority")
        or candidate.get("profit_priority_score", 0)
        or 0
    )

    conviction = 0.0
    if score > 0:
        conviction += min(score / 100.0, 1.0) * 0.40
    if profit >= 110:
        conviction += 0.25
    elif profit >= 100:
        conviction += 0.18
    elif profit >= 90:
        conviction += 0.12
    elif profit >= 80:
        conviction += 0.06
    if avg_win >= 0.58:
        conviction += 0.15
    elif avg_win >= 0.52:
        conviction += 0.10
    elif avg_win <= 0.45 and avg_win > 0:
        conviction -= 0.10
    if avg_ret >= 2.3:
        conviction += 0.10
    elif avg_ret >= 1.8:
        conviction += 0.06
    elif avg_ret <= 0.6 and avg_ret > 0:
        conviction -= 0.08
    return max(0.05, min(conviction, 1.0))


def _vol_scale(volatility: float, *, floor: float = 15.0, ceiling: float = 60.0) -> float:
    if volatility <= 0:
        return 0.6
    clamped = max(floor, min(volatility, ceiling))
    return round(floor / clamped, 4)


def _drawdown_scale(dd_pct: float, *, threshold: float = 5.0, max_dd: float = 15.0) -> float:
    if dd_pct < 0:
        dd_pct = 0.0
    if dd_pct <= threshold:
        return 1.0
    if dd_pct >= max_dd:
        return 0.0
    return round(1.0 - (dd_pct - threshold) / (max_dd - threshold), 4)


def _correlation_penalties(
    candidates: list[dict[str, Any]],
    allocations: list[PositionAllocation],
    *,
    group_cap_pct: float = 25.0,
) -> list[float]:
    groups: dict[str, float] = {}
    for alloc in allocations:
        if alloc.weight_pct > 0:
            groups.setdefault("_allocated", 0.0)
            groups["_allocated"] += alloc.weight_pct

    penalties: list[float] = []
    for candidate in candidates:
        group = str(candidate.get("correlation_group", "") or "").strip()
        if not group:
            penalties.append(1.0)
            continue
        used = groups.get(group, 0.0)
        headroom = max(0.0, group_cap_pct - used)
        if headroom <= 1.0:
            penalties.append(0.0)
        elif headroom < 5.0:
            penalties.append(round(headroom / 5.0, 3))
        else:
            penalties.append(1.0)
    return penalties


def _entry_cap_for_window(window_key: str) -> float:
    caps = {
        "10:00": 0.6,
        "10:30": 0.4,
        "11:00": 0.4,
        "13:30": 0.3,
        "14:00": 0.2,
        "14:30": 0.2,
        "14:50": 0.15,
    }
    return caps.get(window_key, 0.4)


def compute_position_weights(
    candidates: list[dict[str, Any]],
    total_assets: float,
    drawdown_pct: float = 0.0,
    *,
    window_key: str = "",
    config: SizerConfig | None = None,
) -> tuple[list[PositionAllocation], dict[str, Any]]:
    cfg = config or SizerConfig()
    dd_scale = _drawdown_scale(
        drawdown_pct,
        threshold=cfg.drawdown_threshold_pct,
        max_dd=cfg.max_drawdown_pct,
    )
    entry_cap = _entry_cap_for_window(window_key)

    temp: list[dict[str, Any]] = []
    for i, c in enumerate(candidates):
        conviction = _safe_conviction(c)
        vol = float(c.get("volatility", 0) or 0)
        vs = _vol_scale(vol, floor=cfg.vol_floor, ceiling=cfg.vol_ceiling)
        kelly_raw = conviction * vs
        if cfg.kelly_half:
            kelly_raw *= 0.5
        base_weight = kelly_raw * cfg.base_conviction * dd_scale * 100.0
        temp.append(
            {
                "index": i,
                "code": str(c.get("code", "")).strip(),
                "name": str(c.get("name", "")).strip(),
                "conviction": round(conviction, 4),
                "vol_scale": vs,
                "kelly_raw": round(kelly_raw, 4),
                "base_weight": round(base_weight, 4),
                "group": str(c.get("correlation_group", "") or "").strip(),
            }
        )

    temp.sort(key=lambda x: (-x["conviction"], -x["vol_scale"], x["code"]))

    allocations: list[PositionAllocation] = []
    groups_used: dict[str, float] = {}
    total_weight: float = 0.0
    count: int = 0

    if dd_scale <= 0.0:
        debug = {
            "input_count": len(candidates),
            "output_count": 0,
            "drawdown_pct": round(drawdown_pct, 4),
            "drawdown_scale": dd_scale,
            "entry_cap_ratio": entry_cap,
            "max_positions": cfg.max_positions,
            "kelly_half": cfg.kelly_half,
            "window_key": window_key,
            "blocked": "drawdown_exceeded_max",
        }
        return [], debug

    for item in temp:
        if count >= cfg.max_positions:
            break
        w = item["base_weight"]

        group = item["group"]
        if group:
            group_used = groups_used.get(group, 0.0)
            headroom = cfg.max_correlation_group_pct - group_used
            if headroom <= 0:
                continue
            w = min(w, headroom)
            corr_penalty = round(w / item["base_weight"], 3) if item["base_weight"] > 0 else 0.0
        else:
            corr_penalty = 1.0

        w = max(cfg.min_single_pct, min(w, cfg.max_single_pct))
        w = min(w, entry_cap * 100.0)

        total_weight += w
        if total_weight > 100.0:
            w -= (total_weight - 100.0)
            total_weight = 100.0

        if w < cfg.min_single_pct:
            continue

        target_amount = round(total_assets * w / 100.0, 2)
        alloc = PositionAllocation(
            code=item["code"],
            name=item["name"],
            weight_pct=round(w, 4),
            conviction_score=item["conviction"],
            vol_scale=item["vol_scale"],
            correlation_penalty=corr_penalty,
            drawdown_scale=dd_scale,
            target_amount=target_amount,
            entry_cap_ratio=entry_cap,
        )
        allocations.append(alloc)
        if group:
            groups_used[group] = groups_used.get(group, 0.0) + w
        count += 1

    debug = {
        "input_count": len(candidates),
        "output_count": len(allocations),
        "drawdown_pct": round(drawdown_pct, 4),
        "drawdown_scale": dd_scale,
        "entry_cap_ratio": entry_cap,
        "max_positions": cfg.max_positions,
        "kelly_half": cfg.kelly_half,
        "window_key": window_key,
    }
    return allocations, debug
