#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘前多源资讯复核：隔夜外盘 + 新闻联播 + 政策产业 + 地缘商品汇率。

设计目标：
1. 在 09:31 开盘门控附近生成可结构化消费的外部资讯判断；
2. 不直接替代盘中事实，只提供板块预判、风险偏好预判与应变建议；
3. 重点考验过滤与归因能力，而不是机械堆资讯。
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from package_paths import DATA_DIR
from workbuddy_runtime import WORKBUDDY_CANDIDATE_POOL_FILE


OUTPUT_JSON = DATA_DIR / "v10_external_market_review_latest.json"
OUTPUT_HISTORY = DATA_DIR / "automation_status" / "external_market_review_history.jsonl"
OUTPUT_CSV = DATA_DIR / "v10_external_market_review_latest.csv"
OPENING_TRADABILITY_FILE = DATA_DIR / "opening_tradability_latest.json"
WORKBUDDY_POOL_FILE = WORKBUDDY_CANDIDATE_POOL_FILE
MX_SEARCH_SKILL = Path.home() / ".trae" / "skills" / "mx-search" / "mx_search.py"
MAX_ITEMS_PER_QUERY = 8
MAX_TOP_TITLES = 5
MAX_FLAG_COUNT = 12

SCENARIOS = [
    {
        "name": "overnight_global",
        "label": "隔夜外盘",
        "weight": 1.45,
        "horizon_weights": {"short": 1.3, "mid": 0.55, "long": 0.15},
        "query": "昨夜今晨 美股 纳指 标普 中概股 A50 美债 美元 原油 黄金 对A股板块影响 最新解读",
    },
    {
        "name": "cctv_policy",
        "label": "新闻联播与政策",
        "weight": 1.20,
        "horizon_weights": {"short": 0.65, "mid": 1.0, "long": 1.25},
        "query": "昨夜今晨 新闻联播 宏观政策 资本市场 科技 制造 消费 能源 对A股板块影响 解读",
    },
    {
        "name": "geopolitics_commodities",
        "label": "地缘与大宗",
        "weight": 1.30,
        "horizon_weights": {"short": 0.95, "mid": 0.95, "long": 0.45},
        "query": "昨夜今晨 地缘冲突 关税 航运 原油 黄金 铜 稀土 汇率 对A股板块影响 解读",
    },
    {
        "name": "industry_catalyst",
        "label": "产业催化",
        "weight": 1.00,
        "horizon_weights": {"short": 0.9, "mid": 1.0, "long": 0.85},
        "query": "今晨 AI算力 半导体 机器人 汽车 医药 军工 电力 稳增长 产业消息 对A股板块影响",
    },
    {
        "name": "opening_market_brief",
        "label": "盘前市场总览",
        "weight": 1.10,
        "horizon_weights": {"short": 1.15, "mid": 0.45, "long": 0.15},
        "query": "今日A股盘前 重要资讯汇总 风险提示 板块机会 市场情绪 解读",
    },
    {
        "name": "short_flow_watch",
        "label": "做空资金动向",
        "weight": 1.35,
        "horizon_weights": {"short": 1.4, "mid": 0.7, "long": 0.2},
        "query": "今晨 A股 做空资金 融券 股指期货 量化卖压 北向流出 ETF赎回 空头压力 对板块影响",
    },
]

MONDAY_WEEKEND_SCENARIO = {
    "name": "weekend_digest",
    "label": "周末汇总",
    "weight": 1.35,
    "horizon_weights": {"short": 1.1, "mid": 1.05, "long": 0.8},
    "query": "周末 A股 新闻联播 政策 产业 地缘 海外市场 周末重要资讯 汇总 对周一开盘影响",
}

NEGATIVE_KEYWORDS = {
    "risk_asset_selloff": ("暴跌", "大跌", "跳水", "重挫", "杀跌", "恐慌", "避险升温"),
    "rates_usd_pressure": ("美元走强", "美元指数上行", "美债收益率上行", "高利率", "紧缩"),
    "commodity_inflation": ("油价上涨", "原油大涨", "运价上涨", "通胀压力"),
    "geopolitical_shock": ("地缘冲突", "冲突升级", "关税", "制裁", "封锁", "袭击"),
    "domestic_risk": ("减持", "处罚", "问询", "风险提示", "违规", "业绩下滑", "亏损"),
    "growth_pressure": ("需求走弱", "景气下行", "库存压力", "价格战", "出口承压"),
}

NEUTRAL_KEYWORDS = {
    "range_bound": ("震荡", "分化", "博弈", "观望", "拉锯", "轮动"),
    "wait_and_see": ("等待", "暂未落地", "待观察", "关注后续", "静待"),
    "mixed_signal": ("喜忧参半", "影响有限", "中性", "平稳", "结构性"),
}

POSITIVE_KEYWORDS = {
    "policy_support": ("政策支持", "财政发力", "货币宽松", "稳增长", "支持资本市场", "提振"),
    "industry_catalyst": ("订单", "中标", "突破", "催化", "涨价", "扩产", "景气回升"),
    "safe_haven_support": ("黄金走强", "军工催化", "避险板块受益"),
    "market_stabilize": ("企稳", "修复", "回暖", "超预期", "利好", "回购", "增持"),
}

SHORT_FLOW_KEYWORDS = {
    "index_future_short": ("股指期货空单", "IC空单", "IM空单", "IF空单", "IH空单", "期指空头"),
    "margin_short": ("融券", "转融券", "融券卖出", "券源", "做空"),
    "quant_sell": ("量化砸盘", "程序化卖出", "高频卖压", "量化做空"),
    "northbound_outflow": ("北向流出", "外资流出", "北向净流出"),
    "etf_redeem": ("ETF赎回", "宽基赎回", "资金撤离", "被动卖盘"),
    "short_target_growth": ("高位科技承压", "成长股承压", "小票承压", "题材股承压"),
}

SECTOR_KEYWORDS = {
    "AI硬件": ("算力", "服务器", "CPO", "PCB", "光模块", "液冷", "英伟达", "GPU"),
    "半导体": ("半导体", "芯片", "存储", "晶圆", "EDA", "封测"),
    "机器人": ("机器人", "人形机器人", "减速器", "伺服", "机器视觉"),
    "军工": ("军工", "导弹", "战机", "国防", "卫星"),
    "黄金": ("黄金", "贵金属"),
    "原油化工": ("原油", "油价", "化工", "炼化"),
    "航运港口": ("航运", "集运", "港口", "运价"),
    "电力煤炭": ("电力", "煤炭", "火电", "煤价"),
    "新能源": ("锂电", "光伏", "风电", "储能", "新能源汽车"),
    "医药": ("医药", "创新药", "医疗器械", "CXO"),
    "消费": ("消费", "白酒", "家电", "食品饮料", "零售"),
    "地产基建": ("地产", "基建", "建筑", "水泥", "城中村"),
    "有色资源": ("铜", "铝", "稀土", "小金属", "资源品"),
}

SECTOR_RISK_DIRECTION = {
    "黄金": {"risk": "positive", "safe_haven": True},
    "军工": {"risk": "positive", "safe_haven": False},
    "航运港口": {"risk": "mixed", "safe_haven": False},
    "原油化工": {"risk": "mixed", "safe_haven": False},
    "电力煤炭": {"risk": "mixed", "safe_haven": False},
}


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _read_json(path: Path):
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


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["scenario", "query", "risk_score", "positive_score", "a_share_bias", "top_title"]
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


def _safe_float(value, default: float = 0.0) -> float:
    try:
        text = str(value).strip()
        return float(text) if text else default
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        text = str(value).strip()
        return int(float(text)) if text else default
    except Exception:
        return default


def _normalize_text(value) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _is_monday_opening_window(trigger_slot: str = "", now_dt: datetime | None = None) -> bool:
    now_dt = now_dt or datetime.now()
    slot = str(trigger_slot or "").strip()
    in_opening = not slot or slot in {"09:31", "09:35", "09:40"} or slot < "09:45"
    return now_dt.weekday() == 0 and in_opening


def _resolve_scenarios(*, trigger_slot: str = "", now_dt: datetime | None = None) -> list[dict]:
    scenarios = list(SCENARIOS)
    if _is_monday_opening_window(trigger_slot, now_dt):
        scenarios.append(dict(MONDAY_WEEKEND_SCENARIO))
    return scenarios


def _extract_items(result: dict) -> list[dict]:
    return (
        result.get("data", {})
        .get("data", {})
        .get("llmSearchResponse", {})
        .get("data", [])
        or []
    )


def _match_keywords(text: str, keyword_map: dict[str, tuple[str, ...]]) -> Counter:
    hits: Counter = Counter()
    for flag, words in keyword_map.items():
        for word in words:
            if word and word in text:
                hits[flag] += 1
    return hits


def _match_sectors(text: str) -> Counter:
    hits: Counter = Counter()
    for sector, words in SECTOR_KEYWORDS.items():
        for word in words:
            if word and word in text:
                hits[sector] += 1
    return hits


def _score_sector_direction(sector: str, negative_score: float, positive_score: float) -> str:
    direction = SECTOR_RISK_DIRECTION.get(sector, {}).get("risk", "")
    if direction == "positive":
        return "positive"
    if direction == "mixed":
        if positive_score > negative_score + 0.8:
            return "positive"
        if negative_score > positive_score + 0.8:
            return "negative"
        return "mixed"
    if negative_score > positive_score + 0.5:
        return "negative"
    if positive_score > negative_score + 0.5:
        return "positive"
    return "mixed"


def _resolve_horizon_weights(scenario: dict) -> dict[str, float]:
    weights = scenario.get("horizon_weights", {}) if isinstance(scenario.get("horizon_weights", {}), dict) else {}
    return {
        "short": _safe_float(weights.get("short", 1.0), 1.0),
        "mid": _safe_float(weights.get("mid", 0.8), 0.8),
        "long": _safe_float(weights.get("long", 0.5), 0.5),
    }


def _derive_term_bias(*, negative_score: float, neutral_score: float, positive_score: float, sector_count: int = 0) -> str:
    gap = positive_score - negative_score
    spread = abs(gap)
    if negative_score >= positive_score + 2.5 or (negative_score >= positive_score + 1.2 and sector_count >= 3):
        return "negative"
    if positive_score >= negative_score + 3.5 and sector_count >= 3:
        return "broad_positive"
    if positive_score >= negative_score + 1.5:
        return "selective_positive"
    if neutral_score >= spread or spread <= 1.2:
        return "neutral"
    return "neutral"


def _build_term_outlook(*, label: str, negative_score: float, neutral_score: float, positive_score: float, negative_sectors: list[str], neutral_sectors: list[str], positive_sectors: list[str], flags: list[str]) -> dict:
    sector_count = len(negative_sectors) if negative_score >= positive_score else len(positive_sectors)
    bias = _derive_term_bias(
        negative_score=negative_score,
        neutral_score=neutral_score,
        positive_score=positive_score,
        sector_count=sector_count,
    )
    focus_sectors = positive_sectors[:4] if "positive" in bias else neutral_sectors[:4]
    avoid_sectors = negative_sectors[:4]
    if bias == "negative":
        summary = f"{label}偏谨慎，优先规避 {', '.join(avoid_sectors)}。" if avoid_sectors else f"{label}偏谨慎，先看风险收缩应对。"
    elif bias == "broad_positive":
        summary = f"{label}偏积极，可提高对 {', '.join(focus_sectors[:4])} 的关注。" if focus_sectors else f"{label}偏积极，可适度提高风险偏好。"
    elif bias == "selective_positive":
        summary = f"{label}偏结构性利好，重点观察 {', '.join(focus_sectors[:4])}。" if focus_sectors else f"{label}偏结构性利好。"
    else:
        neutral_focus = focus_sectors or neutral_sectors[:4] or positive_sectors[:2]
        summary = f"{label}偏中性分化，先做结构性观察，关注 {', '.join(neutral_focus[:4])}。" if neutral_focus else f"{label}偏中性分化，暂不做方向性放大。"
    return {
        "label": label,
        "bias": bias,
        "negative_score": round(negative_score, 2),
        "neutral_score": round(neutral_score, 2),
        "positive_score": round(positive_score, 2),
        "negative_sectors": negative_sectors[:6],
        "neutral_sectors": neutral_sectors[:6],
        "positive_sectors": positive_sectors[:6],
        "focus_sectors": focus_sectors[:6],
        "avoid_sectors": avoid_sectors[:6],
        "key_flags": flags[:6],
        "summary": summary,
    }


def _build_short_flow_monitor(summaries: list[dict]) -> dict:
    target = next((item for item in summaries if item.get("scenario") == "short_flow_watch"), {}) or {}
    items = target.get("items", []) if isinstance(target.get("items", []), list) else []
    signal_counter: Counter = Counter()
    sector_counter: Counter = Counter()
    top_titles: list[str] = []
    for item in items:
        blob = " ".join(
            str(item.get(key, "") or "").strip()
            for key in ("title", "institution", "type")
        )
        hits = _match_keywords(blob, SHORT_FLOW_KEYWORDS)
        signal_counter.update(hits)
        sector_counter.update(_match_sectors(blob))
        title = str(item.get("title", "")).strip()
        if title and title not in top_titles and len(top_titles) < 4:
            top_titles.append(title)
    pressure_score = round(
        _safe_float(target.get("negative_score", 0.0), 0.0)
        + sum(signal_counter.values()) * 0.8,
        2,
    )
    if pressure_score >= 8:
        pressure_level = "high"
    elif pressure_score >= 4:
        pressure_level = "medium"
    elif pressure_score > 0:
        pressure_level = "low"
    else:
        pressure_level = "neutral"
    targeted_sectors = [
        sector
        for sector, _ in sector_counter.most_common(6)
    ] or list(target.get("negative_sectors", [])[:6])
    if pressure_level == "high":
        summary = "做空资金信号偏强，优先防范高弹性板块被空头集中压制。"
    elif pressure_level == "medium":
        summary = "存在明确空头/卖压信号，需降低对脆弱题材的主观乐观。"
    elif pressure_level == "low":
        summary = "有轻度空头压力线索，但更多作为辅助风控参考。"
    else:
        summary = "暂未识别到显著做空资金线索。"
    return {
        "available": bool(target),
        "pressure_level": pressure_level,
        "pressure_score": pressure_score,
        "signals": [key for key, _ in signal_counter.most_common(6)],
        "targeted_sectors": targeted_sectors,
        "top_titles": top_titles,
        "recommended_response": {
            "high": "开盘优先防守，降低高弹性科技和情绪票试错冲动。",
            "medium": "缩小追高容忍度，只允许强分支低吸和被动应对。",
            "low": "继续观察做空线索是否发酵，不主动放大风险暴露。",
            "neutral": "做空资金暂无主导性证据，仍以流动性和主线强弱为准。",
        }.get(pressure_level, "继续观察。"),
        "summary": summary,
    }


def _build_opening_anchor_break_monitor() -> dict:
    opening_payload = _read_json(OPENING_TRADABILITY_FILE)
    pool_payload = _read_json(WORKBUDDY_POOL_FILE)
    records = opening_payload.get("records", []) if isinstance(opening_payload.get("records", []), list) else []
    selected_records = pool_payload.get("selected_records", []) if isinstance(pool_payload.get("selected_records", []), list) else []
    if not records:
        return {
            "available": False,
            "pressure_level": "unknown",
            "broken_anchor_codes": [],
            "broken_anchor_names": [],
            "leader_anchor_breaks": [],
            "weight_anchor_breaks": [],
            "summary": "缺少 09:31 开盘样本，无法验证核心锚股是否被开盘即按。",
        }

    by_code = {
        str(item.get("code", "")).zfill(6): item
        for item in records
        if str(item.get("code", "")).strip()
    }

    def _row_snapshot(row: dict, *, role: str, rank: int = 0) -> dict:
        last_close = _safe_float(row.get("last_close", 0.0), 0.0)
        open_price = _safe_float(row.get("open_price", 0.0), 0.0)
        last_price = _safe_float(row.get("last_price", 0.0), 0.0)
        open_change_pct = round((_safe_ratio(open_price - last_close, last_close) * 100.0), 2) if last_close > 0 else 0.0
        latest_change_pct = round((_safe_ratio(last_price - last_close, last_close) * 100.0), 2) if last_close > 0 else 0.0
        amount = _safe_float(row.get("amount", 0.0), 0.0)
        return {
            "code": str(row.get("code", "")).zfill(6),
            "name": str(row.get("name", "")).strip(),
            "role": role,
            "rank": rank,
            "open_change_pct": open_change_pct,
            "latest_change_pct": latest_change_pct,
            "amount": amount,
        }

    leader_anchor_breaks: list[dict] = []
    leader_watch: list[dict] = []
    for item in selected_records[:8]:
        code = str(item.get("code", "")).zfill(6)
        row = by_code.get(code)
        if not row:
            continue
        snap = _row_snapshot(
            row,
            role=str(item.get("role", "leader_anchor")).strip() or "leader_anchor",
            rank=_safe_int(item.get("selection_rank", 0), 0),
        )
        latest_rank = _safe_int(item.get("latest_rank", 0), 0)
        latest_chg_pct = _safe_float(item.get("latest_chg_pct", 0.0), 0.0)
        snap["selection_rank"] = _safe_int(item.get("selection_rank", 0), 0)
        snap["latest_rank"] = latest_rank
        snap["latest_chg_pct"] = latest_chg_pct
        leader_watch.append(snap)
        if snap["open_change_pct"] <= -2.0 or snap["latest_change_pct"] <= -3.0 or (
            latest_chg_pct >= 8.0 and snap["latest_change_pct"] <= -2.0
        ):
            leader_anchor_breaks.append(snap)

    sorted_by_amount = sorted(records, key=lambda row: _safe_float(row.get("amount", 0.0), 0.0), reverse=True)
    weight_anchor_breaks: list[dict] = []
    for idx, row in enumerate(sorted_by_amount[:30], start=1):
        snap = _row_snapshot(row, role="weight_anchor", rank=idx)
        if snap["latest_change_pct"] <= -2.0 or (snap["open_change_pct"] <= -1.0 and snap["latest_change_pct"] <= -1.8):
            weight_anchor_breaks.append(snap)

    pressure_points = 0
    if len(leader_anchor_breaks) >= 3:
        pressure_points += 3
    elif len(leader_anchor_breaks) >= 1:
        pressure_points += 2
    if len(weight_anchor_breaks) >= 6:
        pressure_points += 2
    elif len(weight_anchor_breaks) >= 3:
        pressure_points += 1
    if leader_anchor_breaks and weight_anchor_breaks:
        pressure_points += 1

    if pressure_points >= 5:
        pressure_level = "high"
    elif pressure_points >= 3:
        pressure_level = "medium"
    elif pressure_points > 0:
        pressure_level = "low"
    else:
        pressure_level = "neutral"

    broken_codes = [item["code"] for item in (leader_anchor_breaks + weight_anchor_breaks)]
    broken_names = []
    for item in leader_anchor_breaks + weight_anchor_breaks:
        name = item["name"]
        if name and name not in broken_names:
            broken_names.append(name)
    if pressure_level == "high":
        summary = "前期领涨锚股与大成交权重锚同时走弱，属于明显的开盘做空验证信号。"
    elif pressure_level == "medium":
        summary = "已有部分领涨锚股或权重锚在开盘后被按，需警惕空头定点打击。"
    elif pressure_level == "low":
        summary = "仅有零星核心锚股转弱，暂作预警处理。"
    else:
        summary = "核心锚股暂未出现成片开盘破位，尚未形成明显空头验证。"
    return {
        "available": True,
        "pressure_level": pressure_level,
        "pressure_points": pressure_points,
        "leader_anchor_watch_count": len(leader_watch),
        "leader_anchor_break_count": len(leader_anchor_breaks),
        "weight_anchor_break_count": len(weight_anchor_breaks),
        "broken_anchor_codes": broken_codes[:12],
        "broken_anchor_names": broken_names[:12],
        "leader_anchor_breaks": leader_anchor_breaks[:8],
        "weight_anchor_breaks": weight_anchor_breaks[:8],
        "summary": summary,
    }


def _build_weekend_digest_monitor(summaries: list[dict]) -> dict:
    target = next((item for item in summaries if item.get("scenario") == "weekend_digest"), {}) or {}
    if not target:
        return {
            "available": False,
            "active": False,
            "bias": "inactive",
            "summary": "非周一早盘窗口，不启用周末汇总块。",
        }
    negative_score = _safe_float(target.get("negative_score", 0.0), 0.0)
    neutral_score = _safe_float(target.get("neutral_score", 0.0), 0.0)
    positive_score = _safe_float(target.get("positive_score", 0.0), 0.0)
    gap = positive_score - negative_score
    if negative_score >= positive_score + 2.5:
        bias = "negative"
    elif positive_score >= negative_score + 2.5:
        bias = "positive"
    elif neutral_score >= abs(gap):
        bias = "neutral"
    else:
        bias = "mixed"
    if bias == "negative":
        summary = "周末汇总整体偏谨慎，周一开盘先防止利空集中兑现。"
    elif bias == "positive":
        summary = "周末汇总整体偏积极，但仍需尊重 09:31 真实承接。"
    elif bias == "neutral":
        summary = "周末汇总偏中性分化，周一先做结构性验证。"
    else:
        summary = "周末信息多空交织，周一更应依赖流动性与锚股验证。"
    return {
        "available": True,
        "active": True,
        "bias": bias,
        "negative_score": round(negative_score, 2),
        "neutral_score": round(neutral_score, 2),
        "positive_score": round(positive_score, 2),
        "negative_sectors": list(target.get("negative_sectors", [])[:6]),
        "neutral_sectors": list(target.get("neutral_sectors", [])[:6]),
        "positive_sectors": list(target.get("positive_sectors", [])[:6]),
        "top_titles": list(target.get("top_titles", [])[:5]),
        "institutions": list(target.get("institutions", [])[:5]),
        "summary": summary,
        "recommended_response": {
            "negative": "周一开盘先防守，优先识别周末利空兑现的板块。",
            "positive": "周一可提高对周末催化方向的关注，但不能跳过开盘验真。",
            "neutral": "周一先观察，不因周末资讯主观放大仓位。",
            "mixed": "周一先看锚股与流动性，再决定风险偏好。",
        }.get(bias, "周一先看盘面验证。"),
    }


def _derive_window_tag(trigger_slot: str) -> str:
    slot = str(trigger_slot or "").strip()
    if slot == "09:31":
        return "opening_0931"
    if slot and slot < "09:45":
        return "opening_confirmation"
    if slot:
        return "daytime_followup"
    now = datetime.now()
    if now.hour < 9 or (now.hour == 9 and now.minute <= 31):
        return "opening_0931"
    if now.hour == 9 and now.minute <= 45:
        return "opening_confirmation"
    return "daytime_followup"


def _summarize_scenario(items: list[dict], *, scenario: dict) -> dict:
    negative_flags: Counter = Counter()
    neutral_flags: Counter = Counter()
    positive_flags: Counter = Counter()
    sector_negative: Counter = Counter()
    sector_neutral: Counter = Counter()
    sector_positive: Counter = Counter()
    top_titles: list[str] = []
    institutions: list[str] = []
    reviewed_items: list[dict] = []
    weight = _safe_float(scenario.get("weight", 1.0), 1.0)
    horizon_weights = _resolve_horizon_weights(scenario)
    horizon_scores = {
        "short": {"negative": 0.0, "neutral": 0.0, "positive": 0.0},
        "mid": {"negative": 0.0, "neutral": 0.0, "positive": 0.0},
        "long": {"negative": 0.0, "neutral": 0.0, "positive": 0.0},
    }

    for item in items[:MAX_ITEMS_PER_QUERY]:
        title = _normalize_text(item.get("title", ""))
        content = _normalize_text(item.get("content", ""))
        info_type = _normalize_text(item.get("informationType", ""))
        institution = _normalize_text(item.get("insName", ""))
        date_text = _normalize_text(item.get("date", ""))
        blob = " ".join(part for part in (title, content, info_type, institution) if part)
        neg_hits = _match_keywords(blob, NEGATIVE_KEYWORDS)
        neutral_hits = _match_keywords(blob, NEUTRAL_KEYWORDS)
        pos_hits = _match_keywords(blob, POSITIVE_KEYWORDS)
        sector_hits = _match_sectors(blob)
        negative_flags.update(neg_hits)
        neutral_flags.update(neutral_hits)
        positive_flags.update(pos_hits)
        neg_total = sum(neg_hits.values())
        neutral_total = sum(neutral_hits.values())
        pos_total = sum(pos_hits.values())
        for horizon, weights in horizon_scores.items():
            multiplier = horizon_weights.get(horizon, 1.0) * weight
            weights["negative"] += neg_total * multiplier
            weights["neutral"] += neutral_total * multiplier
            weights["positive"] += pos_total * multiplier
        for sector, count in sector_hits.items():
            direction = _score_sector_direction(
                sector,
                negative_score=neg_total,
                positive_score=pos_total,
            )
            if direction == "negative":
                sector_negative[sector] += count
            elif direction == "positive":
                sector_positive[sector] += count
            else:
                sector_neutral[sector] += count
        if title and title not in top_titles and len(top_titles) < MAX_TOP_TITLES:
            top_titles.append(title)
        if institution and institution not in institutions and len(institutions) < MAX_TOP_TITLES:
            institutions.append(institution)
        reviewed_items.append(
            {
                "title": title,
                "date": date_text,
                "type": info_type,
                "institution": institution,
                "negative_hits": dict(neg_hits),
                "neutral_hits": dict(neutral_hits),
                "positive_hits": dict(pos_hits),
                "sector_hits": dict(sector_hits),
            }
        )

    negative_score = round(sum(negative_flags.values()) * weight, 2)
    neutral_score = round(sum(neutral_flags.values()) * weight, 2)
    positive_score = round(sum(positive_flags.values()) * weight, 2)
    return {
        "scenario": scenario["name"],
        "label": scenario["label"],
        "query": scenario["query"],
        "weight": weight,
        "item_count": len(reviewed_items),
        "negative_score": negative_score,
        "neutral_score": neutral_score,
        "positive_score": positive_score,
        "negative_flags": dict(negative_flags.most_common(MAX_FLAG_COUNT)),
        "neutral_flags": dict(neutral_flags.most_common(MAX_FLAG_COUNT)),
        "positive_flags": dict(positive_flags.most_common(MAX_FLAG_COUNT)),
        "negative_sectors": [key for key, _ in sector_negative.most_common(8)],
        "neutral_sectors": [key for key, _ in sector_neutral.most_common(8)],
        "positive_sectors": [key for key, _ in sector_positive.most_common(8)],
        "horizon_scores": {
            key: {
                side: round(value, 2)
                for side, value in values.items()
            }
            for key, values in horizon_scores.items()
        },
        "top_titles": top_titles,
        "institutions": institutions,
        "items": reviewed_items,
    }


def _merge_sector_rankings(summaries: list[dict], key: str) -> list[str]:
    counter: Counter = Counter()
    for summary in summaries:
        for idx, sector in enumerate(summary.get(key, []) or []):
            counter[sector] += max(1, 5 - idx)
    return [sector for sector, _ in counter.most_common(8)]


def _resolve_sector_views(summaries: list[dict]) -> tuple[list[str], list[str], list[str]]:
    counters = {
        "negative": Counter(),
        "neutral": Counter(),
        "positive": Counter(),
    }
    for summary in summaries:
        weight = _safe_float(summary.get("weight", 1.0), 1.0)
        for side, key in (
            ("negative", "negative_sectors"),
            ("neutral", "neutral_sectors"),
            ("positive", "positive_sectors"),
        ):
            for idx, sector in enumerate(summary.get(key, []) or []):
                sector_name = str(sector).strip()
                if not sector_name:
                    continue
                counters[side][sector_name] += max(1, 5 - idx) * weight

    classified = {
        "negative": [],
        "neutral": [],
        "positive": [],
    }
    all_sectors = set(counters["negative"]) | set(counters["neutral"]) | set(counters["positive"])
    for sector in all_sectors:
        scores = {
            "negative": round(float(counters["negative"].get(sector, 0.0)), 4),
            "neutral": round(float(counters["neutral"].get(sector, 0.0)), 4),
            "positive": round(float(counters["positive"].get(sector, 0.0)), 4),
        }
        best_side, best_score = max(scores.items(), key=lambda item: item[1])
        if best_score <= 0:
            continue
        runner_up = sorted(scores.values(), reverse=True)[1]
        # Conflicted sectors are treated as neutral rather than appearing in both focus and avoid.
        if best_side != "neutral" and runner_up > 0 and best_score - runner_up <= 1.0:
            best_side = "neutral"
            best_score = max(best_score, scores["neutral"])
        classified[best_side].append((sector, best_score))

    def _finalize(side: str) -> list[str]:
        ranked = sorted(classified[side], key=lambda item: (-item[1], item[0]))
        return [sector for sector, _ in ranked[:8]]

    return _finalize("negative"), _finalize("neutral"), _finalize("positive")


def _top_counter_keys(items: list[dict], key: str) -> list[str]:
    counter: Counter = Counter()
    for summary in items:
        counter.update(summary.get(key, {}) or {})
    return [flag for flag, _ in counter.most_common(MAX_FLAG_COUNT)]


def _collect_horizon_totals(summaries: list[dict]) -> dict[str, dict[str, float]]:
    totals = {
        "short": {"negative": 0.0, "neutral": 0.0, "positive": 0.0},
        "mid": {"negative": 0.0, "neutral": 0.0, "positive": 0.0},
        "long": {"negative": 0.0, "neutral": 0.0, "positive": 0.0},
    }
    for summary in summaries:
        horizon_scores = summary.get("horizon_scores", {}) if isinstance(summary.get("horizon_scores", {}), dict) else {}
        for horizon in totals.keys():
            current = horizon_scores.get(horizon, {}) if isinstance(horizon_scores.get(horizon, {}), dict) else {}
            for side in ("negative", "neutral", "positive"):
                totals[horizon][side] += _safe_float(current.get(side, 0.0), 0.0)
    for horizon in totals.keys():
        for side in ("negative", "neutral", "positive"):
            totals[horizon][side] = round(totals[horizon][side], 2)
    return totals


def _derive_a_share_bias(
    *,
    total_negative: float,
    total_neutral: float,
    total_positive: float,
    negative_sectors: list[str],
    positive_sectors: list[str],
    short_term_bias: str = "",
) -> str:
    gap = total_negative - total_positive
    positive_gap = total_positive - total_negative
    short_term_bias = str(short_term_bias or "").strip().lower()
    if positive_gap >= 5 and len(positive_sectors) >= 3:
        return "broad_supportive"
    if short_term_bias == "broad_positive":
        if positive_gap >= 1.5 and len(positive_sectors) >= 2:
            return "broad_supportive"
        if positive_gap >= -1.0 and len(positive_sectors) >= 2 and len(negative_sectors) <= len(positive_sectors):
            return "selective_supportive"
    if gap >= 5 or (gap >= 3.5 and len(negative_sectors) >= 3 and short_term_bias not in {"selective_positive", "broad_positive"}):
        return "risk_off"
    if short_term_bias == "selective_positive" and positive_gap >= 0.5 and len(positive_sectors) >= 2:
        return "selective_supportive"
    if total_positive >= total_negative + 2 and len(positive_sectors) >= 2:
        return "selective_supportive"
    if total_neutral >= abs(gap) or abs(gap) < 2.5:
        return "neutral"
    return "neutral"


def _derive_risk_level(*, total_negative: float, total_positive: float, scenario_count: int) -> str:
    gap = total_negative - total_positive
    if gap >= 8 or (gap >= 5 and scenario_count >= 3):
        return "high"
    if gap >= 4:
        return "medium"
    if gap <= -3:
        return "low"
    return "neutral"


def _build_recommended_actions(*, a_share_bias: str, negative_sectors: list[str], neutral_sectors: list[str], positive_sectors: list[str], horizon_assessment: dict | None = None) -> dict:
    horizon_assessment = horizon_assessment if isinstance(horizon_assessment, dict) else {}
    short_term = horizon_assessment.get("short_term", {}) if isinstance(horizon_assessment.get("short_term", {}), dict) else {}
    short_term_bias = str(short_term.get("bias", "")).strip().lower()
    focus_watch = positive_sectors[:5]
    avoid = negative_sectors[:6]
    neutral_watch = neutral_sectors[:6] or focus_watch[:4]
    opening_gate_bias = {
        "risk_off": "defensive",
        "neutral": "neutral",
        "selective_supportive": "balanced",
        "broad_supportive": "supportive",
    }.get(a_share_bias, "neutral")
    if opening_gate_bias == "balanced" and short_term_bias == "broad_positive" and not avoid:
        opening_gate_bias = "supportive"
    allow_broad_rebound = a_share_bias == "broad_supportive" or (
        a_share_bias == "selective_supportive" and short_term_bias == "broad_positive" and not avoid
    )
    selective_only = not allow_broad_rebound
    return {
        "opening_gate_bias": opening_gate_bias,
        "broad_rebound_allowed": allow_broad_rebound,
        "allow_only_selective_rebound": selective_only,
        "avoid_sectors": avoid,
        "focus_sectors": focus_watch,
        "neutral_watch_sectors": neutral_watch,
        "suggested_response": {
            "risk_off": "先防守，优先降风险暴露。",
            "neutral": "先观察，不脑补普反，只做结构性验证。",
            "selective_supportive": "允许围绕强分支做结构性进攻，但不宜全面扩张。",
            "broad_supportive": "可提升风险偏好，但仍需尊重 09:31 流动性确认。",
        }.get(a_share_bias, "先观察。"),
        "opening_priority": short_term.get("summary", ""),
    }


def _build_impact_summary(*, a_share_bias: str, risk_level: str, negative_sectors: list[str], neutral_sectors: list[str], positive_sectors: list[str], top_flags: list[str], horizon_assessment: dict | None = None) -> str:
    if a_share_bias == "risk_off":
        left = "外部情报偏风险收缩"
    elif a_share_bias == "neutral":
        left = "外部情报偏中性分化"
    elif a_share_bias == "broad_supportive":
        left = "外部情报偏全面支持风险偏好"
    else:
        left = "外部情报偏结构性支持风险偏好"
    neg_text = f"承压板块集中在 {', '.join(negative_sectors[:4])}" if negative_sectors else "未识别到集中承压板块"
    neutral_text = f"中性观察板块包括 {', '.join(neutral_sectors[:4])}" if neutral_sectors else "中性板块信号不强"
    pos_text = f"相对受益或可观察板块包括 {', '.join(positive_sectors[:4])}" if positive_sectors else "暂未识别到明确受益板块"
    flag_text = f"核心触发词: {', '.join(top_flags[:4])}" if top_flags else "未提炼出稳定触发词"
    short_summary = ""
    if isinstance(horizon_assessment, dict):
        short_summary = str(((horizon_assessment.get("short_term", {}) or {}).get("summary") or "")).strip()
    return f"{left}，风险等级 {risk_level}。{neg_text}；{neutral_text}；{pos_text}；{flag_text}。{short_summary}".strip()


def build_external_market_review(*, run_id: str = "", task_name: str = "", trigger_slot: str = "") -> dict:
    if not os.environ.get("MX_APIKEY", "").strip():
        raise RuntimeError("MX_APIKEY 未配置")
    if not MX_SEARCH_SKILL.exists():
        raise RuntimeError(f"mx-search skill 缺失: {MX_SEARCH_SKILL}")

    mx_search_mod = _load_module(MX_SEARCH_SKILL, "mx_search_runtime")
    client = mx_search_mod.MXSearch()
    scenarios = _resolve_scenarios(trigger_slot=trigger_slot)

    scenario_summaries: list[dict] = []
    errors: list[dict] = []
    raw_items: dict[str, dict] = {}

    for scenario in scenarios:
        query = scenario["query"]
        try:
            result = client.search(query)
            raw_items[scenario["name"]] = result
            items = _extract_items(result)
            scenario_summaries.append(_summarize_scenario(items, scenario=scenario))
        except Exception as exc:
            errors.append({"scenario": scenario["name"], "query": query, "error": str(exc)})
            scenario_summaries.append(
                {
                    "scenario": scenario["name"],
                    "label": scenario["label"],
                    "query": query,
                    "weight": _safe_float(scenario.get("weight", 1.0), 1.0),
                    "item_count": 0,
                    "negative_score": 0.0,
                    "neutral_score": 0.0,
                    "positive_score": 0.0,
                    "negative_flags": {},
                    "neutral_flags": {},
                    "positive_flags": {},
                    "negative_sectors": [],
                    "neutral_sectors": [],
                    "positive_sectors": [],
                    "horizon_scores": {
                        "short": {"negative": 0.0, "neutral": 0.0, "positive": 0.0},
                        "mid": {"negative": 0.0, "neutral": 0.0, "positive": 0.0},
                        "long": {"negative": 0.0, "neutral": 0.0, "positive": 0.0},
                    },
                    "top_titles": [],
                    "institutions": [],
                    "items": [],
                }
            )

    total_negative = round(sum(_safe_float(item.get("negative_score", 0.0), 0.0) for item in scenario_summaries), 2)
    total_neutral = round(sum(_safe_float(item.get("neutral_score", 0.0), 0.0) for item in scenario_summaries), 2)
    total_positive = round(sum(_safe_float(item.get("positive_score", 0.0), 0.0) for item in scenario_summaries), 2)
    negative_sectors, neutral_sectors, positive_sectors = _resolve_sector_views(scenario_summaries)
    negative_flags = _top_counter_keys(scenario_summaries, "negative_flags")
    neutral_flags = _top_counter_keys(scenario_summaries, "neutral_flags")
    positive_flags = _top_counter_keys(scenario_summaries, "positive_flags")
    horizon_totals = _collect_horizon_totals(scenario_summaries)
    horizon_assessment = {
        "short_term": _build_term_outlook(
            label="短期",
            negative_score=horizon_totals["short"]["negative"],
            neutral_score=horizon_totals["short"]["neutral"],
            positive_score=horizon_totals["short"]["positive"],
            negative_sectors=negative_sectors,
            neutral_sectors=neutral_sectors,
            positive_sectors=positive_sectors,
            flags=negative_flags or neutral_flags or positive_flags,
        ),
        "mid_term": _build_term_outlook(
            label="中期",
            negative_score=horizon_totals["mid"]["negative"],
            neutral_score=horizon_totals["mid"]["neutral"],
            positive_score=horizon_totals["mid"]["positive"],
            negative_sectors=negative_sectors,
            neutral_sectors=neutral_sectors,
            positive_sectors=positive_sectors,
            flags=negative_flags or neutral_flags or positive_flags,
        ),
        "long_term": _build_term_outlook(
            label="长期",
            negative_score=horizon_totals["long"]["negative"],
            neutral_score=horizon_totals["long"]["neutral"],
            positive_score=horizon_totals["long"]["positive"],
            negative_sectors=negative_sectors,
            neutral_sectors=neutral_sectors,
            positive_sectors=positive_sectors,
            flags=positive_flags or neutral_flags or negative_flags,
        ),
    }
    short_flow_monitor = _build_short_flow_monitor(scenario_summaries)
    opening_anchor_break_monitor = _build_opening_anchor_break_monitor()
    weekend_digest_monitor = _build_weekend_digest_monitor(scenario_summaries)
    a_share_bias = _derive_a_share_bias(
        total_negative=total_negative,
        total_neutral=total_neutral,
        total_positive=total_positive,
        negative_sectors=negative_sectors,
        positive_sectors=positive_sectors,
        short_term_bias=str(((horizon_assessment.get("short_term", {}) or {}).get("bias") or "")),
    )
    if weekend_digest_monitor.get("bias") == "negative" and a_share_bias == "neutral":
        a_share_bias = "risk_off"
    elif weekend_digest_monitor.get("bias") == "positive" and a_share_bias == "neutral":
        a_share_bias = "selective_supportive"
    if opening_anchor_break_monitor.get("pressure_level") == "high":
        short_term_bias = str(((horizon_assessment.get("short_term", {}) or {}).get("bias") or "")).strip().lower()
        if short_term_bias == "broad_positive" and a_share_bias == "broad_supportive":
            a_share_bias = "selective_supportive"
        elif a_share_bias in {"neutral", "selective_supportive"}:
            a_share_bias = "risk_off"
    risk_level = _derive_risk_level(
        total_negative=total_negative,
        total_positive=total_positive,
        scenario_count=sum(1 for item in scenario_summaries if _safe_float(item.get("negative_score", 0.0), 0.0) > 0),
    )
    confidence = round(min(0.92, 0.45 + 0.08 * sum(1 for item in scenario_summaries if item.get("item_count", 0) > 0)), 2)
    recommended_actions = _build_recommended_actions(
        a_share_bias=a_share_bias,
        negative_sectors=negative_sectors,
        neutral_sectors=neutral_sectors,
        positive_sectors=positive_sectors,
        horizon_assessment=horizon_assessment,
    )
    impact_summary = _build_impact_summary(
        a_share_bias=a_share_bias,
        risk_level=risk_level,
        negative_sectors=negative_sectors,
        neutral_sectors=neutral_sectors,
        positive_sectors=positive_sectors,
        top_flags=negative_flags or neutral_flags or positive_flags,
        horizon_assessment=horizon_assessment,
    )

    headline = ""
    for summary in scenario_summaries:
        titles = summary.get("top_titles", [])
        if titles:
            headline = titles[0]
            break

    payload = {
        "generated_at": _now_str(),
        "trade_date": _today_str(),
        "run_id": run_id,
        "task_name": task_name,
        "trigger_slot": trigger_slot,
        "window_tag": _derive_window_tag(trigger_slot),
        "source": "mx-search",
        "available": True,
        "risk_level": risk_level,
        "a_share_bias": a_share_bias,
        "confidence": confidence,
        "headline": headline,
        "impact_summary": impact_summary,
        "negative_flags": negative_flags,
        "neutral_flags": neutral_flags,
        "positive_flags": positive_flags,
        "negative_sectors": negative_sectors,
        "neutral_sectors": neutral_sectors,
        "positive_sectors": positive_sectors,
        "horizon_assessment": horizon_assessment,
        "short_flow_monitor": short_flow_monitor,
        "opening_anchor_break_monitor": opening_anchor_break_monitor,
        "weekend_digest_monitor": weekend_digest_monitor,
        "recommended_actions": recommended_actions,
        "scenario_summaries": scenario_summaries,
        "raw_query_count": len(scenarios),
        "success_query_count": sum(1 for item in scenario_summaries if item.get("item_count", 0) > 0),
        "error_count": len(errors),
        "errors": errors,
        "notes": [
            "本文件用于 09:31 开盘门控、午盘判断和尾盘复检，不直接替代盘中价格与成交事实。",
            "资讯结论先做风险偏好与板块映射，再由 09:31 流动性与盘中事实进行验证。",
            "新闻联播、政策、地缘、商品与外盘被统一纳入一套情报过滤器，而不是各看各的。",
            "情报判断默认拆成短期、中期、长期三层，避免把所有资讯都混成一个即时结论。",
            "做空资金动向单独监控，不与普通新闻情绪混算。",
            "09:31 核心锚股破位与否会作为空头验证层，优先看前期领涨锚股和大成交权重锚。",
            "每周一早盘额外启用周末汇总块，单独评估周末政策、联播、地缘与产业催化对周一开盘的影响。",
        ],
    }
    snapshot = {
        "generated_at": payload["generated_at"],
        "trade_date": payload["trade_date"],
        "window_tag": payload["window_tag"],
        "risk_level": risk_level,
        "a_share_bias": a_share_bias,
        "negative_sectors": negative_sectors,
        "neutral_sectors": neutral_sectors,
        "positive_sectors": positive_sectors,
        "confidence": confidence,
        "impact_summary": impact_summary,
    }
    _write_json_atomic(OUTPUT_JSON, payload)
    _append_jsonl(OUTPUT_HISTORY, snapshot)
    _write_csv(
        OUTPUT_CSV,
        [
            {
                "scenario": item.get("scenario", ""),
                "label": item.get("label", ""),
                "query": item.get("query", ""),
                "item_count": item.get("item_count", 0),
                "negative_score": item.get("negative_score", 0.0),
                "positive_score": item.get("positive_score", 0.0),
                "negative_sectors": ",".join(item.get("negative_sectors", [])),
                "positive_sectors": ",".join(item.get("positive_sectors", [])),
                "top_title": (item.get("top_titles", []) or [""])[0],
            }
            for item in scenario_summaries
        ],
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="盘前外部资讯复核器")
    parser.add_argument("--run-id", default="", help="自动化运行ID")
    parser.add_argument("--task-name", default="", help="任务名")
    parser.add_argument("--trigger-slot", default="", help="触发时段")
    args = parser.parse_args()
    payload = build_external_market_review(
        run_id=args.run_id,
        task_name=args.task_name,
        trigger_slot=args.trigger_slot,
    )
    print(
        json.dumps(
            {
                "generated_at": payload["generated_at"],
                "trade_date": payload["trade_date"],
                "window_tag": payload["window_tag"],
                "risk_level": payload["risk_level"],
                "a_share_bias": payload["a_share_bias"],
                "negative_sectors": payload["negative_sectors"],
                "positive_sectors": payload["positive_sectors"],
                "output_json": str(OUTPUT_JSON),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
