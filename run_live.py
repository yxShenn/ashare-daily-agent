"""盘中实时循环:启动只复盘+优化,下午实时择时选股挂限价单,盘中等回调成交。

流程:
- 启动(复盘+优化):复盘历史 + 严格 walk-forward 优化(不立即选股)。
- 盘中每隔 interval_seconds:监控持仓做智能出场(硬止损/移动止盈);尝试限价成交。
- 到达 live.select_time(默认 14:00):用**实时数据**选最有潜力的标的,
  按 entry_discount 定好**目标买入价**并挂**限价买单**(现价回调到该价才成交)。
- 收盘(afternoon[1])后:智能出场 + 权益快照 -> 写日报,退出当日循环。

用法:
    python run_live.py                  # 交易日开盘前后启动,跑完当日自动结束
    python run_live.py --once           # 复盘+优化 + 选股 + 一次盘中 tick(调试用)
    python run_live.py --eod-now        # 复盘+优化 + 选股 + 立即收盘结算(调试/补跑用)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import time

from ashare_agent import (config, evaluate, optimize, paper_trade,
                          portfolio, recommend, report, store)


def _hm(now: _dt.datetime) -> str:
    return now.strftime("%H:%M")


def _in(now: _dt.datetime, win: list[str]) -> bool:
    return win[0] <= _hm(now) <= win[1]


def market_open(now: _dt.datetime, cfg: dict) -> bool:
    lv = cfg["live"]
    return _in(now, lv["morning"]) or _in(now, lv["afternoon"])


def _position_symbols(today: str) -> list[str]:
    """实时模式:持仓 + 当日(及之前)可建仓的待建仓单。"""
    syms = {p["symbol"] for p in portfolio.get_positions("open")}
    syms |= {p["symbol"] for p in portfolio.get_positions("pending")
             if p["rec_date"] <= today}
    return list(syms)


def brain_review(cfg: dict) -> tuple[dict, list, dict]:
    """启动大脑:复盘历史 + 严格 walk-forward 优化(不选股,选股放到下午)。"""
    print("[大脑] 复盘历史推荐 ...")
    evals = evaluate.evaluate_open(cfg)
    print(f"      完成复盘 {len(evals)} 条")
    print("[大脑] 严格 walk-forward 优化 ...")
    opt = optimize.optimize(cfg)
    print(f"      {opt.get('val', opt.get('msg'))}")
    return config.load_config(), evals, opt


def select_now(cfg: dict, today: str) -> dict | None:
    """下午实时择时:选最有潜力的标的,定目标买入价并挂限价买单。"""
    active_today = [p for p in portfolio.get_positions() if p["rec_date"] == today]
    if active_today:
        p = active_today[0]
        state = "已持仓" if p["status"] == "open" else "挂单中"
        print(f"[选股] 当日已有标的 {p['name']}({p['symbol']})[{state}],不再重复选股")
        return None
    ceiling = recommend.affordable_price_ceiling(cfg)
    print(f"[选股] {_dt.datetime.now():%H:%M} 实时择时(可买单价上限≈{ceiling:.0f}元/股)...")
    rec = recommend.run_recommend(cfg, rec_date=today)
    if rec:
        added = portfolio.add_pending(rec)
        if added:
            print(f"      🎯 推荐 {rec['name']}({rec['symbol']}) 参考现价 {rec['entry_close']} "
                  f"| 目标买入价 **{rec['limit_price']}** | 评分 {rec['score']} -> 限价买单已挂")
        else:
            print(f"      推荐 {rec['name']}({rec['symbol']}) 已持有/已挂单,跳过")
    else:
        print("      今日无符合条件(且买得起)的标的")
    return rec


def intraday_tick(cfg: dict, today: str) -> None:
    ts = f"{_dt.datetime.now():%H:%M:%S}"
    opens = portfolio.get_positions("open")
    pendings = portfolio.get_positions("pending")

    syms = _position_symbols(today)
    if not syms:
        print(f"  {ts} 无可交易标的(尚未到选股时间或当前无持仓/挂单)")
        return

    quotes = paper_trade.build_live_quotes(syms, cfg)
    print(f"  {ts} 行情获取 {len(quotes)}/{len(syms)} 只 | 持仓{len(opens)} 限价挂单{len(pendings)}")

    entered, skipped = paper_trade.enter_pending(
        cfg, {s: q["price"] for s, q in quotes.items()}, today, same_day=True)
    closed = paper_trade.check_exits(cfg, quotes, today, is_eod=False)
    eq = portfolio.equity({s: q["price"] for s, q in quotes.items()})

    for e in entered:
        print(f"  🟢 建仓 {e['name']}({e['symbol']}) {e['shares']}股 @ {e['price']}")
    for sk in skipped:
        print(f"  ⏭️  跳过 {sk['name']}({sk['symbol']}):{sk['reason']}")
    for c in closed:
        print(f"  🔴 平仓 {c['name']}({c['symbol']}) @ {c['exit_price']} [{c['reason']}] "
              f"{c['ret']*100:.2f}%")
    if opens:
        for p in opens:
            q = quotes.get(p["symbol"])
            if q:
                pr = q["price"] / float(p["entry_price"]) - 1
                print(f"     持有 {p['name']}({p['symbol']}) 现价{q['price']} 浮盈{pr*100:+.2f}%")
    print(f"  {ts} 持仓{len(portfolio.get_positions('open'))}笔 权益≈{eq:.0f}")


def run_eod(cfg: dict, today: str, evals: list, opt: dict, rec: dict | None) -> None:
    """收盘结算(智能出场 + 权益快照) + 日报。大脑已在开盘前跑过。"""
    print("\n[收盘结算] 智能出场 + 权益快照 ...")
    syms = _position_symbols(today)
    quotes = paper_trade.build_live_quotes(syms, cfg) if syms else {}
    bars = {s: {"high": q["high"], "low": q["low"], "close": q["price"], "ma": q.get("ma")}
            for s, q in quotes.items()}
    entered, skipped = paper_trade.enter_pending(
        cfg, {s: q["price"] for s, q in quotes.items()}, today, same_day=True)
    closed = paper_trade.check_exits(cfg, bars, today, is_eod=True)
    px = {s: q["price"] for s, q in quotes.items()}
    dd = portfolio.current_drawdown(px)
    snap = portfolio.snapshot_equity(today, px)
    limit = float(cfg.get("risk", {}).get("max_drawdown", 1.0))
    settle = {"entered": entered, "skipped": skipped, "closed": closed, "equity": snap,
              "prices": px, "drawdown": round(dd, 4), "halted": dd >= limit}
    for sk in skipped:
        print(f"  ⏭️  未建仓 {sk['name']}({sk['symbol']}):{sk['reason']}")
    print(f"  建仓 {len(entered)} 笔, 平仓 {len(closed)} 笔, 权益 {snap['equity']}, "
          f"回撤 {dd*100:.2f}%" + ("(熔断)" if settle['halted'] else ""))

    text = report.build_report(cfg, rec, evals, opt, settle, today)
    print("\n" + "=" * 60)
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description="A股盘中实时模拟交易循环")
    parser.add_argument("--once", action="store_true", help="只跑一次盘中 tick")
    parser.add_argument("--eod-now", action="store_true", help="立即执行收盘结算+大脑")
    args = parser.parse_args()

    config.ensure_dirs()
    store.init_db()
    cfg = config.load_config()
    portfolio.init_portfolio(cfg)
    today = _dt.date.today().strftime("%Y-%m-%d")
    interval = int(cfg["live"]["interval_seconds"])
    close_time = cfg["live"]["afternoon"][1]
    select_time = cfg["live"]["select_time"]

    cfg, evals, opt = brain_review(cfg)
    rec = None
    # 若当日已有标的(此前已选过),视为已选股
    selected = any(p["rec_date"] == today for p in portfolio.get_positions())

    if args.once:
        rec = select_now(cfg, today)
        intraday_tick(cfg, today)
        return
    if args.eod_now:
        rec = select_now(cfg, today)
        run_eod(cfg, today, evals, opt, rec)
        return

    print(f"\n盘中循环启动 | 间隔 {interval}s | 选股 {select_time} | 收盘 {close_time} 后结算退出")
    eod_done = False
    while not eod_done:
        now = _dt.datetime.now()
        if _hm(now) > close_time:
            if not selected:                       # 收盘前兜底选一次
                rec = select_now(cfg, today)
            run_eod(cfg, today, evals, opt, rec)
            eod_done = True
        elif market_open(now, cfg):
            if not selected and _hm(now) >= select_time:
                rec = select_now(cfg, today)       # 下午实时择时选股挂限价单
                selected = True
            intraday_tick(cfg, today)
            time.sleep(interval)
        else:
            print(f"  {_hm(now)} 非交易时段,等待 ...")
            time.sleep(30)


if __name__ == "__main__":
    main()
