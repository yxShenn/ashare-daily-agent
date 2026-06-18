"""ReAct 决策循环:组装提示 + 工具 schema -> 调 DeepSeek -> 执行 tool calls -> 迭代产出决策。

全程把每一步写入 data/agent_trace/<today>.jsonl(可解释性 + 日报取数)。
LLM 不可用时(无 key / 调用失败)优雅降级:返回 available=False,由入口回退确定性逻辑。
"""
from __future__ import annotations

import datetime as _dt
import json

from ..config import DATA_DIR, ensure_dirs
from . import memory, prompts
from .llm import LLMClient, LLMError, available
from .skills import SkillManager
from .tools import ToolKit

TRACE_DIR = DATA_DIR / "agent_trace"


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

    def select(self, today: str, slots: int) -> dict:
        if slots <= 0:
            return {"available": self.available, "skipped": "无可用建仓名额", "actions": []}
        return self._run("select", today, prompts.SELECT.format(today=today, slots=slots))

    def intraday_exit(self, today: str) -> dict:
        return self._run("exit", today, prompts.EXIT.format(today=today))

    # ---------------- ReAct 主循环 ----------------

    def _run(self, phase: str, today: str, user_prompt: str) -> dict:
        client = self._client_or_none()
        if client is None:
            return {"available": False, "actions": []}

        toolkit = ToolKit(self.cfg, today, self.skills)
        messages = [
            {"role": "system", "content": prompts.SYSTEM},
            {"role": "user", "content": user_prompt},
        ]
        events: list[dict] = [{"phase": phase, "today": today, "prompt": user_prompt}]
        final_text = ""
        try:
            for _ in range(self.max_iters):
                msg = client.chat(messages, tools=toolkit.schemas())
                if not getattr(msg, "tool_calls", None):
                    final_text = (msg.content or "").strip()
                    events.append({"assistant": final_text})
                    break
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
                    result = toolkit.call(name, args)
                    events.append({"tool": name, "args": args, "result": result})
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            else:
                final_text = "(达到最大迭代轮数,已停止)"
                events.append({"assistant": final_text})
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
