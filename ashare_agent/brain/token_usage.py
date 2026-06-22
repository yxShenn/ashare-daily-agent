"""LLM Token 用量与费用统计。

- Token 数量:来自 DeepSeek chat/completions 响应 usage 字段(API 实测,非估算)。
- 费用金额:DeepSeek 响应不含 cost 字段,按 API 返回的 cache hit/miss + output token × 官方单价折算。
- 账户余额:GET /user/balance 为 DeepSeek 官方余额;可与会话起始余额对比得到实际扣费。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from ..config import DATA_DIR, ensure_dirs

USAGE_PATH = DATA_DIR / "token_usage.jsonl"
SESSION_PATH = DATA_DIR / "llm_session.json"


def _pricing(cfg: dict) -> dict:
    p = cfg.get("agent", {}).get("token_pricing") or {}
    inp = float(p.get("input_per_million", 1.0))
    return {
        "currency": p.get("currency", "CNY"),
        "input_per_million": inp,
        "output_per_million": float(p.get("output_per_million", 2.0)),
        "input_cache_hit_per_million": float(
            p.get("input_cache_hit_per_million", inp * 0.1)
        ),
    }


def compute_cost(
    cfg: dict,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    completion_tokens: int,
) -> float:
    """按 DeepSeek API 返回的 token 分项 × config 单价折算费用(元)。"""
    pr = _pricing(cfg)
    cost = (
        int(cache_hit_tokens) * pr["input_cache_hit_per_million"]
        + int(cache_miss_tokens) * pr["input_per_million"]
        + int(completion_tokens) * pr["output_per_million"]
    ) / 1_000_000
    return round(cost, 6)


def _balance_root_url(cfg: dict) -> str:
    a = cfg.get("agent", {})
    base = (os.environ.get("DEEPSEEK_BASE_URL")
            or a.get("base_url", "https://api.deepseek.com/v1")).rstrip("/")
    return base.removesuffix("/v1")


def fetch_balance(cfg: dict) -> dict | None:
    """DeepSeek 官方账户余额 GET /user/balance(Bearer API Key)。"""
    from .llm import _ensure_env, available

    _ensure_env()
    if not available():
        return None
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    url = f"{_balance_root_url(cfg)}/user/balance"
    req = Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
    except (URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None

    infos = raw.get("balance_infos") or []
    primary = next((i for i in infos if i.get("currency") == "CNY"), None)
    if primary is None and infos:
        primary = infos[0]
    if not primary:
        return None
    return {
        "is_available": bool(raw.get("is_available", True)),
        "currency": primary.get("currency", "CNY"),
        "total_balance": float(primary.get("total_balance") or 0),
        "granted_balance": float(primary.get("granted_balance") or 0),
        "topped_up_balance": float(primary.get("topped_up_balance") or 0),
        "fetched_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_session_start(cfg: dict, today: str) -> dict | None:
    """记录本次 run_agent 启动时 DeepSeek 余额(用于对比实际扣费)。"""
    bal = fetch_balance(cfg)
    if bal is None:
        return None
    ensure_dirs()
    payload = {
        "date": today,
        "started_at": bal["fetched_at"],
        **bal,
    }
    SESSION_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return payload


def load_session_start() -> dict | None:
    if not SESSION_PATH.exists():
        return None
    try:
        return json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def session_spend(cfg: dict, today: str) -> dict | None:
    """会话起始余额 − 当前余额 = 实际扣费(DeepSeek 官方余额 API)。"""
    start = load_session_start()
    if not start or start.get("date") != today:
        return None
    now = fetch_balance(cfg)
    if now is None:
        return None
    spent = round(float(start["total_balance"]) - float(now["total_balance"]), 4)
    return {
        "currency": now["currency"],
        "session_start_balance": start["total_balance"],
        "current_balance": now["total_balance"],
        "spent": max(0.0, spent),
        "started_at": start.get("started_at"),
        "fetched_at": now["fetched_at"],
    }


def record(
    cfg: dict,
    today: str,
    phase: str,
    model: str,
    usage: dict,
    iter_n: int = 0,
) -> dict:
    """追加一条 API 调用记录(usage 来自 DeepSeek 响应)。"""
    ensure_dirs()
    hit = int(usage.get("cache_hit_tokens", 0))
    miss = int(usage.get("cache_miss_tokens", 0))
    pt = int(usage.get("prompt_tokens", hit + miss))
    ct = int(usage.get("completion_tokens", 0))
    cost = compute_cost(cfg, hit, miss, ct)
    rec = {
        "ts": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": today,
        "phase": phase,
        "model": model,
        "iter": iter_n,
        "prompt_tokens": pt,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "completion_tokens": ct,
        "cached_tokens": hit,  # 兼容旧字段
        "total_tokens": pt + ct,
        "cost": cost,
        "tokens_source": "deepseek_api_usage",
        "cost_source": "tokens_x_official_price",
    }
    with open(USAGE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    day = day_summary(today, cfg)
    day["last_call"] = rec
    return day


def _load_day(date: str) -> list[dict]:
    if not USAGE_PATH.exists():
        return []
    out = []
    for line in USAGE_PATH.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("date") == date:
            out.append(r)
    return out


def day_summary(date: str, cfg: dict) -> dict:
    """指定交易日 token(API 累计) / 费用(单价折算)汇总。"""
    rows = _load_day(date)
    pr = _pricing(cfg)
    pt = sum(r.get("prompt_tokens", 0) for r in rows)
    ct = sum(r.get("completion_tokens", 0) for r in rows)
    hit = sum(r.get("cache_hit_tokens", r.get("cached_tokens", 0)) for r in rows)
    miss = sum(r.get("cache_miss_tokens", 0) for r in rows)
    cost = sum(float(r.get("cost", 0)) for r in rows)
    by_phase: dict[str, dict] = {}
    for r in rows:
        ph = r.get("phase", "?")
        b = by_phase.setdefault(ph, {"calls": 0, "tokens": 0, "cost": 0.0})
        b["calls"] += 1
        b["tokens"] += r.get("total_tokens", 0)
        b["cost"] = round(b["cost"] + float(r.get("cost", 0)), 6)
    out = {
        "date": date,
        "calls": len(rows),
        "prompt_tokens": pt,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "completion_tokens": ct,
        "cached_tokens": hit,
        "total_tokens": pt + ct,
        "cost": round(cost, 4),
        "currency": pr["currency"],
        "tokens_source": "deepseek_api_usage",
        "cost_source": "tokens_x_official_price",
        "by_phase": by_phase,
    }
    spend = session_spend(cfg, date)
    if spend:
        out["balance_spent"] = spend["spent"]
        out["current_balance"] = spend["current_balance"]
    return out


def all_days_summary(cfg: dict, limit: int = 30) -> list[dict]:
    if not USAGE_PATH.exists():
        return []
    dates: list[str] = []
    seen: set[str] = set()
    for line in reversed(USAGE_PATH.read_text(encoding="utf-8").splitlines()):
        try:
            d = json.loads(line).get("date")
        except json.JSONDecodeError:
            continue
        if d and d not in seen:
            seen.add(d)
            dates.append(d)
        if len(dates) >= limit:
            break
    return [day_summary(d, cfg) for d in dates]
