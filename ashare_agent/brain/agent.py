"""ReAct 决策循环:组装提示 + 工具 schema -> 调 DeepSeek -> 执行 tool calls -> 迭代产出决策。

全程把每一步写入 data/agent_trace/<today>.jsonl(可解释性 + 日报取数)。
LLM 不可用时(无 key / 调用失败)优雅降级:返回 available=False,由入口回退确定性逻辑。
"""
from __future__ import annotations

import datetime as _dt
import json
import time

from ..config import DATA_DIR, ensure_dirs
from .. import portfolio
from . import memory, prompts
from .llm import LLMClient, LLMError, available
from .skills import SkillManager
from .tools import ToolKit

TRACE_DIR = DATA_DIR / "agent_trace"
_PHASE_ZH = {"review": "开盘复盘", "trade": "自主交易"}


def _tool_brief(name: str, result: dict) -> str:
    """工具结果一行摘要(供进度打印,避免刷屏)。"""
    if result.get("ok") is False:
        err = str(result.get("error", "?"))
        return f"失败: {err[:60]}{'…' if len(err) > 60 else ''}"
    if name == "get_candidates":
        return f"ok, {len(result.get('candidates') or [])} 只候选"
    if name == "get_stock_detail":
        px = result.get("current_price")
        return f"ok, {result.get('symbol')} 现价 {px}"
    if name == "get_portfolio_state":
        return f"ok, 权益 {result.get('equity')} 元, 持仓 {result.get('open_count')}"
    if name == "get_market_overview":
        return f"ok, 涨 {result.get('up')}/跌 {result.get('down')}"
    if name == "load_skill":
        return f"ok, 已载入 {result.get('name')}"
    if name == "list_skills":
        return f"ok, {len(result.get('skills') or [])} 个技能"
    if name in ("place_limit_order", "close_position", "reduce_position",
                "add_to_position", "cancel_order"):
        return f"ok, {result.get('msg', '已执行')}"
    if name == "remember":
        return "ok, 已记入记忆"
    if name == "get_recent_trades":
        return f"ok, {len(result.get('trades') or [])} 笔历史"
    return "ok"


def _strip_skill_body(text: str) -> str:
    """去掉 SKILL.md frontmatter,只保留正文。"""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def _system_prompt(skills: SkillManager) -> str:
    """系统提示 + 强制注入 data-accuracy(不依赖 LLM 是否调用 load_skill)。"""
    base = prompts.SYSTEM
    body = skills.load("data-accuracy")
    if not body:
        return base
    return f"{base}\n\n---\n## data-accuracy(系统强制载入,写操作前必读)\n{_strip_skill_body(body)}"


class TradingAgent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.acfg = cfg.get("agent", {})
        self.max_iters = int(self.acfg.get("max_iters", 12))
        self.skills = SkillManager(cfg)
        self.available = bool(self.acfg.get("enabled", True)) and available()
        self._client: LLMClient | None = None
        self.all_actions: list[dict] = []      # 全天累计的写操作(供日报)
        self.summaries: dict[str, str] = {}    # phase -> 决策总结文本

    def _client_or_none(self) -> LLMClient | None:
        if self._client is None:
            try:
                self._client = LLMClient(self.cfg)
            except LLMError as e:
                print(f"[agent] LLM 不可用,降级为确定性逻辑:{e}", flush=True)
                self.available = False
                return None
        return self._client

    # ---------------- 三个决策阶段 ----------------

    def review(self, today: str) -> dict:
        mem = memory.recent(10)
        mem_txt = "\n".join(f"- {m['ts']}: {m['note']}" for m in mem) or "(暂无历史记忆)"
        return self._run("review", today, prompts.REVIEW.format(today=today, memory=mem_txt))

    def trade(self, today: str, slots: int) -> dict:
        """自主交易:调研后决定买卖/挂撤单(无固定时刻与持有期)。"""
        opens = portfolio.get_positions("open")
        if slots <= 0 and not opens:
            return {"available": self.available, "skipped": "无持仓且无建仓名额", "actions": []}
        return self._run("trade", today, prompts.TRADE.format(today=today, slots=slots))

    def intraday_manage(self, today: str, slots: int) -> dict:
        return self.trade(today, slots)

    def select(self, today: str, slots: int) -> dict:
        return self.trade(today, slots)

    def intraday_exit(self, today: str) -> dict:
        return self.trade(today, 0)

    # ---------------- ReAct 主循环 ----------------

    def _run(self, phase: str, today: str, user_prompt: str) -> dict:
        client = self._client_or_none()
        if client is None:
            return {"available": False, "actions": []}

        toolkit = ToolKit(self.cfg, today, self.skills)
        phase_zh = _PHASE_ZH.get(phase, phase)
        messages = [
            {"role": "system", "content": _system_prompt(self.skills)},
            {"role": "user", "content": user_prompt},
        ]
        events: list[dict] = [{"phase": phase, "today": today, "prompt": user_prompt}]
        final_text = ""
        try:
            for i in range(self.max_iters):
                print(f"  [{phase_zh}] 第 {i + 1}/{self.max_iters} 轮: "
                      f"正在请求 DeepSeek …", flush=True)
                t_llm = time.perf_counter()
                msg = client.chat(messages, tools=toolkit.schemas())
                llm_sec = time.perf_counter() - t_llm
                if not getattr(msg, "tool_calls", None):
                    final_text = (msg.content or "").strip()
                    events.append({"assistant": final_text})
                    print(f"  [{phase_zh}] DeepSeek 回复 ({llm_sec:.1f}s), 决策完成",
                          flush=True)
                    break
                names = [tc.function.name for tc in msg.tool_calls]
                print(f"  [{phase_zh}] DeepSeek 回复 ({llm_sec:.1f}s), "
                      f"执行工具: {', '.join(names)}", flush=True)
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [{
                        "id": tc.id, "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    } for tc in msg.tool_calls],
                })
                for tc in msg.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    arg_hint = args.get("symbol") or args.get("name") or args.get("topn")
                    if arg_hint is not None:
                        print(f"  [{phase_zh}]   → {name}({arg_hint}) …", flush=True)
                    else:
                        print(f"  [{phase_zh}]   → {name} …", flush=True)
                    t_tool = time.perf_counter()
                    result = toolkit.call(name, args)
                    tool_sec = time.perf_counter() - t_tool
                    print(f"  [{phase_zh}]   ← {name} ({tool_sec:.1f}s): "
                          f"{_tool_brief(name, result)}", flush=True)
                    events.append({"tool": name, "args": args, "result": result})
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            else:
                final_text = "(达到最大迭代轮数,已停止)"
                events.append({"assistant": final_text})
                print(f"  [{phase_zh}] 已达最大迭代 {self.max_iters} 轮,停止", flush=True)
        except LLMError as e:
            print(f"[agent] {phase} 阶段 LLM 调用失败,降级:{e}", flush=True)
            self._trace(today, events + [{"error": str(e)}])
            return {"available": False, "actions": toolkit.actions, "error": str(e)}

        self._trace(today, events)
        self.all_actions.extend(toolkit.actions)
        if final_text:
            self.summaries[phase] = final_text
        return {"available": True, "actions": toolkit.actions, "summary": final_text}

    @staticmethod
    def _trace(today: str, events: list[dict]) -> None:
        ensure_dirs()
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        with open(TRACE_DIR / f"{today}.jsonl", "a", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps({"t": ts, **ev}, ensure_ascii=False) + "\n")
