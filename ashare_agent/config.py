"""配置加载:合并 config.yaml 默认配置与优化器写出的 active_config.json。"""
from __future__ import annotations

import json
import copy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
REPORT_DIR = DATA_DIR / "reports"
DB_PATH = DATA_DIR / "journal.db"
ACTIVE_CONFIG_PATH = DATA_DIR / "active_config.json"
STRATEGY_HISTORY_PATH = DATA_DIR / "strategy_history.jsonl"
CONFIG_PATH = ROOT / "config.yaml"


def ensure_dirs() -> None:
    """惰性创建运行期目录。"""
    for d in (DATA_DIR, CACHE_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """加载默认配置,并用 active_config.json 中已优化的因子权重覆盖。"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if ACTIVE_CONFIG_PATH.exists():
        try:
            active = json.loads(ACTIVE_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(active.get("factors"), dict):
                cfg["factors"].update(active["factors"])
                cfg["_active_meta"] = {k: v for k, v in active.items() if k != "factors"}
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_active_config(factors: dict, meta: dict) -> None:
    """优化器写出当前最优因子权重及其样本外指标(meta)。"""
    ensure_dirs()
    from datetime import datetime

    payload = {
        "factors": factors,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **meta,
    }
    ACTIVE_CONFIG_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with open(STRATEGY_HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def default_factor_weights(cfg: dict) -> dict:
    return copy.deepcopy(cfg["factors"])
