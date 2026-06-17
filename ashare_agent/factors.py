"""因子计算:全部由历史K线点位安全(point-in-time)地向量化计算。

`compute_factor_frame` 一次性算出整段K线每个交易日的因子值,
实盘取最后一行,回测取对应日期那一行 —— 同一套逻辑,保证可复现(DRY)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FACTOR_NAMES = ["ma", "macd", "rsi", "kdj", "boll", "momentum", "volume",
                "pullback", "mainflow", "serenity"]


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1 / n, adjust=False).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute_factor_frame(df: pd.DataFrame, fund_flow: pd.DataFrame | None = None,
                         mainflow_days: int = 5, choke: float = 0.0) -> pd.DataFrame:
    """输入标准化K线(date/open/close/high/low/volume,升序),
    返回含 date + 各因子列的 DataFrame(数据不足处为 NaN)。

    fund_flow:个股资金流向(data.get_fund_flow),用于计算 mainflow 因子
    (近 mainflow_days 日主力净流入占比均值);缺失时记 0(中性,不剔除该股)。
    choke:卡脖子赛道成员身份(0/1),作为 serenity 因子(时间不变,权重由优化器学习)。"""
    if df is None or df.empty or len(df) < 60:
        return pd.DataFrame(columns=["date"] + FACTOR_NAMES)

    d = df.copy()
    close, high, low, vol = d["close"], d["high"], d["low"], d["volume"]

    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()

    # MACD
    dif = _ema(close, 12) - _ema(close, 26)
    dea = _ema(dif, 9)

    # RSI
    rsi = _rsi(close, 14)

    # KDJ
    low9 = low.rolling(9).min()
    high9 = high.rolling(9).max()
    rsv = (close - low9) / (high9 - low9).replace(0, np.nan) * 100
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    dd = k.ewm(alpha=1 / 3, adjust=False).mean()

    # BOLL
    mid = ma20
    std = close.rolling(20).std()
    upper = mid + 2 * std
    lower = mid - 2 * std
    pctb = (close - lower) / (upper - lower).replace(0, np.nan)

    out = pd.DataFrame({"date": d["date"]})
    out["ma"] = (
        (close > ma5).astype(int)
        + (ma5 > ma10).astype(int)
        + (ma10 > ma20).astype(int)
        + (close > ma20).astype(int)
    ) / 4.0
    out["macd"] = (dif - dea) / close
    out["rsi"] = -(rsi - 55).abs()
    out["kdj"] = k - dd
    out["boll"] = -(pctb - 0.75).abs()
    out["momentum"] = close / close.shift(20) - 1
    out["volume"] = vol.rolling(5).mean() / vol.rolling(20).mean().replace(0, np.nan)
    out["pullback"] = -((close / ma5) - 1).abs()

    # 主力资金:近 mainflow_days 日主力净流入占比均值(点位安全,按日期对齐)
    if fund_flow is not None and not fund_flow.empty:
        s = fund_flow.set_index("date")["main_ratio"].sort_index()
        roll = s.rolling(mainflow_days, min_periods=1).mean()
        out["mainflow"] = out["date"].map(roll).astype(float).fillna(0.0)
    else:
        out["mainflow"] = 0.0

    # 卡脖子赛道成员身份(0/1,时间不变);权重由 walk-forward 优化器学习
    out["serenity"] = float(choke)
    return out
