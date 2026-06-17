"""选股池构建与A股风控过滤(基于新浪全市场快照)。

剔除:北交所、ST/退市、次新首日、停牌/无效价、流动性不足(成交额)、
当日接近涨跌停(难以买入)的标的。
注:新浪快照不含市值/PE/PB,故市值与估值过滤在此版本不启用(可后续接入)。
"""
from __future__ import annotations

import pandas as pd

from . import data


def _board_limit(symbol: str, name: str) -> float:
    if "ST" in str(name):
        return 5.0
    if symbol.startswith(("688", "300", "301")):
        return 20.0
    return 10.0


def build_universe(cfg: dict) -> pd.DataFrame:
    """返回经风控过滤后的候选池(列:symbol/name/close/pct/amount[亿元])。"""
    u = cfg["universe"]
    spot = data.get_spot()
    df = spot[["symbol", "exchange", "name", "close", "pct", "amount"]].copy()

    df["amount"] = df["amount"] / 1e8        # 元 -> 亿元

    df = df[df["close"].notna() & (df["close"] > 0)]
    df = df[df["exchange"] != "bj"]          # 排除北交所
    if u.get("exclude_st", True):
        df = df[~df["name"].str.contains("ST|退", regex=True, na=False)]
    df = df[~df["name"].str.startswith(("N", "C"))]
    df = df[df["amount"] >= u["min_amount"]]  # 流动性

    limit = df.apply(lambda r: _board_limit(r["symbol"], r["name"]), axis=1)
    buf = float(u.get("limit_buffer", 9.5))
    near = limit - (10.0 - buf)               # 各板按比例缩放的"接近涨停"阈值
    df = df[df["pct"].abs() < near]

    return df.reset_index(drop=True)
