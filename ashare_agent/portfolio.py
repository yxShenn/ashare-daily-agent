"""虚拟账户:账户资金 / 持仓 / 成交 / 权益曲线 + A股交易费用与买卖原语。

费用模型:佣金(买卖双边,有最低收费)+ 印花税(仅卖出)。
交易规则:100 股整数倍、T+1(在 paper_trade 层控制)。
"""
from __future__ import annotations

import json

from .store import connect, init_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital REAL,
    cash REAL
);
CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    name         TEXT,
    status       TEXT DEFAULT 'pending',   -- pending(待建仓) / open(持仓中)
    rec_date     TEXT,
    shares       INTEGER DEFAULT 0,
    entry_price  REAL,
    entry_date   TEXT,
    buy_cost     REAL,                      -- 买入总成本(含佣金)
    target_pct   REAL,
    stop_pct     REAL,
    target_price REAL,
    stop_price   REAL,
    limit_price  REAL,                      -- 目标买入价(限价单);现价≤该值才成交
    high_water   REAL,                      -- 持仓期间最高价(移动止盈用)
    holding_days INTEGER,
    days_held    INTEGER DEFAULT 0,
    created_at   TEXT,
    UNIQUE(rec_date, symbol)
);
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT, name TEXT, shares INTEGER,
    entry_price  REAL, entry_date TEXT,
    exit_price   REAL, exit_date  TEXT,
    buy_cost     REAL, net_proceeds REAL,
    pnl REAL, ret REAL, reason TEXT, win INTEGER
);
CREATE TABLE IF NOT EXISTS equity_curve (
    date TEXT PRIMARY KEY, cash REAL, market_value REAL, equity REAL
);
"""


def init_portfolio(cfg: dict) -> None:
    init_db()
    with connect() as con:
        con.executescript(_SCHEMA)
        # 旧库兼容:缺 high_water 列则补上
        cols = {r["name"] for r in con.execute("PRAGMA table_info(positions)").fetchall()}
        if "high_water" not in cols:
            con.execute("ALTER TABLE positions ADD COLUMN high_water REAL")
        if "limit_price" not in cols:
            con.execute("ALTER TABLE positions ADD COLUMN limit_price REAL")
        if con.execute("SELECT COUNT(*) c FROM account").fetchone()["c"] == 0:
            cap = float(cfg["account"]["initial_capital"])
            con.execute(
                "INSERT INTO account(id, initial_capital, cash) VALUES (1, ?, ?)",
                (cap, cap),
            )


def get_account() -> dict:
    with connect() as con:
        return dict(con.execute(
            "SELECT initial_capital, cash FROM account WHERE id=1").fetchone())


def get_positions(status: str | None = None) -> list[dict]:
    q = "SELECT * FROM positions"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    with connect() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY id", args).fetchall()]


def add_pending(rec: dict) -> bool:
    """根据推荐挂一笔待建仓单(次日开盘建仓)。重复(同日同股)忽略。"""
    limit_price = rec.get("limit_price")
    with connect() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO positions
               (symbol, name, status, rec_date, target_pct, stop_pct,
                limit_price, holding_days, created_at)
               VALUES (?,?, 'pending', ?,?,?,?,?, datetime('now','localtime'))""",
            (rec["symbol"], rec.get("name"), rec["rec_date"],
             float(rec["target"]), float(rec["stop_loss"]),
             None if limit_price is None else float(limit_price),
             int(rec["holding_days"])),
        )
        return cur.rowcount > 0


def open_position(pos_id: int, shares: int, price: float, date: str,
                  target_price: float, stop_price: float, buy_cost: float) -> None:
    with connect() as con:
        con.execute(
            """UPDATE positions SET status='open', shares=?, entry_price=?,
               entry_date=?, target_price=?, stop_price=?, buy_cost=?,
               high_water=?, days_held=0
               WHERE id=?""",
            (shares, price, date, target_price, stop_price, buy_cost, price, pos_id),
        )
        con.execute("UPDATE account SET cash = cash - ? WHERE id=1", (buy_cost,))


def update_high_water(pos_id: int, price: float) -> None:
    """刷新持仓期间最高价(移动止盈基准)。"""
    with connect() as con:
        con.execute(
            "UPDATE positions SET high_water = MAX(COALESCE(high_water, entry_price), ?) WHERE id=?",
            (price, pos_id),
        )


def cancel_pending(pos_id: int) -> None:
    with connect() as con:
        con.execute("DELETE FROM positions WHERE id=? AND status='pending'", (pos_id,))


def bump_days_held(pos_id: int) -> None:
    with connect() as con:
        con.execute("UPDATE positions SET days_held = days_held + 1 WHERE id=?", (pos_id,))


def close_position(pos: dict, exit_price: float, exit_date: str,
                   reason: str, cfg: dict) -> dict:
    """卖出平仓,结算盈亏并写入成交记录。"""
    a = cfg["account"]
    shares = int(pos["shares"])
    gross = shares * exit_price
    commission = max(gross * a["commission_rate"], a["min_commission"])
    stamp = gross * a["stamp_duty"]
    net = gross - commission - stamp
    pnl = net - float(pos["buy_cost"])
    ret = pnl / float(pos["buy_cost"]) if pos["buy_cost"] else 0.0
    win = 1 if ret > float(a.get("win_threshold", 0.0)) else 0
    with connect() as con:
        con.execute(
            """INSERT INTO trades
               (symbol,name,shares,entry_price,entry_date,exit_price,exit_date,
                buy_cost,net_proceeds,pnl,ret,reason,win)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pos["symbol"], pos["name"], shares, pos["entry_price"], pos["entry_date"],
             round(exit_price, 3), exit_date, pos["buy_cost"], round(net, 2),
             round(pnl, 2), round(ret, 4), reason, win),
        )
        con.execute("UPDATE account SET cash = cash + ? WHERE id=1", (net,))
        con.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
    return {"symbol": pos["symbol"], "name": pos["name"], "exit_price": round(exit_price, 3),
            "pnl": round(pnl, 2), "ret": round(ret, 4), "reason": reason, "win": win}


def recent_trades(limit: int = 10) -> list[dict]:
    """最近已平仓交易(按平仓时间倒序),供 agent 复盘反思用。"""
    with connect() as con:
        rows = con.execute(
            """SELECT symbol, name, shares, entry_price, entry_date, exit_price,
                      exit_date, pnl, ret, reason, win
               FROM trades ORDER BY id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


def market_value(price_lookup: dict[str, float]) -> float:
    mv = 0.0
    for p in get_positions("open"):
        px = price_lookup.get(p["symbol"], p["entry_price"]) or p["entry_price"]
        mv += int(p["shares"]) * float(px)
    return mv


def equity(price_lookup: dict[str, float]) -> float:
    return get_account()["cash"] + market_value(price_lookup)


def snapshot_equity(date: str, price_lookup: dict[str, float]) -> dict:
    cash = get_account()["cash"]
    mv = market_value(price_lookup)
    eq = cash + mv
    with connect() as con:
        con.execute(
            """INSERT OR REPLACE INTO equity_curve(date, cash, market_value, equity)
               VALUES (?,?,?,?)""", (date, round(cash, 2), round(mv, 2), round(eq, 2)),
        )
    return {"date": date, "cash": round(cash, 2), "market_value": round(mv, 2),
            "equity": round(eq, 2)}


def peak_equity() -> float:
    """权益曲线历史峰值(用于回撤计算);无记录时取初始资金。"""
    acct = get_account()
    with connect() as con:
        row = con.execute("SELECT MAX(equity) m FROM equity_curve").fetchone()
    return max(row["m"] or 0.0, acct["initial_capital"])


def current_drawdown(price_lookup: dict[str, float]) -> float:
    """当前回撤 = (历史峰值 - 当前权益) / 历史峰值。"""
    eq = equity(price_lookup)
    peak = max(peak_equity(), eq)
    return (peak - eq) / peak if peak > 0 else 0.0


def max_drawdown() -> float:
    """权益曲线历史最大回撤。"""
    with connect() as con:
        rows = con.execute("SELECT equity FROM equity_curve ORDER BY date").fetchall()
    peak = mdd = 0.0
    for r in rows:
        eq = r["equity"]
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak)
    return round(mdd, 4)


def stats(price_lookup: dict[str, float] | None = None) -> dict:
    acct = get_account()
    with connect() as con:
        row = con.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(win),0) wins,
                      COALESCE(SUM(pnl),0) pnl, COALESCE(AVG(ret),0) avg_ret
               FROM trades""").fetchone()
    n, wins = row["n"], row["wins"]
    eq = equity(price_lookup or {})
    return {
        "initial_capital": acct["initial_capital"],
        "cash": round(acct["cash"], 2),
        "equity": round(eq, 2),
        "total_return": round(eq / acct["initial_capital"] - 1, 4) if acct["initial_capital"] else 0.0,
        "max_drawdown": max_drawdown(),
        "closed_trades": n,
        "wins": wins,
        "winrate": round(wins / n, 4) if n else 0.0,
        "realized_pnl": round(row["pnl"], 2),
        "avg_trade_ret": round(row["avg_ret"], 4),
        "open_positions": len(get_positions("open")),
    }
