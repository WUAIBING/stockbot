---
name: a-share-analyst
display_name: A股智能分析师
title: A股智能分析师 — mx-data + mx-xuangu + mx-search + pytdx 四合一 + V10多层级短线策略
description: 集成东方财富妙想(mx-data/mx-xuangu/mx-search)和通达信(pytdx)的全栈A股分析技能。支持行情查询、财务分析、智能选股、资讯搜索、K线技术分析、T+5短线多层级信号扫描，用自然语言驱动全流程。
version: 3.0.0
author: Master Wu Workbuddy Bot
env:
  - MX_APIKEY: "东方财富妙想API Key (User级永久环境变量已配置)"
---

# a-share-analyst — A股智能分析师

四引擎合一 + V10多层级短线策略，自然语言驱动全栈A股分析。

## 四大引擎

| 引擎 | 工具 | 擅长 | 调用方式 |
|------|------|------|----------|
| **mx-data** | 东方财富妙想数据 | 行情/财务/指数/股东/资金流 | 自然语言 → API |
| **mx-xuangu** | 东方财富妙想选股 | 条件选股/板块筛选/成分股 | 自然语言 → API |
| **mx-search** | 东方财富妙想资讯搜索 | 新闻/研报/公告/政策/事件 | 自然语言 → API |
| **pytdx** | 通达信底层协议 | K线/复权/精确tick/5分钟线 | Python SDK |

## 何时用哪个

```
用户意图                          → 路由
──────────────────────────────────────────
"查个股价/财务/资金流"            → mx-data
"帮我选股，条件是..."            → mx-xuangu
"XX股票最新新闻/研报/公告"       → mx-search
"XX行业有什么政策/消息"           → mx-search
"看K线/复权/分钟线"              → pytdx
"分析一下XX股票"                 → 四引擎全开
"哪个板块值得关注"               → mx-xuangu(板块) + mx-data(行情) + mx-search(资讯)
"XX股票值不值得买"               → 四引擎全开
"扫描今天短线信号"               → V10 scanner (scanner_v10.py)
"跑回测"                         → V10 backtest (backtest_t5_v10.py)
```

---

## V10 多层级短线策略（核心功能）

### 策略哲学

**"大肉小肉都是肉"** — 散户最大的优势是灵活。不要追求完美信号而放弃大多数交易机会。

- 单一模式（V9）= 125信号/年，60%交易日有信号 → 对胜率和收益率的放弃
- 多层级多模式（V10）= 1913信号/年，97%交易日有信号 → 灵活吃肉

**"T+5是总框架，不是锁死持仓天数"** — 买靠信号，卖靠判断，T+5只是兜底

**⚠️ A股T+1规则：当日买入的股票，最早T+1才能卖出。**
- 不存在T+0买卖同一天（除部分ETF可T+0）
- 有底仓才能做日内T（先卖底仓→同日买回，或先买→卖底仓部分）
- **V10买入的仓位本身就是底仓！T+1起即可做日内T**

**V10 + 日内T = 完整散户生存体系（一盘棋）：**
- V10信号 = 起手式（何时建仓、建什么仓）
- 日内T = 中盘缠斗（底仓在手，灵活操作）：
  - 正T：T+1大涨 → 卖半仓锁利 → 回落假摔 → 低位买回（底仓完整+差价到手）
  - 反T：T+1低开 → 判断洗盘加仓 → 反弹到高点 → 卖原底仓（成本降低+仍持仓位）
  - 横盘/信号完好 → 继续持有
  - 信号衰减 → 清仓走人
- T+5兜底 = 收官（还没走就强制评估）

灵活止盈窗口（从买入日T算起）：
- T+1赚了感觉今天到顶了 → 半仓止盈（正T）
- T+1低开判断洗盘 → 加仓做反弹（反T）
- T+2有利润但信号转弱 → T+2清仓
- 信号完好 → 继续持有，T+5到期再评估
- 用 `--smart-sell` 开启信号衰减检测，`--sell` 仅做T+5兜底

### 信号分级体系

| 层级 | 定位 | 条件 | 仓位 | 回测WR | 回测EV |
|------|------|------|------|--------|--------|
| **T1 大肉** | V9三条件全命中 | bz_kill + weekly_align + MA20_pull | 100% | 85.6% | +10.76% |
| **T2 中肉** | 两条件命中 | 见6种模式 | 50-60% | 74.6% | +6.38% |
| **T3 小肉** | 一强条件命中 | 见3种模式 | 30% | 67.2% | +5.18% |

### 6种交易模式

| 优先级 | 模式名 | N | WR | EV | 触发条件 |
|--------|--------|---|----|----|----------|
| T1 | **V9_full** | 125 | 85.6% | +10.76% | bz<-0.3% + 周线多头 + MA20(-5%~+2%) |
| T2 | **kill+weekly+nearMA20** | 36 | 88.9% | +10.07% | bz<-0.3% + 周线多头 + MA20(-3%~+3%) |
| T2 | **trend_ride+vol** | 20 | 75.0% | +9.23% | 周线slope>5% + MA20回踩 + 放量 |
| T2 | **near_kill+weekly+MA20** | 112 | 74.1% | +7.18% | bz微跌(-0.3%~0%) + 周线多头 + MA20回踩 |
| T2 | **trend_ride+green** | 93 | 72.0% | +6.30% | 周线slope>5% + MA20回踩 + 阳线 |
| T2 | **vol_breakout** | 229 | 72.9% | +6.69% | 放量 + 阳线 + 周线多头 + RSI<70 |
| T2 | **kill+MA20_pull** | 147 | 75.5% | +4.06% | bz<-0.3% + MA20回踩（无周线多头） |
| T2 | **dip_buy** | - | - | - | 连跌3日+ + MA20回踩 + 周线多头 + 下影线 |
| T3 | **kill_only** | 927 | 66.7% | +5.13% | bz<-0.3%（仅要求weekly_slope>0） |
| T3 | **trend_only** | 168 | 73.8% | +6.72% | 周线slope>5% + MA20附近 |
| T3 | **vol_green** | - | - | - | 放量 + 阳线 + MA20附近 |

### 关键参数阈值

```
# 核心阈值（不要轻易修改，回测验证过的）
BZ_KILL = -0.3          # 尾盘杀跌阈值：14:30-15:00区间跌幅%
BZ_MILD_RANGE = [-0.3, 0)  # 微跌区间
MA20_PULL = [-5.0, 2.0]    # MA20回踩区间（V9标准）
MA20_NEAR = [-3.0, 3.0]    # MA20附近（宽松版）
WEEKLY_STRONG_SLOPE = 5.0  # 周线强势斜率阈值%
VOL_EXPAND = [1.3, 2.5]   # 放量区间（量比）
VOL_BREAKOUT_MA20_CAP = 15.0  # vol_breakout模式MA20偏移上限%

# 股票池
POOL = "中证1000成分股"
TOP_N_AMOUNT = 200           # 每日成交额排名前200

# 买入/卖出
BUY_TIME = "14:55市价"       # 14:50决策，14:55执行
HOLD_DAYS = 5                # T+5兜底（灵活止盈：信号衰减随时走人）
SMART_SELL_SIGNALS = [       # 信号衰减触发条件
    "尾盘杀跌→拉高出货",      # bz从负变>+0.5% → 主力出逃
    "放量滞涨",              # 量比>1.3但涨幅<0.5%
    "冲高回落上影线",         # 上影线>1%
    "周线slope转负",          # 趋势终结
    "浮盈>2%+信号弱化",       # 落袋为安
]

# 仓位管理 — 分批建仓，不满仓！
POSITION_BUILD = {
    'T1': {'initial': '70%', 'full': '100%', 'note': 'V9_full共振时满仓首建'},
    'T2': {'initial': '60%', 'full': '100%', 'note': '留40%子弹给T+1'},
    'T3': {'initial': '50%', 'full': '100%', 'note': '留50%子弹给T+1'},
}
# T+0首次建仓 → T+1确认加仓(--add-position) → 满仓目标
```

### 执行计划（每日实战流程）

```
14:30  ── 预热阶段 ──
       运行 scanner_v10.py
       拉取日线 + 周线数据
       预筛"周线多头 + MA20回踩"的股票（条件1+3）

14:50  ── 决策阶段 ──
       检查预筛股票的尾盘5分钟数据
       计算 bz_rt_direction (14:30-14:50区间)
       bz_rt < -0.3% → 确认T1/T2信号
       其他模式 → 按层级表确认

14:53  ── 确认阶段 ──
       选择Top 5候选（按tier升序，slope降序）
       确认 entry_price = 当前价

14:55  ── 执行阶段 ──
       市价买入 或 当前价+1~2tick限价单
       滑点预估：0.1-0.3%
```

### 前视偏差说明

- **回测**用的是14:30-15:00完整尾盘数据（bz_direction）
- **实盘**14:50只能用14:30-14:50的不完整数据（bz_rt_direction）
- scanner_v10.py 同时输出 `bz_direction` 和 `bz_rt_direction`
- 实战以 `bz_rt_direction` 为准，`bz_direction` 仅供参考
- 5分钟做决策**够用**，但必须14:30开始预热

### 运行命令

#### 每日扫描（14:30开始运行）
```bash
cd "%USERPROFILE%\.workbuddy\skills\a-share-analyst"
set PYTHONIOENCODING=utf-8
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" scanner_v10.py
```

#### 全量回测（周末/非交易时间）
```bash
cd "%USERPROFILE%\.workbuddy\skills\a-share-analyst"
set PYTHONIOENCODING=utf-8
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" backtest_t5_v10.py
```

#### 模拟组合操作
```bash
# 分批建仓（建议14:50-14:57运行，首仓不满仓，留子弹给T+1）
cd "%USERPROFILE%\.workbuddy\skills\a-share-analyst"
set PYTHONIOENCODING=utf-8
set MX_APIKEY=your_mx_apikey_here
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_moni_trader.py --buy

# T+1加仓（交易时段内运行，确认信号完好后加仓到满仓目标）
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_moni_trader.py --add-position

# 智能卖出（建议14:45-14:57运行，A股收盘前完成，信号衰减随时走人 + T+5兜底）
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_moni_trader.py --smart-sell

# 仅T+5兜底卖出（交易时段内运行）
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_moni_trader.py --sell

# 查看持仓和战绩
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_moni_trader.py --status

# 生成账户摘要 / NAV历史 / 学习循环报告（建议收盘后 15:01+）
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_moni_trader.py --report
```

#### 看门狗盯盘 v2（近似同步信号 + 多信号共振）
```bash
# 全量巡检（任意时点运行）
cd "%USERPROFILE%\.workbuddy\skills\a-share-analyst"
set PYTHONIOENCODING=utf-8
set MX_APIKEY=your_mx_apikey_here
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_watchdog.py

# 仅板块资金扫描
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_watchdog.py --sector

# 仅持仓盯盘
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_watchdog.py --holding

# 指定股票资金流分析（含TDX真实资金流+日内形态）
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_watchdog.py --flow 000690

# 仅输出触发告警
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_watchdog.py --alert

# 多信号共振评分（核心！confluence_score>=60才下重注）
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_watchdog.py --confluence 000690

# 日内形态检测（V形反转/双底/恐慌末端/尾盘拉升）
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_watchdog.py --pattern 000690

# 盘前预热扫描（10:30/13:30运行，提前发现setup股票）
"%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe" v10_watchdog.py --prescan
```

核心能力（v2增强）：
- **板块资金流向**：全行业板块flow_score排名(涨幅×log(成交额))，发现资金正在流入的板块
- **个股分时净流入**：5分钟K线量价推算资金流方向，收阳=流入收阴=流出
- **TDX真实资金流**：直连TQLEX API，获取主力/超大单/大单净流入(日频)，3日趋势+连续流入天数 — 远胜K线推算
- **加速度检测**：最近3根K线净流入 vs 前3根，发现资金加速流入/流出的转折点
- **日内形态检测**：V形反转/双底/恐慌末端/尾盘拉升 — 最强的近似同步信号
- **多信号共振评分**：板块+资金流+分时+形态+V10tier → confluence_score(0-100)，>=60才下重注
- **盘前预热扫描**：提前识别资金正在流入但价格还没大幅上涨的"setup"股票
- **触发告警**：持仓异动、板块异动、候选股资金加速流入自动检测
- **定时巡检**：10:30盘中+13:30午后自动运行

#### 输出文件
- `~/.workbuddy/a-share-analyst/v10_scan_full.csv` — 当日扫描完整结果
- `~/.workbuddy/a-share-analyst/v10_data_raw.csv` — 回测原始数据
- `~/.workbuddy/a-share-analyst/v10_summary.json` — 回测汇总

### 进化方向

1. **扩大5分钟数据窗口** — 当前仅约34个交易日，扩大到60天信号量翻倍
2. **优化MA20区间** — kill+weekly+nearMA20 WR=88.9%说明-5%~+2%可微调
3. **加入市场状态判断** — 强势日用trend_ride/vol_breakout，调整日用V9/near_kill
4. **多模式组合优化** — 不同市场环境自动切换主导模式
5. **组合回测** — 每日选Top 5，跑完整的200天模拟
6. **手续费模型** — 当前EV未扣滑点/佣金，需加入真实成本
7. ~~近似同步信号增强~~ — ✅ v2已实现：TDX真实资金流+多信号共振+日内形态+盘前预热
8. ~~TDX MCP资金流向~~ — ✅ 已接入TQLEX直连，主力/超大单/大单净流入(日频)
9. **Confluence回测验证** — 用历史数据验证confluence_score>=60的胜率是否>80%
10. **板块-个股联动** — 板块资金流入时，自动找该板块内中证1000成分股的共振信号

---

## 深度分析模式：四引擎联动

当用户要求"分析XX股票"时，按以下流程执行：

### Step 1: 基本面 (mx-data)
```bash
export MX_APIKEY=$MX_APIKEY
PYTHONIOENCODING=utf-8 python ~/.workbuddy/skills/mx-data/scripts/mx_data.py "XX公司 最新价 涨跌幅 市盈率 市净率" [output_dir]
PYTHONIOENCODING=utf-8 python ~/.workbuddy/skills/mx-data/scripts/mx_data.py "XX公司 近三年净利润 营业收入 毛利率 净资产收益率" [output_dir]
PYTHONIOENCODING=utf-8 python ~/.workbuddy/skills/mx-data/scripts/mx_data.py "XX公司 主力资金流向 十大股东" [output_dir]
```

### Step 2: 技术面 (pytdx)
```python
from pytdx.hq import TdxHq_API
api = TdxHq_API()
api.connect('60.191.117.167', 7709)
# 日K 250根
bars = api.get_security_bars(9, market, code, 0, 250)
# 5分钟K
bars5 = api.get_security_bars(0, market, code, 0, 80)
# 复权信息
xdxr = api.get_xdxr_info(market, code)
api.disconnect()
```
market: 1=沪, 0=深。code: 纯数字如 '600519'

### Step 3: 资讯面 (mx-search)
```bash
PYTHONIOENCODING=utf-8 python ~/.workbuddy/skills/mx-search/scripts/mx_search.py "XX公司最新研报" [output_dir]
PYTHONIOENCODING=utf-8 python ~/.workbuddy/skills/mx-search/scripts/mx_search.py "XX公司最新公告" [output_dir]
```
mx-search 返回研报/新闻/公告，包含：机构名称、评级、日期、详细内容。
重点关注：机构一致预期变化、重大事件（定增/减持/诉讼等）、政策影响。

### Step 4: 横向对比 (mx-xuangu)
```bash
PYTHONIOENCODING=utf-8 python ~/.workbuddy/skills/mx-xuangu/scripts/mx_xuangu.py "同行业市盈率小于XX的股票" --output-dir [output_dir]
```

### Step 5: 综合研判
基于四个引擎的数据，从以下维度输出分析报告：
- 估值水平（PE/PB/行业对比）
- 成长性（营收/利润增速）
- 资金面（主力流向/换手率）
- 技术面（K线趋势/支撑压力位/复权价格）
- 资讯面（机构评级/重大事件/政策风向/行业动态）
- V10信号（如果是中证1000成分股，检查是否有T1/T2/T3信号）
- 风险提示

## 资讯搜索专项用法 (mx-search)

mx-search 是唯一能获取实时金融资讯的引擎，独有场景：

| 场景 | 示例 |
|------|------|
| 个股研报 | "贵州茅台最新研报" → 机构评级+目标价+核心观点 |
| 个股公告 | "比亚迪最新公告" → 分红/定增/减持等公司事件 |
| 行业新闻 | "人工智能板块近期新闻" → 政策+行业动态 |
| 宏观政策 | "美联储加息对A股影响" → 宏观分析 |
| 交易规则 | "科创板交易涨跌幅限制" → 规则查询 |
| 事件解读 | "今日大盘异动原因" → 市场异动归因 |

## 运行环境

- Python: `%USERPROFILE%\.workbuddy\binaries\python\envs\mep\Scripts\python.exe` (3.13, 有numpy/pandas/pytdx)
- 依赖: pandas, numpy, pytdx, openpyxl
- mx-data/mx-xuangu/mx-search 依赖: cryptography, requests
- MX_APIKEY: `your_mx_apikey_here` (User级环境变量，占位示例)
- pytdx server: 60.191.117.167:7709 (备选: 39.105.251.234:7709, 119.147.212.83:7709)
- Windows 注意: 所有命令加 PYTHONIOENCODING=utf-8，输出目录不可用默认 /root/.openclaw/
- 中证1000成分股文件: `%USERPROFILE%\.workbuddy\skills\csi1000-skills\000852cons.xls`
- 输出目录: ~/.workbuddy/a-share-analyst/
- mx-moni模拟账户: ID=260784100000134569, 初始100万
- TDX MCP connector: tdx_quotes/tdx_kline/tdx_screener/tdx_indicator_select/tdx_lookup_stock/tdx_api_data

## 策略迭代历史

| 版本 | 方法 | 胜率 | 核心改进 |
|------|------|------|----------|
| V1 | 基础MA20 | 46.5% | 起步 |
| V4b | 多因子 | 60.3% | 加入周线特征 |
| V7 | 反向工程 | 55.4% | 40+特征筛选 |
| V8 | 5分钟数据 | 58.4% | 发现尾盘杀跌效应 |
| V9 | 三条件组合 | 85.6% | bz_kill+weekly+MA20 |
| **V10** | **多层级多模式** | **67-89%** | **6模式+3层级，信号14.8x** |

## 注意事项

- mx-data/mx-xuangu/mx-search 有每日调用次数限制，避免无意义重复查询
- pytdx 连接是 TCP 长连接，用完必须 disconnect
- mx-data 查大数据范围(如3年每日)可能导致上下文爆炸，建议分批或限制时间范围
- mootdx 指数K线有 bug（返回股票数据），指数行情统一用 mx-data 或 pytdx
- mx-search 返回的研报内容较长，深度分析时建议只提取评级/目标价/核心观点
- V10策略EV未扣滑点和佣金，实战需扣0.2-0.3%滑点+0.1%佣金
- 信号稀缺日（大盘走强时T1=0）是正常的，T2/T3模式覆盖了这种情况
- 本技能仅供分析参考，不构成投资建议
