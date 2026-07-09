#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate self-contained HTML dashboards for long-term curve observation.

Outputs:
  - curve_observatory/charts/html/dashboard_latest.html
  - curve_observatory/charts/html/dashboard_<yyyy>_w<ww>.html
"""

from __future__ import annotations

import argparse
import csv
import html
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from package_paths import DATA_DIR


CURVE_ROOT = DATA_DIR / "curve_observatory"
CURVE_DATA_DIR = CURVE_ROOT / "data"
CURVE_HTML_DIR = CURVE_ROOT / "charts" / "html"


def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def _fnum(value, default: float = 0.0) -> float:
    try:
        text = str(value).strip()
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


def _inum(value, default: int = 0) -> int:
    try:
        text = str(value).strip()
        return int(float(text)) if text else default
    except (TypeError, ValueError):
        return default


def _parse_date(value: str) -> Optional[datetime]:
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _filter_rows_by_date(rows: List[Dict[str, str]], as_of_date: str = "") -> List[Dict[str, str]]:
    if not as_of_date:
        return rows
    filtered: List[Dict[str, str]] = []
    for row in rows:
        date_key = str(row.get("date", "")).strip()
        if date_key and date_key <= as_of_date:
            filtered.append(row)
    return filtered


def _fmt_money(value: float) -> str:
    return f"{value:,.2f}"


def _fmt_pct(value: float) -> str:
    return f"{value:.2f}%"


def _escape(value: object) -> str:
    return html.escape(str(value))


def _series_bounds(series_list: Sequence[Sequence[Optional[float]]]) -> Tuple[float, float]:
    values: List[float] = []
    for series in series_list:
        values.extend([float(v) for v in series if v is not None])
    if not values:
        return 0.0, 1.0
    low, high = min(values), max(values)
    if math.isclose(low, high):
        spread = abs(low) * 0.05 or 1.0
        low -= spread
        high += spread
    else:
        pad = (high - low) * 0.08
        low -= pad
        high += pad
    return low, high


def _line_chart_svg(
    labels: Sequence[str],
    series: Sequence[Tuple[str, str, Sequence[Optional[float]]]],
    *,
    width: int = 980,
    height: int = 280,
    y_formatter=lambda v: f"{v:.2f}",
) -> str:
    if not labels:
        return "<div class='empty-box'>暂无数据</div>"
    left_pad, right_pad, top_pad, bottom_pad = 56, 16, 18, 36
    plot_w = width - left_pad - right_pad
    plot_h = height - top_pad - bottom_pad
    y_min, y_max = _series_bounds([values for _, _, values in series])

    def x_pos(index: int) -> float:
        if len(labels) == 1:
            return left_pad + plot_w / 2
        return left_pad + plot_w * index / (len(labels) - 1)

    def y_pos(value: float) -> float:
        return top_pad + (y_max - value) * plot_h / (y_max - y_min)

    y_ticks = []
    for i in range(5):
        value = y_min + (y_max - y_min) * i / 4
        y = top_pad + plot_h - plot_h * i / 4
        y_ticks.append((value, y))

    parts = [
        f"<svg viewBox='0 0 {width} {height}' class='chart-svg' role='img'>",
        f"<rect x='0' y='0' width='{width}' height='{height}' fill='#0f172a' rx='12'></rect>",
    ]
    for tick_value, tick_y in y_ticks:
        parts.append(
            f"<line x1='{left_pad}' y1='{tick_y:.2f}' x2='{width-right_pad}' y2='{tick_y:.2f}' "
            "stroke='#1e293b' stroke-width='1'></line>"
        )
        parts.append(
            f"<text x='{left_pad-8}' y='{tick_y+4:.2f}' text-anchor='end' class='axis-label'>"
            f"{_escape(y_formatter(tick_value))}</text>"
        )

    for idx, label in enumerate(labels):
        x = x_pos(idx)
        parts.append(
            f"<text x='{x:.2f}' y='{height-10}' text-anchor='middle' class='axis-label'>{_escape(label)}</text>"
        )

    legend_x = left_pad
    for name, color, _ in series:
        parts.append(f"<rect x='{legend_x}' y='8' width='12' height='12' rx='2' fill='{color}'></rect>")
        parts.append(
            f"<text x='{legend_x + 18}' y='18' class='legend-label'>{_escape(name)}</text>"
        )
        legend_x += max(100, 20 + len(name) * 16)

    for _, color, values in series:
        points: List[str] = []
        for idx, value in enumerate(values):
            if value is None:
                continue
            points.append(f"{x_pos(idx):.2f},{y_pos(float(value)):.2f}")
        if len(points) >= 2:
            parts.append(
                f"<polyline fill='none' stroke='{color}' stroke-width='3' stroke-linejoin='round' "
                f"stroke-linecap='round' points='{' '.join(points)}'></polyline>"
            )
        for idx, value in enumerate(values):
            if value is None:
                continue
            x, y = x_pos(idx), y_pos(float(value))
            parts.append(f"<circle cx='{x:.2f}' cy='{y:.2f}' r='3.5' fill='{color}'></circle>")

    parts.append("</svg>")
    return "".join(parts)


def _bar_chart_svg(
    labels: Sequence[str],
    series: Sequence[Tuple[str, str, Sequence[float]]],
    *,
    width: int = 980,
    height: int = 280,
    y_formatter=lambda v: f"{v:.0f}",
) -> str:
    if not labels:
        return "<div class='empty-box'>暂无数据</div>"
    left_pad, right_pad, top_pad, bottom_pad = 56, 16, 18, 36
    plot_w = width - left_pad - right_pad
    plot_h = height - top_pad - bottom_pad
    _, y_max = _series_bounds([values for _, _, values in series])
    y_min = 0.0
    n_groups = len(labels)
    n_series = max(1, len(series))
    group_w = plot_w / max(1, n_groups)
    bar_w = max(8, min(28, group_w / (n_series + 1.2)))

    def y_pos(value: float) -> float:
        if math.isclose(y_max, y_min):
            return top_pad + plot_h / 2
        return top_pad + (y_max - value) * plot_h / (y_max - y_min)

    parts = [
        f"<svg viewBox='0 0 {width} {height}' class='chart-svg' role='img'>",
        f"<rect x='0' y='0' width='{width}' height='{height}' fill='#0f172a' rx='12'></rect>",
    ]
    for i in range(5):
        value = y_min + (y_max - y_min) * i / 4
        y = top_pad + plot_h - plot_h * i / 4
        parts.append(
            f"<line x1='{left_pad}' y1='{y:.2f}' x2='{width-right_pad}' y2='{y:.2f}' "
            "stroke='#1e293b' stroke-width='1'></line>"
        )
        parts.append(
            f"<text x='{left_pad-8}' y='{y+4:.2f}' text-anchor='end' class='axis-label'>{_escape(y_formatter(value))}</text>"
        )
    legend_x = left_pad
    for name, color, _ in series:
        parts.append(f"<rect x='{legend_x}' y='8' width='12' height='12' rx='2' fill='{color}'></rect>")
        parts.append(f"<text x='{legend_x+18}' y='18' class='legend-label'>{_escape(name)}</text>")
        legend_x += max(100, 20 + len(name) * 16)

    for i, label in enumerate(labels):
        gx = left_pad + group_w * i + group_w / 2
        parts.append(
            f"<text x='{gx:.2f}' y='{height-10}' text-anchor='middle' class='axis-label'>{_escape(label)}</text>"
        )
        total_width = n_series * bar_w + max(0, n_series - 1) * 6
        start_x = gx - total_width / 2
        for j, (_, color, values) in enumerate(series):
            value = float(values[i]) if i < len(values) else 0.0
            y = y_pos(value)
            h = top_pad + plot_h - y
            x = start_x + j * (bar_w + 6)
            parts.append(
                f"<rect x='{x:.2f}' y='{y:.2f}' width='{bar_w:.2f}' height='{h:.2f}' rx='4' fill='{color}'></rect>"
            )
    parts.append("</svg>")
    return "".join(parts)


def _card(title: str, value: str, subtitle: str = "") -> str:
    sub = f"<div class='card-subtitle'>{_escape(subtitle)}</div>" if subtitle else ""
    return (
        "<div class='metric-card'>"
        f"<div class='card-title'>{_escape(title)}</div>"
        f"<div class='card-value'>{_escape(value)}</div>"
        f"{sub}"
        "</div>"
    )


def _table_html(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    head = "".join(f"<th>{_escape(h)}</th>" for h in headers)
    body_rows = []
    for row in rows:
        body_rows.append("<tr>" + "".join(f"<td>{_escape(v)}</td>" for v in row) + "</tr>")
    return (
        "<table class='data-table'>"
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
    )


def _load_data(as_of_date: str = "") -> Dict[str, List[Dict[str, str]]]:
    return {
        "nav": _filter_rows_by_date(_read_csv(CURVE_DATA_DIR / "curve_nav_daily.csv"), as_of_date),
        "realized": _filter_rows_by_date(_read_csv(CURVE_DATA_DIR / "curve_realized_pnl_daily.csv"), as_of_date),
        "trade": _filter_rows_by_date(_read_csv(CURVE_DATA_DIR / "curve_trade_success_daily.csv"), as_of_date),
        "benchmark": _filter_rows_by_date(_read_csv(CURVE_DATA_DIR / "curve_benchmark_daily.csv"), as_of_date),
        "learning": _filter_rows_by_date(_read_csv(CURVE_DATA_DIR / "curve_learning_readiness_daily.csv"), as_of_date),
    }


def _build_dashboard_html(data: Dict[str, List[Dict[str, str]]]) -> Tuple[str, str]:
    nav_rows = data["nav"]
    realized_rows = data["realized"]
    trade_rows = data["trade"]
    bench_rows = data["benchmark"]
    learning_rows = data["learning"]
    if not nav_rows:
        raise RuntimeError("curve_nav_daily.csv is empty; generate CSVs first.")

    latest_nav = nav_rows[-1]
    latest_dt = _parse_date(str(latest_nav.get("date", ""))) or datetime.now()
    iso_year, iso_week, _ = latest_dt.isocalendar()
    archive_name = f"dashboard_{iso_year}_w{iso_week:02d}.html"

    latest_assets = _fnum(latest_nav.get("total_assets"))
    latest_realized = _fnum(latest_nav.get("realized_pnl"))
    latest_floating = _fnum(latest_nav.get("floating_pnl"))
    latest_win_rate = _fnum(latest_nav.get("win_rate_pct"))
    latest_avg_return = _fnum(latest_nav.get("avg_return_pct"))

    week_realized_rows = []
    for row in realized_rows:
        dt = _parse_date(str(row.get("date", "")))
        if dt and dt.isocalendar()[:2] == (iso_year, iso_week):
            week_realized_rows.append(row)
    weekly_improvement = 0.0
    if week_realized_rows:
        weekly_improvement = _fnum(week_realized_rows[-1].get("cumulative_closed_pnl")) - _fnum(
            week_realized_rows[0].get("cumulative_closed_pnl")
        ) + _fnum(week_realized_rows[0].get("daily_closed_pnl"))

    latest_trade = trade_rows[-1] if trade_rows else {}
    latest_bench = bench_rows[-1] if bench_rows else {}
    latest_learning = learning_rows[-1] if learning_rows else {}

    labels = [str(r["date"])[5:] for r in nav_rows]
    assets_series = [_fnum(r.get("total_assets")) for r in nav_rows]
    realized_series = [_fnum(r.get("realized_pnl")) for r in nav_rows]
    floating_series = [_fnum(r.get("floating_pnl")) for r in nav_rows]

    result_chart = _line_chart_svg(
        labels,
        [
            ("总资产", "#38bdf8", assets_series),
            ("累计已实现盈亏", "#22c55e", realized_series),
            ("浮动盈亏", "#f59e0b", floating_series),
        ],
        y_formatter=lambda v: f"{v/10000:.1f}w" if abs(v) >= 10000 else f"{v:.0f}",
    )

    realized_labels = [str(r["date"])[5:] for r in realized_rows]
    realized_bar = _bar_chart_svg(
        realized_labels,
        [
            ("日度新增平仓收益", "#22c55e", [_fnum(r.get("daily_closed_pnl")) for r in realized_rows]),
        ],
        y_formatter=lambda v: f"{v/10000:.1f}w" if abs(v) >= 10000 else f"{v:.0f}",
    )

    trade_labels = [str(r["date"])[5:] for r in trade_rows]
    execution_line = _line_chart_svg(
        trade_labels,
        [
            ("总成功率", "#38bdf8", [_fnum(r.get("success_rate_pct")) for r in trade_rows]),
            ("买单成功率", "#22c55e", [_fnum(r.get("buy_success_rate_pct")) for r in trade_rows]),
            ("卖单成功率", "#f59e0b", [_fnum(r.get("sell_success_rate_pct")) for r in trade_rows]),
        ],
        y_formatter=lambda v: f"{v:.0f}%",
    )
    execution_bar = _bar_chart_svg(
        trade_labels,
        [
            ("112 次数", "#ef4444", [_fnum(r.get("rate_limit_112_count")) for r in trade_rows]),
            ("501 次数", "#a855f7", [_fnum(r.get("insufficient_501_count")) for r in trade_rows]),
        ],
        y_formatter=lambda v: f"{v:.0f}",
    )

    bench_labels = [str(r["date"])[5:] for r in bench_rows]
    benchmark_chart = _line_chart_svg(
        bench_labels,
        [
            ("策略净值", "#38bdf8", [_fnum(r.get("strategy_nav_norm"), 0.0) for r in bench_rows]),
            ("中证1000", "#22c55e", [(_fnum(r.get("csi1000_norm")) if str(r.get("csi1000_norm", "")).strip() else None) for r in bench_rows]),
            ("创业板指", "#f59e0b", [(_fnum(r.get("chinext_norm")) if str(r.get("chinext_norm", "")).strip() else None) for r in bench_rows]),
        ],
        y_formatter=lambda v: f"{v:.3f}",
    )

    learning_labels = [str(r["date"])[5:] for r in learning_rows]
    learning_flow_chart = _line_chart_svg(
        learning_labels,
        [
            ("平仓样本", "#38bdf8", [_fnum(r.get("recent_closed_trades")) for r in learning_rows]),
            ("粗匹配样本", "#f59e0b", [_fnum(r.get("gross_matched_trades")) for r in learning_rows]),
            ("可学习样本", "#22c55e", [_fnum(r.get("eligible_matched_trades")) for r in learning_rows]),
        ],
        y_formatter=lambda v: f"{v:.0f}",
    )
    learning_rate_chart = _line_chart_svg(
        learning_labels,
        [
            ("粗匹配率", "#f59e0b", [_fnum(r.get("gross_match_rate_pct")) for r in learning_rows]),
            ("可学习率", "#22c55e", [_fnum(r.get("eligible_rate_pct")) for r in learning_rows]),
            ("匹配后净样本率", "#a855f7", [_fnum(r.get("clean_after_match_rate_pct")) for r in learning_rows]),
        ],
        y_formatter=lambda v: f"{v:.0f}%",
    )

    metrics_html = "".join(
        [
            _card("最新总资产", _fmt_money(latest_assets), str(latest_nav.get("date", ""))),
            _card("累计已实现盈亏", _fmt_money(latest_realized), "落袋结果"),
            _card("当前浮动盈亏", _fmt_money(latest_floating), "持仓账面"),
            _card("本周已实现改善值", _fmt_money(weekly_improvement), f"{iso_year} W{iso_week:02d}"),
            _card("最新胜率", _fmt_pct(latest_win_rate), "closed 口径"),
            _card("最新平均收益", _fmt_pct(latest_avg_return), "closed 口径"),
            _card(
                "最新总成功率",
                _fmt_pct(_fnum(latest_trade.get("success_rate_pct"))),
                f"112={_inum(latest_trade.get('rate_limit_112_count'))}",
            ),
            _card(
                "基准抓取状态",
                str(latest_bench.get("fetch_status", "unavailable")),
                "主基准 + 辅基准",
            ),
            _card(
                "学习准备度",
                str(_inum(latest_learning.get("eligible_matched_trades"))),
                f"可学习 / 平仓={_inum(latest_learning.get('recent_closed_trades'))}",
            ),
            _card(
                "当前学习状态",
                str(latest_learning.get("learning_status", "n/a")),
                str(latest_learning.get("top_block_reason_1", "")),
            ),
        ]
    )

    realized_table = _table_html(
        ["日期", "新增平仓笔数", "日度新增平仓收益", "累计已实现盈亏", "平均单笔收益率"],
        [
            [
                row.get("date", ""),
                row.get("closed_trade_count", ""),
                _fmt_money(_fnum(row.get("daily_closed_pnl"))),
                _fmt_money(_fnum(row.get("cumulative_closed_pnl"))),
                _fmt_pct(_fnum(row.get("avg_closed_return_pct"))),
            ]
            for row in realized_rows
        ],
    )
    trade_table = _table_html(
        ["日期", "总请求", "总成功率", "买单成功率", "卖单成功率", "112", "501"],
        [
            [
                row.get("date", ""),
                row.get("total_requests", ""),
                _fmt_pct(_fnum(row.get("success_rate_pct"))),
                _fmt_pct(_fnum(row.get("buy_success_rate_pct"))),
                _fmt_pct(_fnum(row.get("sell_success_rate_pct"))),
                row.get("rate_limit_112_count", ""),
                row.get("insufficient_501_count", ""),
            ]
            for row in trade_rows
        ],
    )
    learning_table = _table_html(
        ["日期", "平仓样本", "粗匹配", "可学习", "粗匹配率", "可学习率", "主要阻断原因", "次要阻断原因", "学习状态"],
        [
            [
                row.get("date", ""),
                row.get("recent_closed_trades", ""),
                row.get("gross_matched_trades", ""),
                row.get("eligible_matched_trades", ""),
                _fmt_pct(_fnum(row.get("gross_match_rate_pct"))),
                _fmt_pct(_fnum(row.get("eligible_rate_pct"))),
                row.get("top_block_reason_1", ""),
                row.get("top_block_reason_2", ""),
                row.get("learning_status", ""),
            ]
            for row in learning_rows
        ],
    )

    latest_generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_text = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WorkBuddy Curve Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #020617;
      --panel: #0f172a;
      --panel-2: #111827;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --border: #1e293b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: linear-gradient(180deg, #020617 0%, #0f172a 100%);
      color: var(--text);
    }}
    .container {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 24px 20px 48px;
    }}
    .hero {{
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      gap: 16px;
      margin-bottom: 18px;
      flex-wrap: wrap;
    }}
    .hero h1 {{
      margin: 0 0 6px;
      font-size: 30px;
      line-height: 1.2;
    }}
    .hero p {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
      margin-bottom: 20px;
    }}
    .metric-card, .panel {{
      background: rgba(15, 23, 42, 0.88);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: 0 10px 30px rgba(2, 6, 23, 0.28);
    }}
    .metric-card {{
      padding: 16px;
      min-height: 108px;
    }}
    .card-title {{
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 10px;
    }}
    .card-value {{
      font-size: 28px;
      font-weight: 700;
      line-height: 1.15;
      color: #f8fafc;
    }}
    .card-subtitle {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }}
    .section-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
      margin-bottom: 18px;
    }}
    .panel {{
      padding: 18px;
    }}
    .panel h2 {{
      margin: 0 0 8px;
      font-size: 20px;
    }}
    .panel p {{
      margin: 0 0 14px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .chart-svg {{
      width: 100%;
      height: auto;
      display: block;
    }}
    .axis-label {{
      fill: #94a3b8;
      font-size: 11px;
      font-family: Arial, Helvetica, sans-serif;
    }}
    .legend-label {{
      fill: #cbd5e1;
      font-size: 12px;
      font-family: Arial, Helvetica, sans-serif;
    }}
    .dual-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
      gap: 18px;
    }}
    .status-strip {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
      margin: 16px 0 18px;
    }}
    .status-chip {{
      background: rgba(15, 23, 42, 0.72);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px 16px;
    }}
    .status-chip .label {{
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .status-chip .value {{
      color: #f8fafc;
      font-size: 18px;
      font-weight: 700;
      line-height: 1.3;
    }}
    .status-chip .sub {{
      color: #cbd5e1;
      font-size: 12px;
      margin-top: 6px;
      line-height: 1.5;
    }}
    .data-table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 8px;
      font-size: 13px;
    }}
    .data-table th, .data-table td {{
      border-bottom: 1px solid var(--border);
      padding: 10px 8px;
      text-align: left;
    }}
    .data-table th {{
      color: #cbd5e1;
      font-weight: 600;
    }}
    .data-table td {{
      color: #e2e8f0;
    }}
    .footer-note {{
      margin-top: 20px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.6;
    }}
    .empty-box {{
      border: 1px dashed var(--border);
      color: var(--muted);
      padding: 28px;
      border-radius: 12px;
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="hero">
      <div>
        <h1>WorkBuddy Curve Dashboard</h1>
        <p>最新日期：{_escape(latest_nav.get("date", ""))} | 归档周：{iso_year} W{iso_week:02d}</p>
      </div>
      <div>
        <p>生成时间：{latest_generated_at}</p>
      </div>
    </div>

    <div class="status-strip">
      <div class="status-chip">
        <div class="label">数据截至</div>
        <div class="value">{_escape(latest_nav.get("date", ""))}</div>
        <div class="sub">收盘后最新正式观测日期</div>
      </div>
      <div class="status-chip">
        <div class="label">页面生成于</div>
        <div class="value">{_escape(latest_generated_at)}</div>
        <div class="sub">打开页面时先看这里，确认是否为最新自动更新版本</div>
      </div>
      <div class="status-chip">
        <div class="label">学习状态</div>
        <div class="value">{_escape(str(latest_learning.get("learning_status", "n/a")))}</div>
        <div class="sub">样本阻断：{_escape(str(latest_learning.get("top_block_reason_1", "")))}</div>
      </div>
    </div>

    <div class="summary-grid">
      {metrics_html}
    </div>

    <div class="section-grid">
      <section class="panel">
        <h2>结果层</h2>
        <p>同时观察总资产、累计已实现盈亏和浮动盈亏，避免把账面波动误当成真实兑现成果。</p>
        {result_chart}
      </section>

      <div class="dual-grid">
        <section class="panel">
          <h2>本周新增平仓收益</h2>
          <p>来自 closed 记录的日度新增平仓收益，用于观察本周真实落袋质量。</p>
          {realized_bar}
        </section>

        <section class="panel">
          <h2>执行层成功率</h2>
          <p>区分总成功率、买单成功率和卖单成功率，观察执行链路是否在真正变稳。</p>
          {execution_line}
        </section>
      </div>

      <div class="dual-grid">
        <section class="panel">
          <h2>执行层错误次数</h2>
          <p>把 `112` 限流和 `501` 可用数量不足单独拉出来看，避免把执行层噪声混进策略判断。</p>
          {execution_bar}
        </section>

        <section class="panel">
          <h2>基准对标</h2>
          <p>策略净值已做归一化处理，主基准当前为中证1000，辅基准当前为创业板指。</p>
          {benchmark_chart}
        </section>
      </div>

      <div class="dual-grid">
        <section class="panel">
          <h2>学习准备度样本流量</h2>
          <p>按“平仓样本 -> 粗匹配样本 -> 可学习样本”的漏斗看学习燃料是否开始稳定积累。</p>
          {learning_flow_chart}
        </section>

        <section class="panel">
          <h2>学习准备度转化率</h2>
          <p>重点看粗匹配率、可学习率和匹配后净样本率，判断是缺记录、缺映射，还是被执行噪声挡住。</p>
          {learning_rate_chart}
        </section>
      </div>

      <div class="dual-grid">
        <section class="panel">
          <h2>已实现盈亏明细表</h2>
          <p>这张表回答“本周落袋成果怎么累计出来的”，不是单周利润的唯一口径。</p>
          {realized_table}
        </section>

        <section class="panel">
          <h2>执行明细表</h2>
          <p>这张表回答“系统是靠更稳的执行赚到，还是只是在行情好时被动上涨”。</p>
          {trade_table}
        </section>
      </div>

      <section class="panel">
        <h2>学习准备度明细表</h2>
        <p>从 2026-06-18 开始按交易日收盘后更新，用来观察样本进入率和首轮学习触发条件，不直接声称模型已经学会。</p>
        {learning_table}
      </section>
    </div>

    <div class="footer-note">
      <div>说明 1：`CSV` 是长期主数据，`HTML` 是长期观察界面，`PNG` 后续用于归档快照。</div>
      <div>说明 2：学习准备度面板从 2026-06-18 起按交易日收盘后更新，观察的是“样本是否准备好”，不是“模型已产生收益归因”。</div>
      <div>说明 3：非交易日或指数无日线时，基准会保留空值，不伪造行情。</div>
    </div>
  </div>
</body>
</html>
"""
    return html_text, archive_name


def generate_dashboards(as_of_date: str = "") -> Dict[str, str]:
    CURVE_HTML_DIR.mkdir(parents=True, exist_ok=True)
    data = _load_data(as_of_date=as_of_date)
    html_text, archive_name = _build_dashboard_html(data)
    latest_path = CURVE_HTML_DIR / "dashboard_latest.html"
    archive_path = CURVE_HTML_DIR / archive_name
    latest_path.write_text(html_text, encoding="utf-8")
    archive_path.write_text(html_text, encoding="utf-8")
    return {
        "latest": str(latest_path),
        "archive": str(archive_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate curve observatory dashboards")
    parser.add_argument(
        "--as-of-date",
        default="",
        help="Only render rows up to this trading date (YYYY-MM-DD). Empty means no clamp.",
    )
    args = parser.parse_args()
    outputs = generate_dashboards(as_of_date=str(args.as_of_date).strip())
    if args.as_of_date:
        print(f"as_of_date: {args.as_of_date}")
    for key, value in outputs.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
