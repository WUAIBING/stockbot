# [OPEN] Debug Session: challenger-zero-pnl

## Symptom

- Challenger 5 笔仓位在 `2026-07-06 09:48:14` 平仓后，`workbuddy_local_track_record.csv` 中全部显示 `pnl=0`、`pnl_pct=0`
- 用户判断这不合理，要求基于运行时证据仔细排查

## Expected

- 平仓记录应反映本次 `sell_all` 的真实结算口径
- 若使用行情近似成交价，也应明确记录来源，不能静默回填买入价导致全部 `0 pnl`

## Falsifiable Hypotheses

1. 卖出价在平仓落账时被错误继承为买入价
2. `fast_realize` 分支只更新状态未完成真实结算
3. 本地 paper account 在 `D1_0945` 平仓时使用了占位价/默认价
4. CSV 写回逻辑覆盖了已算出的真实 pnl

## Evidence Plan

- 定位 Challenger 平仓执行、结算、写账本入口
- 先加最小化调试上报，记录平仓前持仓、候选卖出价、最终写回价、pnl 计算输入
- 复核现有运行产物，必要时做 dry-run 或最小复现

## Status

- 2026-07-06: Session opened, awaiting instrumentation

## Evidence

- Pre-fix reproduction:
  - Instrumentation showed `quote_present=false`, `quote_price=0`, `ref_source=entry_fallback`
  - The next sell-fill event used `fill_price == entry_price`, reproducing the exact `0 pnl` behavior
- Confirmed root cause:
  - Challenger sell path used `ref_price = _quote_price(...) or entry_price`
  - When live quote was missing, execution silently fell back to `entry_price` and wrote a fake flat close
- Rejected hypotheses:
  - CSV writeback override is not the primary cause
  - `apply_sell_fill()` calculation itself is not wrong; wrong input price was passed in

## Fix

- Tightened Challenger sell execution price to require live `quote['price']`
- If live quote is missing, the position is skipped with `实时行情不可用`
- Added regression test to ensure `last_close` alone cannot trigger a local sell fill

## Verification

- `python -m py_compile workbuddy_local_challenger.py test_execution_layer.py` passed
- Targeted tests passed:
  - `test_challenger_do_smart_sell_executes_local_fill`
  - `test_challenger_do_smart_sell_skips_when_only_last_close_is_available`
- Post-fix reproduction:
  - Same empty-quote scenario now produced `拟卖0只 | 跳过5只`
  - No sell-fill instrumentation events were emitted for those 5 positions
