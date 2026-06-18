"""生成每日 Markdown 日报。"""
from __future__ import annotations

import datetime as _dt
import json

from .config import REPORT_DIR, ensure_dirs
from . import store, portfolio, exits


_PHASE_ZH = {"review": "开盘复盘", "select": "择时选股", "exit": "盘中离场"}
_ACTION_ZH = {
    "place_order": lambda a: f"挂买单 {a.get('name','')}({a['symbol']}) @ {a['limit_price']} —— {a.get('reason','')}",
    "cancel_order": lambda a: f"撤单 {a['symbol']}",
    "close": lambda a: f"平仓 {a['symbol']} @ {a.get('exit_price')} ({a.get('ret',0)*100:+.2f}%) —— {a.get('reason','')}",
    "remember": lambda a: f"记忆:{a.get('note','')}",
}


def _append_agent_section(lines: list[str], agent) -> None:
    """日报追加 Agent 决策摘要(决策文本 + 实际写操作)。无 agent 或未启用则跳过。"""
    if agent is None or not getattr(agent, "available", False):
        return
    summaries = getattr(agent, "summaries", {}) or {}
    actions = getattr(agent, "all_actions", []) or []
    if not summaries and not actions:
        return
    lines.append("## 🤖 Agent 决策摘要(LLM 主导)\n")
    for phase, text in summaries.items():
        lines.append(f"**[{_PHASE_ZH.get(phase, phase)}]** {text}")
    if actions:
        lines.append("\n**本日 Agent 执行的操作:**")
        for a in actions:
            fmt = _ACTION_ZH.get(a.get("type"))
            lines.append(f"- {fmt(a) if fmt else a}")
    lines.append("")


def build_report(cfg: dict, rec: dict | None, evals: list[dict],
                 opt: dict, settle: dict | None, run_date: str, agent=None) -> str:
    s = store.stats()
    pf = portfolio.stats()
    lines: list[str] = []
    lines.append(f"# A股每日选股日报 · {run_date}\n")

    # 1. 今日推荐
    lines.append("## 1. 今日推荐\n")
    if rec is None:
        lines.append("> 今日未新增推荐(已有持仓/挂单,或无符合条件标的;详见下方「当前持仓」)。\n")
    else:
        reason = json.loads(rec["reason"]) if isinstance(rec["reason"], str) else rec.get("reason", {})
        dup = "(当日已存在,未重复入库)" if rec.get("already_exists") else ""
        lines.append(f"- **标的**:{rec['name']} ({rec['symbol']}) {dup}")
        lines.append(f"- **选股参考价**:{rec['entry_close']} | 当日涨跌:{reason.get('pct')}%")
        if rec.get("limit_price") is not None:
            lines.append(f"- **目标买入价(限价)**:{rec['limit_price']} —— 现价回调到该价才成交")
        sr = reason.get("serenity")
        if sr:
            w = cfg.get("factors", {}).get("serenity")
            wtxt = f",优化器学到权重 {round(float(w),3)}" if w is not None else ""
            lines.append(f"- **卡脖子赛道**:命中「{sr}」(serenity 因子=1{wtxt})")
        lines.append(f"- **综合评分**:{rec['score']}")
        lines.append(f"- **持有期**:{rec['holding_days']} 个交易日 | "
                     f"**目标**:最高涨幅 ≥ {rec['target']*100:.1f}% | "
                     f"**参考止损**:{rec['stop_loss']*100:.1f}%")
        lines.append(f"- **成交额**:{reason.get('amount_yi')} 亿元 | "
                     f"换手:{reason.get('turnover')}%")
        fac = reason.get("factors", {})
        fac_str = ", ".join(f"{k}={v}" for k, v in fac.items())
        lines.append(f"- **因子值**:{fac_str}\n")

    # 1.5 Agent 决策摘要(仅 LLM 主导模式;run_live 不传 agent 时跳过)
    _append_agent_section(lines, agent)

    # 2. 历史推荐复盘
    lines.append("## 2. 今日完成复盘\n")
    if not evals:
        lines.append("> 今日无到期(持有期已满)的历史推荐。\n")
    else:
        lines.append("| 推荐日 | 代码 | 入场价 | 最大涨幅 | 持有期收益 | 结果 |")
        lines.append("|---|---|---|---|---|---|")
        for e in evals:
            flag = "✅成功" if e["success"] else "❌失败"
            lines.append(
                f"| {e['rec_date']} | {e['symbol']} | {e['entry_price']} | "
                f"{e['max_gain']*100:.2f}% | {e['ret']*100:.2f}% | {flag} |"
            )
        lines.append("")

    # 3. 模拟交易
    lines.append("## 3. 今日模拟交易\n")
    if settle:
        if settle["entered"]:
            for e in settle["entered"]:
                lines.append(f"- 🟢 建仓 {e['name']}({e['symbol']}) "
                             f"{e['shares']}股 @ {e['price']} 成本 {e['cost']}元")
        if settle["closed"]:
            for c in settle["closed"]:
                flag = "✅" if c["win"] else "❌"
                lines.append(f"- 🔴 平仓 {c['name']}({c['symbol']}) @ {c['exit_price']} "
                             f"[{exits.REASON_ZH.get(c['reason'], c['reason'])}] 盈亏 {c['pnl']}元 "
                             f"({c['ret']*100:.2f}%) {flag}")
        if not settle["entered"] and not settle["closed"]:
            lines.append("> 今日无建仓/平仓。")
        for sk in settle.get("skipped", []):
            lines.append(f"- ⏭️ 未建仓 {sk['name']}({sk['symbol']}):{sk['reason']}")
        if settle.get("halted"):
            lines.append(f"- ⚠️ 回撤熔断:当前回撤 {settle['drawdown']*100:.1f}% "
                         f"已达上限,暂停建仓以保护本金。")

        opens = portfolio.get_positions("open")
        prices = settle.get("prices", {})
        if opens:
            lines.append("\n**当前持仓:**")
            for p in opens:
                px = prices.get(p["symbol"]) or p["entry_price"]
                fp = px / float(p["entry_price"]) - 1
                lines.append(f"- {p['name']}({p['symbol']}) {int(p['shares'])}股 | "
                             f"成本 {float(p['entry_price']):.2f} 现价 {float(px):.2f} | "
                             f"浮盈 **{fp*100:+.2f}%** | 买入日 {p['entry_date']} "
                             f"持有 {int(p['days_held'])} 日")
        eq = settle["equity"]
        lines.append(f"\n- 权益快照:总权益 {eq['equity']} 元 "
                     f"(现金 {eq['cash']} + 持仓市值 {eq['market_value']})\n")
    else:
        lines.append("> 无结算数据。\n")

    # 4. 策略优化
    obj = opt.get("objective", "winrate")
    obj_name = "回撤约束下收益最大化" if obj == "profit" else "命中率最大化"
    lines.append(f"## 4. 策略自优化(严格 walk-forward · 目标:{obj_name})\n")
    if "val" in opt:
        val, tr = opt["val"], opt["train"]
        status = "已更新因子权重" if opt.get("updated") else "保持基线权重(样本外未超越基线)"
        if obj == "profit":
            lines.append(f"- {status} | 样本外:收益 **{val.get('total_return',0)*100:.1f}%** / "
                         f"最大回撤 **{val.get('max_drawdown',0)*100:.1f}%** / 胜率 {val.get('winrate',0)*100:.1f}% "
                         f"({val.get('trades',0)}笔)")
            lines.append(f"- 训练集:收益 {tr.get('total_return',0)*100:.1f}% / "
                         f"回撤 {tr.get('max_drawdown',0)*100:.1f}% | 训练{opt['train_days']}日/验证{opt['val_days']}日")
        else:
            lines.append(f"- {status} | 样本外验证胜率 **{val.get('winrate',0)*100:.1f}%** "
                         f"(训练集 {tr.get('winrate',0)*100:.1f}% / 训练{opt['train_days']}日/验证{opt['val_days']}日)")
        w = ", ".join(f"{k}={v}" for k, v in opt["weights"].items())
        lines.append(f"- 权重:{w}\n")
    else:
        lines.append(f"- 未更新:{opt.get('msg', '')}\n")

    # 5. 虚拟账户战绩
    lines.append("## 5. 虚拟账户战绩(模拟实盘)\n")
    lines.append(f"- 初始资金:{pf['initial_capital']:.0f} 元 | "
                 f"**当前总权益:{pf['equity']:.2f} 元** | "
                 f"**累计收益率:{pf['total_return']*100:.2f}%**")
    dd_limit = cfg.get("risk", {}).get("max_drawdown", 0.15)
    lines.append(f"- 历史最大回撤:**{pf['max_drawdown']*100:.2f}%**(上限 {dd_limit*100:.0f}%) | "
                 f"现金:{pf['cash']:.2f} | 持仓:{pf['open_positions']} 笔 | "
                 f"已实现盈亏:{pf['realized_pnl']:.2f} 元")
    lines.append(f"- 已平仓交易:**{pf['closed_trades']}** 笔 | 盈利:**{pf['wins']}** 笔 | "
                 f"**胜率:{pf['winrate']*100:.1f}%** | 单笔平均收益:{pf['avg_trade_ret']*100:.2f}%\n")

    # 6. 信号命中率(选股模型本身,非账户)
    lines.append("## 6. 选股信号命中率(5日内最高涨幅≥目标)\n")
    lines.append(f"- 已评估:**{s['evaluated']}** | 命中:**{s['wins']}** | "
                 f"**命中率:{s['winrate']*100:.1f}%** | "
                 f"平均最高涨幅:{s['avg_maxgain']*100:.2f}%\n")

    lines.append("---")
    lines.append("> 本报告由量化模型自动生成,仅供研究参考,不构成任何投资建议。")
    text = "\n".join(lines)

    ensure_dirs()
    out = REPORT_DIR / f"report_{run_date}.md"
    out.write_text(text, encoding="utf-8")
    return text
