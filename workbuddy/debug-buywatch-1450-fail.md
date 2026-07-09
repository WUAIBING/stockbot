[OPEN] buywatch-1450-fail

# 14:50 Buy-Watch 首次失败调试记录

## 症状
- 2026-07-06 14:50 `TLFZ-WorkBuddy-BuyWatch` 第 1 次执行中，`v10_moni_trader.py --buy` 返回 `exit_code=1`。
- 同一轮第 2 次重试恢复为 `no_action`。

## 预期
- 若尾盘无可执行买点，应稳定返回 `no_action`。
- 不应出现“第一次 failed，第二次 no_action”的非确定性抖动。

## 初始假设
1. `data_freshness_probe` 超时后，某个输入产物在第一次 `buy` 执行时处于半更新状态，导致主策略买入链首次读取失败。
2. `v10_moni_trader.py --buy` 在尾盘空仓/无信号路径上存在未覆盖的异常分支，第一次触发异常，第二次因输入变化落回 `no_action`。
3. 主策略买入链依赖的某个外部连接或文件锁在 14:50 首次调用时短暂失败，重试后恢复。
4. 自动化 runner 传给 `v10_moni_trader.py --buy` 的上下文或工作目录在首次执行时不一致，导致首次失败并非业务逻辑本身。

## 待取证
- 记录 `v10_moni_trader.py --buy` 首次失败的关键输入、分支、异常信息。
- 对比同轮第 2 次 `no_action` 的同位置运行证据，确认差异点。

## 当前状态
- 已建调试会话，待插桩与复现。

## 当前证据
- `14:50:23-14:51:05` 的第 1 次 `buy-watch` 失败时，`14:49 decision` 其实还没完成；`decision` 直到 `14:51:36` 才落成 `ok`。
- `wait_for_today_decision_ready()` 在 `latest_decision_status.json` 读到空/不可解析内容时，会直接走 `return True`，存在把“半更新状态文件”误判成 ready 的风险。
- `_read_json()` 对任意解析异常都返回 `{}`，而 `record_status()` 又是原子替换写状态文件；这两者组合会放大并发读取时的空读窗口。
- `manual-repro-1` 的受控复现没有重现首次异常，而是稳定落到 `learning gate blocked buy`，说明原故障更像时间竞态，不像稳定业务分支错误。

## 当前判断
- 假设 1（输入产物半更新）当前最强，重点怀疑 `decision` 状态文件与扫描快照在首轮 `buy-watch` 读取时处于半更新窗口。
- 假设 2（空仓/无信号分支漏异常）暂未得到证据支持。
- 假设 3（外部连接/文件锁抖动）暂无直接证据。
- 假设 4（runner 上下文不一致）目前不如假设 1 强。
