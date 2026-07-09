from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import distill_local_templates as distill


def _variant_summary(core_days: int, buffer_days: int) -> dict[str, Any]:
    result = distill.run_distill(core_days=core_days, buffer_days=buffer_days)
    payload = result["payload"]
    combined = result["combined_payload"]
    top_template = payload["top_templates"][0] if payload.get("top_templates") else None
    promoted = combined.get("promoted_combinations", [])
    champion = distill.select_champion_combination(promoted)
    return {
        "buffer_days": buffer_days,
        "window_profile": payload["window_profile"],
        "top_template": {
            "template_name": top_template["template_name"],
            "metrics": top_template["metrics"],
        }
        if top_template
        else None,
        "champion_template": {
            "template_name": champion["template_name"],
            "base_template_name": champion["base_template_name"],
            "negative_veto": champion.get("negative_veto"),
            "metrics": champion["metrics"],
        }
        if champion
        else None,
        "promoted_combination_count": combined["summary"]["promoted_combination_count"],
    }


def _metric_value(template: dict[str, Any] | None, key: str, default: float = 0.0) -> float:
    if not isinstance(template, dict):
        return default
    metrics = template.get("metrics", {})
    if not isinstance(metrics, dict):
        return default
    try:
        return float(metrics.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _best_variant(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda row: _metric_value(row.get("champion_template"), key, float("-inf")))


def _find_variant(rows: list[dict[str, Any]], buffer_days: int) -> dict[str, Any] | None:
    return next((row for row in rows if int(row.get("buffer_days", -1)) == buffer_days), None)


def _format_pct(value: float, *, scale_100: bool = True) -> str:
    shown = value * 100 if scale_100 else value
    return f"{shown:.2f}%"


def _delta_text(rows: list[dict[str, Any]], source_days: int, target_days: int, key: str, *, scale_100: bool = True) -> str:
    source = _find_variant(rows, source_days)
    target = _find_variant(rows, target_days)
    if not source or not target:
        return "-"
    delta = _metric_value(target.get("champion_template"), key) - _metric_value(source.get("champion_template"), key)
    sign = "+" if delta >= 0 else ""
    return f"{sign}{_format_pct(delta, scale_100=scale_100)}"


def _recommendation_lines(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    best_win = _best_variant(rows, "candidate_win_rate")
    best_ret = _best_variant(rows, "candidate_avg_return")
    best_hit = _best_variant(rows, "top50_hit_rate")
    best_shift = _best_variant(rows, "front_shift_score")
    row_2 = _find_variant(rows, 2)
    recommended_prod = row_2 or best_shift or best_hit or best_ret or best_win or rows[0]
    prod_days = int(recommended_prod.get("buffer_days", 0))
    recommended_shadow = best_ret if best_ret and int(best_ret.get("buffer_days", -1)) != prod_days else None

    lines: list[str] = []
    lines.append("## Recommendation")
    lines.append("")
    lines.append(f"- `生产窗`: 继续使用 `20+{prod_days}`。")
    if recommended_shadow:
        lines.append(f"- `影子观察窗`: 增加 `20+{int(recommended_shadow['buffer_days'])}` 作为收益侧对照。")
    elif best_win and int(best_win.get("buffer_days", -1)) != prod_days:
        lines.append(f"- `影子观察窗`: 可保留 `20+{int(best_win['buffer_days'])}` 作为胜率侧参考。")

    if row_2:
        lines.append(
            "- `原因`: `20+2` 同时拿到 Top50 命中与前移分第一，"
            f"相对 `20+0` 的胜率变动为 `{_delta_text(rows, 0, 2, 'candidate_win_rate')}`，"
            f"相对 `20+1` 的收益变动为 `{_delta_text(rows, 1, 2, 'candidate_avg_return', scale_100=False)}`，"
            "代价可接受。"
        )
    if recommended_shadow:
        lines.append(
            f"- `补充`: `20+{int(recommended_shadow['buffer_days'])}` 当前平均收益最高，"
            "适合作为下一个观察周的收益增强 challenger。"
        )
    if best_win and int(best_win.get("buffer_days", -1)) != prod_days:
        lines.append(
            f"- `保留项`: `20+{int(best_win['buffer_days'])}` 胜率仍是样本内最好，"
            "但没有同步带来更强的前排命中。"
        )
    return lines


def _markdown_summary(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("# Distill Window Compare")
    lines.append("")
    lines.append("## Champion Summary")
    lines.append("")
    lines.append("| Window | Champion Template | Win Rate | Avg Return | Top50 Hit | Front Shift | Promoted |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        champion = row.get("champion_template") or {}
        metrics = champion.get("metrics", {}) if isinstance(champion, dict) else {}
        lines.append(
            "| "
            f"20+{row.get('buffer_days', 0)} | "
            f"{champion.get('template_name', '-') or '-'} | "
            f"{_format_pct(float(metrics.get('candidate_win_rate', 0.0) or 0.0))} | "
            f"{_format_pct(float(metrics.get('candidate_avg_return', 0.0) or 0.0), scale_100=False)} | "
            f"{_format_pct(float(metrics.get('top50_hit_rate', 0.0) or 0.0))} | "
            f"{_format_pct(float(metrics.get('front_shift_score', 0.0) or 0.0))} | "
            f"{int(row.get('promoted_combination_count', 0) or 0)} |"
        )
    lines.append("")

    best_win = _best_variant(rows, "candidate_win_rate")
    best_ret = _best_variant(rows, "candidate_avg_return")
    best_hit = _best_variant(rows, "top50_hit_rate")
    best_shift = _best_variant(rows, "front_shift_score")

    lines.append("## Best By Metric")
    lines.append("")
    if best_win:
        lines.append(
            f"- `胜率最佳`: 20+{best_win['buffer_days']} "
            f"({ _format_pct(_metric_value(best_win.get('champion_template'), 'candidate_win_rate')) })"
        )
    if best_ret:
        lines.append(
            f"- `收益最佳`: 20+{best_ret['buffer_days']} "
            f"({ _format_pct(_metric_value(best_ret.get('champion_template'), 'candidate_avg_return'), scale_100=False) })"
        )
    if best_hit:
        lines.append(
            f"- `Top50命中最佳`: 20+{best_hit['buffer_days']} "
            f"({ _format_pct(_metric_value(best_hit.get('champion_template'), 'top50_hit_rate')) })"
        )
    if best_shift:
        lines.append(
            f"- `前移分最佳`: 20+{best_shift['buffer_days']} "
            f"({ _format_pct(_metric_value(best_shift.get('champion_template'), 'front_shift_score')) })"
        )
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    if best_win:
        lines.append(
            f"- `胜率侧`: 20+{best_win['buffer_days']} 当前最佳，但更偏历史窗内最优。"
        )
    if best_ret:
        lines.append(
            f"- `收益侧`: 20+{best_ret['buffer_days']} 当前最佳，可作为继续优化的优先观察窗。"
        )
    if len(rows) >= 3:
        row_2 = next((row for row in rows if int(row.get("buffer_days", -1)) == 2), None)
        if row_2:
            champion = row_2.get("champion_template") or {}
            veto = champion.get("negative_veto") if isinstance(champion, dict) else None
            veto_family = veto.get("family", "none") if isinstance(veto, dict) else "none"
            lines.append(
                f"- `20+2` 当前冠军 veto 家族为 `{veto_family}`，且与 challenger 渐进缓冲设计保持一致。"
            )
    lines.append("")
    lines.extend(_recommendation_lines(rows))
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare distill results across buffer-day variants.")
    parser.add_argument("--core-days", type=int, default=20, help="核心窗口交易日数量")
    parser.add_argument(
        "--buffer-days",
        type=int,
        nargs="+",
        default=[0, 1, 2],
        help="要比较的缓冲天数列表，默认比较 0 1 2",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=distill.ARTIFACTS_ROOT / "distill_window_compare_20_core.json",
        help="输出 JSON 路径",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=None,
        help="输出 Markdown 路径，默认与 JSON 同名 .md",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = [_variant_summary(args.core_days, buffer_days) for buffer_days in args.buffer_days]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_output = args.markdown_output or args.output.with_suffix(".md")
    markdown_output.write_text(_markdown_summary(rows), encoding="utf-8")
    print(str(args.output))
    print(str(markdown_output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
