"""Agent 层:用 LLM(DeepSeek)主导选股/择时/出场决策。

把现有确定性引擎(选股/因子/账户/交易)封装为受硬风控约束的 tools,
并支持懒加载、可扩展的 SKILL.md 技能体系。现有 run_live.py 流程不受影响。
"""
