"""LLM 主导的 A 股模拟交易循环(run_live.py 的 Agent 版)。

Agent 完全自主:无固定建仓时刻、无机械持有期/止盈规则;充分调研后自主买卖。
代码层仅保留硬风控(回撤熔断/T+1/持仓上限/可买性/-5%硬止损)。
run_live.py 仍为确定性模式,互不影响。

用法:
    python run_agent.py                  # 交易日启动,跑完当日自动结束
    python run_agent.py --once           # 复盘 + 一次自主交易 + tick(调试,不限交易时段)
    python run_agent.py --eod-now        # 复盘 + 自主交易 + 收盘结算+日报(调试)
    python run_agent.py --once --no-optimize   # 跳过 walk-forward,非开盘快速冒烟
"""
from __future__ import annotations

import argparse
import datetime as _dt
import time

from ashare_agent import (config, paper_trade, portfolio, report, store, trade_log)
from ashare_agent.brain.agent import TradingAgent
from run_live import (_hm, _position_symbols, brain_review, intraday_tick, market_open)


def _free_slots(cfg: dict) -> int:
    n = len(portfolio.get_positions("open")) + len(portfolio.get_positions("pending"))
    return max(0, int(cfg["account"]["max_positions"]) - n)


def _print_agent_actions(res: dict, prefix: str = "      ") -> None:
    icons = {"place_order": "🎯", "add": "➕", "reduce": "📉", "close": "🔴",
             "cancel_order": "⏹️", "remember": "📝"}
    for a in res.get("actions", []):
        t = a["type"]
        if t == "remember":
            print(f"{prefix}{icons[t]} 记忆: {a.get('note','')}")
        elif t == "place_order":
            print(f"{prefix}{icons[t]} 挂单 {a.get('name','')}({a['symbol']}) "
                  f"目标价 {a['limit_price']} —— {a.get('reason','')}")
        elif t == "add":
            print(f"{prefix}{icons[t]} 加仓 {a.get('name','')}({a['symbol']}) "
                  f"{a['shares']}股 @ {a['price']} —— {a.get('reason','')}")
        elif t == "reduce":
            print(f"{prefix}{icons[t]} 减仓 {a['symbol']} {a['shares']}股 "
                  f"@ {a['exit_price']} ({a['ret']*100:+.2f}%) —— {a.get('reason','')}")
        elif t == "close":
            print(f"{prefix}{icons[t]} 平仓 {a['symbol']} @ {a['exit_price']} "
                  f"({a['ret']*100:+.2f}%) —— {a.get('reason','')}")
        elif t == "cancel_order":
            print(f"{prefix}{icons[t]} 撤单 {a['symbol']}")


def agent_review(agent: TradingAgent, today: str) -> None:
    if not agent.available:
        return
    print("[Agent] 开盘复盘 ...")
    print("  (调用 DeepSeek + 工具调研,通常需 1~3 分钟,请稍候)", flush=True)
    res = agent.review(today)
    _print_agent_actions(res)
    if res.get("summary"):
        print(f"      {res['summary']}")


def agent_trade(agent: TradingAgent, cfg: dict, today: str) -> None:
    """自主交易:调研市场后决定买卖/挂撤单(无固定时刻与持有期)。"""
    if not agent.available:
        return
    slots = _free_slots(cfg)
    opens = portfolio.get_positions("open")
    if not opens and slots <= 0:
        return
    print(f"[Agent] 自主交易(持仓{len(opens)} 可用名额{slots})...")
    print("  (调用 DeepSeek + 工具调研,通常需 1~3 分钟,请稍候)", flush=True)
    res = agent.trade(today, slots)
    if res.get("available"):
        _print_agent_actions(res, prefix="  ")
        if res.get("summary"):
            print(f"  {res['summary']}")
    elif res.get("skipped"):
        print(f"  (跳过:{res['skipped']})")


def run_eod(agent: TradingAgent, cfg: dict, today: str, evals: list,
            opt: dict) -> None:
    print("\n[收盘结算] 权益快照 ...")
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
    auto = cfg.get("agent", {}).get("auto_exits", True)
    exit_note = "(仅硬止损)" if auto is False else ""
    print(f"  建仓 {len(entered)} 笔, 平仓 {len(closed)} 笔{exit_note}, "
          f"权益 {snap['equity']}, 回撤 {dd*100:.2f}%"
          + (" (熔断)" if settle["halted"] else ""))
    n = len(trade_log.load_all())
    print(f"  成交台账 {n} 笔 → {trade_log.TRADE_LOG_PATH}")

    text = report.build_report(cfg, None, evals, opt, settle, today, agent=agent)
    print("\n" + "=" * 60)
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description="A股 LLM 完全自主模拟交易")
    parser.add_argument("--once", action="store_true", help="复盘+一次自主交易+tick(不限交易时段)")
    parser.add_argument("--eod-now", action="store_true", help="复盘+自主交易+立即收盘+日报")
    parser.add_argument("--no-optimize", action="store_true",
                        help="跳过 walk-forward 优化(非开盘快速测试用)")
    args = parser.parse_args()

    config.ensure_dirs()
    store.init_db()
    cfg = config.load_config()
    portfolio.init_portfolio(cfg)
    today = _dt.date.today().strftime("%Y-%m-%d")
    acfg = cfg.get("agent", {})
    interval = int(cfg["live"]["interval_seconds"])
    close_time = cfg["live"]["afternoon"][1]
    trade_mins = int(acfg.get("trade_interval_minutes")
                     or acfg.get("manage_interval_minutes", 15))

    evals, opt = [], {}
    if acfg.get("run_optimize", True) and not args.no_optimize:
        cfg, evals, opt = brain_review(cfg)
    else:
        print("[大脑] 跳过 walk-forward 优化(快速测试模式)")

    agent = TradingAgent(cfg)
    if not agent.available:
        print("[Agent] 未启用或无 DEEPSEEK_API_KEY,无法自主交易(参考 .env.example)。")
        return

    agent_review(agent, today)
    last_trade: _dt.datetime | None = None

    if args.once:
        agent_trade(agent, cfg, today)
        intraday_tick(cfg, today)
        return
    if args.eod_now:
        agent_trade(agent, cfg, today)
        run_eod(agent, cfg, today, evals, opt)
        return

    print(f"\n盘中循环 | tick {interval}s | Agent 自主交易每 {trade_mins}min | "
          f"收盘 {close_time} 后结算")
    eod_done = False
    while not eod_done:
        now = _dt.datetime.now()
        if _hm(now) > close_time:
            agent_trade(agent, cfg, today)          # 收盘前最后一轮决策
            run_eod(agent, cfg, today, evals, opt)
            eod_done = True
        elif market_open(now, cfg):
            due = (last_trade is None
                   or (now - last_trade).total_seconds() >= trade_mins * 60)
            if due:
                agent_trade(agent, cfg, today)
                last_trade = now
            intraday_tick(cfg, today)
            time.sleep(interval)
        else:
            print(f"  {_hm(now)} 非交易时段,等待 ...")
            time.sleep(30)


if __name__ == "__main__":
    main()
