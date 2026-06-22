"""LLM 主导的 A 股模拟交易循环(run_live.py 的 Agent 版)。

Agent 完全自主:无固定建仓时刻、无机械持有期/止盈规则;充分调研后自主买卖。
代码层仅保留硬风控(回撤熔断/T+1/持仓上限/可买性/-5%硬止损)。
run_live.py 仍为确定性模式,互不影响。

用法:
    python run_agent.py                  # 交易日启动,跑完当日自动结束
    python run_agent.py --once           # 复盘 + 一次自主交易 + tick(调试,不限交易时段)
    python run_agent.py --eod-now        # 复盘 + 自主交易 + 收盘结算+日报(调试)
    python run_agent.py --once --no-optimize   # 跳过 walk-forward,非开盘快速冒烟
    python run_agent.py --ask 601958     # 询价单只股票是否值得建仓(只分析不下单)
    盘中运行时可输入: ask 601958  或  601958  + 回车
"""
from __future__ import annotations

import argparse
import datetime as _dt
import queue
import re
import threading
import time

from ashare_agent import (config, paper_trade, portfolio, report, store, trade_log)
from ashare_agent.brain.agent import TradingAgent
from run_live import (_hm, _position_symbols, brain_review, intraday_tick, market_open)

_SYMBOL_RE = re.compile(r"\b(\d{6})\b")


def _free_slots(cfg: dict) -> int:
    n = len(portfolio.get_positions("open")) + len(portfolio.get_positions("pending"))
    return max(0, int(cfg["account"]["max_positions"]) - n)


def _parse_ask_symbol(line: str) -> str | None:
    """从用户输入解析 6 位股票代码。"""
    line = line.strip()
    if not line or line.lower() in ("help", "?", "h", "帮助"):
        return None
    m = _SYMBOL_RE.search(line)
    return m.group(1) if m else None


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


def agent_ask(agent: TradingAgent, cfg: dict, today: str, symbol: str) -> None:
    """盘中询价:分析是否值得建仓(只读,不自动下单)。"""
    if not agent.available:
        print("[Agent] 未启用,无法询价。")
        return
    symbol = str(symbol).strip()
    if not _SYMBOL_RE.fullmatch(symbol):
        print(f"[询价] 代码格式错误:{symbol}(须为6位数字)")
        return
    slots = _free_slots(cfg)
    print(f"\n[询价] {symbol} 是否值得建仓?(可用名额 {slots}, 仅分析不下单)")
    print("  (调用 DeepSeek + 工具调研,通常需 1~2 分钟,请稍候)", flush=True)
    res = agent.ask(today, symbol, slots)
    if res.get("summary"):
        print("\n" + "─" * 50)
        print(res["summary"])
        print("─" * 50 + "\n")
    elif res.get("error"):
        print(f"  询价失败: {res['error']}")


def _input_listener(q: queue.Queue) -> None:
    """后台线程:读取用户 stdin,供盘中询价。"""
    while True:
        try:
            line = input()
        except EOFError:
            break
        q.put(line)


def _drain_ask_queue(q: queue.Queue, agent: TradingAgent, cfg: dict, today: str) -> None:
    while True:
        try:
            line = q.get_nowait()
        except queue.Empty:
            break
        sym = _parse_ask_symbol(line)
        if sym:
            agent_ask(agent, cfg, today, sym)
        elif line.strip():
            print("  [询价] 未识别代码。输入 ask 601958 或直接输入 6 位代码; help 查看帮助")


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
    parser.add_argument("--ask", metavar="SYMBOL",
                        help="询价单只股票是否值得建仓(只分析不下单,完成后退出)")
    args = parser.parse_args()

    if args.ask and not _SYMBOL_RE.fullmatch(args.ask.strip()):
        parser.error("--ask 须为 6 位 A 股代码,如 601958")

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
    if acfg.get("run_optimize", True) and not args.no_optimize and not args.ask:
        cfg, evals, opt = brain_review(cfg)
    elif not args.no_optimize:
        print("[大脑] 跳过 walk-forward 优化(快速测试模式)")

    agent = TradingAgent(cfg)
    if not agent.available:
        print("[Agent] 未启用或无 DEEPSEEK_API_KEY,无法自主交易(参考 .env.example)。")
        return

    if args.ask:
        agent_ask(agent, cfg, today, args.ask.strip())
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

    ask_q: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_input_listener, args=(ask_q,), daemon=True).start()

    print(f"\n盘中循环 | tick {interval}s | Agent 自主交易每 {trade_mins}min | "
          f"收盘 {close_time} 后结算")
    print("  盘中询价: 输入 ask 601958 或 601958 + 回车 (仅分析,不自动下单)")
    eod_done = False
    while not eod_done:
        now = _dt.datetime.now()
        if _hm(now) > close_time:
            agent_trade(agent, cfg, today)          # 收盘前最后一轮决策
            run_eod(agent, cfg, today, evals, opt)
            eod_done = True
        elif market_open(now, cfg):
            _drain_ask_queue(ask_q, agent, cfg, today)
            due = (last_trade is None
                   or (now - last_trade).total_seconds() >= trade_mins * 60)
            if due:
                agent_trade(agent, cfg, today)
                last_trade = now
            intraday_tick(cfg, today)
            _drain_ask_queue(ask_q, agent, cfg, today)
            time.sleep(interval)
        else:
            _drain_ask_queue(ask_q, agent, cfg, today)
            print(f"  {_hm(now)} 非交易时段,等待 ...")
            time.sleep(30)


if __name__ == "__main__":
    main()
