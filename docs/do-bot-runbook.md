# DO Bot Runbook

This runbook is for bots operating on the DO-hosted `stockbot` environment.

Read this before doing any account query, runtime inspection, or repository operation.

## Read Order

1. Read `docs/stockbot-code-map.md`
2. Read `docs/do-bot-runbook.md`
3. If the task involves the mock trading account, read `docs/mx-moni-bot-prompt.md`
4. Only then read the specific implementation file you need

## Main Goals

Use this runbook to:

- understand how to safely inspect the repository on DO
- understand how to safely query the MX mock account
- avoid accidental trading actions
- know where runtime outputs live
- know when to escalate from read-only to action mode

## Environment Facts

- Repository root on DO:
  - `/opt/stockbot`
- Docs directory on DO:
  - `/opt/stockbot/docs`
- Runtime output directory:
  - `/opt/stockbot/workbuddy/a-share-analyst`
- Main mock account skill:
  - `/opt/stockbot/workbuddy/skills/mx-moni/mx_moni.py`

## Scheduler Source Of Truth

Bots must understand that there are two scheduler models in this project:

- current active scheduler:
  - DO local `systemd` timer on the droplet
- legacy scheduler:
  - local Windows `schtasks`

Current rule:

- treat the DO local timer as the guaranteed 09:25 trigger
- treat GitHub Actions as manual control and recovery only
- treat Windows `schtasks` as the legacy timing reference
- do not assume local Windows tasks are still enabled

## Code Sync Source Of Truth

Bots must also understand the code synchronization model:

- GitHub repository is the source of truth
- `/opt/stockbot` is the synced execution repository on DO

Important rule:

- do not assume `/opt/stockbot` was updated manually
- prefer the GitHub sync workflow as the official update path
- treat hand-copied files as temporary or emergency behavior, not the standard model

## Current Active GitHub Workflows

### Full Trading Day Workflow

File:

- `/opt/stockbot/.github/workflows/trading-day-self-hosted.yml`

Purpose:

- starts one full intraday control job on the DO self-hosted runner
- can also be triggered manually

Important details:

- runner labels:
  - `self-hosted`
  - `linux`
  - `stockbot-do`
- main entrypoint:
  - `scripts/github-actions/run_trading_day_on_do.sh`
- day controller:
  - `workbuddy/skills/a-share-analyst/github_actions_trade_day.py`
- important manual inputs:
  - `trade_date`
  - `start_from_slot`
  - `dry_run`

Meaning:

- GitHub no longer owns the guaranteed morning trigger
- it is now the manual entrypoint that calls the same launcher used by the DO timer
- the shared launcher holds a lock so a manual run and the timer do not start two full-day controllers at once

### DO Local Trading Day Timer

Files:

- `/opt/stockbot/scripts/github-actions/install_do_trading_day_timer.sh`
- `/opt/stockbot/scripts/github-actions/run_trading_day_on_do.sh`

Purpose:

- guarantee one local `09:25` China-time start on the DO machine
- remove dependence on GitHub `schedule` event delivery
- keep the same day-controller logic and runtime paths

Important details:

- installed unit:
  - `stockbot-trading-day.service`
- installed timer:
  - `stockbot-trading-day.timer`
- schedule:
  - `01:25 UTC`, Monday-Friday
  - this is `09:25` China Standard Time
- env override file:
  - `/etc/stockbot/trading-day.env`
- MX secret fallback:
  - `/opt/stockbot/.mx_apikey`

Meaning:

- if GitHub `schedule` is delayed or dropped, the DO machine still starts the day on time
- GitHub remains the code source, sync trigger, and manual recovery plane

### Manual Phase Workflow

File:

- `/opt/stockbot/.github/workflows/manual-phase-self-hosted.yml`

Purpose:

- runs one selected phase manually through `v10_auto_runner.py`
- used for retries, targeted checks, and operational recovery

Important details:

- runner labels:
  - `self-hosted`
  - `linux`
  - `stockbot-do`
- main entrypoint:
  - `workbuddy/skills/a-share-analyst/v10_auto_runner.py`
- important inputs:
  - `phase`
  - `trigger_slot`
  - `task_name`
  - `with_email`
  - `max_attempts`
  - `interval_seconds`

### GitHub Workflow Runtime Notes

- both workflows export runtime environment variables before execution
- both workflows sync the GitHub checkout into `/opt/stockbot` before running code
- both workflows inject:
  - `TLFZ_*` runtime paths
  - `MX_APIKEY`
  - `MX_API_URL`
- runtime artifacts are uploaded from:
  - `/opt/stockbot/workbuddy/a-share-analyst/automation_status/`
  - `/opt/stockbot/workbuddy/a-share-analyst/*latest*.json`

### Dedicated Repo Sync Workflow

File:

- `/opt/stockbot/.github/workflows/sync-do-repo.yml`

Purpose:

- sync GitHub `master` into `/opt/stockbot`
- keep the DO execution repository aligned with the repository source of truth

Trigger modes:

- automatic on push to `master`
- manual via `workflow_dispatch`

Sync implementation:

- workflow entry:
  - `/opt/stockbot/.github/workflows/sync-do-repo.yml`
- sync script:
  - `/opt/stockbot/scripts/github-actions/sync_repo_to_do.sh`

Preserved during sync:

- `.venv`
- `.mx_apikey`
- runtime output directories
- generated pool/distill artifact directories

## Legacy Windows `schtasks` Model

Primary reference file:

- `/opt/stockbot/workbuddy/skills/a-share-analyst/register_workbuddy_tasks.py`

Purpose:

- this file defines the original local Windows task timing map
- bots should use it to understand intended intraday sequence and timing semantics

Important note:

- this is legacy scheduling logic
- current DO automation does not depend on Windows `schtasks`
- however, the time slots still explain the intended execution order

### Legacy Intraday Timing Map

- `09:31` -> `opening-data`
- `09:33` -> `workbuddy-status`
- `09:36` -> `add-position`
- `09:45` -> `smart-sell`
- `09:47` -> `workbuddy-smart-sell` with trigger slot `09:45`
- `10:02` -> `workbuddy-buy` with trigger slot `10:00`
- `10:15` -> `smart-sell`
- `10:32` -> `workbuddy-smart-sell` with trigger slot `10:30`
- `10:34` -> `workbuddy-buy` with trigger slot `10:30`
- `10:45` -> `smart-sell`
- `11:02` -> `workbuddy-buy` with trigger slot `11:00`
- `11:15` -> `smart-sell`
- `11:35` -> `midday-node`
- `13:00` -> `midday-gate`
- `13:15` -> `smart-sell`
- `13:28` -> `add-position`
- `13:32` -> `workbuddy-buy` with trigger slot `13:30`
- `13:38` -> `workbuddy-refresh`
- `13:45` -> `smart-sell`
- `14:02` -> `workbuddy-buy` with trigger slot `14:00`
- `14:15` -> `smart-sell`
- `14:30` -> `prewarm`
- `14:32` -> `workbuddy-buy` with trigger slot `14:30`
- `14:45` -> `smart-sell`
- `14:49` -> `decision`
- `14:50` -> `buy-watch`
- `14:52` -> `workbuddy-smart-sell` with trigger slot `14:50`
- `14:54` -> `workbuddy-buy` with trigger slot `14:50`
- `15:03` -> `workbuddy-status`
- `15:06` -> `close-node`

### How To Read Legacy `schtasks`

Each Windows task originally:

- used the prefix `TLFZ-WorkBuddy-`
- called a generated wrapper script under:
  - `workbuddy/skills/a-share-analyst/task_wrappers/`
- exported runtime paths such as:
  - `TLFZ_WORKBUDDY_ROOT`
  - `TLFZ_ARKCLAW_ROOT`
  - `TLFZ_WORKBUDDY_DATA_DIR`
- then called:
  - `v10_auto_runner.py`

Meaning:

- the old Windows scheduler and the new GitHub/DO scheduler share the same core execution engine
- the difference is the scheduler layer, not the strategy engine

## Bot Decision Rule For Scheduling Questions

If a user asks:

- "What runs now?"
- "What is the active scheduler?"
- "Why did this phase trigger?"
- "What should happen at 14:49?"

Use this rule:

1. check the DO local timer and launcher first
2. then check `github_actions_trade_day.py`
3. then use `register_workbuddy_tasks.py` to interpret intended legacy slot semantics
4. then check GitHub workflow logic for manual or recovery runs
5. only talk about Windows `schtasks` as historical or fallback context unless explicitly asked

## Default Operating Mode

Always start in read-only mode.

That means:

- read docs first
- inspect code second
- query account information third
- do not place orders unless explicitly instructed

## Safe First Actions

If you are asked to help with the mock account, do this first:

1. Read:
   - `docs/stockbot-code-map.md`
   - `docs/do-bot-runbook.md`
   - `docs/mx-moni-bot-prompt.md`
2. Confirm what the user actually wants:
   - account balance
   - positions
   - orders
   - explanation only
   - actual trading action
3. Prefer these three safe queries first:
   - balance
   - positions
   - orders

## Safe MX Mock Account Queries

Use read-only queries first.

Typical safe examples:

- `python /opt/stockbot/workbuddy/skills/mx-moni/mx_moni.py "Show my account balance"`
- `python /opt/stockbot/workbuddy/skills/mx-moni/mx_moni.py "Show my current positions"`
- `python /opt/stockbot/workbuddy/skills/mx-moni/mx_moni.py "Show my orders"`

Equivalent Chinese-style examples often also work:

- `python /opt/stockbot/workbuddy/skills/mx-moni/mx_moni.py "我的资金"`
- `python /opt/stockbot/workbuddy/skills/mx-moni/mx_moni.py "我的持仓"`
- `python /opt/stockbot/workbuddy/skills/mx-moni/mx_moni.py "我的委托"`

## Prohibited By Default

Do not do any of the following unless the user explicitly asks for it:

- buy orders
- sell orders
- cancel all orders
- posting operation summaries
- modifying strategy files
- changing workflow definitions
- deleting runtime artifacts

## Escalation Levels

### Level 0: Explanation Only

Allowed:

- read docs
- explain repository structure
- explain account methods

Not allowed:

- any account query
- any file modification

### Level 1: Read-Only Account Inspection

Allowed:

- balance query
- positions query
- orders query
- runtime status inspection

Not allowed:

- buy
- sell
- cancel
- summary posting

### Level 2: Operational Account Action

Allowed only with explicit user instruction:

- buy
- sell
- cancel one order
- cancel all pending orders
- post a summary

Before doing Level 2 actions:

- restate the requested action clearly
- confirm stock code, quantity, and price mode
- prefer one action at a time

## Input Rules For Mock Trading

When an order action is explicitly requested:

- A-share stock codes must be 6 digits
- quantity is in shares
- market-price and limit-price behavior must be distinguished clearly
- canceling a single order requires a valid order ID

## Runtime Inspection Paths

If the task is about system execution or debugging, inspect:

- `/opt/stockbot/workbuddy/a-share-analyst/automation_status/`
- `/opt/stockbot/workbuddy/a-share-analyst/*latest*.json`

Typical things to check:

- latest phase status
- phase history
- `v10_opening_node_latest.json` for the `09:31 opening-data` morning node summary
- `v10_midday_inspection_latest.json` for the midday inspection summary
- close-node outputs
- candidate pool freshness
- account summary outputs

## Recommended File Entry Points

### For scheduling questions

Read:

- `/opt/stockbot/scripts/github-actions/run_trading_day_on_do.sh`
- `/opt/stockbot/workbuddy/skills/a-share-analyst/register_workbuddy_tasks.py`
- `/opt/stockbot/workbuddy/skills/a-share-analyst/github_actions_trade_day.py`
- `/opt/stockbot/.github/workflows/trading-day-self-hosted.yml`

### For phase runner behavior

Read:

- `/opt/stockbot/workbuddy/skills/a-share-analyst/v10_auto_runner.py`
- `/opt/stockbot/workbuddy/skills/a-share-analyst/workbuddy_runtime.py`

### For main strategy logic

Read:

- `/opt/stockbot/workbuddy/skills/a-share-analyst/v10_moni_trader.py`

### For challenger logic

Read:

- `/opt/stockbot/workbuddy/skills/a-share-analyst/workbuddy_local_challenger.py`

### For MX mock account behavior

Read:

- `/opt/stockbot/docs/mx-moni-bot-prompt.md`
- `/opt/stockbot/workbuddy/skills/mx-moni/mx_moni.py`
- `/opt/stockbot/workbuddy/skills/mx-moni/SKILL.md`

## Response Style For Bots

When answering the user:

- be concrete
- use short steps
- separate read-only actions from action-taking operations
- explicitly warn before any trade action
- report exact file paths when relevant

## Minimal Safe Workflow

If the user says: "Check my mock account"

Do this:

1. Read `docs/mx-moni-bot-prompt.md`
2. Query balance
3. Query positions
4. Query orders
5. Summarize findings
6. Stop there unless the user explicitly requests trading action

## Hard Safety Rule

Never place a mock buy or sell order just because the user asked for "help", "check", "review", "inspect", or "see what to do".

Only place an order when the user explicitly instructs the exact action.
