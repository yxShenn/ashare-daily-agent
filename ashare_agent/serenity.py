"""Serenity 卡脖子赛道:成员判定(供因子化使用)。

把 serenity-stock-choke 框架"沿产业链上溯找供给受限瓶颈环节"的思想,落地为
一个**可优化的因子**:个股是否属于卡脖子赛道(0/1 成员身份),由 config.yaml 的
serenity 段(定性清单)判定;其**权重不在此处人工设定**,而是作为 factors.serenity
纳入 walk-forward 优化,由优化器根据样本外迭代结果自动学习与调整。

人工只提供"哪些算卡脖子赛道"(行业/赛道关键词 + 锁定标的),数值权重全自动。
"""
from __future__ import annotations


def membership(symbol: str | None, name: str | None, industry: str | None,
               cfg: dict) -> tuple[float, str | None]:
    """返回 (成员身份 0/1, 命中标签)。未启用或未命中返回 (0.0, None)。

    判定:① 在 serenity.symbols 锁定清单 → 成员;② 名称[+行业]命中 serenity.themes 关键词。
    """
    sr = cfg.get("serenity", {})
    if not sr.get("enabled", False):
        return 0.0, None

    symbols = {str(s) for s in (sr.get("symbols") or [])}
    if str(symbol) in symbols:
        return 1.0, "锁定标的"

    text = f"{name or ''} {industry or ''}"
    for theme in (sr.get("themes") or []):
        if theme and str(theme) in text:
            return 1.0, str(theme)
    return 0.0, None
