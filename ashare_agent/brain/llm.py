"""DeepSeek LLM 客户端(OpenAI 兼容接口 + function calling)。

密钥从环境变量 DEEPSEEK_API_KEY 懒加载(优先读项目根 .env,绝不入库)。
模型/接口可被 DEEPSEEK_MODEL / DEEPSEEK_BASE_URL 覆盖,否则用 config.yaml 的 agent 配置。
"""
from __future__ import annotations

import os

from ..config import ROOT

_ENV_LOADED = False


def _ensure_env() -> None:
    """懒加载项目根目录 .env(只做一次)。缺 dotenv 时静默跳过,仍可走系统环境变量。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    _ENV_LOADED = True


class LLMError(RuntimeError):
    """LLM 不可用或调用失败(缺 key、缺依赖、网络/接口错误)。"""


def available() -> bool:
    """是否已配置可用的 API key(用于优雅降级判断)。"""
    _ensure_env()
    return bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())


class LLMClient:
    """一次性创建,多次调用。封装"带 tools 的 chat completion"。"""

    def __init__(self, cfg: dict):
        _ensure_env()
        a = cfg.get("agent", {})
        self.model = os.environ.get("DEEPSEEK_MODEL") or a.get("model", "deepseek-chat")
        self.base_url = (os.environ.get("DEEPSEEK_BASE_URL")
                         or a.get("base_url", "https://api.deepseek.com/v1"))
        self.temperature = float(a.get("temperature", 0.3))
        self.timeout = float(a.get("timeout_seconds", 120))

        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise LLMError(
                "未配置 DEEPSEEK_API_KEY。请在项目根目录新建 .env 并填写"
                "(参考 .env.example),或设置同名系统环境变量。"
            )
        try:
            from openai import OpenAI
        except ImportError as e:
            raise LLMError("缺少 openai 库,请先 pip install -r requirements.txt") from e

        self._client = OpenAI(api_key=key, base_url=self.base_url, timeout=self.timeout)

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str = "auto"):
        """发起一次对话,返回 assistant message 对象(可能含 tool_calls)。"""
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:  # 网络/鉴权/限流等统一归一为 LLMError,交由上层降级
            raise LLMError(f"LLM 调用失败: {e}") from e
        return resp.choices[0].message
