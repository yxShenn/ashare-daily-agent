"""每日选股:风控池 -> 成交额活跃度预筛 -> 技术多因子打分 -> Top1 推荐入库。"""
from __future__ import annotations

import datetime as _dt
import json

import pandas as pd

from . import data, portfolio, serenity, store, universe
from .factors import FACTOR_NAMES, compute_factor_frame
from .strategy import score_cross_section


def affordable_price_ceiling(cfg: dict) -> float:
    """按当前资金状态,能买入至少 1 手的最高单价(元/股)。

    允许买1手时以单仓上限 max_position_fraction 折算,否则以目标仓位 position_fraction。
    """
    a = cfg["account"]
    eq = portfolio.equity({})
    frac = (float(a.get("max_position_fraction", a["position_fraction"]))
            if a.get("allow_one_lot", True) else float(a["position_fraction"]))
    return eq * frac / int(a["lot_size"])


def run_recommend(cfg: dict, rec_date: str | None = None) -> dict | None:
    """执行选股,写入台账并返回推荐详情。无可选标的返回 None。"""
    weights = cfg["factors"]
    rec_date = rec_date or _dt.date.today().strftime("%Y-%m-%d")

    pool = universe.build_universe(cfg)
    if pool.empty:
        return None

    # 资金可买性过滤:剔除当前资金买不起 1 手的高价股(不要先推荐再撤单)
    ceiling = affordable_price_ceiling(cfg)
    pool = pool[pool["close"] <= ceiling]
    if pool.empty:
        return None

    # 预筛:按成交额(流动性/资金关注度)取 top_candidates,再算技术因子(懒加载)
    n = int(cfg["run"]["top_candidates"])
    short = pool.sort_values("amount", ascending=False).head(n).reset_index(drop=True)

    mf_days = int(cfg["run"].get("mainflow_days", 5))
    sr_cfg = cfg.get("serenity", {})
    use_industry = bool(sr_cfg.get("use_industry", False))
    serenity_hits: dict[str, str] = {}
    rows = []
    for _, r in short.iterrows():
        hist = data.get_hist(
            r["symbol"], cfg["hist"]["lookback_days"], cfg["hist"]["adjust"]
        )
        industry = data.get_base_info(r["symbol"]).get("industry") if use_industry else None
        choke, theme = serenity.membership(r["symbol"], r.get("name"), industry, cfg)
        if theme:
            serenity_hits[r["symbol"]] = theme
        ff = compute_factor_frame(hist, data.get_fund_flow(r["symbol"]), mf_days, choke)
        if ff.empty:
            continue
        last = ff.iloc[-1]
        if last[FACTOR_NAMES].isna().any():
            continue
        turnover = float(hist.iloc[-1].get("turnover", float("nan")))
        rows.append({
            "symbol": r["symbol"],
            "turnover": turnover,
            **{c: float(last[c]) for c in FACTOR_NAMES},
        })
    if not rows:
        return None

    fdf = pd.DataFrame(rows).set_index("symbol")
    scores = score_cross_section(fdf, weights).sort_values(ascending=False)

    best = scores.index[0]
    binfo = short.set_index("symbol").loc[best]
    brow = fdf.loc[best]
    reason = {
        "score": round(float(scores.iloc[0]), 4),
        "serenity": serenity_hits.get(best),   # 命中的卡脖子赛道标签(若有)
        "factors": {c: round(float(brow[c]), 4) for c in FACTOR_NAMES},
        "turnover": None if pd.isna(brow["turnover"]) else round(float(brow["turnover"]), 2),
        "amount_yi": round(float(binfo["amount"]), 2),
        "pct": round(float(binfo["pct"]), 2),
    }
    ref_price = float(binfo["close"])                       # 选股时(实时)参考价
    discount = float(cfg.get("live", {}).get("entry_discount", 0.0))
    limit_price = round(ref_price * (1 - discount), 2)       # 目标买入价(限价)
    rec = {
        "rec_date": rec_date,
        "symbol": best,
        "name": str(binfo.get("name", "")),
        "entry_close": round(ref_price, 3),
        "limit_price": limit_price,
        "score": reason["score"],
        "holding_days": int(cfg["run"]["holding_days"]),
        "target": float(cfg["run"]["success_threshold"]),
        "stop_loss": float(cfg["run"]["stop_loss"]),
        "reason": json.dumps(reason, ensure_ascii=False),
    }
    rec_id = store.add_recommendation(rec)
    rec["id"] = rec_id
    rec["already_exists"] = rec_id is None
    return rec
