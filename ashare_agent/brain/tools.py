"""Agent 工具集:把现有确定性引擎封装为受硬风控约束的 function-calling tools。

设计要点:
- 全部复用现有模块(data/universe/factors/strategy/recommend/portfolio/paper_trade/exits),不重复实现逻辑。
- 写操作(下单/撤单/平仓)一律先过硬风控,校验失败把错误**回传给 LLM**(而非抛异常),让其改决策。
- 工具结果均为可 JSON 序列化的 dict;LLM 不可绕过代码层的本金保护。
"""
from __future__ import annotations

import datetime as _dt
import json

from .. import data, portfolio, recommend, store
from . import memory


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def _ok(**kw) -> dict:
    return {"ok": True, **kw}


class ToolKit:
    """一次决策周期内复用。持有 cfg/today/skills,并记录已执行的写操作(供日报)。"""

    def __init__(self, cfg: dict, today: str, skills):
        self.cfg = cfg
        self.today = today
        self.skills = skills
        self.actions: list[dict] = []          # 本次决策实际发生的写操作
        self._price_cache: dict[str, float] = {}

    # ---------------- 工具 schema(OpenAI function calling 格式) ----------------

    def schemas(self) -> list[dict]:
        skill_tools = self.skills.tool_schemas() if self.skills else []
        return _BASE_SCHEMAS + skill_tools

    # ---------------- 调用分发 ----------------

    def call(self, name: str, args: dict) -> dict:
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
        positions = []
        for p in opens:
            cur = px.get(p["symbol"])
            fp = (cur / float(p["entry_price"]) - 1) if cur and p["entry_price"] else None
            positions.append({
                "symbol": p["symbol"], "name": p["name"], "shares": int(p["shares"]),
                "entry_price": float(p["entry_price"]), "entry_date": p["entry_date"],
                "days_held": int(p["days_held"]),
                "current_price": cur,
                "float_return_pct": None if fp is None else round(fp * 100, 2),
                "sellable_today": p["entry_date"] < self.today,   # T+1
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
        )

    def _t_get_market_overview(self) -> dict:
        spot = data.get_spot()
        df = spot[spot["exchange"] != "bj"]
        pct = df["pct"].dropna()
        up = int((pct > 0).sum())
        down = int((pct < 0).sum())
        limit_up = int((pct >= 9.8).sum())
        return _ok(
            total=int(len(pct)), up=up, down=down, flat=int(len(pct) - up - down),
            limit_up_approx=limit_up,
            avg_pct=round(float(pct.mean()), 2) if len(pct) else None,
            median_pct=round(float(pct.median()), 2) if len(pct) else None,
            total_amount_yi=round(float(df["amount"].sum()) / 1e8, 1),
        )

    def _t_get_candidates(self, topn: int | None = None) -> dict:
        n = int(topn or self.cfg.get("agent", {}).get("candidates_topn", 25))
        cands = recommend.rank_candidates(self.cfg)
        ceiling = recommend.affordable_price_ceiling(self.cfg)
        return _ok(
            affordable_price_ceiling=round(ceiling, 2),
            note="score 为确定性多因子打分(仅供参考,你可自由取舍);close 已过可买性过滤",
            candidates=cands[:n],
        )

    def _t_get_stock_detail(self, symbol: str) -> dict:
        from ..factors import FACTOR_NAMES, compute_factor_frame
        from .. import serenity

        symbol = str(symbol).strip()
        hist = data.get_hist(symbol, self.cfg["hist"]["lookback_days"], self.cfg["hist"]["adjust"])
        if hist.empty:
            return _err(f"{symbol} 无历史K线(可能停牌/不支持的板块)")
        tail = hist.tail(20)
        closes = tail["close"].tolist()
        last = float(closes[-1])
        ma = {f"ma{n}": (round(float(hist['close'].tail(n).mean()), 3) if len(hist) >= n else None)
              for n in (5, 10, 20)}
        choke, theme = serenity.membership(symbol, None, None, self.cfg)
        ff = compute_factor_frame(hist, data.get_fund_flow(symbol),
                                  int(self.cfg["run"].get("mainflow_days", 5)), choke)
        factors = {}
        if not ff.empty:
            row = ff.iloc[-1]
            factors = {c: (None if row[c] != row[c] else round(float(row[c]), 4)) for c in FACTOR_NAMES}
        ret5 = round((last / float(closes[-6]) - 1) * 100, 2) if len(closes) >= 6 else None
        ret20 = round((last / float(tail.iloc[0]["close"]) - 1) * 100, 2)
        return _ok(
            symbol=symbol, last_close=last,
            ret_5d_pct=ret5, ret_20d_pct=ret20, **ma,
            serenity_theme=theme, factors=factors,
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
        if portfolio.current_drawdown(self._quotes([])) >= float(self.cfg["risk"]["max_drawdown"]):
            return _err("账户已触发回撤熔断,暂停建仓(本金保护)")

        # 标的有效性 + 现价(用于限价合理性校验)
        spot = data.get_spot().set_index("symbol")
        if symbol not in spot.index:
            return _err(f"{symbol} 不在全市场快照内(代码错误/停牌/北交所)")
        info = spot.loc[symbol]
        name = str(info.get("name", ""))
        cur = float(info.get("close") or 0)
        if cur <= 0:
            return _err(f"{symbol} 无有效现价")
        if not (cur * 0.8 <= limit_price <= cur * 1.1):
            return _err(f"限价 {limit_price} 偏离现价 {cur} 过大(允许区间 现价×[0.8,1.1])")

        # 可买性
        ceiling = recommend.affordable_price_ceiling(self.cfg)
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

        rec = {
            "rec_date": self.today, "symbol": symbol, "name": name,
            "entry_close": round(cur, 3), "limit_price": round(limit_price, 2),
            "score": None, "holding_days": int(self.cfg["run"]["holding_days"]),
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
                   current_price=cur, msg="限价买单已挂,现价回调到目标价才成交")

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

    def _t_remember(self, note: str, tags: list[str] | None = None) -> dict:
        note = str(note).strip()
        if not note:
            return _err("note 不能为空")
        memory.add_note(note, tags)
        self.actions.append({"type": "remember", "note": note})
        return _ok(msg="已记入跨日复盘记忆")

    # ---------------- 内部:实时报价(带本周期缓存) ----------------

    def _quotes(self, symbols: list[str]) -> dict[str, float]:
        need = [s for s in symbols if s not in self._price_cache]
        if need:
            for s, q in data.get_realtime(need).items():
                self._price_cache[s] = q["price"]
        return {s: self._price_cache[s] for s in symbols if s in self._price_cache}


_BASE_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "get_portfolio_state",
        "description": "查看虚拟账户当前状态:现金、总权益、回撤、持仓(含浮盈/是否可卖T+1)、挂单。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_market_overview",
        "description": "全市场情绪概览:涨跌家数、平均涨跌幅、涨停数、总成交额。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_candidates",
        "description": "获取经风控+可买性过滤后的候选股列表及其多因子打分(确定性引擎产出,供你参考,你可自由取舍)。",
        "parameters": {"type": "object", "properties": {
            "topn": {"type": "integer", "description": "返回数量上限,默认取配置 candidates_topn"}}},
    }},
    {"type": "function", "function": {
        "name": "get_stock_detail",
        "description": "查看单只股票细节:最近收盘、5/20日涨幅、均线、各因子值、卡脖子赛道标签、近10日收盘序列。",
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
        "description": "按当前实时价平掉某只持仓(自由裁量出场;硬止损由系统自动执行,无需你操作)。受T+1约束。",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"},
            "reason": {"type": "string", "description": "卖出理由(简短)"}},
            "required": ["symbol"]},
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
