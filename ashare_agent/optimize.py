"""策略自优化:严格 walk-forward 随机搜索因子权重,支持两种优化目标。

目标(config: optimize.objective):
- winrate:最大化每日 Top1 的命中率(持有期内达到 success_threshold)。
- profit :在最大回撤约束(risk.max_drawdown)下最大化累计收益。
          按出场规则(止盈/止损/到期)模拟每笔实际收益,串成单笔轮动的权益曲线。

严格之处:回测窗口按时间切成训练集(较早)+ 验证集(最近、样本外);
只在训练集挑参,用验证集估计样本外表现,仅当样本外不劣于基线时才更新。
"""
from __future__ import annotations

import datetime as _dt

import numpy as np
import pandas as pd

from . import config, data, exits, serenity
from .factors import FACTOR_NAMES, compute_factor_frame

_MIN_CROSS = 10        # 单日参与横截面的最少股票数
_DD_PENALTY = 3.0      # profit 目标下超额回撤的惩罚系数


def _sample_symbols(cfg: dict) -> list[str]:
    df = data.get_spot().copy()
    df = df[df["exchange"] != "bj"]
    df = df[~df["name"].astype(str).str.contains("ST|退", regex=True, na=False)]
    df = df.dropna(subset=["amount"]).sort_values("amount", ascending=False)
    return df["symbol"].head(int(cfg["optimize"]["sample_size"])).tolist()


def _trade_return(i: int, opens, highs, lows, closes, ma, cfg: dict) -> float:
    """用与实盘一致的智能出场(exits.evaluate_exit)模拟单笔收益率。

    entry 取 i+1 开盘;T+1 起从 i+2 起逐日按日线 bar 判定出场(is_eod=True)。
    """
    n = len(closes)
    entry = opens[i + 1]
    end = min(i + 1 + int(cfg["exit"]["max_hold"]), n - 1)
    hw = highs[i + 1]
    for d in range(i + 2, end + 1):
        state = {"entry_price": entry, "high_water": hw, "days_held": d - (i + 1)}
        bar = {"high": highs[d], "low": lows[d], "close": closes[d],
               "ma": (None if np.isnan(ma[d]) else ma[d])}
        reason, price = exits.evaluate_exit(state, bar, True, cfg)
        if reason:
            return price / entry - 1
        hw = max(hw, highs[d])
    return closes[end] / entry - 1


def _build_panels(cfg: dict) -> list[tuple]:
    """返回按日期升序的 [(date, Z, success, ret)]:Z 为横截面标准化因子矩阵,
    success 为信号是否命中(holding_days 内达 success_threshold),
    ret 为按智能出场模拟的实际收益(浮点向量)。"""
    hold = int(cfg["run"]["holding_days"])
    target = float(cfg["run"]["success_threshold"])
    trend_ma = int(cfg["exit"]["trend_ma"])
    mf_days = int(cfg["run"].get("mainflow_days", 5))
    name_lut = data.get_spot().set_index("symbol")["name"].to_dict()
    by_date: dict[np.datetime64, list] = {}

    for sym in _sample_symbols(cfg):
        hist = data.get_hist(sym, cfg["hist"]["lookback_days"], cfg["hist"]["adjust"])
        if hist.empty or len(hist) < 80:
            continue
        choke, _ = serenity.membership(sym, name_lut.get(sym), None, cfg)
        ff = compute_factor_frame(hist, data.get_fund_flow(sym), mf_days, choke)
        if ff.empty:
            continue
        fvals = ff[FACTOR_NAMES].values
        opens = hist["open"].values
        highs = hist["high"].values
        lows = hist["low"].values
        closes = hist["close"].values
        ma = pd.Series(closes).rolling(trend_ma).mean().values
        dates = hist["date"].values
        n = len(hist)
        for i in range(n - hold - 1):
            row = fvals[i]
            if np.isnan(row).any():
                continue
            entry = opens[i + 1]
            if entry <= 0:
                continue
            win = slice(i + 1, i + 1 + hold)
            max_gain = highs[win].max() / entry - 1
            ret = _trade_return(i, opens, highs, lows, closes, ma, cfg)
            by_date.setdefault(dates[i], []).append((row, max_gain >= target, ret))

    sel_dates = sorted(by_date.keys())[-int(cfg["optimize"]["window_days"]):]
    panels = []
    for d in sel_dates:
        items = by_date[d]
        if len(items) < _MIN_CROSS:
            continue
        F = np.array([it[0] for it in items], dtype=float)
        S = np.array([it[1] for it in items], dtype=bool)
        R = np.array([it[2] for it in items], dtype=float)
        sd = F.std(0)
        sd[sd == 0] = 1.0
        panels.append((d, (F - F.mean(0)) / sd, S, R))
    return panels


def _eval(panels: list[tuple], w: np.ndarray, cfg: dict) -> tuple[float, dict]:
    """按当前目标评估一组权重,返回 (优化得分, 指标字典)。"""
    if not panels:
        return -1e9, {}
    obj = cfg["optimize"].get("objective", "winrate")
    if obj == "profit":
        hold = int(cfg["run"]["holding_days"])
        limit = float(cfg["risk"]["max_drawdown"])
        equity, peak, maxdd, i, nt, wins = 1.0, 1.0, 0.0, 0, 0, 0
        while i < len(panels):
            _, Z, _S, R = panels[i]
            r = float(R[int((Z @ w).argmax())])
            equity *= (1 + r)
            peak = max(peak, equity)
            maxdd = max(maxdd, (peak - equity) / peak)
            wins += int(r > 0)
            nt += 1
            i += hold                       # 单笔轮动,不重叠
        total = equity - 1
        score = total - _DD_PENALTY * max(0.0, maxdd - limit)
        return score, {"total_return": round(total, 4), "max_drawdown": round(maxdd, 4),
                       "winrate": round(wins / nt, 4) if nt else 0.0, "trades": nt}
    # winrate
    wins = trades = 0
    for _, Z, S, _R in panels:
        wins += int(S[int((Z @ w).argmax())])
        trades += 1
    wr = wins / trades if trades else 0.0
    return wr, {"winrate": round(wr, 4), "trades": trades}


def optimize(cfg: dict) -> dict:
    """严格 walk-forward 优化;仅当样本外不劣于基线时更新 active_config。"""
    if not cfg["optimize"].get("enabled", True):
        return {"updated": False, "msg": "优化已禁用"}

    obj = cfg["optimize"].get("objective", "winrate")
    full = _build_panels(cfg)
    min_trades = int(cfg["optimize"]["min_trades"])
    if len(full) < min_trades:
        return {"updated": False, "objective": obj,
                "msg": f"有效交易日 {len(full)} < {min_trades},跳过优化"}

    val_frac = float(cfg.get("walk_forward", {}).get("val_fraction", 0.3))
    cut = int(len(full) * (1 - val_frac))
    train, val = full[:cut], full[cut:]
    if not train or not val:
        return {"updated": False, "objective": obj, "msg": "训练/验证集为空,跳过优化"}

    baseline = np.array([float(cfg["factors"][c]) for c in FACTOR_NAMES])
    seed = int(_dt.date.today().strftime("%Y%m%d"))
    rng = np.random.default_rng(seed)

    best_w, best_train = baseline.copy(), _eval(train, baseline, cfg)[0]
    for _ in range(int(cfg["optimize"]["n_trials"])):
        w = rng.uniform(0.0, 1.5, size=len(FACTOR_NAMES))
        sc, _m = _eval(train, w, cfg)
        if sc > best_train:
            best_train, best_w = sc, w

    cand_val_score, cand_val = _eval(val, best_w, cfg)
    base_val_score, base_val = _eval(val, baseline, cfg)
    if cand_val_score >= base_val_score:
        chosen_w, val_m = best_w, cand_val
        improved = not np.allclose(chosen_w, baseline)
    else:
        chosen_w, val_m = baseline, base_val
        improved = False

    _, train_m = _eval(train, chosen_w, cfg)
    weights = {c: round(float(v), 4) for c, v in zip(FACTOR_NAMES, chosen_w)}
    meta = {"objective": obj, "val": val_m, "train": train_m, "n_val": len(val)}
    config.save_active_config(weights, meta)
    return {"updated": improved, "objective": obj, "weights": weights,
            "train": train_m, "val": val_m,
            "train_days": len(train), "val_days": len(val)}
