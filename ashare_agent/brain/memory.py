"""跨日复盘记忆:把 agent 的经验教训持久化到 data/agent_memory.jsonl。

开盘复盘阶段把最近若干条回灌进上下文,让 agent "记住"过往教训(轻量自我进化)。
"""
from __future__ import annotations

import datetime as _dt
import json

from ..config import DATA_DIR, ensure_dirs

MEM_PATH = DATA_DIR / "agent_memory.jsonl"


def add_note(note: str, tags: list[str] | None = None) -> None:
    ensure_dirs()
    rec = {
        "ts": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": str(note).strip(),
        "tags": tags or [],
    }
    with open(MEM_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def recent(limit: int = 20) -> list[dict]:
    if not MEM_PATH.exists():
        return []
    out: list[dict] = []
    for line in MEM_PATH.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
