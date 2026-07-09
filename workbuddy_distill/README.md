# WorkBuddy Distill

This directory is the learning zone for the `workbuddy` distillation pipeline.

## First Window

- Trade-date window: `2026-05-13` to `2026-06-18`
- Scope: `hs_a_share`
- Source of truth for T0 outcomes: `TDX`

## Directory Layout

- `raw_top100/`
  - Per-trade-date TDX full ranking and top100 artifacts.
- `templates/`
  - Template registry, active templates, and retired templates.
- `evaluations/`
  - T+1 template acceptance results.
- `evolution/`
  - Promotion, downgrade, split, and retirement records.
- `artifacts/`
  - Window-level summaries and rollup reports.
- `scripts/`
  - Distillation utilities.

## Current Scripts

- `scripts/build_tdx_rankings.py`
  - Builds full-rank and top100 artifacts for one or more trade dates in a single TDX pass.
- `scripts/evaluate_template_hits.py`
  - Evaluates a T-1 candidate pool against T0 top100/top30/top10 outcomes.
