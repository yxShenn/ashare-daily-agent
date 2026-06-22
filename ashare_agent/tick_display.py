"""盘中 tick 终端展示(持仓/挂单/账户汇总表格)。"""
from __future__ import annotations

from . import portfolio


def _label(name: str, symbol: str, width: int = 14) -> str:
    text = f"{name}({symbol})"
    return text if len(text) <= width else text[: width - 1] + "…"


def _line(width: int = 76) -> str:
    return "  " + "─" * width


def _print_table(title: str, header: str, rows: list[str]) -> None:
    if not rows:
        return
    print(f"  【{title}】")
    print(header)
    print(_line(len(header) - 2))
    for row in rows:
        print(row)


def print_intraday_status(
    ts: str,
    opens: list[dict],
    pendings: list[dict],
    quotes: dict[str, dict],
    skipped: list[dict],
    eq: float,
    entered: list[dict] | None = None,
    closed: list[dict] | None = None,
) -> None:
    """打印一轮 intraday_tick 的完整账户快照(表格 + 汇总)。"""
    n_q = len(quotes)
    n_sym = len({p["symbol"] for p in opens} | {p["symbol"] for p in pendings})
    print(f"  {ts} 行情 {n_q}/{n_sym} 只 | 持仓 {len(opens)} | 挂单 {len(pendings)}")

    for e in entered or []:
        print(f"  🟢 建仓 {e['name']}({e['symbol']}) {e['shares']}股 @ {e['price']:.2f} "
              f"成本 {e.get('cost', 0):,.0f}元")
    for c in closed or []:
        print(f"  🔴 平仓 {c['name']}({c['symbol']}) @ {c['exit_price']:.2f} "
              f"[{c.get('reason', '')}] {c.get('ret', 0) * 100:+.2f}% "
              f"盈亏 {c.get('pnl', 0):+,.0f}元")

    skip_map = {sk["symbol"]: sk.get("reason", "") for sk in (skipped or [])}

    # ---- 持仓 ----
    hold_hdr = (
        f"  {'标的':<14}{'股数':>6}{'成本价':>8}{'现价':>8}{'市值':>10}"
        f"{'浮盈%':>8}{'浮盈元':>10}{'持有':>5}"
    )
    hold_rows: list[str] = []
    total_mv = 0.0
    total_float = 0.0
    for p in opens:
        sym = p["symbol"]
        q = quotes.get(sym, {})
        price = float(q.get("price") or p.get("entry_price") or 0)
        entry = float(p.get("entry_price") or 0)
        shares = int(p.get("shares") or 0)
        mv = shares * price
        cost = float(p.get("buy_cost") or shares * entry)
        pnl = mv - cost
        ret = (price / entry - 1) if entry > 0 else 0.0
        total_mv += mv
        total_float += pnl
        days = int(p.get("days_held") or 0)
        hold_rows.append(
            f"  {_label(p.get('name', ''), sym):<14}{shares:>6}"
            f"{entry:>8.2f}{price:>8.2f}{mv:>10,.0f}"
            f"{ret * 100:>+7.2f}%{pnl:>+10,.0f}{days:>4}日"
        )
    if hold_rows:
        _print_table("持仓", hold_hdr, hold_rows)
        print(
            f"  {'小计':<14}{'':>6}{'':>8}{'':>8}{total_mv:>10,.0f}"
            f"{'':>8}{total_float:>+10,.0f}"
        )

    # ---- 挂单 ----
    pend_hdr = (
        f"  {'标的':<14}{'目标价':>8}{'现价':>8}{'价差':>8}{'状态':<22}"
    )
    pend_rows: list[str] = []
    for p in pendings:
        sym = p["symbol"]
        q = quotes.get(sym, {})
        cur = float(q.get("price") or 0)
        limit = p.get("limit_price")
        limit_f = float(limit) if limit is not None else 0.0
        if cur > 0 and limit_f > 0:
            gap = cur - limit_f
            gap_s = f"{gap:+.2f}"
            if sym in skip_map:
                status = skip_map[sym]
                if len(status) > 22:
                    status = status[:21] + "…"
            elif gap > 0:
                status = f"等待回调(高{gap:.2f})"
            else:
                status = "现价≤限价,待成交"
        else:
            gap_s = "—"
            status = skip_map.get(sym, "无行情")
            if len(status) > 22:
                status = status[:21] + "…"
        pend_rows.append(
            f"  {_label(p.get('name', ''), sym):<14}{limit_f:>8.2f}{cur:>8.2f}"
            f"{gap_s:>8}{status:<22}"
        )
    if pend_rows:
        _print_table("限价挂单", pend_hdr, pend_rows)

    # ---- 账户汇总 ----
    cash = portfolio.get_account()["cash"]
    px = {p["symbol"]: float(quotes.get(p["symbol"], {}).get("price") or p["entry_price"])
          for p in opens}
    mv_all = portfolio.market_value(px) if opens else 0.0
    float_all = total_float if hold_rows else 0.0
    print(_line())
    print(
        f"  【账户】现金 {cash:,.2f} | 持仓市值 {mv_all:,.0f} | "
        f"总权益 {eq:,.0f} | 持仓浮盈 {float_all:+,.0f}元"
    )
