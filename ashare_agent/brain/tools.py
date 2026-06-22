"""Agent 工具集:把现有确定性引擎封装为受硬风控约束的 function-calling tools。

设计要点:
- 全部复用现有模块(data/universe/factors/strategy/recommend/portfolio/paper_trade/exits),不重复实现逻辑。
- 写操作(下单/撤单/平仓)一律先过硬风控,校验失败把错误**回传给 LLM**(而非抛异常),让其改决策。
- 工具结果均为可 JSON 序列化的 dict;LLM 不可绕过代码层的本金保护。
"""
from __future__ import annotations

import datetime as _dt
import json
import math

from .. import data, portfolio, recommend, store
from . import memory


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def _ok(**kw) -> dict:
    return {"ok": True, **kw}


_WRITE_TOOLS = frozenset({
    "place_limit_order", "cancel_order", "close_position",
    "reduce_position", "add_to_position", "remember",
})


class ToolKit:
    """一次决策周期内复用。持有 cfg/today/skills,并记录已执行的写操作(供日报)。"""

    def __init__(self, cfg: dict, today: str, skills, readonly: bool = False):
        self.cfg = cfg
        self.today = today
        self.skills = skills
        self.readonly = readonly
        self.actions: list[dict] = []          # 本次决策实际发生的写操作
        self._price_cache: dict[str, float] = {}
        self._mkt_cache: dict | None = None    # 本周期大盘广度(避免重复拉全市场)

    # ---------------- 工具 schema(OpenAI function calling 格式) ----------------

    def schemas(self) -> list[dict]:
        skill_tools = self.skills.tool_schemas() if self.skills else []
        base = _BASE_SCHEMAS
        if self.readonly:
            base = [s for s in base if s["function"]["name"] not in _WRITE_TOOLS]
        return base + skill_tools

    # ---------------- 调用分发 ----------------

    def call(self, name: str, args: dict) -> dict:
        if self.readonly and name in _WRITE_TOOLS:
            return _err("询价模式仅分析不下单;如需自动挂单请等待自主交易轮次")
        fn = getattr(self, f"_t_{name}", None)
        if fn is None:
            # 交给技能注册的工具
            if self.skills and self.skills.has_tool(name):
                try:
                    return self.skills.call_tool(name, args, self)
                except Exception as e:  # 技能工具不可信,失败回传 LLM
                    return _err(f"技能工具 {name} 执行失败: {e}")
            return _err(f"未知工具: {name}")
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return _err(f"参数错误: {e}")
        except Exception as e:
            return _err(f"工具 {name} 执行失败: {e}")

    # ---------------- 只读工具 ----------------

    def _t_get_portfolio_state(self) -> dict:
        opens = portfolio.get_positions("open")
        pendings = portfolio.get_positions("pending")
        px = self._quotes([p["symbol"] for p in opens])
        eq = portfolio.equity(px)
        acct = portfolio.get_account()
        mkt = self._market_overview()
        market_avg = mkt.get("avg_pct") if mkt.get("ok") else None
        positions = []
        for p in opens:
            cur = px.get(p["symbol"])
            fp = (cur / float(p["entry_price"]) - 1) if cur and p["entry_price"] else None
            sc = data.get_sector_context(p["symbol"], market_avg)
            positions.append({
                "symbol": p["symbol"], "name": p["name"], "shares": int(p["shares"]),
                "entry_price": float(p["entry_price"]), "entry_date": p["entry_date"],
                "days_held": int(p["days_held"]),
                "current_price": cur,
                "float_return_pct": None if fp is None else round(fp * 100, 2),
                "sellable_today": p["entry_date"] < self.today,   # T+1
                "industry": sc.get("industry"),
                "board_name": sc.get("board_name"),
                "board_pct": sc.get("board_pct"),
                "stock_pct": sc.get("stock_pct"),
                "vs_board_pct": sc.get("vs_board_pct"),
                "board_vs_market_pct": sc.get("board_vs_market_pct"),
            })
        return _ok(
            cash=round(acct["cash"], 2),
            equity=round(eq, 2),
            initial_capital=round(acct["initial_capital"], 2),
            total_return_pct=round((eq / acct["initial_capital"] - 1) * 100, 2),
            drawdown_pct=round(portfolio.current_drawdown(px) * 100, 2),
            drawdown_limit_pct=round(float(self.cfg["risk"]["max_drawdown"]) * 100, 2),
            max_positions=int(self.cfg["account"]["max_positions"]),
            open_count=len(opens), pending_count=len(pendings),
            positions=positions,
            pending_orders=[{"symbol": p["symbol"], "name": p["name"],
                             "limit_price": p["limit_price"], "rec_date": p["rec_date"]}
                            for p in pendings],
            price_note="positions.current_price 为新浪实时价,与成交 tick 同源",
            sector_note="止盈/止损须同时看大盘(avg_pct)与持仓 board_pct/board_vs_market_pct,不可只看大盘",
            market_avg_pct=market_avg,
        )

    def _market_overview(self) -> dict:
        """本决策周期内复用的大盘广度(实时新浪快照)。"""
        if self._mkt_cache is None:
            self._mkt_cache = self._t_get_market_overview()
        return self._mkt_cache

    def _t_get_market_overview(self) -> dict:
        spot = data.get_live_spot()
        br = data.market_breadth(spot)
        meta = data.live_spot_meta()
        return _ok(
            **br,
            as_of=meta.get("as_of"),
            data_source=meta.get("source", "sina_live"),
            note="涨跌家数为新浪全市场实时快照(盘中会刷新);个股现价请用 current_price;"
                 "决策须结合 get_sector_context 看板块",
        )

    def _t_get_sector_context(self, symbol: str) -> dict:
        symbol = str(symbol).strip()
        mkt = self._market_overview()
        market_avg = mkt.get("avg_pct") if mkt.get("ok") else None
        sc = data.get_sector_context(symbol, market_avg)
        legend = data.agent_field_legend(self.cfg)
        return _ok(
            **sc,
            market_avg_pct=market_avg,
            field_legend={k: legend[k] for k in (
                "board_pct", "vs_board_pct", "board_vs_market_pct", "pct")},
            note="大盘弱但 board_vs_market_pct>0 表示板块逆势走强,不宜仅凭大盘弱就卖出",
        )

    def _t_get_candidates(self, topn: int | None = None) -> dict:
        n = int(topn or self.cfg.get("agent", {}).get("candidates_topn", 25))
        cands = recommend.rank_candidates(self.cfg)
        px = self._quotes([c["symbol"] for c in cands[:n]])
        ceiling = recommend.affordable_price_ceiling(self.cfg, px)
        legend = data.agent_field_legend(self.cfg)
        return _ok(
            affordable_price_ceiling=round(ceiling, 2),
            field_legend=legend,
            price_note="close/current_price 为新浪实时价,与成交 tick 同源;决策现价以此为准",
            mainflow_note="mainflow_ratio_nd_avg_pct 与 factors 中已移除的 mainflow 同义,单位百分点(%),不是亿元",
            candidates=cands[:n],
        )

    def _t_get_stock_detail(self, symbol: str) -> dict:
        from ..factors import FACTOR_NAMES, compute_factor_frame
        from .. import serenity

        symbol = str(symbol).strip()
        hist = data.get_hist(symbol, self.cfg["hist"]["lookback_days"], self.cfg["hist"]["adjust"])
        if hist.empty:
            return _err(f"{symbol} 无历史K线(可能停牌/不支持的板块)")
        mf_days = int(self.cfg["run"].get("mainflow_days", 5))
        tail = hist.tail(20)
        closes = tail["close"].tolist()
        last_bar = hist.iloc[-1]
        last_daily_close = float(last_bar["close"])
        last_daily_date = (
            last_bar["date"].strftime("%Y-%m-%d")
            if hasattr(last_bar["date"], "strftime") else str(last_bar["date"])
        )
        live = data.get_live_quotes([symbol]).get(symbol, {})
        current = live.get("price")
        pc = float(live.get("prev_close") or 0)
        pct_live = round((float(current) / pc - 1) * 100, 2) if current and pc > 0 else None
        ma = {f"ma{n}": (round(float(hist['close'].tail(n).mean()), 3) if len(hist) >= n else None)
              for n in (5, 10, 20)}
        choke, theme = serenity.membership(symbol, None, None, self.cfg)
        ff = compute_factor_frame(hist, data.get_fund_flow(symbol), mf_days, choke)
        factors = {}
        mf_avg = None
        if not ff.empty:
            row = ff.iloc[-1]
            factors = {
                c: (None if row[c] != row[c] else round(float(row[c]), 4))
                for c in FACTOR_NAMES if c != "mainflow"
            }
            mf_avg = round(float(row["mainflow"]), 4) if row.get("mainflow") == row.get("mainflow") else None
        ret5 = round((last_daily_close / float(closes[-6]) - 1) * 100, 2) if len(closes) >= 6 else None
        ret20 = round((last_daily_close / float(tail.iloc[0]["close"]) - 1) * 100, 2)
        name = str(live.get("name") or "")
        if not name:
            spot = data.get_spot().set_index("symbol")
            if symbol in spot.index:
                name = str(spot.loc[symbol].get("name", ""))
        mkt = self._market_overview()
        market_avg = mkt.get("avg_pct") if mkt.get("ok") else None
        sector = data.get_sector_context(symbol, market_avg)
        return _ok(
            symbol=symbol, name=name,
            current_price=round(float(current), 3) if current else None,
            price_source=live.get("source"),
            pct_vs_prev_close=pct_live,
            last_daily_close=round(last_daily_close, 3),
            last_daily_date=last_daily_date,
            ret_5d_pct=ret5, ret_20d_pct=ret20, **ma,
            serenity_theme=theme,
            factors=factors,
            mainflow_ratio_nd_avg_pct=mf_avg,
            mainflow_note=(
                f"近{mf_days}日主力净流入占比均值={mf_avg}% (百分点,不是亿元)"
                if mf_avg is not None else None
            ),
            fund_flow_recent=data.fund_flow_recent(symbol, mf_days),
            sector_context=sector,
            field_legend=data.agent_field_legend(self.cfg),
            recent_closes=[round(float(c), 3) for c in closes[-10:]],
        )

    def _t_get_recent_trades(self, limit: int = 10) -> dict:
        return _ok(trades=portfolio.recent_trades(int(limit)))

    def _t_list_skills(self) -> dict:
        if not self.skills:
            return _ok(skills=[])
        return _ok(skills=self.skills.catalog())

    def _t_load_skill(self, name: str) -> dict:
        if not self.skills:
            return _err("未启用技能系统")
        body = self.skills.load(name)
        if body is None:
            return _err(f"技能不存在: {name}(用 list_skills 查看可用技能)")
        return _ok(name=name, content=body)

    # ---------------- 写操作(过硬风控) ----------------

    def _t_place_limit_order(self, symbol: str, limit_price: float,
                             reason: str = "") -> dict:
        a = self.cfg["account"]
        symbol = str(symbol).strip()
        try:
            limit_price = float(limit_price)
        except (TypeError, ValueError):
            return _err("limit_price 必须为数字")
        if limit_price <= 0:
            return _err("limit_price 必须 > 0")

        # 回撤熔断
        if portfolio.current_drawdown(self._portfolio_px()) >= float(self.cfg["risk"]["max_drawdown"]):
            return _err("账户已触发回撤熔断,暂停建仓(本金保护)")

        # 标的有效性 + 现价(与成交 tick 同源)
        live = data.get_live_quotes([symbol]).get(symbol)
        if not live or not live.get("price"):
            spot = data.get_spot().set_index("symbol")
            if symbol not in spot.index:
                return _err(f"{symbol} 不在全市场快照内(代码错误/停牌/北交所)")
            return _err(f"{symbol} 无法获取实时价,请稍后再试")
        name = str(live.get("name", ""))
        if not name:
            spot = data.get_spot().set_index("symbol")
            if symbol in spot.index:
                name = str(spot.loc[symbol].get("name", ""))
        cur = float(live["price"])
        if cur <= 0:
            return _err(f"{symbol} 无有效现价")
        if not (cur * 0.8 <= limit_price <= cur * 1.1):
            return _err(f"限价 {limit_price} 偏离现价 {cur} 过大(允许区间 现价×[0.8,1.1])")

        px = self._quotes([p["symbol"] for p in portfolio.get_positions("open")])
        ceiling = recommend.affordable_price_ceiling(self.cfg, px)
        if cur > ceiling:
            return _err(f"{symbol} 现价 {cur} 高于可买上限 {round(ceiling,2)}(资金不足买1手)")

        # 持仓/挂单上限 + 去重
        opens = portfolio.get_positions("open")
        pendings = portfolio.get_positions("pending")
        held = {p["symbol"] for p in opens} | {p["symbol"] for p in pendings}
        if symbol in held:
            return _err(f"{symbol} 已持有或已挂单,不重复下单")
        if len(opens) + len(pendings) >= int(a["max_positions"]):
            return _err(f"持仓+挂单已达上限 {a['max_positions']},不再新开")

        hold = int(self.cfg.get("agent", {}).get("signal_eval_days")
                 or self.cfg["run"]["holding_days"])
        rec = {
            "rec_date": self.today, "symbol": symbol, "name": name,
            "entry_close": round(cur, 3), "limit_price": round(limit_price, 2),
            "score": None, "holding_days": hold,
            "target": float(self.cfg["run"]["success_threshold"]),
            "stop_loss": float(self.cfg["run"]["stop_loss"]),
            "reason": json.dumps({"by": "agent", "rationale": reason}, ensure_ascii=False),
        }
        store.add_recommendation(rec)                 # 写台账(供复盘评估)
        added = portfolio.add_pending(rec)            # 挂限价买单
        if not added:
            return _err(f"{symbol} 当日已存在挂单记录")
        self.actions.append({"type": "place_order", "symbol": symbol, "name": name,
                             "limit_price": round(limit_price, 2), "reason": reason})
        return _ok(symbol=symbol, name=name, limit_price=round(limit_price, 2),
                   current_price=round(cur, 3), price_source=live.get("source"),
                   msg="限价买单已挂,现价回调到目标价才成交")

    def _t_cancel_order(self, symbol: str) -> dict:
        symbol = str(symbol).strip()
        for p in portfolio.get_positions("pending"):
            if p["symbol"] == symbol:
                portfolio.cancel_pending(p["id"])
                self.actions.append({"type": "cancel_order", "symbol": symbol})
                return _ok(symbol=symbol, msg="挂单已撤销")
        return _err(f"{symbol} 无待成交挂单")

    def _t_close_position(self, symbol: str, reason: str = "") -> dict:
        symbol = str(symbol).strip()
        pos = next((p for p in portfolio.get_positions("open") if p["symbol"] == symbol), None)
        if pos is None:
            return _err(f"{symbol} 不在持仓中")
        if pos["entry_date"] >= self.today:           # T+1
            return _err(f"{symbol} 当日买入,T+1 次日才可卖出")
        px = self._quotes([symbol]).get(symbol)
        if not px:
            return _err(f"{symbol} 未取到实时价,稍后再试")
        closed = portfolio.close_position(pos, px, self.today, "agent", self.cfg)
        self.actions.append({"type": "close", "symbol": symbol,
                             "exit_price": closed["exit_price"], "ret": closed["ret"],
                             "reason": reason})
        return _ok(symbol=symbol, exit_price=closed["exit_price"],
                   pnl=closed["pnl"], return_pct=round(closed["ret"] * 100, 2),
                   msg="已平仓")

    def _t_reduce_position(self, symbol: str, shares: int, reason: str = "") -> dict:
        """部分减仓(整手);shares 等于全部持仓时等同全平。"""
        symbol = str(symbol).strip()
        pos = next((p for p in portfolio.get_positions("open") if p["symbol"] == symbol), None)
        if pos is None:
            return _err(f"{symbol} 不在持仓中")
        if pos["entry_date"] >= self.today:
            return _err(f"{symbol} 当日买入,T+1 次日才可卖出")
        px = self._quotes([symbol]).get(symbol)
        if not px:
            return _err(f"{symbol} 未取到实时价,稍后再试")
        try:
            shares = int(shares)
            result = portfolio.reduce_position(pos, shares, px, self.today, "agent", self.cfg)
        except ValueError as e:
            return _err(str(e))
        self.actions.append({"type": "reduce", "symbol": symbol, "shares": result.get("shares", shares),
                             "exit_price": result["exit_price"], "ret": result["ret"], "reason": reason})
        return _ok(symbol=symbol, shares=result.get("shares"), exit_price=result["exit_price"],
                   pnl=result["pnl"], return_pct=round(result["ret"] * 100, 2),
                   remaining=result.get("remaining"), msg="已部分减仓")

    def _t_add_to_position(self, symbol: str, limit_price: float, reason: str = "") -> dict:
        """对已持仓标的加仓;现价≤限价时按单仓上限与现金预算买入整手。"""
        symbol = str(symbol).strip()
        pos = next((p for p in portfolio.get_positions("open") if p["symbol"] == symbol), None)
        if pos is None:
            return _err(f"{symbol} 不在持仓中,请用 place_limit_order 新建仓")
        try:
            limit_price = float(limit_price)
        except (TypeError, ValueError):
            return _err("limit_price 必须为数字")
        if limit_price <= 0:
            return _err("limit_price 必须 > 0")
        if portfolio.current_drawdown(self._portfolio_px()) >= float(self.cfg["risk"]["max_drawdown"]):
            return _err("账户已触发回撤熔断,暂停加仓")

        cur = self._quotes([symbol]).get(symbol)
        if not cur:
            return _err(f"{symbol} 未取到实时价,稍后再试")
        if not (cur * 0.8 <= limit_price <= cur * 1.1):
            return _err(f"限价 {limit_price} 偏离现价 {cur} 过大(允许区间 现价×[0.8,1.1])")
        if cur > limit_price:
            return _ok(symbol=symbol, current_price=cur, limit_price=round(limit_price, 2),
                       msg=f"等待回调:现价 {cur:.2f} > 目标加仓价 {limit_price:.2f}")

        add_n, err = self._calc_add_shares(symbol, cur, pos)
        if err:
            return _err(err)
        try:
            result = portfolio.add_shares(pos, add_n, cur, self.today, self.cfg)
        except ValueError as e:
            return _err(str(e))
        self.actions.append({"type": "add", "symbol": symbol, "name": pos["name"],
                             "shares": add_n, "price": result["price"], "reason": reason})
        return _ok(symbol=symbol, added_shares=add_n, price=result["price"],
                   new_shares=result["new_shares"], msg="已加仓")

    def _calc_add_shares(self, symbol: str, price: float, pos: dict) -> tuple[int, str | None]:
        """计算可加仓股数(整手);受单仓上限与现金约束。"""
        a = self.cfg["account"]
        lot = int(a["lot_size"])
        px = self._portfolio_px()
        eq = portfolio.equity(px)
        max_frac = float(a.get("max_position_fraction", a["position_fraction"]))
        cap = eq * max_frac
        current_mv = int(pos["shares"]) * price
        room = cap - current_mv
        if room < price * lot:
            return 0, f"单仓已达上限(≈{cap:,.0f}元),无法再加仓"
        cash = portfolio.get_account()["cash"]
        budget = min(cash * 0.999, room, eq * float(a["position_fraction"]))
        add_n = int(math.floor(budget / (price * lot)) * lot)
        if add_n < lot:
            return 0, f"预算不足1手(可用≈{budget:,.0f}元,1手需≈{price * lot:,.0f}元)"
        return add_n, None

    def _t_remember(self, note: str, tags: list[str] | None = None) -> dict:
        note = str(note).strip()
        if not note:
            return _err("note 不能为空")
        memory.add_note(note, tags)
        self.actions.append({"type": "remember", "note": note})
        return _ok(msg="已记入跨日复盘记忆")

    # ---------------- 内部:实时报价(带本周期缓存) ----------------

    def _portfolio_px(self) -> dict[str, float]:
        return self._quotes([p["symbol"] for p in portfolio.get_positions("open")])

    def _quotes(self, symbols: list[str]) -> dict[str, float]:
        need = [s for s in symbols if s not in self._price_cache]
        if need:
            for s, q in data.get_live_quotes(need).items():
                self._price_cache[s] = float(q["price"])
        return {s: self._price_cache[s] for s in symbols if s in self._price_cache}


_BASE_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "get_portfolio_state",
        "description": "查看虚拟账户;持仓含行业板块涨跌(board_pct)与相对大盘强弱(board_vs_market_pct)。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_market_overview",
        "description": "全市场实时广度:涨跌家数/均涨跌幅(新浪实时快照,盘中刷新);含 as_of 时间戳。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_candidates",
        "description": "候选股列表:close/current_price=实时价;mainflow_ratio_nd_avg_pct=占比(%);详见 field_legend。",
        "parameters": {"type": "object", "properties": {
            "topn": {"type": "integer", "description": "返回数量上限,默认取配置 candidates_topn"}}},
    }},
    {"type": "function", "function": {
        "name": "get_sector_context",
        "description": "个股所属行业板块涨跌、板块内涨跌家数、个股相对板块/大盘强弱;止盈止损须与大盘一并参考。",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "6位A股代码"}},
            "required": ["symbol"]},
    }},
    {"type": "function", "function": {
        "name": "get_stock_detail",
        "description": "个股详情:现价、板块(sector_context)、资金流、因子与均线;卖出决策须看板块不单看大盘。",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "6位A股代码,如 600519"}},
            "required": ["symbol"]},
    }},
    {"type": "function", "function": {
        "name": "get_recent_trades",
        "description": "查看最近已平仓交易(用于复盘反思)。",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "返回数量,默认10"}}},
    }},
    {"type": "function", "function": {
        "name": "list_skills",
        "description": "列出可用技能(name+description)。需要某个分析框架时先列出再 load_skill 载入。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "load_skill",
        "description": "载入指定技能的完整内容(分析框架/决策指引)到上下文,据此分析。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "place_limit_order",
        "description": "对某只股票挂限价买单(现价回调到目标价才成交)。会自动做风控校验,失败会返回原因。",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "6位A股代码"},
            "limit_price": {"type": "number", "description": "目标买入价(应≤现价、在现价±10%内)"},
            "reason": {"type": "string", "description": "买入理由(简短)"}},
            "required": ["symbol", "limit_price"]},
    }},
    {"type": "function", "function": {
        "name": "cancel_order",
        "description": "撤销某只股票的待成交限价买单。",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"}}, "required": ["symbol"]},
    }},
    {"type": "function", "function": {
        "name": "close_position",
        "description": "按当前实时价全部平仓(自由裁量止盈/止损;硬止损由系统自动执行)。受T+1约束。",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"},
            "reason": {"type": "string", "description": "卖出理由(简短)"}},
            "required": ["symbol"]},
    }},
    {"type": "function", "function": {
        "name": "reduce_position",
        "description": "部分减仓(整手);用于分批止盈/控风险。shares 等于全部持仓时等同全平。受T+1约束。",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"},
            "shares": {"type": "integer", "description": "卖出股数(须为100整数倍)"},
            "reason": {"type": "string", "description": "减仓理由(简短)"}},
            "required": ["symbol", "shares"]},
    }},
    {"type": "function", "function": {
        "name": "add_to_position",
        "description": "对已持仓标的加仓(现价≤限价时成交);受单仓上限与现金约束,须为整手。",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "已在持仓中的6位代码"},
            "limit_price": {"type": "number", "description": "目标加仓价(现价≤该价才买入)"},
            "reason": {"type": "string", "description": "加仓理由(简短)"}},
            "required": ["symbol", "limit_price"]},
    }},
    {"type": "function", "function": {
        "name": "remember",
        "description": "把一条经验教训写入跨日记忆,未来开盘复盘会回灌(例:某类形态止损偏晚)。",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}}},
            "required": ["note"]},
    }},
]
