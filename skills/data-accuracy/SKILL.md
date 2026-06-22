---
name: data-accuracy
description: 交易数据准确性纪律。任何涉及现价、涨跌幅、资金流、因子的决策前必须载入;防止单位误读与双源价格不一致导致错误交易。
---

# 交易数据准确性纪律(必读)

本系统是**严谨模拟交易系统**。数据误读会直接导致错误挂单、错误止盈止损。每次**开盘复盘**与**自主交易**阶段,在调用写操作工具(place_limit_order / add_to_position / close_position / reduce_position)之前,**必须先 load_skill data-accuracy** 并遵守下列规则。

## 1. 现价:唯一可信字段

| 场景 | 正确字段 | 错误用法 |
|------|----------|----------|
| 决策/口头总结中的「现价」 | `current_price` | 日K `last_daily_close`、`recent_closes[-1]`、spot 缓存价 |
| 候选列表 | `close` 或 `current_price`(已刷新为实时) | 选股预筛时的陈旧 spot |
| 持仓浮盈 | `get_portfolio_state.positions[].current_price` | 成本价、推荐日 entry_close |
| 限价校验/成交 | 与 `current_price` 同源(新浪 hq.sinajs.cn) | 凌晨拉取后不更新的 spot |

**规则:**
- 对外表述价格时,必须引用工具返回的 `current_price`,并注明 `price_source`(sina_realtime 优先)。
- `last_daily_close` + `last_daily_date` 仅表示**最近一根日K收盘**,盘中可能滞后;**不得**当作现价。
- 若 `current_price` 与 `last_daily_close` 差异较大,以 `current_price` 为准。

## 2. 主力资金:严禁「亿」字误读

| 字段 | 单位 | 含义 |
|------|------|------|
| `mainflow_ratio_nd_avg_pct` / `factors.mainflow` | **百分点(%)** | 近 N 日「主力净流入占比」的算术平均 |
| `fund_flow_recent[].main_net_yi` | **亿元** | 当日主力净流入金额 |
| `fund_flow_recent[].main_ratio_pct` | **百分点(%)** | 当日主力净流入占成交额比例 |

**典型错误(禁止):**
- 把 `mainflow=9.14` 说成「主力净流入 +9.14 **亿**」→ 正确:近5日主力净流入占比均值约 **9.14%**。
- 把 `score` 当成涨跌幅 → score 是无量纲 z-score 综合分。

**正确表述示例:**
- 「近5日主力净流入占比均值 +9.1%,资金面偏强」
- 「昨日主力净流入 1.2 亿(main_net_yi),占比 8.5%(main_ratio_pct)」

## 3. 其他字段单位

- `pct` / `pct_vs_prev_close`: 相对**昨收**的涨跌幅,单位 **%**。
- `amount_yi`: 成交额,单位 **亿元**。
- `turnover`: 换手率,单位 **%**。
- `float_return_pct`: 相对**买入成本**的浮盈,单位 **%**。

工具返回的 `field_legend` 是权威说明;不确定时先读 legend,**禁止臆造单位**。

## 4. 数据源与局限

- **个股现价**: `get_live_quotes` → 新浪实时;失败时 `spot_fallback`(标注在 price_source)。
- **全市场广度**: `get_market_overview` 为新浪**实时**涨跌家数(盘中刷新,看 `as_of`);勿用旧记忆里的涨跌家数。
- **日K**: 可能不含「当日」未收盘 bar;均线/ret_5d 基于日K,与盘中现价可不一致。

## 5. 决策自检清单(写操作前)

1. 已 load 本 skill。
2. 现价来自 `current_price`,与同花顺/工具 tick 一致。
3. 资金流表述区分 **占比(%)** 与 **金额(亿)**。
4. 限价在现价 × [0.8, 1.1] 内,且 ≤ 现价(买入)。
5. 口头总结中的数字与工具 JSON **逐字段对应**,无四舍五入到错误量级。
6. **止盈/止损/建仓**已同时参考大盘(`market_avg_pct`/`avg_pct`)与板块(`board_pct`,`board_vs_market_pct`,`decision_hint`),未仅凭大盘判断。

违反以上任一条,宁可**不下单**,先重新调用 get_stock_detail / get_candidates 核对。

> 本框架仅用于模拟盘研究,不构成投资建议。
