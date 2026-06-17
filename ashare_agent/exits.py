"""智能动态出场规则(实盘/日线结算/回测共享,DRY)。

设计参考 freqtrade 的 custom_stoploss/custom_exit 与高星趋势策略的
"亏损+时间"级联止损思想:让盈利的单子靠移动止盈/趋势奔跑,亏损或走坏的单子尽快离场,
不再机械地固定持有 N 天到期。

优先级(自上而下):
1. 硬止损       —— 跌破 entry×(1+stop_loss),本金保护(盘中即时)
2. 移动止盈     —— 浮盈达到 trail_activate 后,自最高点回撤 trail_pct 离场(盘中即时)
3. 趋势走坏     —— 收盘跌破 MA(trend_ma)(仅收盘判定)
4. 级联止损     —— 持有≥cut1_days 且浮亏≥cut1_loss;或 持有≥cut2_days 且仍≈持平(死钱)
5. 最长持仓     —— 持有≥max_hold(防僵尸仓)
"""
from __future__ import annotations

REASON_ZH = {
    "stop": "硬止损", "trail": "移动止盈", "trend": "跌破均线",
    "losscut": "级联止损", "deadmoney": "死钱换仓", "maxhold": "最长持仓",
}


def evaluate_exit(state: dict, bar: dict, is_eod: bool, cfg: dict) -> tuple[str | None, float]:
    """返回 (出场原因, 出场价);无出场返回 (None, 参考价)。

    state: {entry_price, high_water, days_held}
    bar:   {high, low, close, ma}(ma 为趋势均线,可为 None;盘中可令 high=low=close=现价)
    is_eod: True 收盘结算(可触发趋势/级联/最长持仓);False 盘中(仅硬止损+移动止盈)
    """
    e = cfg["exit"]
    entry = state["entry_price"]
    hw = max(state["high_water"], bar["high"])

    # 1. 硬止损
    stop_price = entry * (1 + float(cfg["run"]["stop_loss"]))
    if bar["low"] <= stop_price:
        return "stop", stop_price

    # 2. 移动止盈
    if hw / entry - 1 >= float(e["trail_activate"]):
        trail_stop = hw * (1 - float(e["trail_pct"]))
        if bar["low"] <= trail_stop:
            return "trail", trail_stop

    if is_eod:
        close = bar["close"]
        profit = close / entry - 1
        d = int(state["days_held"])
        # 3. 趋势走坏(收盘跌破均线)
        ma = bar.get("ma")
        if ma and close < ma:
            return "trend", close
        # 4. 级联止损(亏损+时间)
        if d >= int(e["cut1_days"]) and profit <= float(e["cut1_loss"]):
            return "losscut", close
        if d >= int(e["cut2_days"]) and profit <= float(e["cut2_flat"]):
            return "deadmoney", close
        # 5. 最长持仓
        if d >= int(e["max_hold"]):
            return "maxhold", close

    return None, bar["close"]
