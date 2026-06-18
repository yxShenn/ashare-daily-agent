"""技能加载器:扫描 skills/ 下的 SKILL.md(frontmatter: name/description),懒加载全文。

技能默认是 markdown 决策框架:agent 先 list_skills 看目录,需要时 load_skill 注入全文。
可选在同目录放 tools.py 注册额外只读工具(模块级 TOOLS=[schema...] + call(name,args,toolkit)),
首次用到才导入(懒加载),不拖慢启动。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from ..config import ROOT


class SkillManager:
    def __init__(self, cfg: dict):
        self.dir = ROOT / cfg.get("agent", {}).get("skills_dir", "skills")
        self._catalog: list[dict] | None = None    # [{name, description, _path}]
        self._bodies: dict[str, str] = {}
        self._tool_schemas: list[dict] | None = None
        self._tool_index: dict[str, object] = {}

    # ---------------- 目录(只读 frontmatter,懒加载) ----------------

    def _scan(self) -> None:
        if self._catalog is not None:
            return
        cat: list[dict] = []
        if self.dir.is_dir():
            for sub in sorted(self.dir.iterdir()):
                if not sub.is_dir():
                    continue
                md = sub / "SKILL.md"
                if not md.exists():
                    continue
                meta = self._frontmatter(md)
                cat.append({
                    "name": meta.get("name") or sub.name,
                    "description": meta.get("description", ""),
                    "_path": md,
                })
        self._catalog = cat

    @staticmethod
    def _frontmatter(md: Path) -> dict:
        text = md.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                import yaml
                try:
                    return yaml.safe_load(text[3:end]) or {}
                except Exception:
                    return {}
        return {}

    def catalog(self) -> list[dict]:
        self._scan()
        return [{"name": c["name"], "description": c["description"]} for c in self._catalog]

    def load(self, name: str) -> str | None:
        """返回技能全文(懒加载并缓存);不存在返回 None。"""
        self._scan()
        if name in self._bodies:
            return self._bodies[name]
        for c in self._catalog:
            if c["name"] == name:
                body = c["_path"].read_text(encoding="utf-8")
                self._bodies[name] = body
                return body
        return None

    # ---------------- 技能自带工具(可选,懒导入) ----------------

    def _ensure_tools(self) -> None:
        if self._tool_schemas is not None:
            return
        self._scan()
        schemas: list[dict] = []
        index: dict[str, object] = {}
        for c in self._catalog:
            tpy = c["_path"].parent / "tools.py"
            if not tpy.exists():
                continue
            try:
                mod = self._import_module(tpy, c["name"])
            except Exception:
                continue                                  # 单个技能工具坏掉不影响整体
            for sch in (getattr(mod, "TOOLS", None) or []):
                fname = sch.get("function", {}).get("name")
                if fname:
                    index[fname] = mod
                    schemas.append(sch)
        self._tool_schemas = schemas
        self._tool_index = index

    @staticmethod
    def _import_module(path: Path, skill_name: str):
        safe = "".join(ch if ch.isalnum() else "_" for ch in skill_name)
        spec = importlib.util.spec_from_file_location(f"ashare_skill_{safe}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def tool_schemas(self) -> list[dict]:
        self._ensure_tools()
        return self._tool_schemas or []

    def has_tool(self, name: str) -> bool:
        self._ensure_tools()
        return name in self._tool_index

    def call_tool(self, name: str, args: dict, toolkit) -> dict:
        self._ensure_tools()
        mod = self._tool_index[name]
        return mod.call(name, args or {}, toolkit)
