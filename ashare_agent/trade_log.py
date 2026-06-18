"""成交台账:全部已平仓/减仓记录写入单一 CSV 文件(data/trades.csv)。

与 SQLite trades 表同步:每笔成交追加一行;启动时若文件缺失或与库不一致则全量重建。
"""
from __future__ import annotations

import csv
from pathlib import Path

from .config import TRADE_LOG_PATH, ensure_dirs
from .exits import REASON_ZH
from .store import connect

_CSV_HEADER = [
    "序号", "代码", "名称", "股数", "成本价", "买入日期", "买入成本",
    "卖出价", "卖出日期", "卖出净额", "盈亏", "收益率_pct", "是否盈利",
    "平仓原因", "原因说明", "类型",
]

_EXTRA_REASON_ZH = {"agent": "Agent自主平仓"}


def _reason_zh(reason: str) -> str:
    return _EXTRA_REASON_ZH.get(reason, REASON_ZH.get(reason, reason or ""))


def _row_from_trade(r: dict, trade_type: str = "平仓") -> list:
    ret_pct = round(float(r["ret"]) * 100, 2)
    win = int(r.get("win", 0))
    reason = str(r.get("reason") or "")
    return [
        r.get("id", ""),
        r["symbol"],
        r.get("name") or "",
        int(r["shares"]),
        round(float(r["entry_price"]), 3),
        r["entry_date"],
        round(float(r["buy_cost"]), 2),
        round(float(r["exit_price"]), 3),
        r["exit_date"],
        round(float(r["net_proceeds"]), 2),
        round(float(r["pnl"]), 2),
        ret_pct,
        "是" if win else "否",
        reason,
        _reason_zh(reason),
        trade_type,
    ]


def _write_all(rows: list[list]) -> None:
    ensure_dirs()
    with open(TRADE_LOG_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(_CSV_HEADER)
        w.writerows(rows)


def sync_from_db() -> Path:
    """从 SQLite trades 表全量导出到 trades.csv(幂等重建)。"""
    with connect() as con:
        rows_db = con.execute(
            """SELECT id, symbol, name, shares, entry_price, entry_date, buy_cost,
                      exit_price, exit_date, net_proceeds, pnl, ret, reason, win
               FROM trades ORDER BY id"""
        ).fetchall()
    rows = [_row_from_trade(dict(r), "平仓") for r in rows_db]
    _write_all(rows)
    return TRADE_LOG_PATH


def _file_row_count() -> int:
    if not TRADE_LOG_PATH.exists():
        return 0
    with open(TRADE_LOG_PATH, encoding="utf-8-sig") as f:
        return max(0, sum(1 for _ in f) - 1)


def _db_row_count() -> int:
    with connect() as con:
        return con.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]


def ensure_synced() -> None:
    """启动时调用:文件缺失或与库笔数不一致则重建;无成交时也保证文件存在(仅表头)。"""
    if not TRADE_LOG_PATH.exists() or _file_row_count() != _db_row_count():
        sync_from_db()


def append(trade_id: int, record: dict, trade_type: str = "平仓") -> None:
    """追加一笔成交记录(须在 SQLite INSERT 之后调用)。"""
    ensure_dirs()
    record = {**record, "id": trade_id}
    row = _row_from_trade(record, trade_type)
    write_header = not TRADE_LOG_PATH.exists() or TRADE_LOG_PATH.stat().st_size == 0
    with open(TRADE_LOG_PATH, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(_CSV_HEADER)
        w.writerow(row)


def load_all() -> list[dict]:
    """读取 trades.csv 全部记录(懒加载,供报表/工具用)。"""
    ensure_synced()
    if not TRADE_LOG_PATH.exists():
        return []
    with open(TRADE_LOG_PATH, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)
