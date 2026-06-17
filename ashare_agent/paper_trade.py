"""模拟交易逻辑:待建仓 -> 建仓(开盘/现价) -> 智能动态出场平仓。

日线结算(收盘后)与盘中实时,共用 exits.evaluate_exit 出场规则(DRY)。
交易规则:T+1(当日买入次日才可卖)、100 股整数倍、按权益比例分配仓位。
建仓会返回每笔的跳过原因(diagnostics),便于排查"为什么没建仓"。
"""
from __future__ import annotations

import datetime as _dt
import math

import pandas as pd

from . import data, exits, portfolio


def _age_days(rec_date: str, trade_date: str) -> int:
    try:
        a = _dt.date.fromisoformat(rec_date)
        b = _dt.date.fromisoformat(trade_date)
        return (b - a).days
    except ValueError:
        return 0


def enter_pending(cfg, entry_px: dict[str, float], trade_date: str,
                  same_day: bool = False) -> tuple[list[dict], list[dict]]:
    """对到期的待建仓单建仓,返回 (已建仓, 跳过原因)。

    same_day=False(日线批处理):次日建仓,仅 rec_date < trade_date 生效。
    same_day=True (盘中实时):当日即按实时价建仓,rec_date <= trade_date 生效
                  (T+1 仅约束卖出:当日买入次日才可卖,见 check_exits)。
    本金保护:若账户当前回撤已达 risk.max_drawdown,熔断暂停建仓并撤销待建仓单。
    高价股:预算不足1手时,若 allow_one_lot 且现金/单仓上限允许,至少买入1手。
    """
    a = cfg["account"]
    lot = int(a["lot_size"])
    allow_one_lot = bool(a.get("allow_one_lot", True))
    max_frac = float(a.get("max_position_fraction", a["position_fraction"]))
    entered: list[dict] = []
    skipped: list[dict] = []

    def skip(p, reason):
        skipped.append({"symbol": p["symbol"], "name": p["name"], "reason": reason})

    def eligible(p) -> bool:
        return p["rec_date"] <= trade_date if same_day else p["rec_date"] < trade_date

    limit = float(cfg.get("risk", {}).get("max_drawdown", 1.0))
    if portfolio.current_drawdown(entry_px) >= limit:
        for p in portfolio.get_positions("pending"):
            if eligible(p):
                skip(p, f"回撤熔断(≥{limit:.0%}),暂停建仓并撤单")
                portfolio.cancel_pending(p["id"])
        return entered, skipped

    ttl = int(cfg.get("live", {}).get("pending_ttl_days", 99))
    for p in portfolio.get_positions("pending"):
        if not eligible(p):                    # 未来日期(批处理下含当日)暂不建仓
            if not same_day:
                skip(p, f"T+1:推荐日{p['rec_date']}≥交易日{trade_date},次日才建仓")
            continue
        # 限价单超期未成交 → 撤单(仅实时限价模式)
        if same_day and _age_days(p["rec_date"], trade_date) > ttl:
            skip(p, f"限价单超期未成交({ttl}日),撤单")
            portfolio.cancel_pending(p["id"])
            continue
        price = entry_px.get(p["symbol"])
        if not price or price <= 0:
            skip(p, "未取到行情/价格无效")
            continue
        # 限价买入:现价未回调到目标买入价 → 继续等待(不撤单)
        limit = p.get("limit_price")
        if same_day and limit and price > float(limit):
            skip(p, f"等待回调:现价 {price:.2f} > 目标买入价 {float(limit):.2f}")
            continue
        if len(portfolio.get_positions("open")) >= int(a["max_positions"]):
            skip(p, f"持仓已满({a['max_positions']}只),撤单")
            portfolio.cancel_pending(p["id"])
            continue

        cash = portfolio.get_account()["cash"]
        eq = portfolio.equity(entry_px)
        budget = min(cash * 0.999, eq * float(a["position_fraction"]))
        shares = int(math.floor(budget / (price * lot)) * lot)
        if shares < lot:
            one_lot_val = price * lot
            cap = eq * max_frac
            if allow_one_lot and one_lot_val <= cap and one_lot_val * 1.001 <= cash * 0.999:
                shares = lot                      # 高价股至少买1手
            else:
                why = (f"1手需≈{one_lot_val:,.0f}元 > 单仓预算{budget:,.0f}/上限{cap:,.0f}"
                       if one_lot_val > cap else f"1手需≈{one_lot_val:,.0f}元,现金不足")
                skip(p, why)
                portfolio.cancel_pending(p["id"])
                continue

        gross = shares * price
        commission = max(gross * a["commission_rate"], a["min_commission"])
        buy_cost = gross + commission
        if buy_cost > cash:
            skip(p, f"成本{buy_cost:,.0f}元 > 现金{cash:,.0f}元")
            portfolio.cancel_pending(p["id"])
            continue

        target_price = price * (1 + float(p["target_pct"]))
        stop_price = price * (1 + float(p["stop_pct"]))
        portfolio.open_position(p["id"], shares, price, trade_date,
                               target_price, stop_price, buy_cost)
        entered.append({"symbol": p["symbol"], "name": p["name"], "shares": shares,
                        "price": round(price, 3), "cost": round(buy_cost, 2)})
    return entered, skipped


def check_exits(cfg, bars: dict[str, dict], trade_date: str, is_eod: bool) -> list[dict]:
    """检查持仓的智能出场。bars[symbol] = {high, low, close, ma}。

    盘中(is_eod=False):仅硬止损 + 移动止盈(快速保护);并刷新最高价。
    收盘(is_eod=True):追加趋势离场/级联止损/最长持仓,并累加持有天数。
    """
    closed = []
    for p in portfolio.get_positions("open"):
        if p["entry_date"] >= trade_date:      # T+1:当日买入不可卖
            continue
        q = bars.get(p["symbol"])
        if not q:
            continue
        portfolio.update_high_water(p["id"], q["high"])
        hw = max(float(p.get("high_water") or p["entry_price"]), q["high"])
        state = {"entry_price": float(p["entry_price"]), "high_water": hw,
                 "days_held": int(p["days_held"]) + (1 if is_eod else 0)}
        reason, price = exits.evaluate_exit(state, q, is_eod, cfg)
        if reason:
            closed.append(portfolio.close_position(p, price, trade_date, reason, cfg))
        elif is_eod:
            portfolio.bump_days_held(p["id"])
    return closed


# ---- 行情构造(含趋势均线 ma) ----

def _ma_upto(h: pd.DataFrame, n: int) -> float | None:
    return float(h["close"].tail(n).mean()) if len(h) >= n else None


def build_live_quotes(symbols: list[str], cfg) -> dict[str, dict]:
    """从实时报价构造行情 + 趋势均线(盘中用;ma 取最近 trend_ma 日收盘)。"""
    syms = list(set(symbols))
    rt = data.get_realtime(syms)
    n = int(cfg["exit"]["trend_ma"])
    out = {}
    for s, q in rt.items():
        h = data.get_hist(s, cfg["hist"]["lookback_days"], cfg["hist"]["adjust"])
        out[s] = {"open": q["open"], "high": q["high"], "low": q["low"],
                  "close": q["price"], "price": q["price"],
                  "ma": _ma_upto(h, n) if not h.empty else None}
    return out
