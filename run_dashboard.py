"""A 股模拟盘 Web 仪表盘 — 账户/台账/日志/累计盈亏。

用法:
    streamlit run run_dashboard.py
    python run_dashboard.py          # 等价启动(自动调 streamlit)
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd
import streamlit as st

from ashare_agent import config, store
from ashare_agent.dashboard_data import (
    account_snapshot,
    agent_memory,
    cumulative_pnl_df,
    equity_curve_df,
    latest_report_path,
    load_trace,
    pnl_summary,
    recommendations_df,
    strategy_history,
    trace_dates,
    trace_summary,
    token_usage_detail,
    token_usage_history,
    token_usage_today,
    trades_df,
)

st.set_page_config(
    page_title="A股模拟盘仪表盘",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(ttl=15, show_spinner=False)
def _cached_snapshot(fetch_live: bool) -> dict:
    cfg = config.load_config()
    return account_snapshot(cfg, fetch_live=fetch_live)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_equity() -> pd.DataFrame:
    return equity_curve_df()


@st.cache_data(ttl=30, show_spinner=False)
def _cached_trades() -> pd.DataFrame:
    return trades_df()


@st.cache_data(ttl=30, show_spinner=False)
def _cached_cum_pnl() -> pd.DataFrame:
    return cumulative_pnl_df()


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _render_kpi_row(summary: dict) -> None:
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("总权益", _fmt_money(summary["equity"]),
              delta=_fmt_pct(summary["total_return_pct"]))
    c2.metric("累计盈亏", _fmt_money(summary["total_pnl"]))
    c3.metric("已实现盈亏", _fmt_money(summary["realized_pnl"]))
    c4.metric("浮动盈亏", _fmt_money(summary["unrealized_pnl"]))
    c5.metric("胜率", f"{summary['winrate_pct']:.1f}%",
              delta=f"{summary['closed_trades']} 笔")
    c6.metric("最大回撤", f"{summary['max_drawdown_pct']:.2f}%")


def _tab_account(fetch_live: bool) -> None:
    snap = _cached_snapshot(fetch_live)
    cfg = config.load_config()
    tok = token_usage_today(cfg)
    st.caption(f"数据更新: {snap['stats']['updated_at']}"
               + (" | 实时行情已接入" if snap["quotes_ok"] else " | 离线(用成本价兜底)"))

    _render_kpi_row(pnl_summary())

    if tok.get("calls", 0) > 0:
        cur = tok.get("currency", "CNY")
        sym = "¥" if cur == "CNY" else f"{cur} "
        t1, t2, t3, t4 = st.columns(4)
        t1.metric("今日 API 调用", f"{tok['calls']} 次")
        t2.metric("今日 Token(API)", f"{tok['total_tokens']:,}")
        t3.metric("费用(单价折算)", f"{sym}{tok['cost']:.4f}")
        if tok.get("balance_spent") is not None:
            st.metric("余额扣费(官方)", f"{sym}{tok['balance_spent']:.4f}",
                      delta=f"余 {sym}{tok['current_balance']:.2f}")
        acfg = cfg.get("agent", {})
        if tok.get("balance_spent") is None:
            pos_m = acfg.get("trade_interval_with_positions_minutes", 2)
            idle_m = acfg.get("trade_interval_minutes", 10)
            t4.metric("交易间隔", f"持仓 {pos_m} / 空仓 {idle_m} min")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("持仓")
        if snap["positions"].empty:
            st.info("当前无持仓")
        else:
            st.dataframe(snap["positions"], use_container_width=True, hide_index=True)
    with c2:
        st.subheader("限价挂单")
        if snap["pendings"].empty:
            st.info("无待成交挂单")
        else:
            st.dataframe(snap["pendings"], use_container_width=True, hide_index=True)

    eq = _cached_equity()
    if not eq.empty:
        st.subheader("权益曲线")
        chart_df = eq.set_index("date")[["equity", "total_pnl"]]
        st.line_chart(chart_df, use_container_width=True)

    s = snap["stats"]
    st.subheader("账户明细")
    d1, d2, d3, d4 = st.columns(4)
    d1.write(f"**现金** {_fmt_money(s['cash'])}")
    d2.write(f"**初始资金** {_fmt_money(s['initial_capital'])}")
    d3.write(f"**当前回撤** {s.get('current_drawdown', 0):.2f}%")
    d4.write(f"**持仓数** {s['open_positions']}")


def _tab_pnl() -> None:
    summary = pnl_summary()
    _render_kpi_row(summary)

    cum = _cached_cum_pnl()
    eq = _cached_equity()

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("已实现盈亏累计")
        if cum.empty:
            st.info("尚无平仓记录")
        else:
            st.line_chart(cum.set_index("exit_date")["cum_pnl"], use_container_width=True)
            st.dataframe(cum, use_container_width=True, hide_index=True)
    with c2:
        st.subheader("总权益 / 累计盈亏(含浮盈)")
        if eq.empty:
            st.info("尚无权益曲线(需至少一次收盘结算)")
        else:
            st.line_chart(eq.set_index("date")[["total_pnl", "equity"]], use_container_width=True)

    tdf = _cached_trades()
    if not tdf.empty:
        st.subheader("按标的汇总")
        by_sym = (
            tdf.groupby(["symbol", "name"], as_index=False)
            .agg(笔数=("id", "count"), 总盈亏=("pnl", "sum"), 均收益率=("ret_pct", "mean"))
            .sort_values("总盈亏", ascending=False)
        )
        by_sym["总盈亏"] = by_sym["总盈亏"].round(2)
        by_sym["均收益率"] = by_sym["均收益率"].round(2)
        st.dataframe(by_sym, use_container_width=True, hide_index=True)

    m1, m2, m3 = st.columns(3)
    m1.metric("单笔最佳", _fmt_money(summary["best_trade"]))
    m2.metric("单笔最差", _fmt_money(summary["worst_trade"]))
    m3.metric("均笔收益率", f"{summary['avg_trade_ret_pct']:.2f}%")


def _tab_trades() -> None:
    tdf = _cached_trades()
    if tdf.empty:
        st.info("成交台账为空")
        return

    show = tdf[[
        "id", "symbol", "name", "shares", "entry_price", "entry_date",
        "exit_price", "exit_date", "pnl", "ret_pct", "win_label", "reason",
    ]].rename(columns={
        "id": "序号", "symbol": "代码", "name": "名称", "shares": "股数",
        "entry_price": "成本价", "entry_date": "买入日", "exit_price": "卖出价",
        "exit_date": "卖出日", "pnl": "盈亏", "ret_pct": "收益率%",
        "win_label": "盈利", "reason": "原因",
    })
    st.dataframe(show, use_container_width=True, hide_index=True)

    total_pnl = float(tdf["pnl"].sum())
    st.caption(f"合计 {len(tdf)} 笔 | 累计已实现盈亏 **{_fmt_money(total_pnl)}** 元")


def _tab_agent() -> None:
    sub1, sub2, sub3 = st.tabs(["决策 Trace", "跨日记忆", "日报"])

    with sub1:
        dates = trace_dates()
        if not dates:
            st.info("暂无 agent_trace 日志(run_agent.py 运行后产生)")
        else:
            sel = st.selectbox("交易日", dates, index=0)
            events = load_trace(sel)
            df = trace_summary(events)
            st.caption(f"共 {len(events)} 条事件 | {sel}")
            filt = st.multiselect("过滤类型", ["阶段", "工具", "回复", "错误"],
                                  default=["阶段", "工具", "回复"])
            if filt:
                df = df[df["类型"].isin(filt)]
            st.dataframe(df, use_container_width=True, hide_index=True, height=400)

            assistants = [e for e in events if "assistant" in e]
            if assistants:
                st.subheader("Agent 总结")
                for e in assistants[-5:]:
                    with st.expander(f"{e.get('t', '')} 回复"):
                        st.markdown(e["assistant"])

    with sub2:
        mem = agent_memory(50)
        if not mem:
            st.info("暂无 agent_memory.jsonl")
        else:
            for m in reversed(mem):
                tags = ", ".join(m.get("tags") or [])
                st.markdown(f"**{m['ts']}** `{tags}`")
                st.write(m["note"])
                st.divider()

    with sub3:
        rp = latest_report_path()
        if rp is None:
            st.info("暂无日报 (data/reports/)")
        else:
            st.caption(str(rp))
            st.markdown(rp.read_text(encoding="utf-8"))


def _tab_token() -> None:
    cfg = config.load_config()
    today = _dt.date.today().strftime("%Y-%m-%d")
    tok = token_usage_today(cfg, today)

    st.subheader("今日 Token 消耗")
    if tok.get("calls", 0) == 0:
        st.info("今日尚无 LLM 调用记录(run_agent.py 运行后产生)")
    else:
        cur = tok.get("currency", "CNY")
        sym = "¥" if cur == "CNY" else f"{cur} "
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("调用次数", tok["calls"])
        c2.metric("输入 Token", f"{tok['prompt_tokens']:,}")
        c3.metric("输出 Token", f"{tok['completion_tokens']:,}")
        c4.metric("费用(单价折算)", f"{sym}{tok['cost']:.4f}")
        if tok.get("balance_spent") is not None:
            st.metric("余额扣费(DeepSeek API)", f"{sym}{tok['balance_spent']:.4f}",
                      delta=f"账户余 {sym}{tok['current_balance']:.2f}")

        st.caption("Token 数来自 chat/completions 响应 usage(API 实测);"
                   "费用=usage×config 官方单价;余额扣费=会话起始余额−当前余额(/user/balance)")

        if tok.get("by_phase"):
            st.write("**按阶段**")
            phase_rows = [
                {"阶段": k, "次数": v["calls"], "Token": v["tokens"],
                 "费用(折算)": f"{sym}{v['cost']:.4f}"}
                for k, v in tok["by_phase"].items()
            ]
            st.dataframe(pd.DataFrame(phase_rows), hide_index=True, use_container_width=True)

        detail = token_usage_detail(today)
        if not detail.empty:
            st.write("**调用明细**")
            show = detail[["ts", "phase", "iter", "total_tokens", "cost", "model"]]
            st.dataframe(show, hide_index=True, use_container_width=True)

    st.subheader("历史 Token 费用")
    hist = token_usage_history(cfg, 30)
    if not hist:
        st.info("暂无历史记录")
    else:
        hdf = pd.DataFrame(hist)[["date", "calls", "total_tokens", "cost"]]
        hdf = hdf.rename(columns={"date": "日期", "calls": "调用次数",
                                  "total_tokens": "Token", "cost": "费用(估)"})
        st.dataframe(hdf, hide_index=True, use_container_width=True)
        st.line_chart(hdf.set_index("日期")["费用(估)"], use_container_width=True)

    pr = cfg.get("agent", {}).get("token_pricing") or {}
    st.caption(
        f"单价(config.yaml): 缓存命中 {pr.get('input_cache_hit_per_million', 0.1)}、"
        f"未命中 {pr.get('input_per_million', 1)}、输出 {pr.get('output_per_million', 2)} 元/百万tok"
    )
    pos_m = cfg.get("agent", {}).get("trade_interval_with_positions_minutes", 2)
    idle_m = cfg.get("agent", {}).get("trade_interval_minutes", 10)
    st.caption(f"自主交易: 有持仓每 {pos_m} 分钟 / 无持仓每 {idle_m} 分钟 (+ 开盘复盘 + 收盘前一轮)")


def _tab_strategy() -> None:
    sub1, sub2 = st.tabs(["推荐台账", "策略优化历史"])

    with sub1:
        rdf = recommendations_df()
        if rdf.empty:
            st.info("暂无推荐记录")
        else:
            st.dataframe(rdf, use_container_width=True, hide_index=True)
            sig = store_stats()
            if sig:
                st.caption(
                    f"信号评估: {sig['evaluated']} 条 | 胜率 {sig['winrate']*100:.1f}%"
                )

    with sub2:
        hist = strategy_history(15)
        if not hist:
            st.info("暂无 strategy_history.jsonl")
        else:
            for h in hist:
                ts = h.get("updated_at", "")
                obj = h.get("objective", "")
                val = h.get("val") or {}
                with st.expander(f"{ts} | objective={obj}"):
                    st.json(h)


@st.cache_data(ttl=60, show_spinner=False)
def store_stats() -> dict:
    config.ensure_dirs()
    store.init_db()
    return store.stats()


def main() -> None:
    st.title("📊 A股模拟盘仪表盘")
    st.caption("只读展示 · 与 run_agent / run_live 并行运行")

    with st.sidebar:
        st.header("设置")
        fetch_live = st.toggle("拉取实时行情", value=True)
        if st.button("🔄 立即刷新", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.divider()
        st.write(f"**当前时间** {_dt.datetime.now():%Y-%m-%d %H:%M:%S}")
        st.caption("行情缓存 15s · 台账 30s")

    tabs = st.tabs(["账户概览", "盈亏统计", "成交台账", "Agent 日志", "Token 费用", "策略/推荐"])
    with tabs[0]:
        _tab_account(fetch_live)
    with tabs[1]:
        _tab_pnl()
    with tabs[2]:
        _tab_trades()
    with tabs[3]:
        _tab_agent()
    with tabs[4]:
        _tab_token()
    with tabs[5]:
        _tab_strategy()


if __name__ == "__main__":
    import subprocess
    import sys

    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        in_streamlit = get_script_run_ctx() is not None
    except Exception:
        in_streamlit = False

    if in_streamlit:
        main()
    else:
        subprocess.run(
            [sys.executable, "-m", "streamlit", "run", __file__],
            check=False,
        )
