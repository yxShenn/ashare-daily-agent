"""台账存储:SQLite 记录每日推荐与回测评估结果(Shadow Account)。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Optional

import pandas as pd

from .config import DB_PATH, ensure_dirs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recommendations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rec_date      TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    name          TEXT,
    entry_close   REAL,          -- 推荐日收盘价(参考)
    score         REAL,
    holding_days  INTEGER,
    target        REAL,          -- 目标涨幅阈值
    stop_loss     REAL,
    reason        TEXT,          -- JSON: 各因子贡献/资金面
    status        TEXT DEFAULT 'open',   -- open / evaluated
    created_at    TEXT,
    UNIQUE(rec_date, symbol)
);

CREATE TABLE IF NOT EXISTS evaluations (
    rec_id        INTEGER PRIMARY KEY,
    entry_price   REAL,          -- 次日开盘价(实际入场)
    max_high      REAL,          -- 持有期内最高价
    max_gain      REAL,          -- 持有期内最大涨幅
    end_close     REAL,          -- 持有期末收盘
    ret           REAL,          -- 持有期末收益率
    success       INTEGER,       -- 1/0
    n_days        INTEGER,       -- 实际评估的交易日数
    eval_date     TEXT,
    FOREIGN KEY(rec_id) REFERENCES recommendations(id)
);
"""


@contextmanager
def connect():
    ensure_dirs()
    con = sqlite3.connect(DB_PATH)
    try:
        con.row_factory = sqlite3.Row
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with connect() as con:
        con.executescript(_SCHEMA)


def add_recommendation(rec: dict) -> Optional[int]:
    """写入一条推荐;若当日该股已存在则跳过返回 None。"""
    init_db()
    with connect() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO recommendations
               (rec_date, symbol, name, entry_close, score, holding_days,
                target, stop_loss, reason, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?, 'open', datetime('now','localtime'))""",
            (
                rec["rec_date"], rec["symbol"], rec.get("name"),
                rec.get("entry_close"), rec.get("score"), rec.get("holding_days"),
                rec.get("target"), rec.get("stop_loss"), rec.get("reason"),
            ),
        )
        return cur.lastrowid if cur.rowcount else None


def get_open_recommendations() -> list[dict]:
    init_db()
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM recommendations WHERE status='open' ORDER BY rec_date"
        ).fetchall()
        return [dict(r) for r in rows]


def save_evaluation(rec_id: int, ev: dict) -> None:
    init_db()
    with connect() as con:
        con.execute(
            """INSERT OR REPLACE INTO evaluations
               (rec_id, entry_price, max_high, max_gain, end_close, ret,
                success, n_days, eval_date)
               VALUES (?,?,?,?,?,?,?,?, date('now','localtime'))""",
            (
                rec_id, ev["entry_price"], ev["max_high"], ev["max_gain"],
                ev["end_close"], ev["ret"], int(ev["success"]), ev["n_days"],
            ),
        )
        con.execute(
            "UPDATE recommendations SET status='evaluated' WHERE id=?", (rec_id,)
        )


def stats() -> dict:
    """累计胜率统计。"""
    init_db()
    with connect() as con:
        row = con.execute(
            """SELECT COUNT(*) n, SUM(success) wins,
                      AVG(ret) avg_ret, AVG(max_gain) avg_maxgain
               FROM evaluations"""
        ).fetchone()
    n = row["n"] or 0
    wins = row["wins"] or 0
    return {
        "evaluated": n,
        "wins": wins,
        "winrate": (wins / n) if n else 0.0,
        "avg_ret": row["avg_ret"] or 0.0,
        "avg_maxgain": row["avg_maxgain"] or 0.0,
    }


def recommendations_df() -> pd.DataFrame:
    init_db()
    with connect() as con:
        return pd.read_sql_query(
            """SELECT r.*, e.entry_price, e.max_gain, e.ret, e.success, e.n_days
               FROM recommendations r LEFT JOIN evaluations e ON r.id=e.rec_id
               ORDER BY r.rec_date DESC""",
            con,
        )
