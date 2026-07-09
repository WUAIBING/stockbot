from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
WORKBUDDY_DATA_DIR = (
    Path(os.environ.get("TLFZ_WORKBUDDY_DATA_DIR", "")).resolve()
    if os.environ.get("TLFZ_WORKBUDDY_DATA_DIR", "").strip()
    else ROOT / "workbuddy" / "a-share-analyst"
)

POOL_DIR = ROOT / "workbuddy_pool"
DISTILL_DIR = ROOT / "workbuddy_distill"

COMBINED_TEMPLATE_REGISTRY = DISTILL_DIR / "templates" / "combined_template_registry.json"
WINDOW_PROFILE_FILE = DISTILL_DIR / "artifacts" / "distill_window_profile_latest.json"
CANDIDATE_POOL_FILE = POOL_DIR / "workbuddy_candidate_pool_latest.json"
FORWARD_EVAL_GLOB = "workbuddy_candidate_close_eval_*.json"

LOCAL_REVIEW_FILE = WORKBUDDY_DATA_DIR / "workbuddy_local_review_latest.json"
LOCAL_SUMMARY_FILE = WORKBUDDY_DATA_DIR / "workbuddy_local_account_summary_latest.json"
STATE_ROUTING_FILE = DISTILL_DIR / "artifacts" / "market_state_t1_prediction_latest.json"
STATE_SCORECARD_FILE = DISTILL_DIR / "artifacts" / "template_state_scorecard_latest.json"

REPORT_JSON_FILE = POOL_DIR / "workbuddy_distill_daily_review_latest.json"
REPORT_MD_FILE = POOL_DIR / "workbuddy_distill_daily_review_latest.md"
SHADOW_ROUTE_JSON_FILE = POOL_DIR / "workbuddy_distill_shadow_route_latest.json"
SHADOW_ROUTE_MD_FILE = POOL_DIR / "workbuddy_distill_shadow_route_latest.md"
SHADOW_ROUTE_LEDGER_JSON_FILE = POOL_DIR / "workbuddy_distill_shadow_route_ledger.json"
SHADOW_ROUTE_LEDGER_CSV_FILE = POOL_DIR / "workbuddy_distill_shadow_route_ledger.csv"
SHADOW_ROUTE_LEDGER_MD_FILE = POOL_DIR / "workbuddy_distill_shadow_route_ledger.md"


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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            with path.open("r", encoding=encoding) as f:
                payload = json.load(f)
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                rows = payload.get("rows", [])
                if isinstance(rows, list):
                    return [item for item in rows if isinstance(item, dict)]
        except Exception:
            continue
    return []


def _write_csv_atomic(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})
    tmp.replace(path)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except Exception:
        return default


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _latest_forward_eval_file() -> Path | None:
    files = sorted(POOL_DIR.glob(FORWARD_EVAL_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _dated_report_json_path(trade_date: str, evaluation_trade_date: str) -> Path:
    return POOL_DIR / f"workbuddy_distill_daily_review_{trade_date}_to_{evaluation_trade_date}.json"


def _dated_report_md_path(trade_date: str, evaluation_trade_date: str) -> Path:
    return POOL_DIR / f"workbuddy_distill_daily_review_{trade_date}_to_{evaluation_trade_date}.md"


def _build_template_section(registry: dict[str, Any], window_profile: dict[str, Any], pool_payload: dict[str, Any]) -> dict[str, Any]:
    champion = registry.get("champion_template", {}) if isinstance(registry, dict) else {}
    champion_metrics = champion.get("metrics", {}) if isinstance(champion, dict) else {}
    pool_window = (pool_payload.get("source_distill_registry", {}) or {}).get("window", {})
    effective_window = pool_window if isinstance(pool_window, dict) and pool_window else window_profile
    return {
        "template_name": str(registry.get("champion_template_name", "")).strip(),
        "candidate_win_rate": round(_safe_float(champion_metrics.get("candidate_win_rate"), 0.0), 4),
        "candidate_win_rate_pct": round(_safe_float(champion_metrics.get("candidate_win_rate"), 0.0) * 100, 2),
        "candidate_avg_return_pct": round(_safe_float(champion_metrics.get("candidate_avg_return"), 0.0), 4),
        "top50_hit_rate": round(_safe_float(champion_metrics.get("top50_hit_rate"), 0.0), 4),
        "front_shift_score": round(_safe_float(champion_metrics.get("front_shift_score"), 0.0), 4),
        "pool_trade_date": str(pool_payload.get("trade_date", "")).strip(),
        "window": {
            "mode": str(effective_window.get("mode", "")).strip(),
            "selected_trade_date_count": _safe_int(effective_window.get("selected_trade_date_count"), 0),
            "core_trade_date_count": _safe_int(effective_window.get("core_trade_date_count"), 0),
            "buffer_trade_date_count": _safe_int(effective_window.get("buffer_trade_date_count"), 0),
            "buffer_trade_dates": list(effective_window.get("buffer_trade_dates", []) or []),
        },
        "selected_count": _safe_int(pool_payload.get("selected_count"), 0),
    }


def _build_forward_section(forward_eval: dict[str, Any], forward_eval_file: Path | None) -> dict[str, Any]:
    if not forward_eval:
        return {
            "available": False,
            "detail_file": str(forward_eval_file) if forward_eval_file else "",
        }
    return {
        "available": True,
        "detail_file": str(forward_eval_file) if forward_eval_file else "",
        "source_trade_date": str(forward_eval.get("source_trade_date", "")).strip(),
        "evaluation_trade_date": str(forward_eval.get("evaluation_trade_date", "")).strip(),
        "candidate_count": _safe_int(forward_eval.get("candidate_count"), 0),
        "win_count": _safe_int(forward_eval.get("win_count"), 0),
        "loss_count": _safe_int(forward_eval.get("loss_count"), 0),
        "win_rate_pct": round(_safe_float(forward_eval.get("win_rate_pct"), 0.0), 2),
        "avg_return_pct": round(_safe_float(forward_eval.get("avg_return_pct"), 0.0), 4),
        "weighted_return_pct": round(_safe_float(forward_eval.get("weighted_return_pct"), 0.0), 4),
        "top_candidates": [
            {
                "code": str(item.get("code", "")).strip(),
                "name": str(item.get("name", "")).strip(),
                "selection_rank": _safe_int(item.get("selection_rank"), 0),
                "return_pct_close_to_close": round(_safe_float(item.get("return_pct_close_to_close"), 0.0), 4),
            }
            for item in list(forward_eval.get("candidates", []) or [])[:5]
            if isinstance(item, dict)
        ],
    }


def _build_execution_section(local_review: dict[str, Any], local_summary: dict[str, Any]) -> dict[str, Any]:
    if not local_review:
        account_snapshot = local_summary.get("account_snapshot", {}) if isinstance(local_summary, dict) else {}
        return {
            "available": False,
            "detail_file": str(LOCAL_REVIEW_FILE),
            "holding_count": _safe_int(account_snapshot.get("holding_count"), 0),
        }

    source_alignment = local_review.get("source_alignment", {}) if isinstance(local_review, dict) else {}
    execution_health = local_review.get("execution_health", {}) if isinstance(local_review, dict) else {}
    trade_quality = local_review.get("trade_quality", {}) if isinstance(local_review, dict) else {}
    return {
        "available": True,
        "detail_file": str(LOCAL_REVIEW_FILE),
        "review_verdict": str(local_review.get("review_verdict", "")).strip(),
        "learning_sample_ready": bool(local_review.get("learning_sample_ready", False)),
        "trade_date": str(local_review.get("trade_date", "")).strip(),
        "source_trade_date": str(source_alignment.get("source_trade_date", "")).strip(),
        "source_status": str(source_alignment.get("source_status", "")).strip(),
        "today_order_count": _safe_int(execution_health.get("today_order_count"), 0),
        "today_buy_count": _safe_int(execution_health.get("today_buy_count"), 0),
        "today_sell_count": _safe_int(execution_health.get("today_sell_count"), 0),
        "closed_trade_count": _safe_int(trade_quality.get("closed_trade_count"), 0),
        "closed_trade_win_rate_pct": round(_safe_float(trade_quality.get("closed_trade_win_rate_pct"), 0.0), 2),
        "avg_closed_return_pct": round(_safe_float(trade_quality.get("avg_closed_return_pct"), 0.0), 4),
        "realized_pnl": round(_safe_float(trade_quality.get("realized_pnl"), 0.0), 2),
        "blockers": list(execution_health.get("blockers", []) or []),
    }


def _build_alignment(template_section: dict[str, Any], forward_section: dict[str, Any], execution_section: dict[str, Any]) -> dict[str, Any]:
    pool_trade_date = str(template_section.get("pool_trade_date", "")).strip()
    forward_source_trade_date = str(forward_section.get("source_trade_date", "")).strip()
    execution_source_trade_date = str(execution_section.get("source_trade_date", "")).strip()
    execution_trade_date = str(execution_section.get("trade_date", "")).strip()
    notes: list[str] = []

    if forward_section.get("available"):
        if forward_source_trade_date == pool_trade_date:
            notes.append("前向验证口径与当前候选池 trade_date 对齐。")
        else:
            notes.append("前向验证口径与当前候选池未对齐，需要注意是否是历史批次。")
    else:
        notes.append("暂缺昨日候选池到今日收盘的前向验证文件。")

    if execution_section.get("available"):
        if execution_source_trade_date == forward_source_trade_date and execution_source_trade_date:
            notes.append("实际执行样本与前向验证使用同一 source_trade_date。")
        elif execution_source_trade_date:
            notes.append("实际执行样本与前向验证不是同一批 source_trade_date，不能直接横向比较。")
        else:
            notes.append("实际执行样本缺少 source_trade_date。")
        if _safe_int(execution_section.get("closed_trade_count"), 0) <= 0:
            notes.append("实际执行层尚无已平仓样本，胜率和盈利率暂不具备统计意义。")
    else:
        notes.append("暂缺实际执行复核文件。")

    return {
        "pool_trade_date": pool_trade_date,
        "forward_source_trade_date": forward_source_trade_date,
        "forward_evaluation_trade_date": str(forward_section.get("evaluation_trade_date", "")).strip(),
        "execution_source_trade_date": execution_source_trade_date,
        "execution_trade_date": execution_trade_date,
        "notes": notes,
    }


def _build_gap_section(template_section: dict[str, Any], forward_section: dict[str, Any], execution_section: dict[str, Any]) -> dict[str, Any]:
    template_win_rate_pct = _safe_float(template_section.get("candidate_win_rate_pct"), 0.0)
    template_avg_return_pct = _safe_float(template_section.get("candidate_avg_return_pct"), 0.0)
    forward_win_rate_pct = _safe_float(forward_section.get("win_rate_pct"), 0.0) if forward_section.get("available") else None
    forward_avg_return_pct = _safe_float(forward_section.get("avg_return_pct"), 0.0) if forward_section.get("available") else None
    execution_win_rate_pct = (
        _safe_float(execution_section.get("closed_trade_win_rate_pct"), 0.0)
        if execution_section.get("available") and _safe_int(execution_section.get("closed_trade_count"), 0) > 0
        else None
    )
    execution_avg_return_pct = (
        _safe_float(execution_section.get("avg_closed_return_pct"), 0.0)
        if execution_section.get("available") and _safe_int(execution_section.get("closed_trade_count"), 0) > 0
        else None
    )
    return {
        "template_vs_forward_win_rate_pct_gap": round(forward_win_rate_pct - template_win_rate_pct, 2) if forward_win_rate_pct is not None else None,
        "template_vs_forward_avg_return_pct_gap": round(forward_avg_return_pct - template_avg_return_pct, 4) if forward_avg_return_pct is not None else None,
        "forward_vs_execution_win_rate_pct_gap": round(execution_win_rate_pct - forward_win_rate_pct, 2) if execution_win_rate_pct is not None and forward_win_rate_pct is not None else None,
        "forward_vs_execution_avg_return_pct_gap": round(execution_avg_return_pct - forward_avg_return_pct, 4) if execution_avg_return_pct is not None and forward_avg_return_pct is not None else None,
    }


def _build_shadow_route_section(state_payload: dict[str, Any], scorecard_payload: dict[str, Any]) -> dict[str, Any]:
    if not state_payload:
        return {
            "available": False,
            "detail_file": str(STATE_ROUTING_FILE),
            "scorecard_file": str(STATE_SCORECARD_FILE),
        }

    prediction = state_payload.get("prediction", {}) if isinstance(state_payload, dict) else {}
    routing = state_payload.get("v32_calibrated_routing", {}) if isinstance(state_payload, dict) else {}
    backtest = (state_payload.get("v32_calibrated_backtest", {}) or {}).get("summary", {})
    advantage_report = (scorecard_payload.get("template_advantage_report", {}) or {}).get("rows", [])
    state_scorecards = scorecard_payload.get("template_state_scorecards", {}) if isinstance(scorecard_payload, dict) else {}

    current_state = str(prediction.get("current_state", "")).strip()
    current_advantage = next(
        (row for row in advantage_report if isinstance(row, dict) and str(row.get("state", "")).strip() == current_state),
        {},
    )
    champion_state = ((state_scorecards.get("champion", {}) or {}).get(current_state, {}) if current_state else {})
    attack_state = ((state_scorecards.get("attack", {}) or {}).get(current_state, {}) if current_state else {})

    return {
        "available": bool(routing),
        "detail_file": str(STATE_ROUTING_FILE),
        "scorecard_file": str(STATE_SCORECARD_FILE),
        "current_state": current_state,
        "predicted_state": str(prediction.get("predicted_state", "")).strip(),
        "predicted_probability": round(_safe_float(prediction.get("predicted_probability"), 0.0), 4),
        "route_action": str(routing.get("route_action", "")).strip(),
        "primary_template": str(routing.get("primary_template", "")).strip(),
        "shadow_template": str(routing.get("shadow_template", "")).strip(),
        "direct_confidence": round(_safe_float(routing.get("direct_confidence"), 0.0), 4),
        "attack_probability": round(_safe_float(routing.get("attack_probability"), 0.0), 4),
        "summary_reason": str(routing.get("summary_reason", "")).strip(),
        "base_route_action": str(routing.get("base_route_action", "")).strip(),
        "calibration_adjustments": [
            {
                "side": str(item.get("side", "")).strip(),
                "delta": round(_safe_float(item.get("delta"), 0.0), 4),
                "reason": str(item.get("reason", "")).strip(),
            }
            for item in list(routing.get("calibration_adjustments", []) or [])
            if isinstance(item, dict)
        ],
        "backtest_summary": {
            "sample_days": _safe_int(backtest.get("sample_days"), 0),
            "route_win_rate_vs_champion": round(_safe_float(backtest.get("route_win_rate_vs_champion"), 0.0), 4),
            "route_avg_return": round(_safe_float(backtest.get("route_avg_return"), 0.0), 4),
            "champion_avg_return": round(_safe_float(backtest.get("champion_avg_return"), 0.0), 4),
            "route_return_edge_vs_champion": round(_safe_float(backtest.get("route_return_edge_vs_champion"), 0.0), 4),
        },
        "current_state_advantage": {
            "sample_reliability": str(current_advantage.get("sample_reliability", "")).strip(),
            "preferred_template": str(current_advantage.get("preferred_template", "")).strip(),
            "attack_state_score_gap": round(_safe_float(current_advantage.get("attack_state_score_gap"), 0.0), 4),
            "attack_return_gap": round(_safe_float(current_advantage.get("attack_return_gap"), 0.0), 4),
            "attack_front_shift_gap": round(_safe_float(current_advantage.get("attack_front_shift_gap"), 0.0), 4),
            "attack_hit_day_gap": round(_safe_float(current_advantage.get("attack_hit_day_gap"), 0.0), 4),
        },
        "current_state_scorecards": {
            "champion": {
                "days": _safe_int(champion_state.get("days"), 0),
                "candidate_win_rate": round(_safe_float(champion_state.get("candidate_win_rate"), 0.0), 4),
                "candidate_avg_return": round(_safe_float(champion_state.get("candidate_avg_return"), 0.0), 4),
                "top50_hit_rate": round(_safe_float(champion_state.get("top50_hit_rate"), 0.0), 4),
                "front_shift_score": round(_safe_float(champion_state.get("front_shift_score"), 0.0), 4),
                "hit_day_rate": round(_safe_float(champion_state.get("hit_day_rate"), 0.0), 4),
                "state_score": round(_safe_float(champion_state.get("state_score"), 0.0), 4),
            },
            "attack": {
                "days": _safe_int(attack_state.get("days"), 0),
                "candidate_win_rate": round(_safe_float(attack_state.get("candidate_win_rate"), 0.0), 4),
                "candidate_avg_return": round(_safe_float(attack_state.get("candidate_avg_return"), 0.0), 4),
                "top50_hit_rate": round(_safe_float(attack_state.get("top50_hit_rate"), 0.0), 4),
                "front_shift_score": round(_safe_float(attack_state.get("front_shift_score"), 0.0), 4),
                "hit_day_rate": round(_safe_float(attack_state.get("hit_day_rate"), 0.0), 4),
                "state_score": round(_safe_float(attack_state.get("state_score"), 0.0), 4),
            },
        },
    }


def _build_shadow_route_markdown(section: dict[str, Any]) -> str:
    lines = [
        "# Workbuddy Distill Shadow Route",
        "",
        f"- 生成时间: `{_now_str()}`",
    ]
    if not section.get("available"):
        lines.append("- 暂无可用的 V3.2 路由产物")
        return "\n".join(lines) + "\n"

    backtest = section.get("backtest_summary", {})
    advantage = section.get("current_state_advantage", {})
    scorecards = section.get("current_state_scorecards", {})
    lines.extend(
        [
            f"- 当前状态: `{section.get('current_state', '')}`",
            f"- 路由动作: `{section.get('route_action', '')}`",
            f"- 主模板: `{section.get('primary_template', '')}`",
            f"- 影子模板: `{section.get('shadow_template', '')}`",
            f"- 进攻概率: `{round(_safe_float(section.get('attack_probability'), 0.0) * 100, 2)}%`",
            f"- 直接置信度: `{section.get('direct_confidence', 0.0)}`",
            f"- 说明: {section.get('summary_reason', '')}",
            "",
            "## 历史边际",
            f"- 回测样本: `{backtest.get('sample_days', 0)}`",
            f"- 路由胜过冠军比例: `{round(_safe_float(backtest.get('route_win_rate_vs_champion'), 0.0) * 100, 2)}%`",
            f"- 路由平均收益: `{backtest.get('route_avg_return', 0.0)}%`",
            f"- 冠军平均收益: `{backtest.get('champion_avg_return', 0.0)}%`",
            f"- 路由相对冠军收益边际: `{backtest.get('route_return_edge_vs_champion', 0.0)}%`",
            "",
            "## 当前状态模板优势",
            f"- 状态可靠性: `{advantage.get('sample_reliability', '')}`",
            f"- 当前状态偏好模板: `{advantage.get('preferred_template', '')}`",
            f"- Attack state score gap: `{advantage.get('attack_state_score_gap', 0.0)}`",
            f"- Attack return gap: `{advantage.get('attack_return_gap', 0.0)}%`",
            f"- Attack front shift gap: `{advantage.get('attack_front_shift_gap', 0.0)}`",
            f"- Attack hit day gap: `{advantage.get('attack_hit_day_gap', 0.0)}`",
            "",
            "## 当前状态成绩单",
            f"- Champion: `score={((scorecards.get('champion', {}) or {}).get('state_score', 0.0))}` / `avg_return={((scorecards.get('champion', {}) or {}).get('candidate_avg_return', 0.0))}%` / `hit_day={((scorecards.get('champion', {}) or {}).get('hit_day_rate', 0.0))}`",
            f"- Attack: `score={((scorecards.get('attack', {}) or {}).get('state_score', 0.0))}` / `avg_return={((scorecards.get('attack', {}) or {}).get('candidate_avg_return', 0.0))}%` / `hit_day={((scorecards.get('attack', {}) or {}).get('hit_day_rate', 0.0))}`",
        ]
    )
    adjustments = list(section.get("calibration_adjustments", []) or [])
    if adjustments:
        lines.extend(["", "## 校准动作"])
        for item in adjustments:
            lines.append(f"- `{item.get('side', '')}` `{item.get('delta', 0.0)}`: {item.get('reason', '')}")
    return "\n".join(lines) + "\n"


def _shadow_route_ledger_key(source_trade_date: str) -> str:
    return source_trade_date or "unknown"


def _build_shadow_route_ledger_row(report: dict[str, Any]) -> dict[str, Any] | None:
    shadow_route = report.get("shadow_route_review", {}) if isinstance(report, dict) else {}
    if not shadow_route.get("available"):
        return None

    template_section = report.get("template_metrics", {}) if isinstance(report, dict) else {}
    forward_section = report.get("forward_validation", {}) if isinstance(report, dict) else {}
    execution_section = report.get("actual_execution", {}) if isinstance(report, dict) else {}
    alignment = report.get("alignment", {}) if isinstance(report, dict) else {}
    backtest = shadow_route.get("backtest_summary", {}) if isinstance(shadow_route, dict) else {}
    advantage = shadow_route.get("current_state_advantage", {}) if isinstance(shadow_route, dict) else {}
    adjustments = list(shadow_route.get("calibration_adjustments", []) or [])

    source_trade_date = str(template_section.get("pool_trade_date", "")).strip()
    if not source_trade_date:
        return None

    execution_closed_count = _safe_int(execution_section.get("closed_trade_count"), 0)
    forward_available = bool(forward_section.get("available")) and (
        str(forward_section.get("source_trade_date", "")).strip() == source_trade_date
    )
    execution_available = bool(execution_section.get("available")) and (
        str(execution_section.get("source_trade_date", "")).strip() == source_trade_date
    )
    if execution_available and execution_closed_count > 0:
        outcome_status = "execution_closed_ready"
    elif forward_available:
        outcome_status = "forward_eval_ready"
    else:
        outcome_status = "pending_eval"

    return {
        "ledger_key": _shadow_route_ledger_key(source_trade_date),
        "source_trade_date": source_trade_date,
        "snapshot_trade_date": str(report.get("trade_date", "")).strip(),
        "snapshot_generated_at": str(report.get("generated_at", "")).strip(),
        "route_action": str(shadow_route.get("route_action", "")).strip(),
        "base_route_action": str(shadow_route.get("base_route_action", "")).strip(),
        "current_state": str(shadow_route.get("current_state", "")).strip(),
        "predicted_state": str(shadow_route.get("predicted_state", "")).strip(),
        "primary_template": str(shadow_route.get("primary_template", "")).strip(),
        "shadow_template": str(shadow_route.get("shadow_template", "")).strip(),
        "attack_probability_pct": round(_safe_float(shadow_route.get("attack_probability"), 0.0) * 100, 2),
        "direct_confidence": round(_safe_float(shadow_route.get("direct_confidence"), 0.0), 4),
        "summary_reason": str(shadow_route.get("summary_reason", "")).strip(),
        "sample_reliability": str(advantage.get("sample_reliability", "")).strip(),
        "preferred_template": str(advantage.get("preferred_template", "")).strip(),
        "attack_state_score_gap": round(_safe_float(advantage.get("attack_state_score_gap"), 0.0), 4),
        "attack_return_gap_pct": round(_safe_float(advantage.get("attack_return_gap"), 0.0), 4),
        "attack_front_shift_gap": round(_safe_float(advantage.get("attack_front_shift_gap"), 0.0), 4),
        "attack_hit_day_gap": round(_safe_float(advantage.get("attack_hit_day_gap"), 0.0), 4),
        "route_backtest_sample_days": _safe_int(backtest.get("sample_days"), 0),
        "route_win_rate_vs_champion_pct": round(_safe_float(backtest.get("route_win_rate_vs_champion"), 0.0) * 100, 2),
        "route_avg_return_pct": round(_safe_float(backtest.get("route_avg_return"), 0.0), 4),
        "champion_avg_return_pct": round(_safe_float(backtest.get("champion_avg_return"), 0.0), 4),
        "route_return_edge_vs_champion_pct": round(_safe_float(backtest.get("route_return_edge_vs_champion"), 0.0), 4),
        "pool_selected_count": _safe_int(template_section.get("selected_count"), 0),
        "alignment_notes": " | ".join(str(note).strip() for note in list(alignment.get("notes", []) or []) if str(note).strip()),
        "calibration_notes": " | ".join(
            f"{str(item.get('side', '')).strip()} {round(_safe_float(item.get('delta'), 0.0), 4)} {str(item.get('reason', '')).strip()}"
            for item in adjustments
            if isinstance(item, dict)
        ),
        "forward_eval_available": forward_available,
        "forward_evaluation_trade_date": (
            str(forward_section.get("evaluation_trade_date", "")).strip() if forward_available else ""
        ),
        "forward_candidate_count": _safe_int(forward_section.get("candidate_count"), 0) if forward_available else 0,
        "forward_win_rate_pct": round(_safe_float(forward_section.get("win_rate_pct"), 0.0), 2) if forward_available else "",
        "forward_avg_return_pct": round(_safe_float(forward_section.get("avg_return_pct"), 0.0), 4) if forward_available else "",
        "forward_weighted_return_pct": round(_safe_float(forward_section.get("weighted_return_pct"), 0.0), 4) if forward_available else "",
        "execution_available": execution_available,
        "execution_trade_date": str(execution_section.get("trade_date", "")).strip() if execution_available else "",
        "execution_review_verdict": str(execution_section.get("review_verdict", "")).strip() if execution_available else "",
        "execution_learning_sample_ready": bool(execution_section.get("learning_sample_ready", False)) if execution_available else False,
        "execution_closed_trade_count": execution_closed_count if execution_available else 0,
        "execution_closed_trade_win_rate_pct": (
            round(_safe_float(execution_section.get("closed_trade_win_rate_pct"), 0.0), 2) if execution_available and execution_closed_count > 0 else ""
        ),
        "execution_avg_closed_return_pct": (
            round(_safe_float(execution_section.get("avg_closed_return_pct"), 0.0), 4) if execution_available and execution_closed_count > 0 else ""
        ),
        "execution_realized_pnl": (
            round(_safe_float(execution_section.get("realized_pnl"), 0.0), 2) if execution_available else ""
        ),
        "outcome_status": outcome_status,
    }


def _upsert_shadow_route_ledger(existing_rows: list[dict[str, Any]], report: dict[str, Any]) -> list[dict[str, Any]]:
    row = _build_shadow_route_ledger_row(report)
    if not row:
        return existing_rows

    key = str(row.get("ledger_key", "")).strip()
    if not key:
        return existing_rows

    rows_by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in existing_rows:
        item_key = str(item.get("ledger_key", "")).strip() or _shadow_route_ledger_key(str(item.get("source_trade_date", "")).strip())
        if not item_key:
            continue
        normalized = dict(item)
        normalized["ledger_key"] = item_key
        if item_key not in rows_by_key:
            order.append(item_key)
        rows_by_key[item_key] = normalized

    merged = dict(rows_by_key.get(key, {}))
    merged.update(row)
    rows_by_key[key] = merged
    if key not in order:
        order.append(key)

    result = [rows_by_key[item_key] for item_key in order]
    result.sort(
        key=lambda item: (
            str(item.get("source_trade_date", "")).strip(),
            str(item.get("snapshot_generated_at", "")).strip(),
        ),
        reverse=True,
    )
    return result


def _shadow_route_ledger_fieldnames() -> list[str]:
    return [
        "ledger_key",
        "source_trade_date",
        "snapshot_trade_date",
        "snapshot_generated_at",
        "route_action",
        "base_route_action",
        "current_state",
        "predicted_state",
        "primary_template",
        "shadow_template",
        "attack_probability_pct",
        "direct_confidence",
        "summary_reason",
        "sample_reliability",
        "preferred_template",
        "attack_state_score_gap",
        "attack_return_gap_pct",
        "attack_front_shift_gap",
        "attack_hit_day_gap",
        "route_backtest_sample_days",
        "route_win_rate_vs_champion_pct",
        "route_avg_return_pct",
        "champion_avg_return_pct",
        "route_return_edge_vs_champion_pct",
        "pool_selected_count",
        "alignment_notes",
        "calibration_notes",
        "forward_eval_available",
        "forward_evaluation_trade_date",
        "forward_candidate_count",
        "forward_win_rate_pct",
        "forward_avg_return_pct",
        "forward_weighted_return_pct",
        "execution_available",
        "execution_trade_date",
        "execution_review_verdict",
        "execution_learning_sample_ready",
        "execution_closed_trade_count",
        "execution_closed_trade_win_rate_pct",
        "execution_avg_closed_return_pct",
        "execution_realized_pnl",
        "outcome_status",
    ]


def _build_shadow_route_ledger_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Workbuddy Distill Shadow Route Ledger",
        "",
        f"- 生成时间: `{_now_str()}`",
        f"- 样本条数: `{len(rows)}`",
        "",
        "| Source | Snapshot | Action | State | Attack Prob | Forward Eval | Execution | Edge vs Champion |",
        "| --- | --- | --- | --- | ---: | --- | --- | ---: |",
    ]
    if not rows:
        lines.append("| - | - | - | - | - | - | - | - |")
        return "\n".join(lines) + "\n"

    for row in rows[:30]:
        source_trade_date = str(row.get("source_trade_date", "")).strip() or "-"
        snapshot_trade_date = str(row.get("snapshot_trade_date", "")).strip() or "-"
        route_action = str(row.get("route_action", "")).strip() or "-"
        state_text = str(row.get("current_state", "")).strip() or "-"
        attack_prob = row.get("attack_probability_pct", "")
        forward_eval = (
            f"{row.get('forward_evaluation_trade_date', '')} / {row.get('forward_avg_return_pct', '')}%"
            if row.get("forward_eval_available")
            else "pending"
        )
        if row.get("execution_available"):
            execution_avg_return = str(row.get("execution_avg_closed_return_pct", "")).strip()
            execution_text = (
                f"{row.get('execution_review_verdict', '')} / {execution_avg_return}%"
                if execution_avg_return
                else f"{row.get('execution_review_verdict', '')} / pending"
            )
        else:
            execution_text = "pending"
        edge = row.get("route_return_edge_vs_champion_pct", "")
        lines.append(
            f"| `{source_trade_date}` | `{snapshot_trade_date}` | `{route_action}` | `{state_text}` | `{attack_prob}` | `{forward_eval}` | `{execution_text}` | `{edge}` |"
        )

    latest = rows[0]
    lines.extend(
        [
            "",
            "## 最新样本",
            f"- source_trade_date: `{latest.get('source_trade_date', '')}`",
            f"- 当前动作: `{latest.get('route_action', '')}`",
            f"- 当前状态: `{latest.get('current_state', '')}`",
            f"- 主模板: `{latest.get('primary_template', '')}`",
            f"- 影子模板: `{latest.get('shadow_template', '')}`",
            f"- 前向状态: `{latest.get('outcome_status', '')}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _build_markdown(report: dict[str, Any]) -> str:
    template_section = report["template_metrics"]
    forward_section = report["forward_validation"]
    execution_section = report["actual_execution"]
    alignment = report["alignment"]
    gaps = report["gaps"]
    shadow_route = report.get("shadow_route_review", {}) if isinstance(report, dict) else {}

    lines = [
        "# Workbuddy Distill Daily Review",
        "",
        f"- 生成时间: `{report['generated_at']}`",
        f"- 候选池日期: `{alignment['pool_trade_date']}`",
        "",
        "## 模板值",
        f"- 模板名: `{template_section['template_name']}`",
        f"- 候选胜率: `{template_section['candidate_win_rate_pct']}%`",
        f"- 候选平均收益: `{template_section['candidate_avg_return_pct']}%`",
        f"- Top50 命中率: `{template_section['top50_hit_rate']}`",
        f"- Front Shift: `{template_section['front_shift_score']}`",
        f"- 窗口: `{template_section['window']['mode']}` / `20+{template_section['window']['buffer_trade_date_count']}`",
        "",
        "## 前向验证",
    ]
    if forward_section.get("available"):
        lines.extend(
            [
                f"- 来源日期: `{forward_section['source_trade_date']}` -> 验证日期: `{forward_section['evaluation_trade_date']}`",
                f"- 样本数: `{forward_section['candidate_count']}`",
                f"- 胜率: `{forward_section['win_rate_pct']}%`",
                f"- 平均收益: `{forward_section['avg_return_pct']}%`",
                f"- 加权收益: `{forward_section['weighted_return_pct']}%`",
            ]
        )
    else:
        lines.append("- 暂无前向验证文件")

    lines.append("")
    lines.append("## 实际执行")
    if execution_section.get("available"):
        lines.extend(
            [
                f"- 复核结论: `{execution_section['review_verdict']}`",
                f"- source_trade_date: `{execution_section['source_trade_date']}`",
                f"- 当日订单: `{execution_section['today_order_count']}` / 买 `{execution_section['today_buy_count']}` / 卖 `{execution_section['today_sell_count']}`",
                f"- 已平仓样本: `{execution_section['closed_trade_count']}`",
                f"- 已平仓胜率: `{execution_section['closed_trade_win_rate_pct']}%`",
                f"- 已平仓平均收益: `{execution_section['avg_closed_return_pct']}%`",
                f"- 已实现盈亏: `¥{execution_section['realized_pnl']}`",
            ]
        )
    else:
        lines.append("- 暂无实际执行复核文件")

    lines.extend(
        [
            "",
            "## 偏差",
            f"- 模板 vs 前向 胜率偏差: `{gaps['template_vs_forward_win_rate_pct_gap']}`",
            f"- 模板 vs 前向 平均收益偏差: `{gaps['template_vs_forward_avg_return_pct_gap']}`",
            f"- 前向 vs 实际执行 胜率偏差: `{gaps['forward_vs_execution_win_rate_pct_gap']}`",
            f"- 前向 vs 实际执行 平均收益偏差: `{gaps['forward_vs_execution_avg_return_pct_gap']}`",
            "",
            "## 对齐说明",
        ]
    )
    for note in alignment.get("notes", []):
        lines.append(f"- {note}")

    if shadow_route.get("available"):
        backtest = shadow_route.get("backtest_summary", {})
        advantage = shadow_route.get("current_state_advantage", {})
        lines.extend(
            [
                "",
                "## V3.2 影子路由",
                f"- 当前状态: `{shadow_route.get('current_state', '')}`",
                f"- 路由动作: `{shadow_route.get('route_action', '')}`",
                f"- 主模板: `{shadow_route.get('primary_template', '')}`",
                f"- 影子模板: `{shadow_route.get('shadow_template', '')}`",
                f"- 进攻概率: `{round(_safe_float(shadow_route.get('attack_probability'), 0.0) * 100, 2)}%`",
                f"- 历史收益边际: `{backtest.get('route_return_edge_vs_champion', 0.0)}%`",
                f"- 当前状态可靠性: `{advantage.get('sample_reliability', '')}` / 偏好 `{advantage.get('preferred_template', '')}`",
                f"- 说明: {shadow_route.get('summary_reason', '')}",
            ]
        )

    if forward_section.get("available") and forward_section.get("top_candidates"):
        lines.extend(["", "## 前向样本预览"])
        for item in forward_section["top_candidates"]:
            lines.append(
                f"- `{item['selection_rank']}` `{item['code']}` `{item['name']}` `{item['return_pct_close_to_close']}%`"
            )

    return "\n".join(lines) + "\n"


def build_report(*, run_id: str = "", task_name: str = "", trigger_slot: str = "") -> dict[str, Any]:
    registry = _read_json(COMBINED_TEMPLATE_REGISTRY)
    window_profile = _read_json(WINDOW_PROFILE_FILE)
    pool_payload = _read_json(CANDIDATE_POOL_FILE)
    forward_eval_file = _latest_forward_eval_file()
    forward_eval = _read_json(forward_eval_file) if forward_eval_file else {}
    local_review = _read_json(LOCAL_REVIEW_FILE)
    local_summary = _read_json(LOCAL_SUMMARY_FILE)
    state_payload = _read_json(STATE_ROUTING_FILE)
    scorecard_payload = _read_json(STATE_SCORECARD_FILE)

    template_section = _build_template_section(registry, window_profile, pool_payload)
    forward_section = _build_forward_section(forward_eval, forward_eval_file)
    execution_section = _build_execution_section(local_review, local_summary)
    alignment = _build_alignment(template_section, forward_section, execution_section)
    gaps = _build_gap_section(template_section, forward_section, execution_section)
    shadow_route_section = _build_shadow_route_section(state_payload, scorecard_payload)

    report = {
        "generated_at": _now_str(),
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "run_id": run_id,
        "task_name": task_name,
        "trigger_slot": trigger_slot,
        "template_metrics": template_section,
        "forward_validation": forward_section,
        "actual_execution": execution_section,
        "alignment": alignment,
        "gaps": gaps,
        "shadow_route_review": shadow_route_section,
        "files": {
            "combined_template_registry": str(COMBINED_TEMPLATE_REGISTRY),
            "window_profile_file": str(WINDOW_PROFILE_FILE),
            "candidate_pool_file": str(CANDIDATE_POOL_FILE),
            "forward_eval_file": str(forward_eval_file) if forward_eval_file else "",
            "local_review_file": str(LOCAL_REVIEW_FILE),
            "local_summary_file": str(LOCAL_SUMMARY_FILE),
            "state_routing_file": str(STATE_ROUTING_FILE),
            "state_scorecard_file": str(STATE_SCORECARD_FILE),
            "report_json_file": str(REPORT_JSON_FILE),
            "report_md_file": str(REPORT_MD_FILE),
            "shadow_route_json_file": str(SHADOW_ROUTE_JSON_FILE),
            "shadow_route_md_file": str(SHADOW_ROUTE_MD_FILE),
            "shadow_route_ledger_json_file": str(SHADOW_ROUTE_LEDGER_JSON_FILE),
            "shadow_route_ledger_csv_file": str(SHADOW_ROUTE_LEDGER_CSV_FILE),
            "shadow_route_ledger_md_file": str(SHADOW_ROUTE_LEDGER_MD_FILE),
        },
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build workbuddy distill daily review report")
    parser.add_argument("--run-id", default="", help="自动化运行ID")
    parser.add_argument("--task-name", default="", help="任务名")
    parser.add_argument("--trigger-slot", default="", help="触发时段")
    args = parser.parse_args()

    report = build_report(run_id=args.run_id, task_name=args.task_name, trigger_slot=args.trigger_slot)
    _write_json_atomic(REPORT_JSON_FILE, report)
    _write_text_atomic(REPORT_MD_FILE, _build_markdown(report))
    _write_json_atomic(SHADOW_ROUTE_JSON_FILE, report.get("shadow_route_review", {}))
    _write_text_atomic(SHADOW_ROUTE_MD_FILE, _build_shadow_route_markdown(report.get("shadow_route_review", {})))

    shadow_route_ledger_rows = _upsert_shadow_route_ledger(_read_json_list(SHADOW_ROUTE_LEDGER_JSON_FILE), report)
    _write_json_atomic(SHADOW_ROUTE_LEDGER_JSON_FILE, {"rows": shadow_route_ledger_rows})
    _write_csv_atomic(SHADOW_ROUTE_LEDGER_CSV_FILE, shadow_route_ledger_rows, _shadow_route_ledger_fieldnames())
    _write_text_atomic(SHADOW_ROUTE_LEDGER_MD_FILE, _build_shadow_route_ledger_markdown(shadow_route_ledger_rows))

    source_trade_date = str(report["forward_validation"].get("source_trade_date", "")).strip()
    evaluation_trade_date = str(report["forward_validation"].get("evaluation_trade_date", "")).strip()
    if source_trade_date and evaluation_trade_date:
        _write_json_atomic(_dated_report_json_path(source_trade_date, evaluation_trade_date), report)
        _write_text_atomic(_dated_report_md_path(source_trade_date, evaluation_trade_date), _build_markdown(report))

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
