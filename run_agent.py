"""LLM 主导的 A 股模拟交易循环(run_live.py 的 Agent 版)。

四阶段沿用 run_live:复盘+优化 / 14:00 择时选股 / 盘中 tick / 收盘结算+日报。
区别:**选股**与**自由裁量出场**改由 DeepSeek Agent 决策;执行仍复用 paper_trade,
硬风控(回撤熔断/T+1/持仓上限/可买性/硬止损)全部保留在代码层,LLM 无法绕过。
无 key 或 agent.enabled=false 时优雅降级为确定性逻辑(等价 run_live)。

用法:
    python run_agent.py                  # 交易日启动,跑完当日自动结束
    python run_agent.py --once           # 复盘 + 选股 + 一次盘中 tick(冒烟/调试用)
    python run_agent.py --eod-now        # 复盘 + 选股 + 立即收盘结算(调试/补跑用)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import time

from ashare_agent import (config, paper_trade, portfolio, report, store)
from ashare_agent.brain.agent import TradingAgent
# 复用 run_live 的纯函数(DRY):run_live.py 不改动,仅导入其工具函数
from run_live import (_hm, _position_symbols, brain_review, intraday_tick,
                      market_open, select_now)


def _free_slots(cfg: dict) -> int:
    n = len(portfolio.get_positions("open")) + len(portfolio.get_positions("pending"))
    return max(0, int(cfg["account"]["max_positions"]) - n)


def agent_review(agent: TradingAgent, today: str) -> None:
    """开盘复盘:agent 检视持仓+记忆,做自由裁量离场(降级时跳过,硬规则仍在 tick/eod 生效)。"""
    if not agent.available:
        return
    print("[Agent] 开盘复盘持仓与跨日记忆 ...")
    res = agent.review(today)
    if res.get("summary"):
        print(f"      {res['summary']}")


def agent_or_default_select(agent: TradingAgent, cfg: dict, today: str) -> dict | None:
    """择时选股:优先 agent 决策(可挂多只限价单);降级则回退确定性 Top1。返回用于日报的 rec。"""
    already = any(p["rec_date"] == today for p in portfolio.get_positions())
    if already:
        print("[选股] 当日已有标的,不再重复选股")
        return None
    slots = _free_slots(cfg)
    use_agent = agent.available and bool(cfg.get("agent", {}).get("decide_select", True))
    if use_agent:
        print(f"[Agent] 择时选股(可用名额 {slots})...")
        res = agent.select(today, slots)
        if res.get("available"):
            for a in res.get("actions", []):
                if a["type"] == "place_order":
                    print(f"      🎯 挂单 {a['name']}({a['symbol']}) 目标价 {a['limit_price']} —— {a.get('reason','')}")
            if res.get("summary"):
                print(f"      {res['summary']}")
            return None
    # 降级:确定性选股
    return select_now(cfg, today)


def agent_intraday_exit(agent: TradingAgent, cfg: dict, today: str) -> None:
    """盘中自由裁量离场(硬止损/移动止盈已由 intraday_tick 的 check_exits 处理)。"""
    if not (agent.available and bool(cfg.get("agent", {}).get("decide_exit", True))):
        return
    sellable = [p for p in portfolio.get_positions("open") if p["entry_date"] < today]
    if not sellable:
        return
    res = agent.intraday_exit(today)
    for a in res.get("actions", []):
        if a["type"] == "close":
            print(f"  🔴 Agent 离场 {a['symbol']} @ {a['exit_price']} ({a['ret']*100:+.2f}%) —— {a.get('reason','')}")


def run_eod(agent: TradingAgent, cfg: dict, today: str, evals: list,
            opt: dict, rec: dict | None) -> None:
    """收盘结算(智能硬出场 + 权益快照)+ 日报(含 Agent 决策摘要)。"""
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
    print(f"  建仓 {len(entered)} 笔, 平仓 {len(closed)} 笔, 权益 {snap['equity']}, "
          f"回撤 {dd*100:.2f}%" + ("(熔断)" if settle['halted'] else ""))

    text = report.build_report(cfg, rec, evals, opt, settle, today, agent=agent)
    print("\n" + "=" * 60)
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description="A股 LLM 主导模拟交易循环")
    parser.add_argument("--once", action="store_true", help="复盘+选股+一次盘中 tick")
    parser.add_argument("--eod-now", action="store_true", help="复盘+选股+立即收盘结算")
    args = parser.parse_args()

    config.ensure_dirs()
    store.init_db()
    cfg = config.load_config()
    portfolio.init_portfolio(cfg)
    today = _dt.date.today().strftime("%Y-%m-%d")
    interval = int(cfg["live"]["interval_seconds"])
    close_time = cfg["live"]["afternoon"][1]
    select_time = cfg["live"]["select_time"]

    cfg, evals, opt = brain_review(cfg)             # 复盘 + 严格 walk-forward 优化(确定性底座)
    agent = TradingAgent(cfg)
    if not agent.available:
        print("[Agent] 未启用或无 DEEPSEEK_API_KEY,本次以确定性逻辑运行(参考 .env.example)。")

    agent_review(agent, today)
    rec = None
    selected = any(p["rec_date"] == today for p in portfolio.get_positions())

    if args.once:
        rec = agent_or_default_select(agent, cfg, today)
        intraday_tick(cfg, today)
        agent_intraday_exit(agent, cfg, today)
        return
    if args.eod_now:
        rec = agent_or_default_select(agent, cfg, today)
        run_eod(agent, cfg, today, evals, opt, rec)
        return

    print(f"\n盘中循环启动 | 间隔 {interval}s | 选股 {select_time} | 收盘 {close_time} 后结算退出")
    eod_done = False
    while not eod_done:
        now = _dt.datetime.now()
        if _hm(now) > close_time:
            if not selected:
                rec = agent_or_default_select(agent, cfg, today)
            run_eod(agent, cfg, today, evals, opt, rec)
            eod_done = True
        elif market_open(now, cfg):
            if not selected and _hm(now) >= select_time:
                rec = agent_or_default_select(agent, cfg, today)
                selected = True
            intraday_tick(cfg, today)
            agent_intraday_exit(agent, cfg, today)
            time.sleep(interval)
        else:
            print(f"  {_hm(now)} 非交易时段,等待 ...")
            time.sleep(30)


if __name__ == "__main__":
    main()
