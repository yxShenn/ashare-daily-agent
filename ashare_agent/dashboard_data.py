"""仪表盘只读数据层:复用 portfolio/store/trade_log,聚合账户/台账/日志。"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pandas as pd

from . import config, portfolio, store, trade_log
from .brain import memory
from .config import DATA_DIR, REPORT_DIR, STRATEGY_HISTORY_PATH
from .paper_trade import build_live_quotes

TRACE_DIR = DATA_DIR / "agent_trace"


def _init(cfg: dict) -> None:
    config.ensure_dirs()
    store.init_db()
    portfolio.init_portfolio(cfg)


def _position_symbols(opens: list[dict], pendings: list[dict]) -> list[str]:
    return list({p["symbol"] for p in opens} | {p["symbol"] for p in pendings})


def live_quotes(cfg: dict, symbols: list[str], fetch_live: bool) -> dict[str, dict]:
    if not symbols or not fetch_live:
        return {}
    try:
        return build_live_quotes(symbols, cfg)
    except Exception:
        return {}


def account_snapshot(cfg: dict, fetch_live: bool = True) -> dict:
    """实时账户快照:持仓/挂单/统计/KPI。"""
    _init(cfg)
    opens = portfolio.get_positions("open")
    pendings = portfolio.get_positions("pending")
    syms = _position_symbols(opens, pendings)
    quotes = live_quotes(cfg, syms, fetch_live)

    px = {
        p["symbol"]: float(
            quotes.get(p["symbol"], {}).get("price") or p.get("entry_price") or 0
        )
        for p in opens
    }
    st = portfolio.stats(px if opens else None)
    st["unrealized_pnl"] = round(
        portfolio.market_value(px) - sum(float(p.get("buy_cost") or 0) for p in opens), 2
    ) if opens else 0.0
    st["current_drawdown"] = round(portfolio.current_drawdown(px) * 100, 2) if opens else 0.0
    st["total_pnl"] = round(st["equity"] - st["initial_capital"], 2)
    st["updated_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    hold_rows = []
    for p in opens:
        sym = p["symbol"]
        price = px.get(sym) or float(p.get("entry_price") or 0)
        entry = float(p.get("entry_price") or 0)
        shares = int(p.get("shares") or 0)
        cost = float(p.get("buy_cost") or shares * entry)
        mv = shares * price
        pnl = mv - cost
        ret = (price / entry - 1) if entry > 0 else 0.0
        hold_rows.append({
            "代码": sym,
            "名称": p.get("name", ""),
            "股数": shares,
            "成本价": round(entry, 3),
            "现价": round(price, 3),
            "市值": round(mv, 2),
            "浮盈%": round(ret * 100, 2),
            "浮盈元": round(pnl, 2),
            "持有天数": int(p.get("days_held") or 0),
            "买入日": p.get("entry_date", ""),
        })

    pend_rows = []
    for p in pendings:
        sym = p["symbol"]
        cur = float(quotes.get(sym, {}).get("price") or 0)
        limit = float(p.get("limit_price") or 0)
        gap = cur - limit if cur > 0 and limit > 0 else None
        pend_rows.append({
            "代码": sym,
            "名称": p.get("name", ""),
            "限价": round(limit, 2),
            "现价": round(cur, 3) if cur else None,
            "价差": round(gap, 2) if gap is not None else None,
            "推荐日": p.get("rec_date", ""),
        })

    return {
        "stats": st,
        "positions": pd.DataFrame(hold_rows) if hold_rows else pd.DataFrame(),
        "pendings": pd.DataFrame(pend_rows) if pend_rows else pd.DataFrame(),
        "quotes_ok": bool(quotes),
    }


def equity_curve_df() -> pd.DataFrame:
    _init(config.load_config())
    with store.connect() as con:
        df = pd.read_sql_query(
            "SELECT date, cash, market_value, equity FROM equity_curve ORDER BY date",
            con,
        )
    if df.empty:
        return df
    init = portfolio.get_account()["initial_capital"]
    df["total_pnl"] = df["equity"] - init
    df["return_pct"] = (df["equity"] / init - 1) * 100
    return df


def trades_df() -> pd.DataFrame:
    """成交记录(优先 SQLite,与 CSV 同步)。"""
    _init(config.load_config())
    trade_log.ensure_synced()
    with store.connect() as con:
        df = pd.read_sql_query(
            """SELECT id, symbol, name, shares, entry_price, entry_date, buy_cost,
                      exit_price, exit_date, net_proceeds, pnl, ret, reason, win
               FROM trades ORDER BY id""",
            con,
        )
    if df.empty:
        return df
    df["ret_pct"] = (df["ret"] * 100).round(2)
    df["win_label"] = df["win"].map({1: "是", 0: "否"})
    return df


def cumulative_pnl_df() -> pd.DataFrame:
    """按平仓日聚合的已实现盈亏 + 累计曲线。"""
    df = trades_df()
    if df.empty:
        return pd.DataFrame(columns=["exit_date", "daily_pnl", "cum_pnl", "trade_count"])
    daily = (
        df.groupby("exit_date", as_index=False)
        .agg(daily_pnl=("pnl", "sum"), trade_count=("id", "count"))
        .sort_values("exit_date")
    )
    daily["cum_pnl"] = daily["daily_pnl"].cumsum().round(2)
    return daily


def pnl_summary(cfg: dict | None = None) -> dict:
    cfg = cfg or config.load_config()
    snap = account_snapshot(cfg, fetch_live=False)
    st = snap["stats"]
    tdf = trades_df()
    return {
        "initial_capital": st["initial_capital"],
        "equity": st["equity"],
        "total_pnl": st["total_pnl"],
        "total_return_pct": round(st["total_return"] * 100, 2),
        "realized_pnl": st["realized_pnl"],
        "unrealized_pnl": st["unrealized_pnl"],
        "closed_trades": st["closed_trades"],
        "winrate_pct": round(st["winrate"] * 100, 1),
        "max_drawdown_pct": round(st["max_drawdown"] * 100, 2),
        "avg_trade_ret_pct": round(st["avg_trade_ret"] * 100, 2),
        "best_trade": round(float(tdf["pnl"].max()), 2) if not tdf.empty else 0,
        "worst_trade": round(float(tdf["pnl"].min()), 2) if not tdf.empty else 0,
    }


def recommendations_df() -> pd.DataFrame:
    _init(config.load_config())
    return store.recommendations_df()


def strategy_history(limit: int = 20) -> list[dict]:
    if not STRATEGY_HISTORY_PATH.exists():
        return []
    lines = STRATEGY_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))


def agent_memory(limit: int = 50) -> list[dict]:
    return memory.recent(limit)


def trace_dates() -> list[str]:
    if not TRACE_DIR.exists():
        return []
    dates = sorted(
        p.stem for p in TRACE_DIR.glob("*.jsonl") if p.stem[:4].isdigit()
    )
    return list(reversed(dates))


def load_trace(date: str) -> list[dict]:
    path = TRACE_DIR / f"{date}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def trace_summary(events: list[dict]) -> pd.DataFrame:
    """将 trace 事件压缩为可读表格。"""
    rows = []
    for ev in events:
        t = ev.get("t", "")
        if "phase" in ev:
            rows.append({
                "时间": t, "类型": "阶段", "名称": ev.get("phase", ""),
                "摘要": f"交易日 {ev.get('today', '')}",
            })
        elif "tool" in ev:
            res = ev.get("result") or {}
            brief = res.get("msg") or res.get("error") or ""
            if not brief and ev["tool"] == "get_market_overview":
                brief = f"涨{res.get('up')}/跌{res.get('down')}"
            elif not brief and ev["tool"] == "get_portfolio_state":
                brief = f"权益 {res.get('equity')}"
            rows.append({
                "时间": t, "类型": "工具", "名称": ev["tool"],
                "摘要": str(brief)[:120],
            })
        elif "assistant" in ev:
            txt = str(ev["assistant"])[:200].replace("\n", " ")
            rows.append({"时间": t, "类型": "回复", "名称": "", "摘要": txt})
        elif "error" in ev:
            rows.append({"时间": t, "类型": "错误", "名称": "", "摘要": ev["error"]})
    return pd.DataFrame(rows)


def latest_report_path() -> Path | None:
    if not REPORT_DIR.exists():
        return None
    files = sorted(REPORT_DIR.glob("report_*.md"), reverse=True)
    return files[0] if files else None


def token_usage_today(cfg: dict | None = None, date: str | None = None) -> dict:
    from .brain.token_usage import day_summary

    cfg = cfg or config.load_config()
    date = date or _dt.date.today().strftime("%Y-%m-%d")
    return day_summary(date, cfg)


def token_usage_history(cfg: dict | None = None, limit: int = 30) -> list[dict]:
    from .brain.token_usage import all_days_summary

    return all_days_summary(cfg or config.load_config(), limit)


def token_usage_detail(date: str) -> pd.DataFrame:
    from .brain.token_usage import USAGE_PATH

    if not USAGE_PATH.exists():
        return pd.DataFrame()
    rows = []
    for line in USAGE_PATH.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("date") == date:
            rows.append(r)
    return pd.DataFrame(rows) if rows else pd.DataFrame()
