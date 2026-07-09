# Stockbot Code Map

This document is a high-level code map for bots running on DO.

Use it before reading or operating the repository.

## Recommended Reading Order

1. Read `docs/stockbot-code-map.md`
2. Read `docs/mx-moni-bot-prompt.md` if the task involves mock account queries or operations
3. Read `workbuddy/WORKBUDDY_PROGRESS_LOG.md` for the latest engineering context
4. Read the specific entrypoint script for the task you want to execute

## Repository Purpose

This repository contains an automated A-share trading system plus its candidate-pool and distillation pipelines.

At a high level:

- `workbuddy/`
  - Main trading system and runtime data area
- `workbuddy_distill/`
  - Distillation and template-learning pipeline
- `workbuddy_pool/`
  - Generated candidate-pool outputs
- repository root scripts
  - Cross-module builders and refresh entrypoints

## Top-Level Layout

- `docs/`
  - Bot-facing operational documents, prompts, and code maps
- `.github/workflows/`
  - GitHub Actions workflows for the DO self-hosted runner
- `scripts/github-actions/`
  - Runner setup scripts
- `workbuddy/`
  - Main strategy, challenger, automation runner, and runtime outputs
- `workbuddy_distill/`
  - Template distillation, evaluations, evolution records, and utilities
- `workbuddy_pool/`
  - Latest candidate pool artifacts
- `refresh_distill_pipeline.py`
  - Root entrypoint to refresh the distill/pool pipeline
- `build_workbuddy_pool.py`
  - Candidate-pool builder
- `build_workbuddy_distill_pool.py`
  - Distill-aware candidate ranking and pool builder
- `build_workbuddy_distill_daily_review.py`
  - Daily distill review builder used by close-node flows

## Main Runtime: `workbuddy/`

### Runtime Data

- `workbuddy/a-share-analyst/`
  - Runtime data directory used by the trading system
  - Stores latest JSON outputs, status files, reviews, and execution artifacts

### Main Documentation

- `workbuddy/WORKBUDDY_PROGRESS_LOG.md`
  - Best place to learn what was changed recently
- `workbuddy/WORKBUDDY_EXECUTION_PLAN.md`
  - Strategic execution plan and architecture notes

### Core Strategy Layer

- `workbuddy/skills/a-share-analyst/v10_moni_trader.py`
  - Main strategy logic
  - Handles market judgment, entry logic, smart sell logic, midday/close behavior, and MX-backed trading paths
- `workbuddy/skills/a-share-analyst/workbuddy_local_challenger.py`
  - Challenger execution logic
  - Uses candidate pools and profitability-priority execution rules

### Core Automation Layer

- `workbuddy/skills/a-share-analyst/v10_auto_runner.py`
  - Main phase runner
  - Executes phases such as `opening-data`, `smart-sell`, `workbuddy-buy`, `decision`, and `close-node`
- `workbuddy/skills/a-share-analyst/register_workbuddy_tasks.py`
  - Defines the original local Windows task schedule
  - Useful for understanding the intended intraday timing map
- `workbuddy/skills/a-share-analyst/github_actions_trade_day.py`
  - GitHub Actions day controller
  - Replays the task schedule on the DO self-hosted runner

### Runtime Path and Validation Layer

- `workbuddy/skills/a-share-analyst/workbuddy_runtime.py`
  - Runtime path resolution and preflight validation
  - Checks candidate pool files, opening tradability files, and execution consistency
- `workbuddy/skills/a-share-analyst/package_paths.py`
  - Resolves writable data directories
- `workbuddy/skills/a-share-analyst/trading_calendar.py`
  - Trading-day logic

### Market and MX Integration Layer

- `workbuddy/skills/a-share-analyst/external_market_review.py`
  - Builds external market context and risk bias
- `workbuddy/skills/a-share-analyst/mx_enrich_candidates.py`
  - Adds MX-enriched signals to candidates
- `workbuddy/skills/a-share-analyst/mx_event_review.py`
  - Event review and MX-backed market/event enrichment
- `workbuddy/skills/a-share-analyst/mx_challenger_pool.py`
  - Builds or enriches challenger-side MX candidate information
- `workbuddy/skills/a-share-analyst/mx_workbuddy_portfolio.py`
  - Portfolio-related MX support utilities

### Review and Learning Layer

- `workbuddy/skills/a-share-analyst/workbuddy_local_review.py`
  - Local review builder
- `workbuddy/skills/a-share-analyst/workbuddy_learning_bridge.py`
  - Connects execution outputs to learning/evolution
- `workbuddy/skills/a-share-analyst/update_curve_observatory.py`
  - Curve and observability updater

### Tests

- `workbuddy/skills/a-share-analyst/test_execution_layer.py`
  - Key regression tests around execution logic and strategy alignment
- `workbuddy/skills/a-share-analyst/test_workbuddy_runtime.py`
  - Runtime/path validation tests
- `workbuddy/skills/a-share-analyst/test_security_master_refresh.py`
  - Security master refresh tests

## Mock Trading Skill: `mx-moni`

- `workbuddy/skills/mx-moni/mx_moni.py`
  - Mock trading account operator
  - Supports balance, positions, orders, buy, sell, cancel, and summary posting
- `workbuddy/skills/mx-moni/SKILL.md`
  - Human-readable skill documentation and API behavior
- `workbuddy/skills/mx-moni/_meta.json`
  - Skill metadata

### Important Environment Variables

- `MX_APIKEY`
  - Required secret for the MX mock trading API
- `MX_API_URL`
  - Optional API base URL, defaulting to the standard MX endpoint

If the task is about mock account querying or operations, read:

- `docs/mx-moni-bot-prompt.md`

## Distillation Layer: `workbuddy_distill/`

- `workbuddy_distill/README.md`
  - Distillation overview and layout
- `workbuddy_distill/raw_top100/`
  - Raw ranking artifacts
- `workbuddy_distill/templates/`
  - Template registries and active template definitions
- `workbuddy_distill/evaluations/`
  - Template evaluation outputs
- `workbuddy_distill/evolution/`
  - Promotion/downgrade/evolution records
- `workbuddy_distill/artifacts/`
  - Window-level summaries and rollups
- `workbuddy_distill/scripts/distill_local_templates.py`
  - Core local template evaluation/classification logic
- `workbuddy_distill/scripts/evaluate_template_hits.py`
  - Evaluates template hit quality
- `workbuddy_distill/scripts/build_tdx_rankings.py`
  - Builds ranking artifacts from TDX data

## Candidate Pool Layer: `workbuddy_pool/`

- `workbuddy_pool/README.md`
  - Notes that this directory stores generated candidate pool outputs
- `workbuddy_pool/`
  - Latest candidate pool artifacts used by execution and review steps

## GitHub / DO Deployment Layer

- `.github/workflows/trading-day-self-hosted.yml`
  - Full trading-day GitHub workflow for the DO self-hosted runner
- `.github/workflows/manual-phase-self-hosted.yml`
  - Manual phase runner workflow
- `scripts/github-actions/setup_self_hosted_runner.sh`
  - Linux runner setup for DO
- `scripts/github-actions/setup_self_hosted_runner.ps1`
  - Windows runner setup, kept for the original Windows path

### Scheduler Reality

- Active scheduler:
  - GitHub Actions on the DO self-hosted runner
- Legacy scheduler:
  - local Windows `schtasks`

Important interpretation rule:

- use GitHub workflows as the current operational truth
- use `register_workbuddy_tasks.py` as the legacy timing map and semantic schedule reference

### Code Sync Reality

- GitHub repository is the source of truth
- `/opt/stockbot` is the synced DO execution repository
- `.github/workflows/sync-do-repo.yml` is the dedicated sync workflow
- `scripts/github-actions/sync_repo_to_do.sh` is the local DO sync script

Practical interpretation rule:

- if code on DO and GitHub ever differ, trust GitHub as authoritative
- expect `/opt/stockbot` to be updated by workflow-driven sync, not by manual edits

## Practical Entry Points By Task

### If you need to understand daily scheduling

Read:

- `workbuddy/skills/a-share-analyst/register_workbuddy_tasks.py`
- `workbuddy/skills/a-share-analyst/github_actions_trade_day.py`
- `.github/workflows/trading-day-self-hosted.yml`

### If you need to understand actual phase execution

Read:

- `workbuddy/skills/a-share-analyst/v10_auto_runner.py`
- `workbuddy/skills/a-share-analyst/workbuddy_runtime.py`

### If you need to understand main strategy behavior

Read:

- `workbuddy/skills/a-share-analyst/v10_moni_trader.py`
- `workbuddy/skills/a-share-analyst/test_execution_layer.py`

### If you need to understand challenger behavior

Read:

- `workbuddy/skills/a-share-analyst/workbuddy_local_challenger.py`
- `build_workbuddy_distill_pool.py`

### If you need to understand distillation and template evolution

Read:

- `workbuddy_distill/scripts/distill_local_templates.py`
- `build_workbuddy_distill_pool.py`
- `build_workbuddy_distill_daily_review.py`

### If you need to query or operate the MX mock account

Read:

- `docs/mx-moni-bot-prompt.md`
- `workbuddy/skills/mx-moni/mx_moni.py`
- `workbuddy/skills/mx-moni/SKILL.md`

## DO Paths

Important paths on DO:

- `/opt/stockbot/`
  - Repository root
- `/opt/stockbot/docs/stockbot-code-map.md`
  - This code map
- `/opt/stockbot/docs/mx-moni-bot-prompt.md`
  - Prompt for mock account usage
- `/opt/stockbot/workbuddy/a-share-analyst/`
  - Persistent runtime outputs

## Bot Operating Notes

- Start from the smallest relevant entrypoint, not the whole repo.
- Prefer read-only queries first if the task involves account operations.
- Do not place mock buy/sell orders unless explicitly instructed.
- Use `MX_APIKEY`-backed flows only when the task actually requires MX access.
- For runtime debugging, inspect the latest JSON artifacts under `workbuddy/a-share-analyst/` first.
