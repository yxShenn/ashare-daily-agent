# 技能体系(skills/)

Agent(`run_agent.py`)可加载的可扩展"决策框架"。每个技能是一个子目录,里面放一个
`SKILL.md`。Agent 启动时只读各技能的 frontmatter 做目录(`list_skills`),真正需要时才
`load_skill` 读全文注入上下文(**懒加载**,不拖慢启动)。

## 目录结构

```
skills/
  <skill-name>/
    SKILL.md        # 必需:frontmatter(name/description) + 正文(决策指引)
    tools.py        # 可选:给 agent 注册额外的【只读】信号工具
```

## SKILL.md 写法

开头用 YAML frontmatter 声明元信息,其后正文是给 LLM 看的决策指引:

```markdown
---
name: my-skill
description: 一句话说明这个技能解决什么问题、什么时候该被载入。
---

# 标题
正文:框架步骤、判断标准、与系统硬规则的关系……
```

- `name`:技能唯一标识(`load_skill` 用它载入);缺省用目录名。
- `description`:写清楚"何时该用",Agent 据此决定要不要 load。

## 可选 tools.py(进阶)

若技能想给 Agent 提供额外的**只读**信号工具(如自定义资金面指标),在同目录建 `tools.py`:

```python
# 模块级:OpenAI function-calling 风格的工具 schema 列表
TOOLS = [{
    "type": "function",
    "function": {
        "name": "my_signal",
        "description": "返回某只股票的自定义信号",
        "parameters": {"type": "object",
                       "properties": {"symbol": {"type": "string"}},
                       "required": ["symbol"]},
    },
}]

def call(name: str, args: dict, toolkit) -> dict:
    """name 命中本技能的工具时被调用。toolkit 可复用 toolkit._quotes 等。"""
    if name == "my_signal":
        return {"ok": True, "symbol": args["symbol"], "signal": 0.0}
    return {"ok": False, "error": f"未知工具 {name}"}
```

约定:
- 技能工具应是**只读**的(查询信号),写操作(下单/平仓)统一走核心工具,确保硬风控不被绕过。
- `tools.py` 仅在 Agent 首次需要工具列表时才被导入(懒加载),单个技能出错不影响整体。
