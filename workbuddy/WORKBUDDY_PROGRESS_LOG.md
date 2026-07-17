# TLFZ WorkBuddy 进度日志

## 使用说明

- 本文档用于记录已经完成的工作、当前阶段判断、运行状态和下一步动作。
- 长期路线、目标、验收标准请看 `WORKBUDDY_EXECUTION_PLAN.md`。
- 后续协作方式：
  - 先看本日志，确认当前做到哪一步；
  - 再看执行计划书，确认下一步按哪条路线推进。

---

## 2026-06-17

### 今日结论

- 项目已经从“原型自动化”推进到“`V3` 初段，且 `V1` 关键底座大幅补齐”的状态。
- 当前最适合的策略不是继续大改参数，而是先带着新框架跑一周，观察真实执行效果，再决定下一轮调整。

### 今日完成

#### 1. 执行与对仓底座增强

- 新增 pending order 基础设施，开始记录未完成委托状态。
- 支持的状态包括：
  - `submitted`
  - `partial`
  - `filled`
  - `stale`
- 成功买卖后会登记 pending 委托，后续复盘可以区分“报单了但未成”和“根本没报”。

- 新增真实持仓全量覆盖账本逻辑：
  - 真实持仓里有、账本里没有的，会自动导入；
  - 真实持仓里有、账本里也有的，会按真实仓位覆盖；
  - 账本里仍是 holding，但真实仓位里没有且也没有未完成卖单的，会自动 `paused`。

- 将全量对仓接入这些流程：
  - `smart-sell`
  - `status`
  - `add-position`
  - `report`

#### 2. 报告与监控增强

- 修复 `report` 在接口空返回时写出零资产快照的问题。
- 当前逻辑改为：
  - 若账户接口临时空返回，不再把 NAV 写坏；
  - 会回退到上一份账户快照；
  - 同时在摘要中明确标注 `account_status`。

- 账户摘要现在会带上：
  - `pending_orders`
  - `account_status`
  - `scan_status`

#### 3. 卖出逻辑进入 V3 初段

- `smart-sell` 不再只看趋势衰减，已经开始结合真实盈利状态。
- 当前新增的盈利敏感度规则：
  - 浮盈 `>= 15%` 且出现轻微转弱时，更倾向兑现；
  - 浮盈 `>= 8%` 且信号转弱较明显时，也会提高卖出概率。

- 当前卖出逻辑的结构已变为：
  - 趋势衰减
  - 真实盈利比例
  - 持仓天数
  - 真实可卖数量
  - pending 卖单状态提示

#### 4. 午间复盘 phase 正式上线

- 新增 `11:35` 午间复盘任务：
  - 任务名：`TLFZ-WorkBuddy-MiddayReview`
  - phase：`midday-review`

- 午间复盘不会下单，只做中场校准，输出文件：
  - `v10_midday_review_latest.json`

- 午间复盘当前会做：
  - 拉真实 `balance / positions / orders`
  - 先同步账本、再全量对仓
  - 汇总 `pending_orders`
  - 按风险对持仓排序
  - 输出下午观察清单

- 午间复盘当前输出的重点字段包括：
  - `market_temperature`
  - `avg_profit_pct`
  - `pending_orders`
  - `focus_sell_watch`
  - `holdings_review_top15`
  - `afternoon_watchlist`
  - `full_reconcile`

#### 5. 自动化调度已更新

- 当前工作日自动化时间表包括：
  - `09:36` `AddPosition`
  - `09:45` `SmartSell`
  - `10:15` `SmartSell`
  - `10:45` `SmartSell`
  - `11:15` `SmartSell`
  - `11:35` `MiddayReview`
  - `13:15` `SmartSell`
  - `13:45` `SmartSell`
  - `14:15` `SmartSell`
  - `14:30` `Prewarm`
  - `14:45` `SmartSell`
  - `14:49` `Decision`
  - `14:50` `BuyWatch`
  - `15:06` `Report`

#### 6. 正向进化保障机制已落地第一版

- 已把“持续进化必须沿着正向路径”的硬约束正式落实到模型底座。
- 当前新增了这些约束能力：
  - 冻结基线快照
  - 样本门槛
  - 冷却周期
  - 小步微调
  - 变更日志
  - 跳过更新时也记录原因

- 当前对应文件包括：
  - `v10_evolving_model_state.json`
  - `v10_evolving_model_baseline.json`
  - `v10_evolving_model_changelog.jsonl`
  - `v10_model_decisions.jsonl`

- 当前约束口径：
  - 匹配样本不足，不更新权重；
  - 近期已平仓样本不足，不更新权重；
  - 冷却时间未到，不更新权重；
  - 权重、mode bias、tier bias 只能小步变化；
  - 每次更新和每次跳过，都会写入变更日志。

- 当前的实际策略不是“让模型自由进化”，而是：
  - 先记录；
  - 再比较；
  - 再小步调整；
  - 随时可冻结。

### 当前阶段判断

- `V1 稳执行`：
  - 核心缺口已补大半，特别是：
    - 全量对仓
    - pending order 骨架
    - report 防零值覆盖
  - 但 pending order 还不是完整状态机，仍有后续空间。

- `V2 强买点`：
  - 第一版多层评分模型已经接入。
  - 当前处于带样本观察阶段。

- `V3 灵活卖`：
  - 已正式启动。
  - 半小时巡检已经上线。
  - 盈利敏感卖出已经开始接入。
  - 午间复盘已经成为中场校准环节。

- `V4 持续进化`：
  - 骨架已具备。
  - 还需要更多真实样本推动权重和模式偏置演化。

### 当前最重要的判断

- 午间复盘已经能起到“校准”和“下午观察准备”的作用。
- 但午间复盘结果目前还没有直接改写下午 `smart-sell` 或 `14:50 buy` 的参数。
- 当前建议先运行观察一周，再决定是否把午间结论进一步接入下午买卖阈值。
- 持续进化现在已经从“会学习”升级到“受约束地学习”，但仍处于保守期，不适合现在就放开高频自动调参。

### 接下来先观察什么

未来一周优先观察：

- `pending_orders` 是否开始稳定区分未成/部分成交/已成。
- `full_reconcile` 是否持续提升账本与真实持仓一致性。
- `smart-sell` 对高盈利但转弱仓位是否更敏感。
- 午间复盘输出的 `focus_sell_watch` 和下午真实卖出是否更接近。
- `report` 是否仍出现异常资产快照覆盖。
- 进化日志里是否持续出现：
  - 样本不足而冻结；
  - 冷却期保护生效；
  - 小步微调而不是大幅漂移。

### 建议的下一步

当前先不急于继续大改卖出或买入参数。  
建议顺序如下：

1. 带着当前版本运行一周。
2. 每天盘后检查：
   - 午间复盘是否有价值；
   - 下午卖出是否更准确；
   - pending 与对仓是否稳定。
3. 一周后再决定是否推进：
   - 午间复盘结果直接影响下午卖出敏感度；
   - 午间市场温度直接影响尾盘买入阈值；
   - pending order 进入完整状态机。

### 本日志之后的维护方式

- 每完成一批关键改造，就在本文档追加一节。
- 每次新增内容至少包括：
  - 今日完成
  - 当前阶段判断
  - 当前最重要结论
  - 下一步动作

这样后续只要读取：

- `WORKBUDDY_PROGRESS_LOG.md`
- `WORKBUDDY_EXECUTION_PLAN.md`

就能同时知道：

- 目前做到哪里；
- 后面应该往哪里推进。

---

## 2026-06-22

### 今日完成

- 针对交易接口 `112` 限流，先完成第一轮执行层补丁，落点在 `v10_moni_trader.py`。
- 本轮补丁不改市场映射，不改策略打分，只修执行恢复能力，目标是先改善：
  - `smart sell` 单票连续撞限流；
  - 尾盘 `buy decision` 某一笔先撞限流后直接被跳过。

#### 1. 交易重试与节奏增强

- 交易请求保留统一入口，但补上了更清晰的重试观测：
  - `112` 时写入 `retry_wait` 日志；
  - 最终交易结果写入 `trade_result`；
  - 日志里新增：
    - `retry_attempts`
    - `trade_min_interval`
    - `execution_phase`
    - `strategy_action`
    - `final_outcome`
- 买卖最小间隔拆分：
  - `buy` 放慢到 `2.5s`
  - `sell` 放慢到 `3.5s`
- `112` 的等待从固定线性重试，升级为“基准等待 + 小抖动”。

#### 2. 尾盘买入补单

- `do_buy()` 增加 `retry_tail_queue`。
- 首轮买入若某只因 `112` 失败：
  - 不立即放弃；
  - 先放入尾部补单队列；
  - 首轮其他候选跑完后，延迟 `6s` 再补一次。
- 目标是修复今天这类现象：
  - 单票不是策略不要；
  - 而是刚好撞上限流被跳过。

#### 3. Smart Sell 单票冷却

- `smart sell` 新增单票 `112` 状态文件：
  - `v10_smart_sell_retry_state.json`
- 当前逻辑：
  - 首轮卖出若命中 `112`，先加入本窗口延时重试；
  - 同窗口延迟 `6s` 再试一次；
  - 若尾部重试后仍是 `112`，写入冷却状态；
  - 下一窗口若仍在冷却期，则先跳过该票，避免每个窗口机械撞限流。
- 冷却窗口当前设为约 `35` 分钟，目的是优先跳过紧邻下一轮巡检，再看明天实盘效果。

#### 4. 兼容性处理

- `buy_stock()` / `sell_stock()` 的返回值已升级为结构化结果。
- `add-position` 加仓入口已同步兼容，避免旧逻辑因返回值变化失效。
- 已通过 `py_compile` 语法校验。

### 当前判断

- 这不是“最终版限流治理”，而是第一轮实战补丁。
- 这一轮的设计原则是：
  - 先提高恢复能力；
  - 先让失败单有第二次机会；
  - 先把重试证据写清楚；
  - 明天跑完再按结果微调。

#### 5. Pending 清理与订单状态收口补丁

- 在 `v10_moni_trader.py` 中补齐了 `orders -> pending` 的状态映射，不再只认：
  - `已成`
  - `部成`
- 新增可识别终态/中间态：
  - `cancel_pending`
  - `cancelled`
  - `rejected`
  - `cancel_failed`
- 这样 `v10_pending_orders.json` 后续不再只能靠“超时 -> stale”粗暴收口，而是能优先按模拟账户订单状态精确落账。

- 补入撤单能力：
  - 接通 `/api/claw/mockTrading/cancel`
  - 新增 `cancel_result` 日志写入 `v10_trade_api_log.jsonl`
  - 支持对单笔 stale / 待撤 / 撤单失败订单做主动清理

- 执行层入口同步接入了自动清理：
  - `do_buy()` 在尾盘买入前先清一轮旧 pending
  - `do_smart_sell()` / `do_sell()` 在卖出前也先清一轮旧 pending
  - 当 `smart sell` 发现“已有旧卖单占仓，且下一轮应重评估”时，不再只提示等待，而是会先发起撤单清理，再等下一次复核重报

- 增加了 `dry-run` 保护：
  - 演练模式不真正调用撤单
  - 避免测试动作污染模拟账户状态

### 本轮结论

- 执行层现在不再只依赖收盘 `full_reconcile` 被动救火。
- 新增这两刀后，目标是把问题前移到盘中：
  - 先把无效 pending 清掉；
  - 先把订单终态写准；
  - 再减少 `stale pending` 对买入和复核的污染。

- 这仍然属于 `执行层稳态化补丁`，不是策略层改动。
- 今天到这里的状态就是：
  - `策略层先稳住`
  - `执行层继续收口`

### 明天重点观察

- `688001` 这类 smart sell 单票，是否从“每窗口都失败”变成“同窗口补卖/下一窗口不再无脑重打”。
- `688107` 这类尾盘买单，是否能在尾部补单阶段成交。
- `v10_trade_api_log.jsonl` 中是否能清楚看到：
  - `retry_wait`
  - `trade_result`
  - `execution_phase=tail_retry`
  - 最终是否成功。

### 下一步

1. 明天实盘观察补丁效果。
2. 若 `112` 明显下降，再细调：
   - 买卖最小间隔；
   - 冷却时长；
   - 尾部补单等待时间。
3. 在执行层补丁落地后，继续按 `执行层 -> 策略层 -> 学习层` 顺序复核。

---

## 2026-07-06

### 今日完成

- 修复 `v10_moni_trader.py -> do_add_position()` 在“主策略空仓”场景下异常失败的问题。
- 这次问题正式归档为“代码能力进化层 / 工程守则”案例，而不是策略层结论。

#### 1. 问题定性

- `09:36 add-position` 是主策略链动作，本意只对主策略已有底仓做 T+1 加仓。
- 今天主策略空仓，本应直接 `EXIT_NO_ACTION`。
- 但旧实现存在两个工程性缺陷：
  - 先跑 `learning gate`，后判空仓；
  - 使用“全量 holding”而不是“主策略 native holding”。

#### 2. 本轮修复

- 把 `native holding` 判断前置到 `learning gate / cleanup pending / reconcile` 之前。
- 主策略空仓时，直接输出“当前无主策略持仓需要加仓”并返回 `EXIT_NO_ACTION`。
- `add-position` 的处理对象改成“主策略原生 holding only”，不再让 `[LIVE_POSITION_ONLY]` 或非主策略记录干扰。
- 同时补了一处健壮性容错：
  - `balance['total_assets']` 改为安全读取，避免字段缺失导致无关异常。

#### 3. 工程守则沉淀

- 守则 1: `no_action` 场景必须先于重逻辑短路。
  - 在进入 `learning gate / cleanup / reconcile / TDX / 远端 IO` 前，先判断是否根本无需执行。
- 守则 2: 多策略并存时，必须先做 `scope / owner / native` 过滤。
  - 不能把“所有 holding”都当成当前动作对象。
- 守则 3: 无关失败不得污染 `no_action`。
  - 预期应为空操作的场景，不能因为后续工程链异常被打成 `failed`。
- 守则 4: 此类问题进入 `engineering_review / engineering_evolution`，不得直接写成策略降权或模式学习。

#### 4. 验证

- 新增两条回归测试：
  - 非主策略 `[LIVE_POSITION_ONLY]` 持仓不会触发加仓流程；
  - 空仓场景会在 `stale learning gate` 前直接 `EXIT_NO_ACTION`。
- `python -m unittest test_execution_layer.AddPositionGuardTests` 通过。
- `python -m py_compile v10_moni_trader.py test_execution_layer.py` 通过。
- 真实复跑 `python v10_moni_trader.py --add-position --dry-run`，现直接输出：
  - `当前无主策略持仓需要加仓`

### Workbuddy Challenger 编排层大修

#### 1. 问题定性

- 今日复核确认：`Workbuddy local challenger` 没有真正接入自动化主链。
- 之前自动化里挂着的 `workbuddy-buy / workbuddy-sell / workbuddy-status`，实际仍指向旧脚本：
  - `workbuddy_moni_trader.py`
- 与此同时，`close-node` 复核和学习桥接读取的却是：
  - `workbuddy_local_challenger.py` 生成的本地账本文件
- 这导致系统出现“旧执行链在跑、新复核链在读旧文件”的错位，形成：
  - challenger 没上场
  - 复核层却误判为 `ok`

#### 2. 本轮修复

- `v10_auto_runner.py`
  - 新增 `workbuddy-smart-sell` phase。
  - 将：
    - `workbuddy-buy`
    - `workbuddy-sell`
    - `workbuddy-status`
  - 全部切换到：
    - `workbuddy_local_challenger.py`
  - 主策略 `prewarm / decision / buy / smart-sell / close-node` 仍保持原链，不与 challenger 混线。

- `register_workbuddy_tasks.py`
  - 将 Workbuddy 计划任务改成 challenger 独立时刻表：
    - `09:31 OpeningData`
    - `09:33 WorkbuddyStatus`
    - `09:47 / 10:17 / 10:47 / 11:17 / 13:47 / 14:17 / 14:47 WorkbuddySmartSell`
    - `13:38 WorkBuddyRefresh`
    - `14:54 WorkbuddyBuy`
    - `15:03 WorkbuddyStatus`
    - `15:06 CloseNode`
  - 同时把旧的错误任务列入 `LEGACY_TASK_NAMES`，避免新旧任务并存。

- `workbuddy_local_review.py`
  - 收紧复核门槛，新增 blocker：
    - `candidate_source_trade_date_stale`
    - `account_summary_missing`
    - `track_record_missing`
    - `order_log_missing`
    - `account_summary_stale`
    - `no_execution_evidence_today`
    - `no_buy_sell_evidence_today`
  - 以后若 challenger 当日根本没跑、文件缺失、来源过旧，将直接判为 `degraded`，不再给假绿灯。

#### 3. 当前结论

- 这轮不是策略层调整，而是 `Workbuddy challenger 编排层纠偏`。
- 修完后，系统应当满足：
  - 真正执行的是 `local challenger`
  - 复核层读取的是当天 challenger 新文件
  - 学习桥接不再基于过期摘要误判

#### 4. 下一步

1. 重新注册 Workbuddy 计划任务，清掉旧任务并换成 challenger 独立时刻表。
2. 明天盘中观察：
   - `workbuddy_local_order_log.jsonl`
   - `workbuddy_local_track_record.csv`
   - `workbuddy_local_account_summary_latest.json`
   是否随各时点真实刷新。
3. 收盘复核时重点检查：
   - challenger 是否真正形成今日执行证据
   - 复核层是否不再对“没跑”的 challenger 给 `ok`

### Distill 窗口机制重构

#### 1. 20+5 渐进换窗正式落地

- `distill_local_templates.py` 已从“直接吃全量 raw_top100 目录”改为：
  - `20日核心窗 + 5日缓冲窗 + 渐进加权`
- 当前窗口模式定义为：
  - `core_plus_buffer_progressive`

- 核心行为：
  - 当样本仍只有 `20` 日时，全部归入 `core`
  - 当出现第 `21` 个交易日后：
    - 维持前 `20` 日为 `core`
    - 新增的最近交易日先进入 `buffer`
  - 随着新交易周继续推进，`buffer` 最多扩展到 `5` 个交易日
  - 当超过 `25` 个交易日后，最旧日期开始逐日淘汰

- 这等价于：
  - `先延长，再平滑剔除`
  - 不是“每天硬滚20日重算”

#### 2. 权重机制

- 窗口内样本不再等权：
  - 最旧一段 `core`：低权重 `0.7`
  - 中段 `core`：中性权重 `1.0`
  - 最近一段 `core`：增强权重 `1.1`
  - `buffer`：高权重 `1.35`

- 这些权重已经接入模板验收指标计算：
  - `top100/top50/top30/top10_hit_rate`
  - `hit_day_rate`
  - `front_shift_score`
  - `avg_hit_rank`
  - `candidate_win_rate`
  - `candidate_avg_return`
  - `portfolio_positive_day_rate`

- 这样做的目的就是：
  - 保留旧模板惯性
  - 让新近资金迁移更快进入评分
  - 避免冠军模板日级剧烈换脸

#### 3. 新增窗口产物

- 新增：
  - `workbuddy_distill/artifacts/distill_window_profile_latest.json`
- 注册表与候选池主链现在都会携带窗口画像：
  - `combined_template_registry.json`
  - `workbuddy_candidate_pool_latest.json`

- 当前已验证：
  - `2026-06-22` 已作为第一个 `buffer` 交易日并入窗口
  - 当前窗口为：
    - `20 core + 1 buffer`

#### 4. Distill 自动化入口

- 新增总入口：
  - `refresh_distill_pipeline.py`
- 它负责：
  - 自动识别最新已完成交易日
  - 若缺少当日 `raw_top100`，自动抓取 `TDX` 排名
  - 重跑本地蒸馏
  - 重建 `Workbuddy distill challenger` 主候选池

- 已新增计划任务注册脚本：
  - `register_distill_refresh_tasks.py`
- 正式注册任务：
  - `TLFZ-Distill-Refresh`
  - 执行时间：`15:12`

#### 5. Workbuddy challenger 自动化正式注册

- `register_workbuddy_tasks.py` 已按新的 challenger 链路正式执行。
- 当前系统内已确认存在的新任务：
  - `TLFZ-WorkBuddy-OpeningData`
  - `TLFZ-WorkBuddy-Status0933`
  - `TLFZ-WorkBuddy-SmartSell0947`
  - `TLFZ-WorkBuddy-SmartSell1017`
  - `TLFZ-WorkBuddy-SmartSell1047`
  - `TLFZ-WorkBuddy-SmartSell1117`
  - `TLFZ-WorkBuddy-WorkBuddyRefresh`
  - `TLFZ-WorkBuddy-SmartSell1347`
  - `TLFZ-WorkBuddy-SmartSell1417`
  - `TLFZ-WorkBuddy-SmartSell1447`
  - `TLFZ-WorkBuddy-Buy1454`
  - `TLFZ-WorkBuddy-Status1503`
  - `TLFZ-WorkBuddy-CloseNode`

#### 6. 当前状态

- 这轮完成后，`Workbuddy challenger` 不再是“空接线”。
- 现在已经具备：
  - `收盘后自动同步 TDX 基础数据`
  - `20+5 渐进换窗蒸馏`
  - `冠军模板与候选池自动刷新`
  - `次日 Workbuddy challenger 按独立任务链运行`

#### 7. 下一步观察重点

1. 明天收盘后确认 `2026-06-23` 是否作为第二个 `buffer` 日自动进入窗口。
2. 当 `buffer` 累积到 `5` 日后，继续观察下一交易周是否开始逐日剔除最旧日期。
3. 复核冠军模板是否因新窗口而保持“平滑进化”，而不是剧烈跳变。

---

## 2026-06-18

### 今日完成

#### 1. 卖单执行稳版本补强

- 按“真实可卖优先”的思路重构卖出数量判断：
  - 先看真实 `avail_count`；
  - 再看账本持仓上限；
  - 再扣减同股票未完成卖单占用；
  - 三者取最小值后再按整手取整。

- `smart-sell` 与买入前的 `T+5` 兜底卖出现在都会识别同股票未完成卖单。
- 若挂单已经占满可卖股数，不再重复报卖，避免再次出现“可用数量不足”的废单。

#### 2. pending 卖单跟踪增强

- 补了活跃卖单上下文汇总：
  - 逐股统计未完成卖单占用股数；
  - 逐股保留活跃挂单列表，供后续复核。

- 修复了委托号提取兼容性：
  - 同时兼容 `orderId` 和 `orderID`；
  - 避免成功报单后 `pending` 里丢失委托号，导致后续状态跟踪失真。

#### 3. 下一巡检窗口重评估框架

- 已把“撤单重报”改成保守框架，不再用秒级超时。
- 当前逻辑是：
  - 至少等待到下一轮巡检重评估窗口；
  - 若盘面进一步转弱，标记为可进入撤单重评估；
  - 若盘面未进一步转弱，则继续等待。

- 当前代码里还没有稳定可用的撤单接口，因此这一版先不直接自动撤单。
- 先把“该不该重复卖”判断做准确，把重复卖单和废单先堵住。

### 当前阶段判断

- 卖单链路已经从“知道有 pending，但仍可能重复报卖”，推进到“按真实可卖和挂单占用做硬约束”。
- 这属于 `V1 稳执行` 的继续收口，也是 `V3 灵活卖` 进入更真实订单管理的第一步。

### 当前最重要结论

- 今天上午那类“前一笔卖单还占着仓位，后一笔又继续卖”的问题，核心不是策略判断错，而是执行层缺少挂单占用约束。
- 这一轮已经把这个核心漏洞补上。
- 但“撤单后自动重报”和“更主动的限价卖出”还要等接口能力确认后再继续推进。

### 下一步动作

1. 继续观察今天下午和后续几天是否还出现同股票重复卖单。
2. 继续核查 `mx` 是否提供稳定可用的撤单接口。
3. 若接口支持撤单，再把“下一窗口重评估 -> 撤单 -> 刷新持仓/订单 -> 重报”正式闭环。

#### 4. 计划任务窗口长时间不退出问题已收口

- 已定位到今天计划任务 PowerShell 窗口长时间不关闭的两个根因：
  - `buy-watch` 在 `14:50` 还会再次执行 `scanner_v10.py`，与 `14:49 decision` 重复扫描，容易拖慢甚至卡住尾盘执行；
  - `v10_auto_runner.py` 对子步骤使用无超时的 `subprocess.run()`，只要扫描或报告任一步骤卡住，任务窗口就会一直挂着。

- 已做的修复：
  - `buy` phase 不再重复跑 `scanner_v10.py`，改为直接消费最新扫描结果；
  - `auto_runner` 为各步骤增加超时控制，超时会记录到 `phase_history` 并退出；
  - `buy-watch` 过了 `14:57` 会立即结束，不再继续挂着窗口等待。

- 这次修复的直接意义：
  - 避免尾盘 `decision` 和 `buy-watch` 同时重复扫描；
  - 避免单个子脚本卡死时把整条计划任务窗口无限拖住；
  - 提高尾盘自动买入真正走到下单阶段的概率。

#### 5. 划线计划第一版已完成设计

- 已把“如何用曲线真实观察系统改进”正式写入执行计划书。
- 当前不再把“收益线”当成唯一答案，而是明确拆成四层：
  - 结果曲线
  - 执行曲线
  - 策略曲线
  - 学习曲线

- 当前确定的核心思想：
  - 不能只画一条净值线；
  - 必须把市场行情、执行稳定性、策略质量、模型进化拆开观察；
  - 否则很容易把“市场帮忙”误判成“模型学会了”。

- 当前确定的第一版重点曲线包括：
  - 总资产曲线；
  - 累计已实现盈亏曲线；
  - 本周新增平仓收益曲线；
  - 买卖成功率与 `112` 次数曲线；
  - 主基准与辅基准对比曲线。

- 当前确定的基准口径：
  - 主基准随真实交易宇宙动态切换；
  - 当前阶段默认以 `中证1000` 为主基准；
  - 同时观察 `创业板指` 作为成长风格和市场温度参考。

- 当前还明确了：
  - 图上必须标注关键版本节点；
  - 以后要把“修复动作”和“曲线拐点”建立对应关系；
  - 这样才能更真实地判断哪些改进真的有效。

### 当前阶段判断

- 系统现在已经不只是“能跑自动化”，而是开始进入“可观测、可解释、可对标”的阶段。
- 这一步虽然还没开始真正画图，但已经把后续仪表板的骨架和数据口径先定住了。

### 当前最重要结论

- 后续看系统是否进步，不能只盯总资产或单周盈利。
- 更重要的是用分层曲线回答三个问题：
  - 是不是赚到了；
  - 是不是靠稳定执行赚到的；
  - 是不是靠更好的决策和学习赚到的。

### 下一步动作

1. 先按新设计确定每条曲线的日度/周度落盘格式。
2. 再做第一版“结果 + 执行 + 基准”三类图。
3. 样本再积累一段后，接着补“策略 + 学习”两类图。

#### 6. 曲线输出格式和目录骨架已定

- 已在 `a-share-analyst` 下创建统一输出目录：
  - `curve_observatory/data/`
  - `curve_observatory/charts/png/`
  - `curve_observatory/charts/html/`
  - `curve_observatory/reports/`

- 当前明确的格式分工：
  - `CSV` 作为主数据格式；
  - `PNG` 作为正式归档图格式；
  - `HTML` 作为交互分析格式；
  - `MD` 作为口径说明和周报格式；
  - `XLSX` 不作为底层标准格式，只在需要人工导出时辅助使用。

- 当前已把命名规范也固定下来，避免后续脚本各写各的：
  - 数据文件：`curve_<metric_name>_<granularity>.csv`
  - 图表文件：`curve_<theme>_<window>_<yyyymmdd>.png/html`
  - 周报文件：`curve_review_<yyyy_ww>.md`

- 这一步的意义：
  - 先把“数据、图、解释”三层拆开；
  - 后面不管是自动生成图，还是手工做周报，都不会口径漂移或目录混乱。

#### 7. 第一批基础曲线 CSV 已落地

- 已新增导出脚本：
  - `skills/a-share-analyst/generate_curve_csvs.py`

- 当前已经能自动生成这 4 份基础数据表：
  - `curve_nav_daily.csv`
  - `curve_realized_pnl_daily.csv`
  - `curve_trade_success_daily.csv`
  - `curve_benchmark_daily.csv`

- 当前 4 张表的口径分工如下：
  - `curve_nav_daily.csv`
    - 取每天最后一份有效账户快照；
    - 作为总资产、浮盈、已实现盈亏曲线的基础表。
  - `curve_realized_pnl_daily.csv`
    - 按 `track_record` 的真实 `closed` 记录汇总；
    - 同时保留日度新增平仓盈亏和累计已实现盈亏。
  - `curve_trade_success_daily.csv`
    - 按 `trade_api_log` 汇总每天买卖请求、成功率、`112` 次数、`501` 次数；
    - 以后看执行层改善，优先看这张表。
  - `curve_benchmark_daily.csv`
    - 当前用 `中证1000` + `创业板指`；
    - 同时写入策略净值归一化线和基准归一化线；
    - 若某天不是交易日或指数当日无数据，就保留空值，不伪造行情。

- 当前这一步已经把“口头复盘”推进到“结构化可出图数据”阶段：
  - 以后出图不再直接从原始日志临时拼；
  - 而是优先从这 4 张标准表出图。

### 当前阶段判断

- 曲线观测体系已经从“只有设计”推进到“第一批基础数据真正落盘”。
- 现在已经具备直接制作第一版结果图、执行图和基准对比图的条件。

### 当前最重要结论

- 当前最值钱的不是图本身，而是我们已经把底层口径固定住了：
  - 总资产怎么取；
  - 已实现盈亏怎么累计；
  - 买卖成功率怎么算；
  - 主基准和辅基准怎么归一化。

### 下一步动作

1. 直接基于这 4 张表生成第一版 `PNG` 图。
2. 把关键版本节点叠加到图上。
3. 再补周度解读 `MD` 模板，让“图 + 解释”一起成型。

#### 8. 第一版 HTML 仪表盘已落地

- 已新增仪表盘生成脚本：
  - `skills/a-share-analyst/generate_curve_dashboard.py`

- 当前已经正式生成两层 HTML：
  - 最新总仪表盘：
    - `curve_observatory/charts/html/dashboard_latest.html`
  - 周度归档仪表盘：
    - `curve_observatory/charts/html/dashboard_2026_w25.html`

- 当前这两层 HTML 已经具备：
  - 顶部账户总览卡片；
  - 结果层曲线：
    - 总资产
    - 累计已实现盈亏
    - 浮动盈亏
  - 本周新增平仓收益图；
  - 执行层成功率图；
  - `112` / `501` 错误次数图；
  - 策略净值 vs `中证1000` vs `创业板指` 对比图；
  - 已实现盈亏表和执行明细表。

- 当前采用的是完全自包含 HTML：
  - 不依赖外部 CDN；
  - 不依赖浏览器联网拉图表库；
  - 双击本地文件即可打开查看。

- 当前这一步的意义：
  - 已经从“只有标准化 CSV 数据”推进到“真正可日常查看的仪表盘界面”；
  - 以后日更时更新 `dashboard_latest.html`；
  - 周更时归档一个按周编号命名的 HTML 快照。

### 当前阶段判断

- 曲线观测体系已经从“设计 -> 数据表 -> 仪表盘”连续落地。
- 现在已经具备长期观察系统结果、执行质量和相对基准表现的正式入口。

### 当前最重要结论

- 这套 HTML 仪表盘已经可以作为后续长期观察的主界面使用。
- `latest` 负责日常看当前状态，`weekly archive` 负责长期回看每周快照。

### 下一步动作

1. 继续补第一版 `PNG` 归档图。
2. 把关键版本节点叠加到图和周报里。
3. 后续再把策略层和学习层曲线继续接入仪表盘。

#### 9. 学习样本过滤器已接入并完成首轮验证

- 已在 `skills/a-share-analyst/evolving_model.py` 正式接入学习样本过滤逻辑，目标是把“决策质量”和“执行噪声”分开记账。
- 当前新增的过滤口径包括：
  - 决策日期优先从 `run_slot` 归一化，修复 `recorded_at` 与 intended trade date 错位；
  - `external_sync` 和 `[AUTO_IMPORTED]` 样本直接阻断，不进入学习；
  - `closed` 样本必须同时具备买入和卖出执行证据；
  - 若买卖日志中出现 `112` 或 `501`，标记为 `execution_noise`，不进入学习。

- `learning` 结构已新增：
  - `sample_filter`
  - `gross_matched_trades`
  - `eligible_matched_trades`
  - `reason_counts`
  - `reason_counts_top`

- `v10_evolving_model_changelog.jsonl` 的 `skip_update` 现在也会带上样本过滤摘要，后续能直接看出：
  - 是普通样本不足；
  - 还是“有样本但被执行噪声过滤掉了”。

- 已完成两类验证：
  - 语法校验：`python -m py_compile evolving_model.py` 通过；
  - 真实账本验证：当前 `14` 条 `closed` 记录里，`5` 条被识别为 `external_sync_record`，`9` 条被识别为 `missing_decision_match`，`eligible_matched_trades = 0`。

- 已完成定向验算，确认过滤分支可用：
  - 缺买入成功证据的样本会落到 `missing_buy_fill_evidence`；
  - 带真实 `112/501` 卖出噪声的样本会落到 `execution_noise`；
  - 干净样本会落到 `eligible`。

### 当前阶段判断

- 学习层现在已经不是“拿所有平仓结果直接学”，而是进入“先判样本资格，再决定能不能学”的阶段。
- 这一步是学习层图表能否有真实解释力的前置条件。

### 当前最重要结论

- 目前学习层还没有开始真实更新，不是模型失效，而是当前账本里的已平仓样本大多属于历史遗留、外部同步或缺少完整决策映射。
- 这正说明过滤器在起作用：它宁可先不学，也不让脏样本把学习层带偏。

### 下一步动作

1. 继续积累带完整 `decision -> buy -> sell` 证据链的真实闭环样本。
2. 等 `eligible_matched_trades` 开始稳定出现后，再把学习层曲线接入 HTML 仪表盘。
3. 后续若需要，再把 `missing_sell_fill_evidence` 等阻断原因单独做成学习层质量监控表。

#### 10. 学习准备度面板已落地第一版

- 已新增日度数据表：
  - `curve_observatory/data/curve_learning_readiness_daily.csv`
- 当前口径固定为：
  - 从 `2026-06-18` 开始记；
  - 每个交易日收盘后更新一次；
  - 取当日最后一笔学习状态快照作为该交易日正式记录。

- 当前这张表已记录的核心字段包括：
  - `recent_closed_trades`
  - `gross_matched_trades`
  - `eligible_matched_trades`
  - `gross_match_rate_pct`
  - `eligible_rate_pct`
  - `clean_after_match_rate_pct`
  - `top_block_reason_1`
  - `top_block_reason_2`
  - `learning_status`

- 已把学习准备度面板正式接入：
  - `curve_observatory/charts/html/dashboard_latest.html`
  - `curve_observatory/charts/html/dashboard_2026_w25.html`

- 当前仪表盘新增的内容包括：
  - 顶部学习准备度卡片；
  - 学习准备度样本流量图；
  - 学习准备度转化率图；
  - 学习准备度明细表。

- 当前这一步的意义：
  - 先不画“模型学会了多少”的成效图；
  - 先正式观测“学习飞轮的燃料有没有开始稳定进入”；
  - 把“没开始学”与“正在等干净样本”这两件事分开显示。

### 当前阶段判断

- 学习层现阶段已经进入“可日更监控准备度”的状态。
- 后续每天收盘后都能看到：
  - 新增平仓样本有多少；
  - 决策匹配进了多少；
  - 真正可学习的样本有多少；
  - 主要卡在哪个阻断原因上。

### 当前最重要结论

- 学习飞轮现在还没有正式点火，但已经有了稳定的点火前仪表。
- 之后最重要的不是猜模型会不会学，而是直接看：
  - `gross_matched_trades` 何时开始出现；
  - `eligible_matched_trades` 何时开始稳定增长；
  - `learning_status` 何时从等待状态进入首次更新。

### 下一步动作

1. 继续按交易日收盘后更新学习准备度面板。
2. 重点观察 `missing_decision_match` 是否下降。
3. 等出现连续可学习样本后，再补学习层成效曲线和权重变化图。

#### 11. 仪表盘自动化与交易日历已接入第一版

- 已先核实当前可用的交易日历来源：
  - `pytdx` 在本项目里主要用于指数/行情抓取，不适合作为权威交易日历主来源；
  - 当前仓库里的 `MX` 接口是模拟交易接口，不是 `EMQuant` 交易日历接口，现成并不提供交易日历能力。
- 因此本轮先采用更稳的方案：
  - 新增 `skills/a-share-analyst/trading_calendar.py`；
  - 内置 `2025-2026` 年沪深交易所官方休市日；
  - 统一用它判断“今天是否交易日”“最新已完成交易日”“本周归档周编号”。

- 已新增：
  - `skills/a-share-analyst/update_curve_observatory.py`
- 当前职责是：
  - 先按交易日历解析 `as_of_date`；
  - 再自动生成 `curve_observatory/data/*.csv`；
  - 再自动生成：
    - `curve_observatory/charts/html/dashboard_latest.html`
    - `curve_observatory/charts/html/dashboard_<yyyy>_w<ww>.html`

- 已改造：
  - `generate_curve_csvs.py`
  - `generate_curve_dashboard.py`
- 当前都支持 `--as-of-date YYYY-MM-DD`：
  - 节假日或手动补跑时，会按最近已完成交易日裁剪数据；
  - 避免把休市日误写成新的正式观测日期。

- 已改造：
  - `v10_auto_runner.py`
- 当前 `report` phase 会自动追加执行：
  - `update_curve_observatory.py`
- 同时新增交易日日历保护：
  - `prewarm`
  - `decision`
  - `buy`
  - `add-position`
  - `smart-sell`
  - `sell`
  - `midday-review`
  - `report`
  在非交易日会直接 `skipped`，避免节假日误更新。

- 已完成验证：
  - `python -m py_compile trading_calendar.py generate_curve_csvs.py generate_curve_dashboard.py update_curve_observatory.py v10_auto_runner.py` 通过；
  - `python update_curve_observatory.py --as-of-date 2026-06-18` 通过；
  - 校验 `2026-06-19` 端午休市日时，最新已完成交易日会正确回退到 `2026-06-18`，周归档仍落在 `2026 W25`。

### 当前阶段判断

- 现在两层仪表盘已经从“手动生成”推进到“可挂在收盘后自动更新”的状态。
- 同时节假日误跑导致日期和周归档错位的问题也已经在自动化层先堵住了。

### 当前最重要结论

- 仪表盘自动化现在已经具备正式接入日常收盘流程的条件。
- 当前交易日历口径已统一，不再依赖“今天是不是工作日”的粗判断，而是按真实交易日判断更新日期和周期。

### 下一步动作

1. 下一个交易日观察 `report` phase 是否自动带出 observatory 更新。
2. 若运行稳定，再考虑把交易日历从内置表升级成可选 `EMQuant` 动态源。
3. 后续如需跨 2026 年之后继续用，只需补一版新的官方休市日表。

#### 12. 仪表盘头部状态条与预览验收已补齐

- 已继续优化 `dashboard_latest.html` / 周归档仪表盘头部展示：
  - 新增 `数据截至`
  - 新增 `页面生成于`
  - 新增 `学习状态`
- 这三个字段现在会固定显示在页面顶部，打开页面后第一眼就能判断：
  - 这是不是当天最新版本；
  - 数据截至哪一个交易日；
  - 学习层当前处于什么状态。

- 今天已完成本地预览验证：
  - `dashboard_latest.html` 可以正常通过本地预览服务打开；
  - 页面头部状态条显示正常；
  - 当前可直接作为下周一自动更新验收入口使用。

- 当前这一步的意义：
  - 不是再增加新指标；
  - 而是把“自动化是否真的更新成功”做成肉眼可直接核对的页面信号；
  - 避免下周一只看到图，却不能第一时间确认是不是当天新生成的版本。

### 当前阶段判断

- 仪表盘现在已经不只是“能自动生成”，还具备了“打开即能验证是否更新成功”的可验收性。

### 当前最重要结论

- 下周一收盘后，优先打开 `dashboard_latest.html`，先看：
  - `数据截至`
  - `页面生成于`
- 只要这两项更新到下周一收盘后的新时间，就说明这条自动化链路已经跑通。

### 下一步动作

1. 下周一收盘后按页面头部状态条验证自动更新是否生效。
2. 若验证通过，后续把这套仪表盘作为日常固定观察入口。

#### 13. 尾盘执行纪律已正式收口到 `14:49` / `14:50` 分界

- 已正式确认新的尾盘执行纪律：
  - 当天卖单只允许在 8 个半小时巡检窗口内处理：
    - `09:45`
    - `10:15`
    - `10:45`
    - `11:15`
    - `13:15`
    - `13:45`
    - `14:15`
    - `14:45`
  - 当天卖单必须在 `14:49` 前完成本日最后一次处理；
  - `14:50` 开始以后，执行资源全部留给当天买单计划，不再让卖单继续占用尾盘买入窗口。

- 当前这样定的原因已经明确：
  - `14:57` 后进入尾盘竞价排队阶段，越晚报入，成交概率越容易下降；
  - 健康日运行证据表明，这台机器在流程顺畅时可以在 `14:51` 左右发出首批成功买单；
  - 异常日拖穿窗口的核心问题是流程阻塞，不是单纯硬件算力不够；
  - 因此 `14:57` 只能视为最晚止损线，不能再让卖单在 `14:50` 后继续抢占买入时间预算。

- 这条纪律落地后，尾盘时间资源的分工正式变为：
  - `14:45`：卖单最后一轮巡检与处理；
  - `14:49`：完成 decision，冻结当日买入计划；
  - `14:50-14:57`：只服务当天买单执行，不再插入当日卖单动作。

### 当前阶段判断

- 执行层现在已经不再把尾盘买入窗口视为“卖买混用”的模糊区间，而是开始按明确时段分工管理。
- 这属于下周执行层稳定化的核心纪律，不是临时建议。

### 当前最重要结论

- 从今天开始，`14:50` 后的尾盘窗口优先级正式全部让给买单。
- 卖单是否足够灵活，不再靠挤占尾盘买入时间解决，而是靠前面 8 个巡检窗口把该卖的处理完。

### 下一步动作

1. 下周优先继续收口 `buy` phase，确保 `14:50` 后只走短链路，不再引入重扫描或其他阻塞步骤。
2. 继续补齐撤单闭环、pending 状态机和接口健康恢复，让卖单在前置窗口内更稳定完成。
3. 重点观察下个交易日 `14:50-14:57` 是否能更稳定地在 `14:52` 前发出首单、`14:55` 前完成主买单。

#### 14. 尾盘买入短链路与卖单截止边界已做实装优化

- 已把 `14:50` 买入主链路进一步压缩为短链路：
  - `do_buy()` 不再在尾盘买入阶段插入 `T+5` 卖出动作；
  - 买入成功后的对仓与成交回补，改为按本轮成功买单批量回收，不再每下一笔就整套刷新一次账户/持仓/订单；
  - 新增对未完成买单的拦截，若同代码已有活跃买单，尾盘不再重复报单。

- 已把“卖单必须在 `14:49` 前完成”的纪律真正落到代码入口：
  - 卖出/加仓窗口统一收口到 `09:35-14:49`；
  - `smart-sell` 在 `14:49` 后会直接跳过；
  - 这样 `14:50` 以后不会再让卖单动作继续侵占尾盘买入时间。

- 已同步优化 `buy-watch` 调度策略：
  - `BuyWatch` 计划任务改为 `12` 次最多尝试、`30` 秒间隔；
  - `buy` step 超时从 `240s` 收紧到 `180s`；
  - watch 重试只保留更像瞬时故障的返回码：
    - 窗口未到
    - 运行时错误
    - 扫描快照未就绪
    - step timeout
  - 不再对“无信号/无动作”做无意义重试。

- 今天已完成校验：
  - `python -m py_compile v10_moni_trader.py v10_auto_runner.py register_workbuddy_tasks.py` 通过；
  - 已重新注册计划任务，`BuyWatch.cmd` 当前已更新为 `--max-attempts 12 --interval-seconds 30`。

### 当前阶段判断

- 这一轮不是再调策略，而是把尾盘执行从“逻辑上应该分工”推进到“代码和任务调度都按这个分工执行”。
- `14:50` 窗口现在更接近真正的买入专用短链路。

### 当前最重要结论

- 从执行层看，今天已经把两个最容易吞掉尾盘时间的问题直接压下去了：
  - 买入阶段夹带卖单；
  - 买入后逐笔做重型回补。
- 下一个交易日最值得重点观察的，就是首单发出时间是否明显前移、重试是否更聚焦瞬时故障。

### 下一步动作

1. 实盘观察下个交易日 `14:50-14:57` 的首单发出时间和主买单完成时间。
2. 若仍有拖慢，再继续压缩买入后的成交回补与报告写盘动作。
3. 再往下一步，补撤单闭环和更完整的 pending 状态机。

---

## 2026-06-19

### 今日结论

- 今天的重点不是再动交易逻辑，而是先把 `14:30-15:00` 这段执行链路里的可疑串扰点压下去，同时把盘后复查证据链补齐。
- 当前判断进一步明确：
  - `prewarm` 是关键前置扫描环节，允许耗时波动，重点是给它留足时间；
  - 真正更像干扰源的是 `14:45` 这班 `smart-sell`，它更容易和 `14:49 decision`、`14:50 buy-watch` 在尾盘窗口内发生重叠。

### 今日完成

#### 1. 14:45 尾盘卖出干扰项已停用

- 已禁用计划任务 `TLFZ-WorkBuddy-SmartSell1445`。
- 当前状态已确认：
  - `State = Disabled`
  - `Enabled = False`

- 这样处理的目标不是否定 `smart-sell` 本身，而是先拿掉 `14:45` 这个最接近尾盘主链路的重叠点，观察：
  - `14:49 decision` 是否更稳定；
  - `14:50 buy-watch` 是否更少出现被挤占或拖尾。

#### 2. 下午执行层已补 run id 与耗时追踪

- 已为以下下午活跃任务补上任务元信息：
  - `14:15` `TLFZ-WorkBuddy-SmartSell1415`
  - `14:30` `TLFZ-WorkBuddy-Prewarm`
  - `14:49` `TLFZ-WorkBuddy-Decision`
  - `14:50` `TLFZ-WorkBuddy-BuyWatch`

- 当前每次执行都会记录：
  - `run_id`
  - `task_name`
  - `trigger_slot`
  - `started_at`
  - `finished_at`
  - `duration_seconds`
  - `attempt`
  - `status`
  - `exit_code`

- 这样收盘后可以直接按同一个 `run_id` 复盘：
  - `prewarm` 何时开始、何时结束；
  - `decision` 是否在 `prewarm` 完成后正常衔接；
  - `buy-watch` 是否出现异常拖尾、重试或超时。

#### 3. 保留旧日志兼容，同时新增更适合盘后复查的明细输出

- 旧的 `automation_status/phase_history.csv` 继续保留，避免打断现有复盘习惯。
- 同步新增：
  - `automation_status/phase_history_detailed.csv`
  - `automation_status/latest_prewarm_status.json`
  - `automation_status/prewarm_timing_signal.json`

- 新增明细输出的目的，是把过去“只能看到 phase 发生过”推进到“能看到具体是哪次任务、跑了多久、何时结束、是否建议调整时间槽”。

#### 4. 已加入 prewarm 盘后学习信号

- 当前不是自动改计划任务时间，而是先输出学习信号，辅助盘后决策。
- 学习逻辑如下：
  - 若 `prewarm` 正常完成且耗时过长，或距 `14:49 decision` 的缓冲过小，则输出建议：
    - `suggest_move_to_14_25`
  - 若 `prewarm` 正常且窗口充足，则输出：
    - `keep`
  - 若 `prewarm` 失败，则输出：
    - `review`

- 这意味着后续盘后复查时，已经可以把“是否该把 `prewarm` 从 `14:30` 提前到 `14:25`”纳入可执行的学习判断，而不是靠主观感觉决定。

#### 5. 已完成验证

- `v10_auto_runner.py` 已通过 `python -m py_compile` 语法检查。
- 已用临时数据目录做过非交易日跳过演练，确认以下输出会正确落盘：
  - `latest_phase_status.json`
  - `latest_prewarm_status.json`
  - `phase_history.csv`
  - `phase_history_detailed.csv`
  - `prewarm_timing_signal.json`

### 当前阶段判断

- 当前阶段的重点已经从“继续压缩所有阶段耗时”切换为“给关键前置扫描留足空间，并让尾盘主链路可复盘、可归因”。
- 对 `prewarm` 的正确处理不是盲目提速，而是：
  - 允许它因行情复杂度出现耗时波动；
  - 同时确保它不会被更靠后的任务打断；
  - 并用盘后学习信号判断它是否需要从 `14:30` 前移到 `14:25`。

### 当前最重要结论

- 今天已经把 `14:45-15:00` 里最可疑的一个干扰点先移除了：
  - `TLFZ-WorkBuddy-SmartSell1445`
- 同时，盘后已经可以更精确地回答这些问题：
  - `prewarm` 实际跑了多久；
  - `decision` 是否在 `prewarm` 结束后正常衔接；
  - `buy-watch` 是否存在异常拖尾或重试；
  - 是否需要把 `prewarm` 的起始时间提前到 `14:25`。

### 下一步动作

1. 下个交易日收盘后，优先查看 `phase_history_detailed.csv` 与 `prewarm_timing_signal.json`。
2. 重点确认 `prewarm` 到 `decision` 之间的缓冲是否稳定，是否已经明显减少尾盘串扰。
3. 若学习信号连续提示 `suggest_move_to_14_25`，再正式评估并调整 `prewarm` 的计划任务起始时间。

### 三层逻辑梳理（现阶段）

#### 1. 执行层逻辑

- 当前执行层的职责，是把“按交易日历触发的计划任务”稳定地串成一条可复查、可归因的自动化链路，而不是在这一层做交易判断。
- 现阶段的交易日主链路可理解为：
  - `11:35` 午间复盘：中场校准持仓、订单、账本一致性，不直接下单；
  - `14:15` smart-sell：下午卖出巡检窗口之一；
  - `14:30` prewarm：关键前置扫描阶段，为尾盘决策准备数据；
  - `14:49` decision：尾盘决策扫描阶段；
  - `14:50` buy-watch：尾盘买入观察与执行阶段；
  - `15:06` report：盘后汇总、学习层刷新。

- 当前执行层的几个明确原则：
  - 所有 phase 先过交易日历判断，非交易日直接 `skipped`；
  - 买入窗口严格限定在 `14:50-14:57`；
  - 卖出/加仓窗口严格限定在 `09:35-14:49`；
  - `smart-sell` 只允许在固定巡检点触发，不允许任意时刻插入。

- 当前执行层的记录与诊断能力：
  - 已补 `run_id`、`task_name`、`trigger_slot`；
  - 已补 `phase/step` 级 `started_at / finished_at / duration_seconds`；
  - 现在可以明确复盘：
    - 哪个任务实例在何时开始；
    - 跑了多久；
    - 是否与其他 phase 重叠；
    - 是否因为交易日历或窗口判断被跳过。

- 当前执行层对 `14:30-15:00` 的处理共识：
  - `prewarm` 是前置关键环节，允许耗时波动，重点是给足时间；
  - `14:45` 的 `smart-sell` 更像尾盘窗口内的潜在串扰源，因此本轮先禁用；
  - 现阶段暂不改变 `11:35` 午间复盘与 `15:06` report 的位置和职责。

#### 2. 交易策略层逻辑

- 当前交易策略层的职责，是回答两件事：
  - 什么票值得买；
  - 什么持仓该卖、该加仓、还是继续拿。

- 买入侧当前逻辑分成两段：
  - 第一段是 `scanner_v10.py` 的规则扫描：
    - 先根据中证 `1000` 总成交额判断市场冷热；
    - 再决定扫描范围和个股成交额门槛；
    - 再从日线、周线、当日 `5` 分钟数据提特征；
    - 最终把候选按 `Tier1 / Tier2 / Tier3 / no_signal` 归类。
  - 第二段是 `evolving_model.py` 的模型二次排序：
    - 对每个候选再计算 `market / sector / stock / flow` 四类分数；
    - 再叠加 `tier_bias / mode_bias`；
    - 再根据市场状态决定当日最低入场分数门槛；
    - 最终只把模型分足够高的票送进 `buy` 逻辑。

- 买入执行当前强调“分层、分仓、首仓不满仓”：
  - `T1 / T2 / T3` 各有不同的单票目标仓位和首次建仓比例；
  - 默认先建底仓，给 `T+1` 加仓留空间；
  - 只有非常强的 `V9_full` 级别共振，才允许更激进的首仓。

- 买入阶段当前的约束重点：
  - 必须使用当日且足够新的扫描快照；
  - 已有真实持仓、账本 holding、未完成买单的代码会被过滤；
  - `14:50` 后的链路尽量保持为买入短链路，不再夹带卖出动作。

- 卖出侧当前分两类：
  - `sell`：只做 `T+5` 兜底；
  - `smart-sell`：根据信号衰减提前卖出，`T+5` 是兜底而不是硬性持仓期限。

- `smart-sell` 当前主要看这些信号是否转弱：
  - 冲高回落上影线；
  - 放量滞涨；
  - 大阴线或连续走弱；

  - 尾盘从杀跌买点演化成拉高出货；
  - 周线 slope 走平或转负；
  - 高盈利仓位对转弱更敏感，优先兑现。

- 加仓侧当前逻辑也比较克制：
  - 只对已有底仓且未达目标仓位的票加仓；
  - 主要在 `T+1` 到 `T+4` 窗口内考虑；
  - 若信号已衰减，则不加仓。

- 午间复盘与盘后报告当前在策略层的定位：
  - `11:35` 午间复盘主要是持仓、账本、订单状态校准和下午观察清单，不直接驱动 `decision/buy`；
  - `15:06` 盘后报告主要是复盘与学习层输入整理，也不参与当日下午的自动交易决策。

#### 3. 学习层逻辑

- 当前学习层的职责，不是临盘即时改策略，而是收盘后把：
  - 当时为什么买；
  - 后来是否真的成交；
  - 最终赚亏如何；
  - 中间有没有执行噪音；
  串成一条干净的学习样本链。

- 当前学习层主要吃这些输入：
  - `track_record`：真实持仓与已平仓结果；
  - `model_decisions`：买入前所有候选及其模型分；
  - `trade_api_log`：真实下单/成交证据；
  - `backtest_summary`：各 `Tier` 的回测基线；
  - `phase_history_detailed` 与 `prewarm_timing_signal`：执行时序学习证据。

- 当前学习层分成三类输出：
  - 表现学习：
    - 汇总胜率、平均收益、`Tier` 表现、`mode` 表现；
    - 与回测基线对比；
    - 输出 `learning_notes`。
  - 模型学习：
    - 根据已闭环样本调整 `market / sector / stock / flow` 权重；
    - 调整 `tier_bias / mode_bias`；
    - 但有样本数量、冷却期、单次步长等 guardrail，不会暴力漂移。
  - 执行时序学习：
    - 目前先从 `prewarm` 开始；
    - 若 `prewarm` 耗时过长，或距 `14:49 decision` 的缓冲过小，则输出：
      - `suggest_move_to_14_25`
    - 这目前是建议型学习，不会自动改计划任务时间。

- 当前学习层的一个重要原则是“宁可学慢，也不要学脏样本”：
  - 没有真实成交闭环证据的样本不学；
  - 带明显执行噪音的样本不学；
  - 样本不足不学；
  - 冷却期未过不学。

- 因此，当前学习层的定位更适合理解为：
  - 它已经能够影响下一轮候选排序与入场门槛；
  - 但仍然是“收盘后逐步校准”的保守学习机制；
  - 不是日内高频自适应策略。

### 当前三层关系判断

- 执行层负责“按交易日历稳定触发并记录”；
- 交易策略层负责“在合适窗口内做买卖与持仓决策”；
- 学习层负责“在收盘后复盘结果并缓慢修正后续评分与时序建议”。

- 当前最重要的边界共识是：
  - 不在执行层直接改策略；
  - 不让午间复盘和盘后报告直接插手当日下午主交易链；
  - 先让学习层继续积累干净样本，再决定哪些信号值得进一步前移到临盘决策。

### 复核层框架与节点化落地（本轮新增）

#### 4. `复核层` 正式收口为 `午间节点 / 收盘节点 / 人类复核层`

- 今天正式统一口径：
  - `午间节点`：自动化复核上午已发生事实，必要时执行低风险纠偏，并在下午开盘前输出放行状态；
  - `收盘节点`：自动化复核全天结果、执行质量与样本可信度，并输出学习放行状态；
  - `人类复核层`：不要求日更，允许按周复核、月复核或异常触发复核，主要负责把握大方向，防止模型长期偏航。

- 这一层的最终目标已经明确：
  - 不是单纯“再多做一轮复盘”；
  - 而是把执行层、策略层、学习层之间加上一个质量闸门；
  - 只有通过复核层的样本，才进入学习层候选，从而保证模型沿正向路径优化。

- 复核层的正式定义，现阶段统一为：
  - 以自动复核为主、人类复核为辅；
  - 对执行层、交易事实层、基础设施层和学习输入层进行健康检查与质量把关；
  - 决定哪些结果可以进入后续学习候选，哪些异常只能观察、暂不进入学习；
  - 核心目标不是直接替模型做判断，而是防止模型被脏数据、执行噪音和基础设施异常带偏，从而保障模型沿着正向方向稳健进化。

- 现阶段复核层重点检查的范围也统一固定为：
  - 执行层健康：
    - 计划任务是否按交易日历触发；
    - 关键 phase 是否跳过、超时、失败、重叠；
    - `run_id`、耗时和状态链是否完整。
  - 交易事实健康：
    - 账本、持仓、pending、账户快照是否一致；
    - 是否存在重复下单、脏状态、过多人工修补痕迹。
  - 基础设施健康：
    - 仪表盘、曲线观测、收盘增强链路是否完整更新；
    - `MX` 收盘增强链路是否正常，是否出现 `warning / degraded`。
  - 学习输入健康：
    - 当天样本是否干净、是否有执行噪音；
    - 是否适合进入学习层候选。

#### 5. `午间节点` 已接入代码与时间纪律

- 已将原来的 `11:35 midday-review` 重构为 `11:35 MiddayNode`，并新增：
  - `13:00 MiddayGate`
- 现阶段时间纪律正式定义为：
  - `11:35` 启动午间节点主复核；
  - `13:00-13:05` 保留实时午间纠偏与最终放行窗口；
  - `13:05` 为硬截止线，超过该时点不再继续占用下午执行资源。

- 今天代码里已落地的午间节点职责包括：
  - 核对交易数据、底仓/持仓、pending、账户快照；
  - 自动执行低风险纠偏：
    - `sync_track_record`
    - `full_reconcile_positions`
    - `refresh_pending_orders`
    - 必要时保存修正后的账本；
  - 输出：
    - `v10_midday_node_latest.json`
    - `v10_midday_gate_latest.json`
    - `v10_pm_gate_status.json`

- `pm_gate_status` 现阶段会自动给出：
  - `pass`
  - `pass_with_limit`
  - `block_buy`
  - `block_all`

- 当前午间节点已经明确遵守一条硬约束：
  - `13:00-13:05` 只保留低风险实时纠偏，不自动执行高风险市场动作；
  - 例如撤单、改价重报、临时大改计划任务时间，仍不在这一轮自动化范围内。

#### 6. `收盘节点` 已接管原 `report` 的复核复盘职责

- 已将原 `15:06 report` 重构为 `15:06 CloseNode`。
- 当前收盘节点会：
  - 做收盘对仓与账本清理；
  - 刷新账户摘要与 NAV 相关产物；
  - 形成收盘节点复核结果；
  - 输出学习放行状态。

- 今天代码里已新增这些收盘节点输出：
  - `v10_close_node_latest.json`
  - `v10_learning_gate_status.json`

- `learning_gate_status` 现阶段会自动给出：
  - `allow`
  - `hold`
  - `reject`

- 当前学习放行的主要判断依据包括：
  - 账户快照是否为 `live`；
  - 收盘时是否仍有 `stale pending`；
  - 对仓是否仍依赖较多导入 / 覆盖 / 暂停动作；
  - 当天是否形成新增 `closed` 样本。

#### 7. 计划任务与包装器已同步更新

- 今天已重新注册计划任务，新的关键节点已生效：
  - `TLFZ-WorkBuddy-MiddayNode` `11:35`
  - `TLFZ-WorkBuddy-MiddayGate` `13:00`
  - `TLFZ-WorkBuddy-CloseNode` `15:06`

- 同时已确认：
  - `TLFZ-WorkBuddy-SmartSell1445` 没有被重新拉起；
  - 新包装器已经带上 `task_name` 与 `trigger_slot`，继续保障 `run_id` 和耗时链路可追踪。

#### 8. 本轮实现的边界与后续方向

- 本轮已经实现的是：
  - 复核层的自动化骨架；
  - 午间节点 / 收盘节点的结构化输出；
  - 下午放行状态与学习放行状态的明确落盘。

- 本轮暂未自动化的是高风险动作：
  - 自动撤单
  - 自动改价重报
  - 自动调整计划任务时间

- 当前共识保持为：
  - 先用复核层把事实核对、问题分级、低风险纠偏、放行状态做扎实；
  - 再观察一段交易日数据，决定哪些高风险动作值得继续自动化；
  - 人类复核层后续主要以周复核 / 月复核 / 异常触发复核为辅，不强绑定每日固定流程。
  - 当前 `pm_gate_status` 与 `learning_gate_status` 的定位是“观察型 / 诊断型 / 放行建议型”，不是学习层的强控制开关。
  - 也就是说，现阶段复核层虽然已经输出 `learning_gate_status`，但暂时不直接接入学习层参数更新，不直接拦截或放行模型学习。
  - 这条边界将保持不变，直到后续正式引入人类复核参与，并验证复核层规则足够稳定、足够严谨后，再决定是否把复核层结果正式接入学习放行链路。

#### 9. MX 三技能已按“收盘节点最小版”接入，不碰盘中交易主链

- 今天已正式落地三支最小版脚本，并统一挂到 `close-node` 阶段，不改 `prewarm -> decision -> buy-watch` 的盘中主链：
  - `mx_enrich_candidates.py`
  - `mx_event_review.py`
  - `mx_challenger_pool.py`

- 当前挂接顺序为：
  - `v10_moni_trader.py --close-node`
  - `mx_enrich_candidates.py`
  - `mx_event_review.py`
  - `mx_challenger_pool.py`
  - `update_curve_observatory.py`
  - 可选 `send_email.py --type review`

- 这意味着：
  - MX 能力先在收盘后进入“观察层 / 复核层”；
  - 先积累权威数据、资讯摘要、challenger 对照池；
  - 暂时不改变盘中买卖节奏，不增加尾盘链路复杂度；
  - 暂时不直接接入学习层参数更新。

- 三支脚本的现阶段职责定义如下：
  - `mx_enrich_candidates.py`
    - 读取现有 `v10_scan_full.csv`；
    - 按 `tier + weekly_slope` 选出前 `12` 只信号票；
    - 用 `mx-data` 为这些候选补充 `最新价 / 市盈率 / 市净率 / ROE`；
    - 输出：
      - `mx_enrich_candidates_latest.csv`
      - `mx_enrich_candidates_latest.json`
  - `mx_event_review.py`
    - 读取当前 `holding` 与当日重点信号票；
    - 用 `mx-search` 生成“最新公告 / 事件”摘要；
    - 形成简单 `positive_event / negative_event / event_conflict` 风险标记；
    - 输出：
      - `mx_event_review_latest.csv`
      - `mx_event_review_latest.json`
  - `mx_challenger_pool.py`
    - 用 `mx-xuangu` 跑固定 challenger 条件；
    - 当前第一版优先尝试：`ROE大于15% 净利润连续三年增长`；
    - 再与当前 `scanner` 池做交集标记；
    - 输出：
      - `mx_challenger_pool_latest.csv`
      - `mx_challenger_pool_latest.json`

- 今天已完成真实脚本烟测，结果如下：
  - `mx_enrich_candidates.py`
    - 成功补特征 `12` 只候选；
    - `success_count = 12`
    - `error_count = 0`
  - `mx_event_review.py`
    - 成功复核 `8` 个重点对象；
    - `reviewed_count = 8`
    - `error_count = 0`
  - `mx_challenger_pool.py`
    - 成功生成 `8` 条 challenger 记录；
    - 当前与扫描池交集 `1` 条；
    - 已修正“市场简称误当股票简称”的字段问题

- 同时已完成：
  - `python -m py_compile` 语法校验通过；
  - 三支脚本单独运行通过；
  - `v10_auto_runner.py` 已更新阶段步骤与 timeout；
  - 非交易日直接跑 `close-node` 时仍会被交易日历正常 `skip`，说明原有交易日纪律未被破坏。

- 当前这版的边界继续保持：
  - 只是“收盘后增强层 / 复核层观察层”；
  - 还不是盘中交易的直接输入；
  - 还不是学习层的正式放行条件；
  - 需要后续结合人类复核，继续验证：
    - 哪些 `mx-data` 特征对模型有稳定增益；
    - 哪些 `mx-search` 风险标记值得进入放行逻辑；
    - 哪些 `mx-xuangu` challenger 结果能长期补足 scanner 盲区。

#### 10. MX 收盘增强链路已补 soft-fail，并计入复核层观察

- 今天继续按“最小改动、边界清晰”的原则补了一刀容错：
  - `close-node` 中这三支 MX 脚本：
    - `mx_enrich_candidates.py`
    - `mx_event_review.py`
    - `mx_challenger_pool.py`
  - 现在都改为 `soft-fail` 处理。

- 当前新的执行口径是：
  - 若 MX 三步中的任一步失败，不再阻断 `close-node` 后续步骤；
  - `update_curve_observatory.py` 和后续收盘链路会继续执行；
  - 失败会记为 `warning`，而不是让整条收盘节点直接中断。

- 这样做的原因已经明确：
  - MX 三步当前属于“收盘后增强层 / 复核层观察层”；
  - 它们不是收盘主复核的唯一关键路径；
  - 即使某一步资讯、数据或 challenger 查询失败，也不应该连带影响：
    - 收盘主对仓
    - 学习准备度仪表
    - 曲线观测更新

- 同时，容错结果不会只留在执行层日志里，而是正式写入新的收盘复核观察产物：
  - `v10_close_node_mx_review_latest.json`

- 同时，MX 容错摘要现在也会合并回收盘复核总入口：
  - `v10_close_node_latest.json`
  - 新增顶层字段：
    - `mx_review`

- 这样当前形成了“总入口 + 明细入口”的结构：
  - 总入口看：
    - `v10_close_node_latest.json`
    - 适合日常直接检查复核层
  - 明细入口看：
    - `v10_close_node_mx_review_latest.json`
    - 适合追具体失败步骤、耗时与容错证据

- 该文件当前会记录：
  - `mx_review_status`：
    - `ok`
    - `warning`
    - `degraded`
  - `mx_error_count`
  - `mx_success_count`
  - `mx_failed_steps`
  - `mx_step_results`
  - `mx_observation_note`

- 这意味着后续当我帮你“检查复核层”时，除了原有：
  - `v10_close_node_latest.json`
  - `v10_learning_gate_status.json`
  现在 `v10_close_node_latest.json` 本身就会带上 `mx_review` 摘要；
  如需看细节，再继续展开：
  - `v10_close_node_mx_review_latest.json`
  从而判断：
  - 当天 MX 收盘增强链路是否完整；
  - 哪一步失败了；
  - 失败是否只是局部降级，还是已经退化为 `degraded`。

- 当前边界继续保持不变：
  - 这次修复本质上属于执行层容错增强；
  - 但失败证据已正式进入复核层观察；
  - 暂时仍不把这些 MX 运行故障直接接入学习层放行或参数更新。

#### 11. 交易窗口 `TDX` 数据新鲜度探针已落地第一版

- 今天新增独立脚本：
  - `data_freshness_probe.py`

- 当前设计目标非常明确：
  - 不直接改 `scanner_v10.py` 的核心扫描逻辑；
  - 不把重检测塞进尾盘买入主链；
  - 先做贴近真实买卖窗口的轻量探针，证明当次买卖所依赖的 `TDX` 数据是否足够新鲜；
  - 再把探针结果沉淀进复核层可读的观察产物。

- 第一版探针当前只检查 `TDX freshness`，不检查 `MX reliability`。
- 这样做的原因是：
  - `TDX` 当前已经直接服务：
    - `scanner_v10.py`
    - `signal decay`
    - `watchdog`
    - `benchmark`
  - 因此应先守住盘中真正影响买卖的主数据源新鲜度；
  - `MX` 当前仍主要属于收盘增强层 / 观察层，后续再独立补可靠度检查更稳。

- 当前探针挂接位置已经前置到这些真实交易窗口：
  - `smart-sell`
  - `prewarm`
  - `decision`
  - `buy`

- 这意味着以后复核层不只看“当天结果对不对”，还可以进一步回答：
  - 这次卖出窗口使用的 `TDX` 数据是否新鲜；
  - `14:30 prewarm` 当下的分钟线是否足够新；
  - `14:49 decision` 当下的分钟线是否足够新；
  - `14:50 buy-watch` 进入买入时，当次数据源是否已经出现滞后。

- 当前探针检查的最小字段包括：
  - 样本股票日线最新时间；
  - 样本股票 `5` 分钟线最新时间；
  - 样本指数日线最新时间；
  - 样本股票 `quote` 是否可取；
  - 首个可用 `TDX` 主机是否可连。

- 当前探针使用的时间锚点规则如下：
  - 日线 / 指数线：
    - 必须至少更新到“最近已完成交易日”；
  - `5` 分钟线：
    - 在交易时段内必须是当天数据；
    - 且滞后时间不能超过 phase 对应阈值；
  - 当前第一版阈值设置为：
    - `smart-sell`: `20` 分钟
    - `prewarm`: `12` 分钟
    - `decision`: `10` 分钟
    - `buy`: `10` 分钟

- 当前探针输出文件如下：
  - `v10_data_freshness_latest.json`
  - `automation_status/data_freshness_history.jsonl`

- 当前状态分级口径如下：
  - `ok`
  - `warning`
  - `degraded`
  - 但第一版仍保持为：
    - `observe_only`
    - 只做记录与解释，不直接阻断交易主链

- 这样定的原因已经固定：
  - 先让复核层能够感知“买卖当下数据是否新鲜”；
  - 先观察几个交易日，再决定是否把：
    - `decision`
    - `buy`
    相关的 `TDX freshness` 异常正式升级为放行门的一部分。

#### 12. `mx-xuangu` challenger 已接入本地 shadow 组合 `workbuddy`

- 今天继续按“最小改动、边界清晰”的原则，把 `mx-xuangu` challenger 预备军接到了一个新的本地 shadow 组合：
  - `workbuddy`

- 当前没有直接把 challenger 打进现有主组合，原因已经明确：
  - 现有 `mx-moni` 文档和本地脚本里，还没有看到稳定的“组合名 / portfolioId / accountId”切换参数；
  - 直接把 challenger 接入主组合，会污染：
    - 当前主组合账本
    - 当前盘中买卖主链
    - 当前学习样本边界
  - 因此先在本地落一个独立的 challenger shadow 组合，是目前最稳的做法。

- 当前新增脚本：
  - `mx_workbuddy_portfolio.py`

- 当前挂接位置：
  - 已接到 `close-node`
  - 顺序位于：
    - `mx_challenger_pool.py` 之后
    - `update_curve_observatory.py` 之前

- 当前 `workbuddy` 的定位已经明确：
  - 它不是主组合；
  - 它是 `mx-xuangu` challenger 的“预备军 / 补盲军 / 学习候选池”；
  - 当前只做收盘后生成和复核层观察，不直接参与现有买卖执行。

- 当前进一步明确了一条正式原则：
  - 预备军培养的是“选股能力”，不是当前交易接口能力；
  - 因此 `workbuddy` 不应被当前主交易程序支持的市场范围、股票代码前缀或模拟交易接口约束硬限制。

- 这也意味着：
  - 像 `920xxx` 这类候选，不应因为“可能属于北交所或当前主程序尚未覆盖的市场”就被预先过滤；
  - 预备军层应尽量保留多市场候选，让复核层判断其可靠性、补盲价值和学习价值；
  - 市场可交易性应由执行层单独判断，而不是反过来限制预备军的选股视野。

- 当前 `MX` 三个 skills 的利用方式，已经围绕 `workbuddy` 进一步形成了闭环：
  - `mx-xuangu`
    - 负责提供 challenger 预备军来源；
    - 形成 `workbuddy` 的基础候选池；
  - `mx-data`
    - 对能交叉上的候选补充：
      - 最新价
      - 估值
      - ROE
    - 用于增强 challenger 的解释力；
  - `mx-search`
    - 提供公告 / 事件 / 风险标签；
    - 让复核层不只看到 challenger 名单，还能看到事件背景和风险提示。

- 当前 `workbuddy` 的选人逻辑是：
  - 以 `mx_challenger_pool_latest.json` 为核心来源；
  - 结合以下因子做轻量综合评分：
    - 是否属于 scanner 盲区补充
    - 是否属于 scanner + challenger 双确认
    - ROE
    - 多期净利润同比增速
    - 最近交易日涨跌幅
    - 换手 / 成交额
    - 估值约束
    - 已知事件风险标签
  - 当前选出 `Top 5`，构成 `workbuddy` shadow 组合。

- 当前新增产物包括：
  - `mx_workbuddy_portfolio_latest.json`
  - `mx_workbuddy_portfolio_latest.csv`
  - `mx_workbuddy_portfolio_history.csv`

- 其中：
  - `latest` 文件用于当天复核层直接查看；
  - `history` 文件用于后续持续验证：
    - 哪些 challenger 真的能补足 scanner 盲区；
    - 哪些 challenger 只是噪音；
    - 哪些 challenger 值得后续晋升为学习候选。

- 当前 `workbuddy` 摘要也已经并入收盘复核总入口：
  - `v10_close_node_latest.json`
  - 新增字段：
    - `workbuddy_challenger`

- 这意味着以后当我帮你“检查复核层”时，除了原有：
  - 执行层健康
  - 账本 / 对仓健康
  - `mx_review`
  现在还可以一起回答：
  - 当天 `workbuddy` 预备军选了哪些 challenger；
  - 这些票更偏“盲区补充”还是“双确认”；
  - 哪些票已经具备进入学习候选观察的价值。

- 当前边界继续保持严格：
  - `workbuddy` 只是本地 shadow 组合；
  - 不直接下单；
  - 不直接接入主组合；
  - 不直接进入学习层参数更新；
  - 先由复核层持续观察，再决定哪些 challenger 值得晋升为学习候选证据。

- 因此，当前代码层面的结论也同步明确：
  - `workbuddy` / challenger 这一层目前不需要因为市场代码问题做收缩性修改；
  - 后续真正需要完善的代码，应放在主交易程序的“市场识别与执行能力层”：
    - 用统一的 market resolver / tradable capability 机制；
    - 替代当前按代码前缀做的简化假设；
    - 为未来逐步接入多市场交易能力预留清晰入口。

#### 15. `09:31` 晨间数据任务已落地：证券主数据映射 + 当日流动性判断

- 今天开始把“市场映射准确性”和“当日可交易性判断”正式做成一条独立晨间数据任务：
  - `opening-data`
  - 计划任务名：
    - `TLFZ-WorkBuddy-OpeningData`
  - 触发时间：
    - `09:31`

- 这样设计的原因已经固定：
  - `09:05` 只能刷新静态主数据；
  - 但“是否停牌 / 是否无竞价 / 09:31 是否仍为 0 成交”这类信息，只有开盘后才有复核价值；
  - 因此真正严谨的执行层口径，应是：
    - `09:31` 同时刷新市场映射与当日流动性判断；
    - 并把“仅今日剔除买卖”的结论留给执行层消费。

- 今天新增基础文件：
  - `security_master_refresh.py`
  - `market_resolver.py`

- 当前新增产物包括：
  - `security_master_latest.json`
  - `security_master_latest.csv`
  - `opening_tradability_latest.json`
  - `opening_tradability_latest.csv`
  - `automation_status/opening_tradability_history.jsonl`

- 当前这些产物已经明确分成两类逻辑：
  - `latest` 快照：
    - 每次 `09:31` 运行都会覆盖；
    - 服务执行层当天消费；
    - 只回答“今天现在该怎么执行”；
  - `history` 摘要：
    - 每次 `09:31` 运行都会追加；
    - 服务复核层留痕；
    - 负责形成晨间数据任务的时间序列证据链。

- 当前 `opening_tradability_history.jsonl` 的作用已经明确：
  - 不给执行层实时消费；
  - 主要给复核层看趋势、看频率、看稳定性；
  - 例如后续可以回答：
    - 最近哪些交易日 `09:31` 的剔除数量明显增多；
    - 哪些股票经常因为 `0` 成交被当日剔除；
    - 哪些市场长期处于 `review_only` 状态；
    - 晨间数据任务本身是否持续稳定。

- 当前 `history` 记录的是“轻量摘要”，不是“全量快照归档”：
  - 目前每次只追加：
    - `generated_at`
    - `trade_date`
    - `record_count`
    - `excluded_today_count`
    - `review_only_count`
    - `excluded_today_codes`
  - 这样设计的原因是：
    - 先满足复核层的趋势观察和证据留存；
    - 不急着把每天完整全市场状态都做成重量级归档；
    - 先稳住执行层行为，再视需要升级为按交易日完整归档。

- 当前这条晨间任务内部明确分成两层：
  - `security master`
    - 负责“代码 -> 市场 / 证券类别 / 当前执行器是否支持”的本地缓存；
    - 不在临场买卖时在线查 `MX`；
  - `opening tradability`
    - 负责“今天这只票在 09:31 是否具备自动买卖条件”的当日判断；
    - 结论只对今日生效，下一交易日重新进队判断。

- 当前主数据映射策略已经明确：
  - 主扫描 universe：
    - 优先使用 `scanner_v10.get_stock_list()` 的市场归属；
  - challenger / holdings / pending / workbuddy extras：
    - 先读已有市场标签；
    - 必要时再用 `mx-data` 做补充映射；
  - 只有在本地来源和 `MX` 都拿不到时，才退回代码前缀 fallback；
  - 且 fallback 只作为兜底，不再是长期主逻辑。

- 当前 `09:31` 当日流动性门的正式规则是：
  - 若早盘无有效开盘迹象，且 `09:31` 仍无成交：
    - `exclude_today_halt_or_no_open`
    - 今日剔除自动买卖；
  - 若 `09:31` 仍为 `0` 成交：
    - `exclude_today_zero_turnover_0931`
    - 今日剔除自动买卖；
  - 若当前执行器尚未支持该市场：
    - `review_today_unsupported_market`
    - 今日仅观察，不自动交易；
  - 若行情快照不完整：
    - `review_today_data_incomplete`
    - 今日进入复核层观察；
  - 其他情况：
    - `tradable_today`
    - 允许自动交易。

- 这里已经正式确认了一条边界：
  - 这不是长期过滤标签；
  - 这是“当日交易有效性门”；
  - 每个交易日 `09:31` 都会重新刷新；
  - “今日剔除”只对今日有效，下一交易日重新进队、重新判断。

- 当前执行层已经轻量接入这条规则：
  - `v10_moni_trader.py`
    - 买入前会读取 `opening_tradability_latest.json`；
    - 对被判定为 `exclude_today_buy_sell` 的标的，直接跳过；
  - `smart-sell / sell`
    - 若某持仓被判定为“今日不具备自动交易条件”，会跳过无效报单；
  - `add-position`
    - 同样会遵守当日流动性门，不再对今日不可交易标的做加仓动作。

- 当前这条晨间数据任务的摘要，现已接入收盘复核总入口：
  - `v10_auto_runner.py` 在 `close-node` 结束时，会自动读取：
    - `opening_tradability_latest.json`
  - 并把摘要合并进：
    - `v10_close_node_latest.json`
  - 新增字段：
    - `opening_data_review`

- 当前 `opening_data_review` 会提供的最小摘要包括：
  - `opening_data_status`
    - `ok / stale / missing`
  - `trade_date`
  - `generated_at`
  - `record_count`
  - `excluded_today_count`
  - `review_only_count`
  - `excluded_today_codes`
  - `unsupported_market_codes`
  - `today_gate_effective`
  - `opening_observation_note`

- 这样以后当你让我“检查复核层”时，除了原有：
  - `mx_review`
  - `workbuddy_challenger`
  - `learning_gate_status`
  现在还能一起看到：
  - 当天 `09:31` 晨间数据任务是否拿到有效结果；
  - 当天有多少标的被“仅今日剔除自动买卖”；
  - 哪些代码因为市场尚未支持而进入 `review_only`；
  - 这份晨间流动性门结论是否对当天收盘复核仍然有效。

- 当前需要说明的一点是：
  - 这次代码已经接好；
  - 但当前机器上还没有现成的 `v10_close_node_latest.json` 历史快照；
  - 因此手动 merge 时不会生成新的收盘总入口文件；
  - 要等下一次真实 `close-node` 运行后，`opening_data_review` 才会自然出现在收盘复核总入口里。

- 当前刻意保持的一条安全边界是：
  - 这次没有直接去改 `scanner_v10.py` 的核心选股逻辑；
  - 而是先把：
    - 市场映射
    - 当日流动性门
    - 今日剔除买卖
    做成独立晨间任务 + 执行层消费；
  - 这样能先提升执行层稳定性与准确性，同时避免碰核心扫描主逻辑带来额外风险。

- 当前与多市场视野的关系也已明确：
  - `920xxx` 等 challenger / workbuddy 预备军候选仍然保留；
  - 若当前执行器尚未支持该市场，则在晨间任务里标记为：
    - `review_today_unsupported_market`
  - 这意味着：
    - 预备军的选股能力不被先验市场过滤浪费；
    - 当日执行层又能清楚知道“今天是否可自动交易”。

- 本次实际烟测结果：
  - `security_master_latest.json`
    - 成功生成 `1007` 条证券主数据记录；
  - `opening_tradability_latest.json`
    - 成功生成 `1007` 条当日流动性判断；
    - 当前 `excluded_today_count = 0`
    - 当前 `review_only_count = 2`
  - `review_only` 的两条记录为：
    - `920178`
    - `920200`
    - 均被正确标记为 `review_today_unsupported_market`
    - 说明当前“预备军保留视野、执行层按能力放行”的边界已经开始在代码里生效。

- 同时已完成：
  - `python -m py_compile`
    - `market_resolver.py`
    - `security_master_refresh.py`
    - `v10_auto_runner.py`
    - `register_workbuddy_tasks.py`
    - `v10_moni_trader.py`
    语法校验通过；
  - `python security_master_refresh.py`
    - 单独烟测通过；
  - `python register_workbuddy_tasks.py --dry-run`
    - 已确认 `09:31` 的 `OpeningData` 计划任务编排正确。

#### 17. `workbuddy` 盘中 challenger 自动化已落地到 `13:35`

- 今天继续把 `MX challenger / workbuddy` 从“手工触发”推进到“自动化定时触发”。

- 当前拍板的时间点是：
  - `13:35`

- 选择这个时间点的原因已经明确：
  - 早盘开盘扰动已经过去；
  - `09:31` 的市场映射与当日流动性门事实已经生成；
  - 午间到下午早段的政策、新闻、国际形势变化已有机会进入当日判断；
  - 同时又和后面的：
    - `13:45 smart-sell`
    - `14:30 prewarm`
    - `14:49 decision`
    保持了安全缓冲，不会贴得过近去挤压主交易链。

- 这次新增了一条独立自动化阶段：
  - `workbuddy-refresh`

- 当前它已经接入：
  - `v10_auto_runner.py`
  - `register_workbuddy_tasks.py`

- 这条 `13:35` 自动化当前执行的步骤是：
  - `mx_enrich_candidates.py`
  - `mx_event_review.py`
  - `mx_challenger_pool.py`
  - `mx_workbuddy_portfolio.py`

- 当前这条链的职责定义是：
  - 在盘中生成当天最新版 `MX challenger` 候选池；
  - 再把它收束成当天最新版 `workbuddy` 预备军组合；
  - 供后续复核层观察与人工决定是否下单。

- 当前边界刻意保持为：
  - 这不是主交易链；
  - 不直接替代 `scanner_v10.py`；
  - 不自动触发买入；
  - 它的任务是先把当天 `MX challenger` 做成“自动更新的预备军组合”。

- 当前这条链内部也做了轻量边界处理：
  - `mx_enrich_candidates.py`
  - `mx_event_review.py`
    仍按增强层处理，可 `soft-fail`；
  - 真正的核心结果产物仍是：
    - `mx_challenger_pool.py`
    - `mx_workbuddy_portfolio.py`
  - 这样即使资讯增强或事件增强偶发失败，也不必让整条 `13:35` challenger 刷新链彻底失效。

- 当前新注册的计划任务名是：
  - `TLFZ-WorkBuddy-WorkBuddyRefresh`

- 当前计划任务时间是：
  - 工作日 `13:35`

- 本次实际完成情况：
  - `python -m py_compile`
    - `v10_auto_runner.py`
    - `register_workbuddy_tasks.py`
    语法校验通过；
  - `python register_workbuddy_tasks.py`
    已真实注册成功；
  - 当前机器上已能看到：
    - `TLFZ-WorkBuddy-WorkBuddyRefresh`
    被正式创建。

- 这意味着从现在开始，`workbuddy` 已经不再只是收盘后 shadow 组合的静态观察对象；
  盘中 `13:35` 会自动刷新当天版本的 `MX challenger` 预备军，
  为后续“下单到 workbuddy 组合，形成第一个 MX challenger”做好自动化基础。

#### 18. `workbuddy` 独立下单链已落地，但底层 `mx-moni` 账户仍是共享的

- 今天继续把 `workbuddy` 从“只有 shadow 组合与候选池”推进到“有独立下单链”。

- 当前新增了一个独立脚本：
  - `workbuddy_moni_trader.py`

- 这条链当前做成的不是“复用主交易账本再打标签”；
  而是：
  - 独立读取 `mx_workbuddy_portfolio_latest.json`
  - 独立生成买入计划
  - 独立发起买卖委托
  - 独立维护：
    - `workbuddy_track_record.csv`
    - `workbuddy_nav_history.csv`
    - `workbuddy_account_summary_latest.json`
    - `workbuddy_pending_orders.json`
    - `workbuddy_trade_api_log.jsonl`

- 这意味着当前 `workbuddy` 已经不再和主交易共用：
  - `v10_track_record.csv`
  - `v10_account_summary_latest.json`
  - `v10_pending_orders.json`
  - `v10_trade_api_log.jsonl`

- 当前最关键的设计边界是：
  - `workbuddy` 不再拿主交易的“全局持仓自动导入/全量对仓逻辑”来维护自己；
  - 而是按自己发出去的委托号回收成交，维护独立账本；
  - 这样可以避免把主交易持仓无意导入到 `workbuddy` 账本里。

- 当前已经明确的一点也必须写清楚：
  - `mx-moni` 接口层目前仍没有看到稳定的：
    - `portfolioId`
    - `accountId`
    - `groupId`
    之类切换参数；
  - 因此底层：
    - 资金接口
    - 持仓接口
    - 委托接口
    仍然是全局共享的同一套模拟账户。

- 所以当前这条“独立下单链”的准确含义是：
  - `账本独立`
  - `委托记录独立`
  - `候选来源独立`
  - `复核摘要独立`
  - 但底层 `mx-moni` 账户本体暂时仍不是物理隔离的第二个子账户。

- 这也意味着一个现实风险：
  - 如果主交易与 `workbuddy` 在共享底层账户上交易同一代码，
    仍可能出现物理仓位混淆；
  - 当前代码已经尽量把账本与委托回收隔离开，
    但“底层账户级别的真正多组合隔离”仍需要后续如果 MX 官方接口支持，再继续做。

- 当前 `workbuddy_moni_trader.py` 已支持：
  - `--buy`
  - `--sell`
  - `--status`
  - `--dry-run`

- 当前买入来源是：
  - `mx_workbuddy_portfolio_latest.json`

- 当前买入逻辑是：
  - 读取 `workbuddy` 最新候选池；
  - 结合候选池中的 `target_weight_pct` 生成买入计划；
  - 自动跳过：
    - 当前执行器不支持市场
    - `09:31` 已被当日流动性门剔除的标的
    - `workbuddy` 自己已持有或已有未完成买单的标的。

- 当前卖出逻辑先做成最小稳版：
  - 只按 `workbuddy` 自己的 holding 记录处理；
  - 先执行 `T+5` 兜底卖出；
  - 不把主交易持仓纳入这条链。

- 当前还把它轻量接进了总调度：
  - `v10_auto_runner.py`

- 当前可用的新 phase 有：
  - `workbuddy-buy`
  - `workbuddy-sell`
  - `workbuddy-status`

- 这表示从代码结构上看，
  `workbuddy` 已经拥有了一条独立于主交易脚本的下单路径，
  后续如果你要指定具体时间点，再把这条 phase 注册成计划任务即可。

- 本次验证情况：
  - `python -m py_compile`
    - `workbuddy_moni_trader.py`
    - `v10_auto_runner.py`
    已通过；
  - `python workbuddy_moni_trader.py --status`
    已成功输出 `workbuddy` 独立摘要；
  - `python workbuddy_moni_trader.py --buy --dry-run`
    已成功生成 dry-run 买入清单；
  - 当前 dry-run 样本中，北交所代码会因为执行器尚不支持而被自动跳过，
    这和此前“预备军不硬过滤市场，但执行层当前要尊重实际下单能力”的边界一致。

#### 19. `workbuddy_distill` 第一轮 `20` 个交易日蒸馏底座已开建

- 今天正式把 `workbuddy` 的蒸馏学习区从成品候选池目录中拆出来，
  新增独立目录：
  - `<repo_root>/workbuddy_distill`

- 当前目录职责已经明确分层：
  - `raw_top100`
    - 放 `TDX` 作为结果真相源生成的每日全市场排序与 `top100`
  - `templates`
    - 放模板注册表与后续稳定模板库
  - `evaluations`
    - 放 `T+1` 验收结果
  - `evolution`
    - 放模板升权 / 降权 / 淘汰策略
  - `artifacts`
    - 放窗口级汇总产物
  - `scripts`
    - 放蒸馏相关脚本

- 第一轮蒸馏窗口已固定为：
  - `2026-05-13` 到 `2026-06-18`
  - 共 `20` 个真实交易日

- 当前已新增正式脚本：
  - `workbuddy_distill/scripts/build_tdx_rankings.py`
  - `workbuddy_distill/scripts/evaluate_template_hits.py`

- 这次没有采用“单个日期跑 20 次”的低效方案，
  而是做成：
  - 单次拉取全市场股票日线；
  - 在同一批日线数据上复用计算 `20` 个交易日的涨幅截面；
  - 一次性生成整段窗口的 `full_rank` 与 `top100`
  - 从而让蒸馏第一段真正可复用、可滚动、可回放。

- 第一轮 `TDX` 榜单底座已经实跑完成：
  - 覆盖范围：
    - `hs_a_share`
  - 股票池总数：
    - `5207`
  - 总耗时：
    - `307.083s`
  - 其中：
    - universe 加载 `1.882s`
    - 多日期日线抓取与排序 `302.750s`

- 当前每个交易日都已落下标准化产物：
  - `full_rank.csv`
  - `top100.csv`
  - `top100.json`
  - `summary.json`

- 当前这 `20` 个交易日的有效样本数整体稳定：
  - 最低：
    - `2026-05-19` 的 `5178`
  - 最高：
    - `2026-06-08` 与 `2026-06-09` 的 `5194`
  - 主要缺失原因仍只是：
    - `target_date_not_found`
    - `empty_bars`
    - 极少量 `no_prev_bar`

- 当前这意味着：
  - `TDX取真相数据 -> 蒸馏模板簇 -> T+1验收 -> 进化`
    这条主干链路的第一段已经不再停留在讨论层；
  - `T0涨幅前100` 现在已经有了本地、可重复、可按日期回放的榜单源。

- 同时已把 `T+1` 验收骨架正式落成：
  - `evaluate_template_hits.py`
  - 当前支持直接读取：
    - `CSV` 候选池
    - `JSON` 候选池
    - 包含 `user_focus_pool` / `selected_records` 等结构的本地产物

- 已做一次实际 smoke test：
  - 候选池：
    - `workbuddy_candidate_pool_latest.json`
  - 验收日期：
    - `2026-06-18`
  - 候选数量：
    - `8`
  - 结果：
    - `top100_hit_count = 0`
    - `top30_hit_count = 0`
    - `verdict = fail`
    - `recommended_action = downgrade`

- 这个 smoke test 的意义不是证明当前池子优秀，
  而是确认现在系统已经具备：
  - 直接对某个候选池执行 `T+1` 验收；
  - 自动给出：
    - `hit count`
    - `hit rate`
    - `lift`
    - `verdict`
    - `recommended_action`

- 当前还同步落了两个蒸馏治理基础文件：
  - `templates/template_registry.json`
  - `evolution/evolution_policy_v1.json`

- 这表示从现在开始，
  `workbuddy` 的蒸馏进化不再只是“口头讨论模板”，
  而是已经拥有：
  - 结果真相底表
  - 候选池验收器
  - 模板注册位置
  - 进化策略落盘位置

- 当前边界继续保持清楚：
  - 这仍然是学习区 / 蒸馏区；
  - 还没有开始自动大规模生成模板簇；
  - 也还没有把模板自动进化闭环完全接到日常自动化；
  - 但第一段数据底座与验收骨架已经建成，可以继续往模板蒸馏和自动进化推进。

#### 20. 第一组 `TDX` 本地蒸馏模板已过线，上一轮“不及格”状态已修正

- 这一轮继续往前推进后，已经不再停留在：
  - `20` 天榜单真相底座；
  - `T+1` 验收骨架；
  而是已经拿到了第一组真正过线的本地蒸馏模板。

- 这里需要把状态写清楚：
  - 上一轮如果按“已经完成蒸馏进化并拿到及格成绩”来判断，
    结论确实是：
    - `不及格`
  - 因为当时只完成了：
    - `TDX` 真相底座；
    - 验收器；
    - 还没有真正找到过线模板。

- 这一轮已经补上了这一步：
  - 新增本地模板搜索脚本：
    - `workbuddy_distill/scripts/distill_local_templates.py`
  - 它当前的设计目标是：
    - 不依赖 `MX` 历史快照；
    - 直接基于 `TDX` 的 `20` 个交易日真相榜单；
    - 枚举本地结构化模板参数；
    - 按既定验收标准判断：
      - `fail`
      - `prototype`
      - `pass`
      - `priority`

- 当前已确认过线的第一组模板是：
  - `carry_lb1_cut20_dec1.0_pctw0.0_minp1_0.0_minapp1_reqp1_1_excltop0`

- 这组模板的准确含义可以解释为：
  - 只看 `T-1`；
  - 直接取 `T-1` 涨幅前 `20`；
  - 不额外加 `pct_weight`；
  - 不要求多日重复出现；
  - 不排除前 `T-1` 的最强头部；
  - 本质上是一个非常朴素的：
    - `T-1 强势延续型原型模板`

- 当前在 `2026-05-13 ~ 2026-06-18` 这个 `20` 交易日窗口上的真实验收结果为：
  - 验收天数：
    - `19`
  - 候选池规模：
    - `20`
  - `top100_hit_count`：
    - `59`
  - `top30_hit_count`：
    - `46`
  - `top10_hit_count`：
    - `18`
  - `top100_hit_rate`：
    - `0.1553`
  - `top30_hit_rate`：
    - `0.1211`
  - `hit_day_count`：
    - `17`
  - `hit_day_rate`：
    - `0.8947`
  - `verdict`：
    - `pass`
  - `recommended_action`：
    - `promote`

- 这意味着它已经满足当前定义的“及格”标准：
  - `Top100命中率 >= 15%`
  - `Top30命中率 >= 5%`
  - 且命中日覆盖远高于最低要求

- 当前产物已经正式落盘：
  - 搜索汇总：
    - `workbuddy_distill/artifacts/template_search_latest.json`
    - `workbuddy_distill/artifacts/template_search_latest.md`
  - 模板注册表：
    - `workbuddy_distill/templates/template_registry.json`

- 当前这个结果有两个重要含义：
  - 第一，
    `TDX取真相 -> 本地模板蒸馏 -> T+1验收`
    这条链路已经不只是理论成立，而是已经拿到第一组通过样本；
  - 第二，
    这组通过模板还只是：
    - `原型通过`
    - `可晋升观察`
    还不是最终定型模板库。

- 当前也必须保持严谨：
  - 这次过线并不等于“已经完成进化”；
  - 更准确地说，是：
    - 蒸馏系统已经拿到第一组 `pass` 级模板；
    - 后续还需要继续扩模板族、做自动升降权、拆分和淘汰。

- 但从项目状态上看，
  现在已经可以正式说：
  - `第一轮蒸馏不再是不及格`
  - 因为已经拿到了：
    - 真相底座；
    - 验收器；
    - 第一组真实过线模板。

#### 21. 蒸馏搜索器已继续进化到 `priority`，并把 `layered_mix` 正式推上 `pass`

- 这一轮没有停在“已有 3 条 `pass` 就汇报”，而是继续把本地模板搜索器往前推了一步：
  - 修正了 `distill_local_templates.py` 里模板 `family` 落盘被写死为 `tdx_carry` 的问题；
  - 把原先手填样例式的 `gap_mix / mix` 参数空间，扩展成系统化的本地网格搜索。

- 当前正式搜索器已经不再只测少量 `gap_mix / mix` 组合，而是覆盖：
  - `gap_mix`
    - `head = 5 / 8 / 10 / 12`
    - `mid_skip = 15 / 20 / 25 / 30 / 35 / 40`
    - `end_rank = 35 / 40 / 45 / 50 / 55 / 60`
  - `mix`
    - `head = 5 / 8 / 10 / 12`
    - `tail_start = 11 / 13 / 16 / 21 / 26 / 31`
    - `tail_end = 30 / 35 / 40 / 45 / 50`

- 这轮继续蒸馏后，窗口级汇总结果已刷新为：
  - `template_trial_count = 848`
  - `unique_template_count = 74`
  - `passed_template_count = 12`
  - `priority_template_count = 1`

- 当前最关键的新结果是：
  - 第一条 `priority` 级模板已经出现：
    - `gapmix_head10_skip40_end50`
  - 它的行为含义是：
    - 保留 `T-1` 涨幅前 `10` 的绝对强势头部；
    - 故意跳过 `11 ~ 40` 这一段；
    - 直接补入 `41 ~ 50` 这段“尾部二层扩散区”；
    - 本质上是在抓：
      - `头部强势 + 中段拥挤回避 + 后段二层扩散`

- 这条 `priority` 模板在 `2026-05-13 ~ 2026-06-18` 的 `19` 个验收日上结果为：
  - `top100_hit_count = 70`
  - `top30_hit_count = 42`
  - `top10_hit_count = 14`
  - `top100_hit_rate = 0.1842`
  - `top30_hit_rate = 0.1105`
  - `hit_day_count = 19`
  - `hit_day_rate = 1.0`
  - `verdict = priority`

- 这个结果很重要，因为它把蒸馏主线从：
  - `已经及格`
  真正推进到了：
  - `已经出现高等级模板`

- 同时，这轮还把此前一直卡在 `prototype` 的 `layered_mix` 族推上了 `pass`：
  - 新过线模板：
    - `mix_head10_tail13_30`
  - 当前成绩：
    - `top100_hit_rate = 0.1526`
    - `top30_hit_rate = 0.1184`
    - `hit_day_rate = 0.8947`
    - `verdict = pass`

- 这意味着当前过线模板已经不再只有：
  - `strong_carry`
  - `layered_gap_mix`
  而是正式形成了更完整的模板簇：
  - `strong_carry`
  - `layered_gap_mix`
  - `late_gap_mix`
  - `layered_mix`

- 当前新的主输出文件都已刷新：
  - `workbuddy_distill/artifacts/template_search_latest.json`
  - `workbuddy_distill/artifacts/template_search_latest.md`
  - `workbuddy_distill/templates/template_registry.json`

- 现在这轮的项目状态可以明确更新为：
  - 已经不只是“有若干及格模板”；
  - 而是已经拿到：
    - 首条 `priority`
    - 多类 `pass` 模板簇
    - 可继续进入自动进化闭环与 `workbuddy` 候选池接入阶段的模板基础。

#### 22. 已把 `反向蒸馏 + 胜率/盈利率验收 + A/B 对比` 接进主蒸馏器

- 这一轮继续把蒸馏系统从“只看命中率”推进到更贴近真实盈利目标的状态：
  - 在主蒸馏脚本里加入了候选池实战指标：
    - `candidate_win_rate`
    - `candidate_avg_return`
    - `candidate_median_return`
    - `portfolio_positive_day_rate`
    - `gain_loss_ratio`
    - `business_score`
  - 并把这些指标和原来的：
    - `top100_hit_rate`
    - `top30_hit_rate`
    - `hit_day_rate`
    一起进入模板比较和排序。

- 这意味着从现在开始，模板不再只是按“命中多少强势股”来比较，
  还会按：
  - 候选池胜率
  - 候选池平均收益
  - 组合正收益日占比
  来判断“是不是更接近赚钱目标”。

- 同时，已经把第一版反向蒸馏正式接入：
  - 负向族：
    - `recent_tail_veto`
  - 当前做法是：
    - 从 `T-1` 更早的近几日里，
    - 寻找那些曾经进入全市场尾部极差区的代码；
    - 如果它们满足：
      - 最近 `2 / 3 / 5` 日内
      - 进入尾部 `50 / 100 / 150 / 200`
      - 且最差日跌幅达到 `4% / 6% / 8%`
      这种失败特征，
      就对正向候选池执行：
      - `veto`

- 当前这条链已经不只是理论设计，而是已经真实跑完一轮：
  - 正向通过模板数：
    - `12`
  - 负向 veto 参数试验数：
    - `72`
  - 正反向组合试验数：
    - `864`
  - 去重后独立组合行为：
    - `571`
  - 被判定为 `promote` 的组合：
    - `12`

- 当前第一版组合蒸馏最有代表性的结果之一是：
  - 基模板：
    - `gapmix_head8_skip35_end50`
  - 接入负向 veto：
    - `tailveto_lb3_tail200_minapp1_maxpct8.0`
  - 组合后结果：
    - `top100_hit_rate = 0.1632`
      - 相比基模板 `+0.0053`
    - `candidate_win_rate = 0.5484`
      - 相比基模板 `+0.0101`
    - `candidate_avg_return = 1.8422`
      - 相比基模板 `+0.1823`
    - `candidate_retention_rate = 0.9816`
  - 这表示：
    - 负向 veto 并没有把池子砍废；
    - 反而在保留大部分候选池的同时，
      真实提高了命中密度和平均收益。

- 另一组更稳的组合结果来自：
  - 基模板：
    - `gapmix_head12_skip35_end45`
  - 接入负向 veto：
    - `tailveto_lb2_tail200_minapp1_maxpct8.0`
  - 组合后结果：
    - `top100_hit_rate = 0.1684`
      - 相比基模板 `+0.0026`
    - `candidate_win_rate = 0.5576`
      - 相比基模板 `+0.0088`
    - `candidate_avg_return = 2.0720`
      - 相比基模板 `+0.1796`
    - `candidate_retention_rate = 0.9842`

- 这轮最重要的结论不是“某一条 veto 完美无缺”，
  而是已经正式证明：
  - `正向模板簇 + 反向剔除层`
  这条路线在当前 `20` 日窗口内是有效的；
  - 而且提升已经不只体现在命中率上，
    还体现在：
    - 胜率
    - 平均收益
    - 候选池质量净化

- 当前新落盘的主产物包括：
  - 正向蒸馏汇总：
    - `workbuddy_distill/artifacts/template_search_latest.json`
    - `workbuddy_distill/artifacts/template_search_latest.md`
  - 正反向组合汇总：
    - `workbuddy_distill/artifacts/combined_template_search_latest.json`
    - `workbuddy_distill/artifacts/combined_template_search_latest.md`
  - 组合注册表：
    - `workbuddy_distill/templates/combined_template_registry.json`

- 这表示当前蒸馏系统已经从：
  - `TDX 真相源 -> 正向模板蒸馏 -> T+1命中验收`
  继续推进到了：
  - `TDX 真相源 -> 正向模板蒸馏 -> 反向失败模板蒸馏 -> 候选池剔除 -> 命中与盈利双验收 -> A/B 比较`

- 从项目阶段上看，
  现在已经可以明确说：
  - `workbuddy_distill` 不再只是“找可能上涨的模板”；
  - 而是已经开始具备：
    - 找强势行为
    - 识别失败行为
    - 用负向 veto 做候选池净化
    - 用真实收益指标判断是否进化成功
  的闭环雏形。

#### 23. 第二版反向蒸馏已加入 `伪强势失败股 veto`，开始抓“昨天强、今天掉队”的失败结构

- 这一轮没有停在第一版：
  - `recent_tail_veto`
  只看“近期尾部差生”；
  而是继续补上第二类更贴近真实交易错误的反向模板：
  - `fake_head_veto`

- 这类 veto 的核心逻辑是：
  - 某只股票在近几次观察中，
    曾经进入过前一日头部强势区；
  - 但第二天却出现：
    - 名次大幅掉队
    - 或次日直接转弱/转负
  - 这种“伪强势、假延续、次日失速”的结构，
    会被当作失败特征写进反向 veto。

- 当前第二版反向蒸馏参数空间已扩成：
  - 第一版：
    - `recent_tail_veto`
  - 第二版新增：
    - `fake_head_veto`

- 因此这轮真实组合搜索规模扩大为：
  - 负向 veto 试验数：
    - `288`
  - 正反向组合试验数：
    - `3456`
  - 去重后独立组合行为：
    - `683`
  - 被判定为 `promote` 的组合：
    - `12`

- 第二版的一个新结果是：
  - `fake_head_veto` 已经不再只是“存在但没用”；
  - 它已经真实挤进 `promote` 组合前排。

- 当前最有代表性的 `fake_head_veto` 组合包括：
  - 基模板：
    - `carry_lb1_cut20_dec1.0_pctw0.0_minp1_0.0_minapp1_reqp1_1_excltop0`
  - 接入：
    - `fakehead_lb5_head20_minprev3.0_failr120_failpct0.0_minf1`
  - 组合后结果：
    - `top100_hit_rate = 0.1526`
      - 相比基模板 `-0.0027`
    - `candidate_win_rate = 0.5241`
      - 相比基模板 `+0.0017`
    - `candidate_avg_return = 1.5734`
      - 相比基模板 `+0.1987`
    - `candidate_retention_rate = 0.9289`

- 另一组新进入 `promote` 的 `fake_head_veto` 组合是：
  - 基模板：
    - `gapmix_head12_skip35_end45`
  - 接入：
    - `fakehead_lb5_head20_minprev3.0_failr120_failpct0.0_minf1`
  - 组合后结果：
    - `top100_hit_rate = 0.1684`
      - 相比基模板 `+0.0026`
    - `candidate_win_rate = 0.5505`
      - 相比基模板 `+0.0017`
    - `candidate_avg_return = 2.0596`
      - 相比基模板 `+0.1672`
    - `candidate_retention_rate = 0.9895`

- 这轮结果说明两点：
  - 第一，
    `fake_head_veto` 方向是有效的，
    它确实比单纯“尾部差生剔除”更贴近我们真正想剔除的失败票；
  - 第二，
    它当前已经开始产生正向收益提升，
    但仍然不是压倒性替代第一版 `tail veto` 的状态。

- 更准确地说，
  当前第二版反向蒸馏的项目判断应更新为：
  - `tail veto` 仍然是当前主力净化器；
  - `fake_head_veto` 已证明有效，开始进入前排；
  - 但还需要继续进化，才有机会成为更强的主反向模板。

- 当前这意味着蒸馏系统的反向层已经从：
  - `差生剔除`
  进化到了：
  - `差生剔除 + 伪强势失败识别`

- 这一步的价值在于：
  以后 `workbuddy` 的候选池净化，
  不再只是在尾部垃圾里挑错；
  而是已经开始学习：
  - 哪些票表面像机会，
    实际上最容易在第二天拖累组合盈利。

#### 24. 已把 `combined_template_registry` 接成新的 `workbuddy distill` 候选池生成链

- 这一轮不再只停在模板研究层，
  而是把：
  - `workbuddy_distill/templates/combined_template_registry.json`
  直接接成了一条新的候选池生成逻辑。

- 新增脚本：
  - `build_workbuddy_distill_pool.py`

- 这条脚本的职责是：
  - 读取当前 `promoted` 的正反向组合模板；
  - 用最新一日 `TDX` 排名截面作为当日输入；
  - 让所有 promoted 组合模板对下一交易日候选池进行“投票”；
  - 聚合出：
    - `selected_records`
    - `user_focus_pool`
    - `primary_pool`
    - `rotation_pool`
    - `observe_only`
    - `veto_watch_pool`

- 其中最关键的是：
  - `selected_records`
  结构已经对齐到后续 `workbuddy` 可消费的方向，
  当前字段包括：
  - `code`
  - `name`
  - `selection_score`
  - `selection_rank`
  - `target_weight_pct`
  - `selection_reasons`
  - `portfolio_name`
  - `portfolio_type`

- 当前新产物已经真实落盘：
  - `workbuddy_pool/workbuddy_distill_candidate_pool_latest.json`
  - `workbuddy_pool/workbuddy_distill_candidate_pool_latest.md`

- 第一版新链运行结果：
  - `trade_date = 2026-06-18`
  - `promoted_template_count = 12`
  - `candidate_count = 34`
  - `selected_count = 5`

- 当前前五名 `selected_records` 为：
  - `300166 东方国信`
  - `300319 麦捷科技`
  - `688486 龙迅股份`
  - `688479 友车科技`
  - `688599 天合光能`

- 这批票当前的共同特征是：
  - 都被多组 promoted 组合模板共同选中；
  - 当前在最新一日 `TDX` 截面中也处于非常靠前的位置；
  - 已经体现出“模板共识 + 当前强势截面”的合力。

- 当前输出里还新增了：
  - `veto_watch_pool`
  它记录的是：
  - 被反向 veto 多次剔除的代码；
  - 方便后续人工复核“哪些票反复被识别为失败结构”。

- 这一步的项目意义是：
  - `workbuddy_distill` 不再只是评估模板；
  - 而是已经第一次产出可直接服务 `workbuddy` 的本地候选池文件。

- 但当前边界仍保持清楚：
  - 这是新的并行候选池链；
  - 还没有直接覆盖原有：
    - `workbuddy_candidate_pool_latest.json`
  - 也还没有直接替换：
    - `mx_workbuddy_portfolio_latest.json`
  - 这么做是为了先保持安全，
    等验证稳定后再决定是否升级为默认入口。

#### 25. 已把利润型 `prototype` 拉入第二轮进化，并新增 `crowded_mid_veto`

- 这一轮继续往：
  - `57%+` 胜率
  - `2.2%+` 平均收益
  推进时，没有只盯原来的 `pass / priority` 模板，
  而是把一批“命中层略弱、但盈利层很强”的 `prototype`
  也拉进了第二轮反向组合搜索。

- 当前被纳入第二轮 base 搜索的不再只是：
  - `passed_templates`
  还包括满足以下条件的利润型 `prototype`：
  - `candidate_win_rate >= 0.55`
  - `candidate_avg_return >= 2.05`
  - `hit_day_rate >= 0.84`

- 这样做的目的非常明确：
  - 不是继续堆更多普通模板；
  - 而是专门寻找那些：
    - 盈利已经够强
    - 但命中稳定性还差一口气
  的模板，
  看能不能通过反向剔除把它们推成正式可用组合。

- 同时，反向层又新增了一类失败模板：
  - `crowded_mid_veto`

- 它要识别的是：
  - 前一日处在中段拥挤区的强势票；
  - 这些票当日看着不差，
    但次日常常出现：
    - 排名快速掉队
    - 或直接转弱/翻绿
  - 这类票不是尾部垃圾，
    也不是头部假龙头，
    而是“中段拥挤掉队”型失败结构。

- 因此这一轮真实组合搜索规模被继续放大为：
  - `base_template_count = 17`
  - `negative_veto_trial_count = 936`
  - `combination_trial_count = 15912`
  - `unique_combination_count = 1553`
  - `promoted_combination_count = 12`

- 这一轮最关键的新结果是：
  - 已经第一次把：
    - `56%+` 胜率
    - `2.2%+` 平均收益
  推进到 `promoted` 组合层。

- 当前最强的新组合之一是：
  - 基模板：
    - `gapmix_head12_skip35_end40`
    - 这个模板此前本身只是利润型 `prototype`
  - 接入：
    - `fakehead_lb5_head20_minprev3.0_failr120_failpct0.0_minf1`
  - 组合后结果：
    - `candidate_win_rate = 0.5629`
    - `candidate_avg_return = 2.2964`
    - `candidate_retention_rate = 0.7947`
    - `top100_hit_rate = 0.1447`
  - 这说明：
    - 它的盈利已经明显上台阶；
    - 但命中层仍然略弱，
      所以更像“高盈利强候选”，而不是稳定主模板。

- 另一组新结果是：
  - 基模板：
    - `gapmix_head12_skip35_end40`
  - 接入：
    - `fakehead_lb3_head20_minprev3.0_failr120_failpct0.0_minf1`
  - 组合后结果：
    - `candidate_win_rate = 0.5659`
    - `candidate_avg_return = 2.2187`
    - `candidate_retention_rate = 0.8184`

- 这意味着项目状态可以更新为：
  - `2.2%+` 平均收益，
    现在已经不是“只存在于 prototype 单模板”；
  - `56%+` 胜率也已经进入正式 `promoted combination` 层；
  - 但真正的 `57%+` 胜率，
    目前仍然还停留在利润型 `prototype` 一侧；
  - 而是已经第一次在正式 `promoted combination` 层面被打出来。

- 同时，`crowded_mid_veto` 也已经不是空想，
  它第一次进入 promoted 组合前排，例如：
  - `gapmix_head8_skip40_end50`
  - 接入：
    - `crowdedmid_lb5_mid12_35_minprev2.0_failr120_failpct0.0_minf1`
  - 结果：
    - `candidate_win_rate = 0.5559`
    - `candidate_avg_return = 2.0358`

- 这说明：
  - 第三类失败结构“中段拥挤掉队”已经开始被读出来；
  - 它现在还没成为最强 veto，
    但已经被证明是有效的补充方向。

- 当前最客观的项目判断应更新为：
  - 命中稳定性最强的主模板，仍然是原先 `late_gap_mix` 主轴；
  - 盈利能力最强的组合，已经开始从利润型 `prototype + veto` 里冒出来；
  - 下一步最值得继续进化的方向，
    已经不是再找更多新家族，
    而是想办法把这些“高盈利 promoted 组合”
    进一步补强命中层稳定性。

#### 24. 前移命中率进化路径第一轮已接入并拿到有效结果

- 这轮不是继续只盯 `top100_hit_rate`，而是把“命中是否前移”正式写进蒸馏器：
  - 新增：
    - `top50_hit_rate`
    - `front_shift_score`
    - `avg_hit_rank`
  - 含义是：
    - 不再把“进了 top100”都看成同一种成功；
    - 而是开始区分：
      - 命中是否已经从后排逐步推向 `top50 / top30 / top10`。

- 当前最强主模板 `gapmix_head10_skip40_end50` 的前移结果已可量化：
  - `top100_hit_rate = 0.1842`
  - `top50_hit_rate = 0.1316`
  - `top30_hit_rate = 0.1105`
  - `front_shift_score = 0.1158`
  - `avg_hit_rank = 36.1143`
  - `candidate_win_rate = 0.5488`
  - `candidate_avg_return = 1.9721`
  - 说明：
    - 这条 `priority` 主模板不只是“能命中”，
    - 而是已经可以量化它命中的前排程度。

- `layered_mix` 也终于出现了更像“前移模板”的过线版本：
  - `mix_head12_tail26_30`
  - `top100_hit_rate = 0.1500`
  - `top50_hit_rate = 0.1316`
  - `top30_hit_rate = 0.1158`
  - `front_shift_score = 0.1112`
  - `avg_hit_rank = 23.8421`
  - `candidate_win_rate = 0.5466`
  - `candidate_avg_return = 2.0885`
  - 说明：
    - `mix` 族第一次不仅是补充家族，
    - 而且已经能体现出更靠前的命中质量与较高盈利能力。

- 为了专门清理“只能命中后排、难以前移”的噪音，我新增了 `rear_hit_veto`：
  - 它不抓纯尾部差生，
  - 而是抓：
    - 最近曾经处在 `top100` 后排，
    - 但后续又长期冲不到前排的“后排命中失败结构”。
  - 第一轮结果表明：
    - `rear_hit_veto` 已经进入 `observed combinations`，
    - 但暂时还没有压过 `fake_head_veto / crowded_mid_veto`，
    - 说明方向成立，但当前锋利度还不够。

- 当前最有代表性的“前移 + 盈利”组合提升样本是：
  - `gapmix_head12_skip35_end45`
  - 接入：
    - `fakehead_lb3_head20_minprev3.0_failr120_failpct0.0_minf1`
  - 结果变成：
    - `top100_hit_rate: 0.1711`（`+0.0053`）
    - `top50_hit_rate: 0.1368`（`+0.0052`）
    - `front_shift_score: 0.1171`（`+0.0046`）
    - `avg_hit_rank: 29.8923`（改善 `-0.5998`）
    - `candidate_win_rate: 0.5553`（`+0.0065`）
    - `candidate_avg_return: 2.0297`（`+0.1373`）
  - 这说明：
    - 前移命中率并不是空概念；
    - 当模板命中往前推时，胜率和盈利率确实开始同步改善。

- 当前最客观的新判断应更新为：
  - 提高胜率与盈利率的正确方向，确实不是继续堆普通 `top100` 命中；
  - 而是找到能把命中持续推向 `top50 / top30` 的模板进化路径；
  - 这条路径现在已经被第一轮结果初步验证为有效；
  - 下一步应继续重点压榨：
    - `front_shift_score`
    - `avg_hit_rank`
    - 以及“后排命中失败结构”的剔除精度。

#### 25. 前移命中率第二轮局部加密已打出更强 pass 模板，但正式组合胜率仍差一点跨过 57%

- 这轮没有盲目扩大全局参数，而是围绕已经证明有效的局部区域做加密：
  - 正向模板重点压：
    - `gap_mix` 的 `head13 / head14`
    - `skip35~40`
    - `end45~50`
    - `mix` 的 `head14 + tail24~32`
  - 负向模板重点压：
    - `fake_head_veto`
    - `crowded_mid_veto`
    - 新增更锋利的短记忆 `lookback=1~3` veto

- 第二轮出现了明显更强的新 `pass` 模板：
  1. `gapmix_head14_skip40_end50`
     - `top100_hit_rate = 0.1711`
     - `top50_hit_rate = 0.1395`
     - `top30_hit_rate = 0.1237`
     - `front_shift_score = 0.1211`
     - `avg_hit_rank = 28.2`
     - `candidate_win_rate = 0.5594`
     - `candidate_avg_return = 2.0856`

  2. `gapmix_head14_skip38_end48`
     - `top100_hit_rate = 0.1684`
     - `top50_hit_rate = 0.1395`
     - `top30_hit_rate = 0.1211`
     - `front_shift_score = 0.1197`
     - `avg_hit_rank = 27.0312`
     - `candidate_win_rate = 0.5567`
     - `candidate_avg_return = 2.0346`

  3. `mix_head14_tail24_30`
     - `top100_hit_rate = 0.1605`
     - `top50_hit_rate = 0.1421`
     - `top30_hit_rate = 0.1237`
     - `front_shift_score = 0.1197`
     - `avg_hit_rank = 23.3443`
     - `candidate_win_rate = 0.5556`
     - `candidate_avg_return = 2.0272`

- 这说明：
  - `head14` 一带已经明显更像“前移模板”而不是普通命中模板；
  - `mix` 家族也不再只是补充族，
    而是已经能在 `top50 / top30 / avg_hit_rank` 上打出更像前排能力的结果。

- 当前第二轮里，单模板层最接近目标的一条新结果是：
  - `gapmix_head14_skip35_end45`
  - `candidate_win_rate = 0.5673`
  - `candidate_avg_return = 2.0095`
  - 说明：
    - 单模板层已经非常接近 `57%` 胜率；
    - 但还没有同时把它稳定转成“正式 promoted 组合冠军”。

- 当前第二轮正式 `promoted combination` 的最强结果仍是：
  - `gapmix_head13_skip35_end45`
  - 接：
    - `fakehead_lb3_head20_minprev3.0_failr120_failpct0.0_minf1`
  - 结果：
    - `top100_hit_rate = 0.1711`
    - `top50_hit_rate = 0.1368`
    - `front_shift_score = 0.1171`
    - `candidate_win_rate = 0.5658`
    - `candidate_avg_return = 2.1061`
    - `candidate_retention_rate = 1.0`

- 所以，这轮的最客观结论应更新为：
  - “前移命中率进化路径”已经不是概念验证，而是实打实打出了更强模板；
  - `head14` 和 `mix14` 区域，已经证明是下一阶段最值得继续压榨的正向主战场；
  - 正式组合胜率现在已经推进到 `56.58%`，
    与 `57%` 只差最后一小截；
  - 下一步不该再扩范围，
    而是专门围绕：
    - `head14`
    - `skip35~40`
    - `tail24~30`
    - `fake_head / crowded_mid` 的短记忆 veto
    做最后一轮“冲线优化”。

#### 26. 前移命中率第三轮微调已正式跨过 57% 胜率门槛

- 这轮沿着上一轮已确认的最优方向，继续做了更窄的微调：
  - 正向继续围绕：
    - `head14 / head15`
    - `skip35~40`
    - `end45~50`
    - `mix_head14_tail24~30`
  - 负向继续压：
    - 短记忆 `fake_head_veto`
    - 短记忆 `crowded_mid_veto`
    - `fail_rank` 下探到 `80 / 100 / 120`

- 这轮最关键的结果是：
  - 正式 `promoted combination` 已出现 `57%+` 胜率：
    - `gapmix_head14_skip35_end45`
    - 接：
      - `fakehead_lb5_head15_minprev2.0_failr100_failpct0.0_minf1`
    - 结果：
      - `top100_hit_rate = 0.1658`
      - `top50_hit_rate = 0.1342`
      - `front_shift_score = 0.1158`
      - `avg_hit_rank = 28.9524`
      - `candidate_win_rate = 0.5763`
      - `candidate_avg_return = 2.2105`
      - `candidate_retention_rate = 1.0`

- 这意味着：
  - 正式组合层面，
    `57%+` 胜率目标已达成；
  - 而且不是靠牺牲前移质量换来的：
    - `top50` 没塌，
    - `front_shift_score` 仍为正提升，
    - `candidate_avg_return` 也同步抬到 `2.2105`。

- 同时，这轮也进一步确认：
  - `head14` 一带已经不是“偶然冒出的好参数”，
    而是真正具备前移命中能力的有效区域；
  - `fake_head_veto` 的短记忆版本，
    已经成为当前把正式组合推过 `57%` 的关键负向引擎。

- 当前最新的项目判断应再更新为：
  - “前移命中率进化路径”已经完成了从概念验证到冲线验证；
  - 提高胜率和盈利率的最有效方法，
    确实不是继续堆普通命中率，
    而是：
    - 找到更靠前的命中区域，
    - 再用短记忆失败结构做净化；
  - 当前已经拿到的可落地门槛成绩是：
    - 正式 `promoted combination`
    - `candidate_win_rate = 0.5763`
    - `candidate_avg_return = 2.2105`

#### 27. 冠军组合已固化并接入 workbuddy 候选池主链

- 本轮已把冠军组合从“汇报结论”正式变成“主链元信息 + 主输出权重”：
  - 冠军组合：
    - `gapmix_head14_skip35_end45__veto__fakehead_lb5_head15_minprev2.0_failr100_failpct0.0_minf1`
  - 成绩：
    - `candidate_win_rate = 0.5763`
    - `candidate_avg_return = 2.2105`

- `combined_template_registry.json` 已新增：
  - `champion_template_name`
  - `champion_template`
  这样后续候选池生成不再需要人工记忆“哪条是冠军”。

- `build_workbuddy_distill_pool.py` 已升级为主链版本：
  - 识别注册表里的冠军组合；
  - 冠军组合命中的候选会获得额外主链加权；
  - 选中记录新增：
    - `champion_hits`
    - `champion_score`
    - `distill_champion_core` 角色标记；
  - 候选理由里会直接写出：
    - `命中冠军组合 1 次`

- 输出口也已从“并行试验链”推进到“默认主链”：
  - 仍保留：
    - `workbuddy_distill_candidate_pool_latest.json`
  - 同时正式覆盖：
    - `workbuddy_candidate_pool_latest.json`
  - 这表示当前 `workbuddy` 默认候选池，
    已经由 `distill` 冠军组合主链接管。

- 本次主链生成结果：
  - `trade_date = 2026-06-18`
  - `candidate_count = 26`
  - `selected_count = 5`
  - 前五名为：
    - `300166 东方国信`
    - `300319 麦捷科技`
    - `688486 龙迅股份`
    - `688479 友车科技`
    - `688599 天合光能`
  - 它们均带有：
    - `champion_hits = 1`
    - `role = distill_champion_core`

- 当前应更新的项目口径是：
  - 冠军组合不再只是一个“回测最优模板”；
  - 它已经成为 `workbuddy` 候选池的实际主导模板；
  - 后续如果再出现更强组合，
    就可以按同一机制自动替换新的冠军主链。

---

## 2026-06-24

### 今日结论

- 今天的主线不是再做单点补丁，而是把两条链同时往前推了一大步：
  - `执行层收盘复检与异常收口`
  - `蒸馏模板从单冠军进化到状态驱动双模板路由`
- 当前最重要的项目判断已经更新为：
  - 主交易执行层今天的主要问题已基本查清、修到可继续观察的状态；
  - 蒸馏系统今天真正进化的，不是“简单换冠军”，而是开始按市场结构决定：
    - 什么时候用 `冠军型模板`
    - 什么时候用 `进攻型模板`

### 今日完成

#### 1. 收盘后执行层总复检已完成

- 主交易收盘总复核结论：
  - `PASS`
  - 关键产物：
    - `a-share-analyst\v10_close_node_latest.json`
- Challenger 收盘复核结论：
  - `degraded`
  - 但主要原因是：
    - `尚无已平仓样本`
    - 不是执行链断裂
  - 关键产物：
    - `a-share-analyst\workbuddy_local_review_latest.json`

- 今天收盘复检实际已经覆盖了：
  - 主交易执行层闭环；
  - challenger 本地执行链；
  - phase 时间线；
  - 挂单与账本状态；
  - 收盘后蒸馏日报链。

#### 2. 今天确认并修掉/收口的执行层问题

- `smart-sell 300s timeout`
  - 根因确认不是卖单失败，而是卖后同步收口过重；
  - 已改为轻量 pending 收口，成交闭环交给后续异步 reconcile；
  - 当前已不再保留原先那种同步重型刷新路径。

- `decision -> buy` 并发竞争
  - 已明确：
    - `14:49 decision` 准点启动；
    - 真正拖慢的是内部扫描；
    - 不是任务晚启动。
  - 买入前已补：
    - `wait_for_today_decision_ready()`
  - 目的是防止 `buy` 在 `decision` 尚未完成时抢跑。

- `碎股残仓无法卖出`
  - 根因确认是卖出数量按整百向下取整；
  - 当前已修成：
    - 当剩余可卖仓位本身不足一手时，允许直接按碎股数卖出。

- `午盘接口失败` 表象
  - 今天已查清：
    - 不是账户 API 故障；
    - 是 `v10_pending_orders.json.tmp` 写入 `PermissionError`；
    - 属于沙箱/文件占用型问题，而不是交易接口本体故障。

- `stale pending` 历史噪音
  - 已明确方向：
    - 活跃 pending 与 archive stale 分层；
    - 历史无效记录不再继续阻塞实时门禁；
    - 但保留审计证据。
  - 这一条仍需后续真实盘中链再验证。

#### 3. 窗口层对比已完成

- 已完成：
  - `20+0 / 20+1 / 20+2`
  对比与 Markdown 摘要输出。

- 当前窗口层结论固定为：
  - `20+0`
    - 胜率最佳；
  - `20+1`
    - 平均收益最佳；
  - `20+2`
    - `Top50 hit rate` 与 `front shift score` 最佳，
      且与 challenger 当前渐进缓冲设计最一致。

- 因此当前窗口口径继续定为：
  - 生产参考围绕 `20+2`
  - `20+1` 作为收益侧对照观察。

#### 4. 模板进化已经从“邻域搜索”推进到“状态路由”

- 第一轮邻域模板进化今天已完成确认：
  - 现冠军对局部扰动很抗打；
  - `skip38` 等邻域变体只够进入观察，不够替换生产。

- 第二轮状态感知模板进化已确认一条有效进攻模板：
  - `carry_lb1_cut10_dec1.0_pctw0.0_minp1_0.0_minapp1_reqp1_1_excltop0__veto__crowdedmid_lb5_mid12_35_minprev2.0_failr100_failpct0.0_minf1`
  - 它在强趋势/前排主导结构里更有攻击性；
  - 但还不够稳定到直接接管生产。

- 当前因此形成的新结构不是“换冠军”，而是：
  - `冠军模板` 继续主导稳态
  - `进攻模板` 作为影子挑战模板持续观察

#### 5. 状态层今天从 `V2` 推进到 `V3.2`

- 今天真正的系统级进化，是把蒸馏从：
  - `历史平均最优模板`
  推进到：
  - `按市场结构路由模板`

- 当前已完成：
  - `V2`
    - 状态标签 + T+1 状态预判骨架
  - `V3`
    - 直接按今天市场结构判别冠军型/进攻型
  - `V3.1`
    - 连续特征轻量打分器
  - `V3.2`
    - 边界校准，压缩 `broad_expansion / front_dominant` 的误切抖动

- 当前最重要的判断更新为：
  - `V2` 的价值更像辅助参考；
  - `V3 / V3.1 / V3.2` 已经开始像真正可用的路由主脑。

#### 6. 当前 `V3.2` 路由口径

- 当前市场状态识别为：
  - `broad_expansion`

- 当前 `V3.2` 给出的建议是：
  - `prefer_champion_template`
  - 即：
    - 冠军模板继续做主模板；
    - 进攻模板继续做影子模板。

- 当前关键数值：
  - `attack_probability = 46.33%`
  - `direct_confidence = 0.6551`
  - `route_return_edge_vs_champion = +0.6863%`

- 当前状态优势也支持冠军模板：
  - `sample_reliability = high`
  - `preferred_template = champion`
  - `attack_return_gap = -0.2654%`
  - `attack_hit_day_gap = -0.2`

#### 7. `V3.2` 已正式接进日报链

- 今天最后又完成了一步非常关键的落地：
  - 把 `V3.2` 影子路由正式接进了日报链

- 当前效果是：
  - 主日报里新增了：
    - `V3.2 影子路由`
  - 同时新增独立影子日报：
    - `workbuddy_pool\workbuddy_distill_shadow_route_latest.json`
    - `workbuddy_pool\workbuddy_distill_shadow_route_latest.md`

- 这意味着从现在开始，
  模板进化成果不再只存在于调试 JSON 或回测报告里，
  而是已经进入日常复核产物。

#### 8. `V3.2` 影子样本台账已正式接上

- 今天又补齐了一层更关键的落地：
  - 不再只每天覆盖刷新 `shadow route latest`
  - 而是开始自动沉淀 `shadow route ledger`

- 当前新增产物包括：
  - `workbuddy_pool\workbuddy_distill_shadow_route_ledger.json`
  - `workbuddy_pool\workbuddy_distill_shadow_route_ledger.csv`
  - `workbuddy_pool\workbuddy_distill_shadow_route_ledger.md`

- 当前这层台账的职责已经明确：
  - 收盘时自动记录当日影子建议快照；
  - 记录字段包括：
    - `source_trade_date`
    - `current_state`
    - `route_action`
    - `primary_template`
    - `shadow_template`
    - `attack_probability`
    - `direct_confidence`
    - `route_return_edge_vs_champion`
    - `outcome_status`
  - 后续若拿到对应 `source_trade_date` 的前向验证或执行复核结果，
    会继续回填到同一条记录，而不是只留下每天覆盖的 latest 快照。

- 当前已经成功落下第一条样本：
  - `source_trade_date = 2026-06-23`
  - `snapshot_trade_date = 2026-06-24`
  - `route_action = prefer_champion_template`
  - `current_state = broad_expansion`
  - `outcome_status = pending_eval`

- 这一步的意义非常直接：
  - 从明天开始，`V3.2` 影子路由不再只是“当日建议”；
  - 而是正式变成一条可以连续留痕、连续验证、连续复盘的样本链。

#### 9. 今天这一步的真正意义

- 今天蒸馏模板确实“进化了一步”，
  但不是简单地说：
  - “又换了一个更强冠军”

- 更准确地说，今天完成的是：
  - 从 `单冠军模板蒸馏`
  - 进化到 `冠军模板 + 进攻模板` 的状态驱动双模板路由蒸馏。

- 当前执行口径仍然保持克制：
  - 主模板继续保守使用冠军模板；
  - 进攻模板通过影子路由持续验证；
  - 等积累更多在线样本后，再决定是否进一步放权。

### 当前阶段判断

- 当前项目重点已经不再只是“继续调更多模板参数”。
- 真正的主线已经切换为：
  - `市场状态识别`
  - `结构判别`
  - `模板路由`
  - `影子日报连续验证`

- 当前最正确的推进方式不是再盲目扩大搜索空间，
  而是：
  - 继续积累 `V3.2` 路由的真实日级样本；
  - 观察它在不同状态下的建议与实际结果是否持续一致；
  - 再决定何时从“影子建议”推进到更强的生产参与度。

### 当前最重要结论

- 主交易执行层今天的主要问题已经基本查清并收口到可继续观察的状态。
- 蒸馏系统今天已经从“找冠军模板”升级到“按状态切主模板/影子模板”的阶段。
- 当前最靠谱的口径不是激进切换，而是：
  - `冠军模板继续主导`
  - `进攻模板持续影子验证`
  - `V3.2` 作为当前最可信的状态路由口径

### 下一步动作

1. 明天继续观察执行层：
   - `smart-sell` 是否彻底不再出现 `300s timeout`
   - `buy` 是否不再撞上未完成的 `decision`
   - `stale pending` 是否不再继续污染实时门禁
2. 明天开始连续观察：
   - `workbuddy_distill_shadow_route_latest.md`
   - 记录当天 `V3.2` 建议与后续真实结果
3. 等影子样本积累后，再决定是否把 `V3.2` 从“影子路由”推进到更强的生产参考级别。

---

## 2026-06-25

### 今日结论

- 今天最重要的收口，不是再加新策略，而是把 `challenger` 买前吃旧候选池的问题真正修穿，并把这次事故正式沉淀成“每日巡检驱动代码质量进化”的制度。
- 当前项目从今天开始，正式形成五层联动：
  - 执行层
  - 策略层
  - 学习层
  - 复检层
  - 代码质量进化层

### 今日完成

#### 1. `challenger` stale 候选池问题已查清并修复

- 已确认今天问题根因有三处：
  - `workbuddy-buy` 阶段买前没有强制刷新 distill pipeline；
  - `workbuddy_local_challenger.py` 只是盲读 `workbuddy_candidate_pool_latest.json`；
  - `workbuddy_local_review.py` 把“不是今天”直接判成 stale，误伤了“上一交易日候选池”的正确语义。

- 已完成三处修复：
  - `v10_auto_runner.py`
    - `workbuddy-buy` 买前先执行 `refresh_distill_pipeline.py`
  - `workbuddy_local_challenger.py`
    - 新增候选池 freshness 校验
    - 若 `trade_date` 不等于 `latest_completed_trading_day(...)`，会主动刷新后再买
  - `workbuddy_local_review.py`
    - `source_trade_date` 不再要求等于“今天”
    - 改为要求等于“最新已完成交易日”

- 当前这条链已经形成双保险：
  - 编排器买前刷新一次；
  - challenger 自身再校验一次 freshness。

#### 2. 最小回归与验证已补齐

- 已新增两条关键回归：
  - stale 候选池会触发刷新
  - review 接受“上一交易日候选池”为 fresh

- 已完成验证：
  - `python -m py_compile workbuddy_local_challenger.py workbuddy_local_review.py v10_auto_runner.py test_execution_layer.py`
  - `python test_execution_layer.py`

- 当前结果：
  - `15` 条测试通过
  - `OK`

#### 3. 代码质量进化机制已正式写入制度层

- 今天已经明确：
  - 以后每天巡检不只服务“执行质量汇报”
  - 还必须服务“代码质量进化”

- 当前正式形成的制度是：
  - 巡检发现问题
  - 归类根因
  - 识别暴露出的代码能力短板
  - 新增防线
  - 新增回归验证
  - 次日继续观察是否复发

- 这条机制已经正式写入：
  - `WORKBUDDY_EXECUTION_PLAN.md`

#### 4. 每日巡检固定 checklist 已纳入制度层

- 今天又补齐了一层非常关键的防漏机制：
  - 每日巡检必看项不再靠临场记忆；
  - 已正式改成固定 checklist。

- 当前 checklist 已写入 `WORKBUDDY_EXECUTION_PLAN.md`，并按四段固定下来：
  - 早盘巡检 checklist
  - 盘中巡检 checklist
  - 收盘巡检 checklist
  - 代码质量进化 checklist

- 当前制度要求已经明确：
  - 每次巡检都必须按固定清单执行；
  - 每次巡检后都要固定输出：
    - 执行质量汇报
    - 异常点与影响范围
    - 根因分类
    - 今日进化项
    - 下一观察点
  - 若某次未覆盖清单中的某一项，必须明确写出“本次未覆盖项”，不允许默默跳过。

### 当前阶段判断

- 执行层：
  - 当前重点已从“边跑边补”转向“每天巡检驱动 fail-fast 和回归加固”。
- 策略层：
  - 继续保持克制，先观察大肉激进加仓与路由样本，不在今天继续扩规则。
- 学习层：
  - 继续强调输入可信，不让 stale source 或复检误判污染后续学习。
- 复检层：
  - 现在不只看结果，还要负责识别当天是否暴露了新的工程短板。
- 代码质量进化层：
  - 今日正式从口头共识变成计划书中的长期机制。

### 今日进化项

- 今日暴露问题：
  - `challenger` 在 `14:54` 买前使用了停在 `2026-06-23` 的旧候选池。
- 根因分类：
  - `freshness 类`
  - `时间语义类`
  - `复检失真类`
- 暴露出的代码能力短板：
  - 对“数据刷新责任归属”锁定不够严；
  - 对“上一交易日”与“今天”的业务语义没有在执行层和复检层统一。
- 今日新增防线：
  - 买前强制刷新
  - challenger 自身 freshness 自校验
  - review 统一使用 `latest_completed_trading_day(...)`
- 今日新增验证：
  - 2 条最小回归
  - `py_compile`
  - `15` 条执行层回归全通过
- 明日重点观察：
  - `workbuddy_local_buy_plan_latest.json` 的 `source_trade_date` 是否已稳定对齐预期上一交易日；
  - 收盘复检是否不再误报 `candidate_source_trade_date_stale`。

### 下一步动作

1. 明天继续把 `challenger 14:54 buy` 作为重点巡检点，先验 source freshness 再看执行结果。
2. 每日巡检汇报后固定追加“今日进化项”，把代码能力短板和新增防线一起沉淀。
3. 后续每出现一次真实执行事故，都至少沉淀为：
   - 一条 fail-fast 校验
   - 一条最小回归测试
   - 一条巡检项或复检项

---

## 2026-06-26

### 今日结论

- 系统已经从“几条脚本串起来跑”进入“多链路并行、带复检、带学习、带 challenger 对照、带工程进化”的阶段。
- 到这个复杂度后，`代码图 / 模块图 / 数据流图` 已经不是可有可无，而是有必要正式纳入项目资产。

### 今日完成

#### 1. 收盘复检链继续扩展

- 主链收盘复检已形成更完整闭环：
  - `盘中判断`
  - `尾盘复检`
  - `学习吸收`
- 当天唯一干净正样本 `301626` 已确认进入可学习样本层。
- `challenger` 当天则被复核为 `degraded`，不进入学习吸收。

#### 2. 代码能力进化层已接入

- 已正式把“代码能力问题”从口头教训推进成结构化复检项。
- 当前收盘节点会额外生成工程侧复盘信息，开始记录：
  - 失败类型
  - 复发次数
  - 硬约束
  - 测试提示
- 这意味着后续像：
  - 接线错误
  - 时间口径不一致
  - fast summary 复用旧快照
  - 未定义 helper / 依赖遗漏
- 这类问题不再只是现场修，而会进入工程进化链条。

#### 3. `challenger 20+5` 刷新链与日期口径问题已修复

- 已确认这次 `candidate_source_trade_date_stale` 属于代码能力问题，不是市场问题。
- 根因主要有三层：
  - `workbuddy-refresh` 没有真正刷新 distill 主池；
  - `buy` 前刷新与 `close review` 的 source trade date 口径不一致；
  - `fast summary` 会复用旧摘要日期。
- 当前已完成修复：
  - `workbuddy-refresh` 接入真正的 `refresh_distill_pipeline.py`；
  - 统一 `challenger / review` 的 source trade date 判断；
  - `fast summary` 改为优先回读最新源池；
  - 已补回归测试锁死。

#### 4. 现在为什么必须有代码图

- 已新增第一版代码图文档：
  - `WORKBUDDY_CODE_MAP_V1.md`
- 当前已补到 `V2` 级别信息：
  - `phase <-> 计划任务名` 对照
  - `关键产物反向读取索引`
  - `主链 / challenger` 共享与隔离文件边界
- 当前已继续补到 `V3` 级别信息：
  - `核心产物生成 phase / 更新时间点 / 默认新鲜度要求`
  - `故障排查入口图`
  - `常见状态码解读`
- 当前已继续补到 `V4` 级别信息：
  - `MX / distill / 主交易 / challenger / review / learn / engineering` 链路标签
  - `关键产物影响面索引`
  - `盘中巡检最短路径图`
- 当前已继续补到 `V5` 级别信息：
  - `phase 正常输出示例`
  - `关键故障最短修复路径`
  - `主链 vs challenger 冲突优先级`
- 当前已继续补到 `V6` 级别信息：
  - `关键 phase 输入 / 输出清单`
  - `主链 / challenger / 学习链最短调用路径图`
  - `明日早盘验证清单与验收标准`
- 目前系统里已经同时存在这些核心链路：
  - `external_market_review.py` 负责外部资讯 / 09:31 验真 / 周一周末汇总等情报层；
  - `v10_moni_trader.py` 负责主链执行、午盘节点、收盘节点与工程复检；
  - `evolving_model.py` 负责学习吸收、mode bias / tier bias 微调；
  - `v10_auto_runner.py` 负责自动化 phase 编排；
  - `workbuddy_local_challenger.py` / `workbuddy_local_review.py` 负责 challenger A/B 观察与本地复检；
  - `refresh_distill_pipeline.py` 负责 `20+5` distill 候选池刷新。
- 现在任何一次改动，往往都不再只落在一个文件，而是会跨：
  - 自动化调度
  - 数据落盘
  - 盘中判断
  - 收盘复检
  - 学习吸收
  - challenger 对照
  - 工程进化
- 在这个复杂度下，如果没有代码图，后续最容易反复出现的问题就是：
  - 接线错位；
  - 读写的不是同一份文件；
  - 盘中和收盘口径不一致；
  - 新功能接上了代码，但没有真正进入运行链。

### 当前阶段判断

- 系统复杂度已经跨过“靠脑子记住所有模块关系”这条线。
- 现在最需要的不是继续盲目加新功能，而是把：
  - 模块职责
  - phase 时序
  - 文件产物流向
  - 复检与学习吸收入口
- 用一张清晰代码图固定下来。

### 当前最重要结论

- `代码图` 现在有必要，而且应当视为“降低接线错误、减少理解成本、提升复检效率”的正式工程资产。
- 它的作用不是为了展示，而是为了压住系统复杂化之后的认知负担。

### 下一步动作

1. 补一版 `Workbuddy code map v1`。
2. 第一版代码图至少拆成四层：
   - `phase 时序图`
   - `模块职责图`
   - `关键 JSON / CSV 产物流转图`
   - `复检 / 学习 / 工程进化接线图`
3. 后续每次出现跨文件改动时，若影响主链路或复检链，代码图同步更新，不再只改代码不改结构说明。

---

## 2026-06-30

### 今日结论

- 今天最重要的不是再补一个局部参数，而是把 6 月整月交易、执行层故障、学习层状态和明日上线版本一起收口成一份可执行结论。
- 当前对 6 月的定性已经非常明确：
  - `账户净值抬升`
  - `native 主策略未达标`
  - `执行层在月末集中暴露工程守时问题`
  - `系统需要部分回退 + add-position 进化`
- 因此今天完成的不是“单点修补”，而是：
  - 完成 6 月月报总复盘；
  - 确认哪些增强失败、哪些工程修复必须保留；
  - 把明天起直接可跑的新版本正式落代码并完成任务注册。

### 今日完成

#### 1. 6 月月报已经形成清晰结论

- 当前月报核心判断已经定稿：
  - 清洗后账户净值从 `06-05` 首个有效非零快照到 `06-30`，表观收益约 `+14.6%`；
  - 但 `native` 主策略真实样本口径下，`27` 笔平仓、胜率 `22.22%`、平均收益 `-2.9607%`、已实现盈亏 `-29612.5`；
  - 这说明 6 月不是“主策略跑赢”，而是“账户净值抬升与主策略真实质量错位”。

- 月末收盘节点也和这一判断一致：
  - `review_status = WARN`
  - `learning_gate_status = hold`
  - `capital_allocation_verdict = underperforming`
  - `intraday_judgment_verdict = invalidated`

- 模式层结论已经明确：
  - `trend_ride+green` 是本月第一亏损源；
  - `V9_full` 在月末进攻段杀伤过大；
  - `near_kill+weekly+MA20`、`trend_only` 相对更稳，适合作为下月更可信的基线模式。

- Tier 层结论也已经拆清：
  - `T1` 不是略差，而是 `V9_full` 首建过猛导致回撤失控；
  - `T2` 是样本最多、最值得优先修复的一层，问题集中在回锅票和 `trend_ride+green`；
  - `T3` 最接近“可用但不够优秀”，真正拖累它的不是基础模式，而是后续资金放大逻辑。

#### 2. 已明确“不能整体回到月初原策略”，只能部分回退

- 今天对“是不是应该整体回退到月初原策略”的判断已经收口：
  - 不能整体回退；
  - 必须做 `分层回退 + 分层保留`。

- 必须保留的不是增强，而是已经被实盘证明必须存在的工程和防错层：
  - `recent_trade_memory / 重入冷却`
  - `add_position` 给新机会让位
  - `external_sync` 样本隔离
  - `add_position` pending cleanup 守时
  - `smart-sell_shared` 共享锁
  - `external_market_review / midday fusion` 冲突修正

- 应当回退的是本月被证伪的激进增强层：
  - `T1 + V9_full` 满仓首建
  - 正向 `capital_bias` 加码
  - `T2 trend_ride+green` 的高优先级放大

#### 3. 部分回退版本已落代码，明天直接生效

- 已在 `v10_moni_trader.py` 中完成第一轮部分回退：
  - `T1` 首建从 `70%` 降到 `50%`
  - 禁用 `T1 V9_full` 满仓首建
  - 停用正向 `capital_bias`
  - 非强确认下直接拦截 `T2 trend_ride+green`
  - 加仓链对近期惩罚票改为硬跳过

- 这一步的意义不是简单“收缩”，而是把系统先拉回更稳健的风险骨架，让 7 月的改进建立在更干净的基础上。

#### 4. `add-position` 已从单点补仓进化为多窗口确认框架

- 今天进一步确认：
  - 下个月想提升胜率和盈利率，不能只靠尾盘新开仓；
  - `add-position` 必须升级成盘中资金利用率引擎。

- 当前第一阶段进化已经落地：
  - `add-position` 不再只在 `09:36` 运行；
  - 新增 `10:28` 与 `13:28` 两个确认窗；
  - 三个时间窗分别承担：
    - `09:36` 开盘确认
    - `10:28` 趋势升格
    - `13:28` 午后回流再加速

- 当前窗口配置已经接入代码：
  - 每个窗口有独立的 `score_min`
  - 每个窗口有独立的 `aggressive_score_min`
  - 每个窗口有独立的 `reserve_cash_ratio`
  - 每个窗口有独立的 `non_aggressive_max_items`

- 这意味着 `add-position` 已经从“统一口径补仓”切换到“按时段确认、按强度分层加仓”的框架。

#### 5. 大肉识别已经从后验确认前移到动态升格

- 今天最关键的策略进化不是“多加两个时间点”，而是把“大肉识别”从后验改成前移：
  - 不再等 `+15%` 才承认是大肉票；
  - 改成在 `+3% ~ +6%` 的早期趋势段就开始识别。

- 当前已完成的大肉识别升级包括：
  - 新增 `ADD_POSITION_BIG_MEAT_EARLY_PROFIT_PCT = 3.0`
  - 把 `ADD_POSITION_BIG_MEAT_PROFIT_PCT` 前移到 `6.0`
  - 把 `ADD_POSITION_BIG_MEAT_DAY_CHG_PCT` 下调到 `3.0`
  - 新增 `flow / sector / stock / total score` 门槛

- 更重要的是：
  - `kill_only`
  - `trend_only`
  - `near_kill+weekly+MA20`
  这类种子模式现在不再只是观察层，
  已可以在盘中通过动态确认被升格成大肉加仓候选。

- 新的大肉确认逻辑已经接入：
  - `贴近日高`
  - `站稳日内锚`
  - `5分钟再突破`
  - `周线向上`
  - `分时回流/再加速`
  - `flow / sector / total score`

- 这一步的真实目标是：
  - 不只是多做几笔加仓；
  - 而是增加“本来只是边缘强票，后来被系统盘中升格成利润核心”的数量。

#### 6. 调度层已完成注册，明天会按新版本自动运行

- 今天已经重新注册计划任务，新增任务已经生效：
  - `TLFZ-WorkBuddy-AddPosition1028`
  - `TLFZ-WorkBuddy-AddPosition1328`

- 当前明天的 `add-position` 实际运行时点已经变成：
  - `09:36`
  - `10:28`
  - `13:28`

- 当前关键意义是：
  - 这不再只是代码里“支持了多窗口”；
  - 而是自动化任务链已经真的会按新时间窗执行。

#### 7. 已确认并修复“账本污染”大 BUG

- 今天额外确认了一条非常严重的底层问题：
  - `v10_track_record.csv` 一度不是主模拟账户真实持仓的干净映射；
  - 正常持仓会被错误打成 `external_sync` 或 `[AUTO_IMPORTED]`；
  - 甚至“不是当天买入”的 holding 也可能被粗暴判成 `legacy`。

- 这条 BUG 的真实危害不是“报表不好看”，而是会直接污染主策略判断：
  - 真实持仓身份丢失，`mode / tier / target_amount / decision_id` 失真；
  - `add-position` 会把强票误当成异常仓或无效仓；
  - 学习层会把亏损样本大量阻断为 `external_sync_record`；
  - 近期出现的“高买低卖”很可能被这层底座污染显著放大。

- 已完成的修复包括：
  - 禁止主模拟账户链路再生成 `external_sync`；
  - 修复 `legacy holding` 误判，不再把前一天正常持仓当成脏账；
  - 真实持仓缺上下文时，优先回填原生策略身份，而不是补空壳标签；
  - 清洗 `v10_track_record.csv` 中历史 `external_sync / [AUTO_IMPORTED]` 残留。

- 当前已完成的核验结果：
  - `mx-moni` 实仓与账本持仓在代码、数量、成本价、买入日期上重新对齐；
  - 当前真实持仓 `7` 只全部恢复为原生策略仓；
  - `non_native_count = 0`；
  - 当前账本已不再存在 `external_sync / [AUTO_IMPORTED] / LIVE_POSITION_ONLY` 活跃污染。

### 当前阶段判断

- 执行层：
  - 6 月末最危险的超时点已经完成根因级修复；
  - 7 月第一目标不是继续扩规则，而是先观察这些修复是否在真实盘中稳定生效。

- 策略层：
  - 当前最正确的方向不是回到月初原策略；
  - 而是：
    - 保留工程修复
    - 回退失败增强
    - 强化 `add-position` 和大肉动态识别

- 学习层：
  - 6 月仍处于 `hold`；
  - 当前不能把不干净样本直接当成学习胜利，而要继续区分：
    - 可直接学习样本
    - 观察样本
    - 执行损坏样本

- 代码能力进化层：
  - 今天又完成了一次从“盘中发现问题”到“制度化收口”的完整闭环；
  - 这次进化不只是策略层，而是第一次把“账本准确性”正式提升为主策略底座的一部分；
  - 项目对“数据事故”的理解已经升级：
    - 账本不是日志附件；
    - 账本就是策略、学习、复盘共同依赖的核心数据层；
    - 一旦账本被污染，后续看到的高买低卖、错误减仓、学习失真都会被同时放大。
  - 以后类似 `高买低卖 / 回锅票 / 错失大肉 / 窗口拥堵`，都应继续沿“月报复盘 -> 设计升级 -> 代码落地 -> 任务注册 -> 次日观察”这条链推进。

### 当前最重要结论

- 6 月真正暴露出的不是一个策略小问题，而是：
  - `选股 / 排序 / 加仓 / 风偏 / 执行 / 工程守时`
  六层同时存在摩擦。

- 今天完成的收口非常关键，因为它把项目从：
  - “知道自己 6 月为什么没达到基线”
  推进到了：
  - “已经把明天开始要跑的 7 月第一版修正版真正上线”

- 当前 7 月第一版的真实核心不是“更激进”，而是：
  - 更稳的风险骨架
  - 更早的大肉识别
  - 更多窗口的确认加仓
  - 更清晰的新机会让位与资金利用率管理

### 下一步动作

1. 明天重点巡检 `09:36 / 10:28 / 13:28` 三个 `add-position` 窗口：
   - 看是否真的出现大肉升格候选；
   - 看是否仍有旧票续命式加仓。
2. 明天重点观察：
   - `T1 V9_full` 是否已不再出现满仓首建；
   - `T2 trend_ride+green` 是否已被明显压缩；
   - `kill_only / trend_only` 中是否出现被盘中升格的强票。
3. 盘后第一时间复盘新增字段：
   - `window_tag`
   - `big_meat_state`
   - `big_meat_score`
   - `big_meat_aggressive_score`

### 追加约束（2026-07-01 盘中）

- 今日盘中进一步确认：`002897 / 301458 / 603267` 出现了同类错误，根因不是单票判断分歧，而是旧版本里“大肉加仓链”和“smart-sell 衰减链”缺少统一状态机，导致同日先加仓、后卖老仓、留下新仓。
- 这次事故证明，`big_meat` 相关改动不能再按“一阶先上、二阶后补”的方式拆分上线。对于会直接改变买卖动作的强耦合链路，只上线半套，等于把系统暴露在新的交易错误里。
- 项目正式新增一条代码能力进化原则：
  - 交易状态机类改动，禁止半套上线。
  - 若 `识别 -> 状态 -> 执行 -> 账本落盘` 不能一次闭环，则该功能先不上线。
  - 若必须临时上线，必须先加门禁，禁止新逻辑与旧卖出/旧加仓分支同时生效。
- 今日已补齐 `big_meat_candidate / big_meat_confirmed / hold_core / risk_trim / hard_exit` 状态机，后续将以这套状态作为大肉选择与卖出动作的统一桥接层，不再允许“加仓看大肉、卖出看普通衰减”两套逻辑各自执行。
   - `big_meat_score`
   - `aggressive_score`
   用来判断这次进化是否真的开始提升大肉数量和资金利用效率。
4. 明天盘中把“账本准确性”列为重点巡检项：
   - 用 `mx-moni` 实仓对照 `v10_track_record.csv`；
   - 优先核查代码、数量、成本价、买入日期、`mode / tier / target_amount`；
   - 若再次出现偏差，先按底层数据事故处理，再解释策略表现。

### 2026-07-06 Challenger fallback 审核追加

- 今日继续把 `workbuddy_local_challenger.py` 按“是否会制造假成交 / 假价格”做了一次专项审核，结论不是单点 bug，而是执行建模约束需要再收紧一层。
- 新确认的代码能力问题：
  - 卖出链虽然已修正为只认实时 `price`，但买入链此前仍允许 `last_close -> entry_price -> 本地成交` 这条路径成立；
  - 持仓摘要在无实时行情时会回退到 `entry_price` 或上一份快照价格，如果不暴露来源，容易把估值错看成实时价格。
- 今日补上的硬约束：
  - 执行型 fallback 必须尊重真实交易世界；
  - 估值 fallback 与成交 fallback 必须物理隔离；
  - 缺实时成交价时允许 `skip / no_action`，不允许伪造一笔“看起来合理”的成交；
  - 所有持仓 fallback 价格必须逐笔暴露来源标签，不能只在 summary 顶层写一个笼统的 `quote_mode`。
- 已落地代码：
  - `workbuddy_local_challenger.py -> build_buy_plan()` 改为买入执行价只认实时 `price`，不再接受 `last_close` 作为本地成交价；
  - `workbuddy_local_challenger.py -> _holding_rows() / _holding_rows_with_fallback()` 新增 `current_price_source / price_is_estimated`；
  - `workbuddy_local_challenger.py -> _build_account_snapshot()` 新增 `live_price_count / estimated_price_count`，把估值质量显式带进账户摘要。
- 已补回归测试：
  - `test_execution_layer.py -> test_challenger_build_buy_plan_skips_when_only_last_close_is_available`
  - `test_execution_layer.py -> test_challenger_holding_rows_marks_fallback_price_source`
- 这次教训正式归档为：
  - 执行层的目标不是“流程不断”，而是“只有满足真实成交前提时才允许落账”；
  - 任何会污染 `track_record / execution_state / order_log / pnl` 的价格 fallback，都应优先按代码能力事故处理。

### 2026-07-06 收盘复检追加

- 收盘节点 `15:06` 已完整跑通，`close-node` 总状态为 `ok`，全链在 `113` 秒内结束。
- 主策略收盘口径：
  - `v10_account_summary_latest.json` 显示 `holding_count=0`、`position_count=0`、`total_pos_value=0.0`；
  - `v10_close_node_latest.json` 显示 `summary.closed_count=55`、`realized_pnl=-21516.36`；
  - `engineering_review.incident_codes` 仍记录当日 `smart-sell / add-position / buy-watch` 的 4 个高影响代码能力事件。
- Challenger 收盘口径：
  - `workbuddy_local_account_summary_latest.json` 显示 `holding_count=0`、`cash_balance=893688`、`total_return_pct=-10.6312`；
  - `workbuddy_local_review_latest.json` 显示 `today_order_count=5`、`today_buy_count=0`、`today_sell_count=5`；
  - `workbuddy_learning_advice_latest.json` 结论为 `adoption_verdict=observe`，今晚只观察，不让 challenger 样本直接改主模型。
- 今日新增确认的代码能力问题：
  - `14:50 buy-watch` 第 1 次失败时，`14:49 decision` 实际上尚未完成；第 2 次重试才恢复为 `no_action`。
  - 静态审查确认 `wait_for_today_decision_ready()` 与 `_read_json()` 存在“空读/半更新状态文件被当成 ready”的竞态风险。
  - 单测曾污染 `workbuddy_local_buy_plan_latest.json`，盘中一度把假票和假资金快照写进真实运行产物。
  - `Challenger readiness` 曾把 `0.0` 盘中涨跌幅错误回退到候选池 `latest_chg_pct`，会虚高连续性评分。
- 今日已落地修复：
  - `workbuddy_local_challenger.py`
    - 执行价只认实时 `price`，无实时价直接 `skip`；
    - `persist_plan` 开关阻断测试写真实 `buy_plan`；
    - `current_chg_pct` 改为“仅缺失时回退”，不再把 `0.0` 当成缺失；
    - 持仓摘要新增 `current_price_source / price_is_estimated / live_price_count / estimated_price_count`。
  - `v10_moni_trader.py`
    - 主策略补账链不再允许用 `ref_price / order_price / entry_price` 伪造真实成交价；
    - 缺真实成交价时，卖出记录进入 `paused` 等后续真实价补齐。
  - `v10_auto_runner.py`
    - `workbuddy_local_challenger.py` 的 `no_action / skipped` 已改成语义化 detail，不再显示 `step failed`。
- 今日仍保持打开的调试会话：
  - `debug-challenger-zero-pnl.md`
  - `debug-buywatch-1450-fail.md`
  - 这两个会话都尚未 cleanup，等待下一个交易日继续观察线上运行证据。
- 今日收盘后的硬结论：
  - 主策略和 Challenger 最终都以空仓进入收盘；
  - Challenger 当日 5 只候选按当前 `readiness` 公式全天都未出现任何一个 `score >= 62` 的买点窗口；
  - 今天的主要收获不是“策略该更激进”，而是进一步确认执行层必须继续强化真实世界约束、状态一致性和测试隔离。

---

## 2026-07-07

### 主策略 learning gate 时序 bug 修复

#### 1. 问题定性

- 今日复核确认：`learning gate` 被错误前移成了盘中 `buy / add-position` 的前置硬门。
- 旧实现要求盘中交易日 `trade_date=D` 时，`v10_learning_gate_status.json` 的日期也必须是 `D`。
- 但 `learning gate` 文件本身由 `15:06 close-node` 生成，这会把：
  - `13:28 add-position`
  - `14:50 buy-watch -> buy`
- 这类盘中动作，错误地绑定到“当天收盘后才会生成的 gate”上，形成时序悖论。
- 这属于代码能力层 / 执行层边界错误，不是策略层 `no_action` 结论。

#### 2. 本轮修复

- `v10_moni_trader.py`
  - `learning gate` 预检恢复为“上一交易日收盘 gate 约束当前盘中交易”。
  - 盘中 `trade_date=D` 时，预期 gate 日期改为 `previous_trading_day(D)`，不再错误要求当天 `close-node` 先完成。
  - 缺 gate / gate 过旧时，错误提示同步升级为：
    - 当前 gate 日期
    - 预期最近有效收盘日期
- 这样 `learning gate` 的语义重新回到：
  - 盘后学习结论 -> 次日盘中执行约束
  - 而不是“盘中执行前先等当天盘后结论”。

#### 3. 工程守则沉淀

- 守则 1: `盘后学习 gate` 不得直接前移成 `盘中执行 gate`。
- 守则 2: 任何 `close-node` 产物若要影响交易，默认只能影响下一交易日，不得反向卡死当日盘中 phase。
- 守则 3: 执行层、复核层、学习层必须分层：
  - 执行层看盘中事实；
  - 复核层在盘后判定样本可信度；
  - 学习层只基于盘后复核结果调整次日偏置。

#### 4. 验证

- 新增两条回归测试：
  - 上一交易日 `learning gate` 会对当前盘中交易日正常放行；
  - 同日 gate 在盘中上下文仍会被视为无效，避免再次把当天 `close-node` 产物提前接进盘中链。
- `python -m unittest test_execution_layer` 中本次新增用例通过。
- `python -m py_compile v10_moni_trader.py test_execution_layer.py` 应通过，用于拦截语法回归。

#### 5. 当前结论

- 这次 bug 的本质不是 `learning gate` 不该存在，而是它被错误地用在了盘中。
- 修复后，`learning gate` 回到正确定位：
  - 盘后样本复核与次日偏置约束；
  - 不再污染当日 `buy / add-position` 的执行时序。

---

## 2026-07-09

### 主策略 / Challenger 目标对齐改造

#### 1. 问题定性

- 今日先从目标函数重新复核了主策略、challenger、模板晋级、候选排序、guardrail 扣罚和学习链。
- 结论不是“参数太保守”这么简单，而是代码层存在明确的目标偏离：
  - 会奖励“低仓防守没出事”；
  - 会把 `selection_rank / champion_hits / latest_rank` 这类代理变量放在胜率和盈利率之前；
  - 会让模板是否晋级更多由旧命中率稳定性决定，而不是收益/胜率质量。
- 按交易目标来讲，这属于逻辑 bug，不是风格差异。

#### 2. 主策略链路改造

- `v10_moni_trader.py`
  - 已修掉 `risk_on + pm_gate=pass + signals充足 + 零开仓` 被误记成防守正样本的问题。
  - `intraday_judgment_review` 与 `regime_execution_review` 不再把这种场景当成 `defensive-low-exposure-good-execution`。
  - 新增 `missed opportunity` 后验收益回填，给“已选未买但次日为正”的样本补 D1 证据。

- 午盘状态机已从“只继承早盘偏防守先验”改成“同步看午后候选强度”：
  - `scan_status`
  - `stocks_with_signal`
  - `signals_by_tier`
  - `midday_release_soft_ready`
  - `midday_release_ready`
  - `midday_release_override`
- 现在如果：
  - `risk_on`
  - `pm_gate=pass`
  - 候选强度达标
  - 且 `short_flow / opening_anchor_break` 不再是高压
- 则午盘判断至少要从 `defensive` 抬到 `balanced`，不能继续机械锁死防守。

- PM gate 也已改成会优先吃“真实盈利机会证据”：
  - `missed_opportunity_positive_count`
  - `missed_opportunity_avg_return_pct`
- 这样尾盘放行不再只看风险标签，还会看近期是否已经证明“过度保守会错过钱”。

- 同时删掉了“防守成功 -> 次日缩仓奖励”这条链。
- 现在 `defensive-low-exposure-good-execution` 只保留为复核记录，不再继续强化保守仓位。

#### 3. Challenger 与候选池改造

- `build_workbuddy_distill_pool.py`
  - 模板和候选排序已提升为“收益/胜率优先”，不再只由 `business_score + champion proxy` 驱动。
  - 新增：
    - `profit_priority_score`
    - `avg_profitability_priority`
  - `template_weight`、最终 `ranked.sort`、`selected_records` 输出都已接入这套盈利优先分。

- `workbuddy_distill/scripts/distill_local_templates.py`
  - 模板 verdict 不再只看：
    - `top100_hit_rate`
    - `top30_hit_rate`
    - `hit_day_rate`
  - 现在 `priority/pass/prototype` 都必须同时过盈利约束：
    - `candidate_win_rate`
    - `candidate_avg_return`
    - `portfolio_positive_day_rate`
    - `profit_priority_score`
  - 组合模板的 `promote/observe/reject` 也已改成：
    - 收益/胜率提升为主门；
    - 旧命中率稳定性只做护栏，不再一票否决。

- `build_workbuddy_distill_pool.py`
  - 之前 `hot crowding` 仍按旧代理顺序扣罚：
    - `latest_rank`
    - `champion_hits`
    - `raw_selection_score`
  - 现已改成按盈利优先分排序后再定义谁吃拥挤惩罚。
  - 避免出现“主排序已按盈利优先，但 guardrail 还在按旧代理先惩罚”的断层。

- `workbuddy_local_challenger.py`
  - challenger 执行本体已正式接入：
    - `avg_profitability_priority`
    - `avg_candidate_win_rate`
    - `avg_candidate_avg_return`
  - 候选会先按盈利优先顺序进入执行链，再做 readiness 判断。
  - `readiness / entry / add / runner exit` 都不再主要依赖旧的 `selection_rank` 代理逻辑。
  - 新增动作包括：
    - `alpha_core_buy`
    - `alpha_probe_buy`
    - `profit_priority_confirm_add`
  - `runner_candidate` 也改成由盈利优先分驱动，不再被错误 rank 条件拦截。

#### 4. 回归测试与校验

- 本轮已补定向测试，覆盖：
  - 主策略 risk-on 零开仓不再被误奖；
  - missed opportunity 后验收益回填；
  - 午盘释放逻辑；
  - challenger 盈利优先 readiness / runner / 排序；
  - 模板 verdict 必须吃盈利条件；
  - hot crowding 要跟盈利优先顺序一致。

- 已通过的校验包括：
  - `python -m unittest test_execution_layer ...`
  - `python -m unittest test_workbuddy_distill_target_alignment.py`
  - `python -m py_compile v10_moni_trader.py test_execution_layer.py`
  - `python -m py_compile build_workbuddy_distill_pool.py workbuddy_distill\scripts\distill_local_templates.py test_workbuddy_distill_target_alignment.py`

#### 5. 明日执行前验证

- 为保障明日交易顺畅，今天额外做了无副作用运行验证。

- 已成功重建最新 candidate pool：
  - `python build_workbuddy_distill_pool.py`
  - 成功生成并覆盖：
    - `workbuddy_candidate_pool_latest.json`

- 已对 challenger 做无落盘计划构建验证：
  - `build_buy_plan(trigger_slot='10:30', force=True, persist_plan=False)`
  - 成功生成买入计划，不报错。
  - 当前输出显示：
    - 候选池来源日：`2026-07-09`
    - 可买候选：`2`
    - 跳过：`3`
  - 前两只计划买入候选为：
    - `688820 盛合晶微`
    - `688258 卓易信息`
  - 这两只的：
    - `avg_profitability_priority`
    - `readiness_score`
    - `runner_candidate`
  - 都已经符合新的盈利优先执行逻辑。

- 已对主策略做午盘节点 / PM gate 纯构建验证：
  - `build_midday_review()`
  - `_build_midday_node_payload(..., stage='pm_gate')`
  - `_build_pm_buy_guardrails()`
  - 三者均能成功返回。

- 当前验证结果显示：
  - 主策略链路代码已可正常执行；
  - 但当前环境下 `scan_status.is_fresh = false`，
  - 因此 PM gate 仍输出 `defensive_limit`，没有触发当天午后释放分支。

#### 6. 当前结论

- 今天这轮不是小修参数，而是把两条线都重新对齐到“胜率 + 盈利率优先”。
- 当前状态可以概括为：
  - `主策略`：执行逻辑已对齐，代码链路可跑通；
  - `challenger`：候选排序 + 执行本体都已对齐，且已用无落盘计划实测通过；
  - `模板晋级 / crowding guardrail`：已补齐，不再被旧代理目标拉偏。

- 目前离“明天一定按新逻辑放仓”只差一个运行前提：
  - 明天午盘 / 尾盘时点的 `scan_status` 必须保持新鲜。
- 若明日盘中扫描新鲜、候选强度达标，则主策略应按新午盘释放逻辑工作；
- 若扫描仍 stale，则主策略仍会保守，这属于当前保护设计，不是本轮新 bug。

#### 7. 下一步动作

1. 明天盘中优先检查：
   - `scan_status.is_fresh`
   - `stocks_with_signal`
   - `midday_release_ready`
   - `midday_release_override`
2. 若午盘扫描新鲜，复核 PM gate 是否从 `defensive_limit` 转入更积极放行状态。
3. 明日收盘后对照：
   - 主策略是否真正按新逻辑释放；
   - challenger 计划顺序与实际表现是否一致；
   - 若仍有残余代理逻辑，再继续收口。

## 2026-07-14

### 收盘复检与主策略执行层修复

#### 1. 收盘节点状态确认

- 已直接复核 DO 上今天的收盘节点落盘：
  - `15:06 close-node` 最终 `status=ok`
  - `duration_seconds=485`
  - `v10_close_node_latest.json` 与 `v10_account_summary_latest.json` 均已刷新
- 收盘产物中的关键后验结论包括：
  - `intraday_judgment_review.verdict = validated`
  - `regime_execution_label = defensive-low-exposure-watch`
  - `missed_opportunity_count = 0`
- 这说明今天的 day-end 产物本身是完整可读的，收盘链没有再像前一轮那样炸在 close-node。

#### 2. 盘中“没买/没加/没卖”根因复盘

- 今天不是简单的“策略太保守”，而是先定位到了主策略执行层时区 bug：
  - DO 主机系统时区是 `UTC`
  - 交易日调度器按中国市场时钟运行
  - 但 `v10_moni_trader.py` 的主交易窗口判断直接用了本地 `datetime.now()`
- 结合 DO 真实 phase history，证据很强：
  - `09:36 / 10:28 / 13:28 add-position` 全部 `window skipped`
  - `14:50 buy-watch` 从首轮到最后一轮重试都 `window skipped`
  - 这些时间在北京时间本来就在合法窗口内，但在 DO 本地 `UTC` 语义下会被错误判成窗口外
- 结论：
  - 今天主策略“无动作”的执行轨迹被时区 bug 污染
  - 今天不能再把“没加仓”直接解释成策略主动且正确地放弃了所有机会

#### 3. 已完成的代码修复

- 在 `v10_moni_trader.py` 中新增了统一市场时区 helper：
  - `MARKET_TZ`
  - `_market_now()`
  - `_market_today()`
- 已将主策略核心时间敏感路径切到市场时区：
  - `ensure_trade_window()`
  - `_resolve_add_position_window()`
  - `do_buy()`
  - `_do_sell_core()`
  - `do_add_position()`
  - `wait_for_today_decision_ready()`
  - `build_midday_review()`
  - `_build_pm_buy_guardrails()`
- 这样做的目的不是改策略，而是让 DO 上的主策略执行层真正按 `CST/UTC+08` 判定交易窗口和当日日期。

#### 4. DO 同步与验证

- 已将下列文件同步到 DO：
  - `v10_moni_trader.py`
  - `test_execution_layer.py`
- 已先在 DO 侧做备份：
  - `/opt/stockbot/workbuddy/skills/a-share-analyst/.bak_20260714_tzfix/`
- 验证口径使用 DO 正式解释器，而不是裸 `python3`：
  - `/opt/stockbot/.venv/bin/python`
- 已完成的 DO 验证包括：
  - `py_compile` 通过
  - `python -m unittest test_execution_layer.TraderWindowTimezoneTests -v` 通过
  - 关键窗口直测通过：
    - `09:36 add-position => allowed=True`
    - `14:52 buy => allowed=True`
    - `14:15 smart-sell => allowed=True`
    - `wait_for_today_decision_ready()` 已按市场日期识别同日 decision

#### 5. 当前判断

- 对“明天主策略执行层会不会再被这个时区 bug 影响”的判断：
  - 主策略核心执行链已高置信修住
  - 明天不应再因为 DO 是 `UTC` 而把合法中国市场窗口误判成 `window skipped`
- 但需要明确：
  - 这不等于“明天一定会买”
  - 它只意味着明天的主策略行为终于可以被干净解释，不会再被这类基础执行 bug 污染

#### 6. 仍待继续处理的问题

- `14:30 prewarm` 的根因已进一步收敛：
  - 昨天本地已经写了 `prewarm-fast`
  - 但第一次 DO 同步漏掉了 `v10_auto_runner.py`
  - 导致 DO 仍按旧路径执行 `['scanner_v10.py']`，看起来“修过”，实际没有生效
- 现已重新同步 `scanner_v10.py / v10_auto_runner.py / test_execution_layer.py`
- DO 正式 `.venv` 验证通过：
  - `test_prewarm_phase_uses_fast_scanner_mode` 通过
  - `prewarm` 单 phase replay `status=ok`
  - `duration_seconds=761`，不再复现 `907s timeout`
  - `decision` 单 phase replay `status=ok`
  - `duration_seconds=169`
  - DO 日志明确显示执行命令为 `scanner_v10.py --decision-fast`
- 当前遗留不再是“prewarm 跑不通”，而是“14:30 对 14:49 仍然偏紧”：
  - `prewarm_timing_signal.json` 已刷新为 `status=ok`
  - 但建议改为 `14:25`
- `native holding` 识别口径仍值得继续核对，因为即便时区修好，`add-position` 也只对主策略原生持仓生效
- 这些问题不会掩盖今天已经完成的最关键修复：
  - 主策略交易窗口和当日 decision 判定已对齐到 `CST`

#### 7. 明日优先观察位

1. `09:36 add-position`
2. `10:28 add-position`
3. `14:49 decision`
4. `14:50 buy-watch`
5. `scan_status.is_fresh`
6. `latest_buy_status.json` 是否还出现伪 `window skipped`
7. 是否正式把 `prewarm` 调度从 `14:30` 提前到 `14:25`
