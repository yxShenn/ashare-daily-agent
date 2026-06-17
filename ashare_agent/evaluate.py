"""回测复盘:对持有期已满的历史推荐判定对错,写入评估台账。

入场价取推荐日的次一交易日开盘价;成功定义为持有期内最高涨幅 >= target。
"""
from __future__ import annotations

import pandas as pd

from . import data, store


def evaluate_open(cfg: dict) -> list[dict]:
    """评估所有持有期已满的 open 推荐,返回本次完成评估的结果列表。"""
    done = []
    for rec in store.get_open_recommendations():
        symbol = rec["symbol"]
        hold = int(rec["holding_days"])
        target = float(rec["target"])
        rec_ts = pd.Timestamp(rec["rec_date"])

        hist = data.get_hist(symbol, cfg["hist"]["lookback_days"], cfg["hist"]["adjust"])
        if hist.empty:
            continue
        future = hist[hist["date"] > rec_ts].reset_index(drop=True)
        if len(future) < hold:        # 持有期未满,暂不评估
            continue

        window = future.iloc[:hold]
        entry = float(window.iloc[0]["open"])
        if entry <= 0:
            continue
        max_high = float(window["high"].max())
        end_close = float(window.iloc[-1]["close"])
        max_gain = max_high / entry - 1
        ret = end_close / entry - 1
        ev = {
            "entry_price": round(entry, 3),
            "max_high": round(max_high, 3),
            "max_gain": round(max_gain, 4),
            "end_close": round(end_close, 3),
            "ret": round(ret, 4),
            "success": max_gain >= target,
            "n_days": int(hold),
        }
        store.save_evaluation(rec["id"], ev)
        done.append({"symbol": symbol, "rec_date": rec["rec_date"], **ev})
    return done
