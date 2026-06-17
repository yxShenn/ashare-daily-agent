"""数据层:akshare 适配 + 本地缓存(懒加载)。

注意:部分网络环境会封锁东方财富实时行情接口(push2 子域名),因此:
- 全市场快照走新浪源(stock_zh_a_spot),稳定可用;
- 历史K线走东方财富(stock_zh_a_hist),含换手率等字段。
所有重型依赖(akshare)在函数内部惰性导入。
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pandas as pd

from .config import CACHE_DIR, ensure_dirs

_MEM: dict[str, pd.DataFrame] = {}

# 新浪全市场快照列(代码形如 sh600519 / sz000001 / bj920000)
_SINA_RENAME = {
    "代码": "symbol_raw", "名称": "name", "最新价": "close", "涨跌幅": "pct",
    "成交额": "amount", "成交量": "volume", "今开": "open",
    "昨收": "prev_close", "最高": "high", "最低": "low",
}

def _today() -> str:
    return _dt.date.today().strftime("%Y%m%d")


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    return _dt.date.fromtimestamp(path.stat().st_mtime) == _dt.date.today()


def get_spot() -> pd.DataFrame:
    """全A股快照(新浪源)。返回标准化列:symbol/exchange/name/close/pct/amount/...。"""
    key = f"spot_{_today()}"
    if key in _MEM:
        return _MEM[key]
    ensure_dirs()
    path = CACHE_DIR / f"{key}.pkl"
    if _is_fresh(path):
        df = pd.read_pickle(path)
    else:
        import akshare as ak

        raw = ak.stock_zh_a_spot()
        df = raw.rename(columns=_SINA_RENAME)
        df["exchange"] = df["symbol_raw"].str[:2]
        df["symbol"] = df["symbol_raw"].str[2:]
        for c in ("close", "pct", "amount", "volume", "open", "prev_close",
                  "high", "low"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df.to_pickle(path)
    _MEM[key] = df
    return df


def _sina_prefix(symbol: str) -> str | None:
    """6位代码 -> 新浪带市场前缀代码(沪 sh / 深 sz);北交所等不支持返回 None。"""
    if symbol.startswith("6"):
        return "sh" + symbol
    if symbol.startswith(("0", "3")):
        return "sz" + symbol
    return None


def get_realtime(symbols: list[str]) -> dict[str, dict]:
    """批量获取实时报价(新浪 hq.sinajs.cn,盘中轮询用)。

    返回 {symbol: {name, price, open, prev_close, high, low}};停牌或无效价跳过。
    """
    import requests

    codes = [(_sina_prefix(s), s) for s in symbols]
    codes = [(p, s) for p, s in codes if p]
    if not codes:
        return {}
    url = "https://hq.sinajs.cn/list=" + ",".join(p for p, _ in codes)
    headers = {"Referer": "https://finance.sina.com.cn",
               "User-Agent": "Mozilla/5.0"}
    out: dict[str, dict] = {}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        r.encoding = "gbk"
        lines = r.text.strip().split("\n")
    except Exception:
        return out
    by_prefix = {p: s for p, s in codes}
    for line in lines:
        if '="' not in line:
            continue
        head, payload = line.split('="', 1)
        prefix = head.split("hq_str_")[-1]
        sym = by_prefix.get(prefix)
        if sym is None:
            continue
        f = payload.strip('";').split(",")
        if len(f) < 6:
            continue
        try:
            price = float(f[3]) or float(f[2])   # 无现价用昨收
            out[sym] = {
                "name": f[0], "open": float(f[1]), "prev_close": float(f[2]),
                "price": price, "high": float(f[4]), "low": float(f[5]),
            }
        except (ValueError, IndexError):
            continue
    return out


def get_hist(symbol: str, lookback_days: int = 400, adjust: str = "qfq") -> pd.DataFrame:
    """个股日线历史K线(新浪源,标准化英文列,按日期升序)。无数据返回空 DataFrame。

    返回列:date/open/close/high/low/volume/amount/turnover(turnover 为百分比)。
    """
    key = f"hist_{symbol}_{adjust}"
    if key in _MEM:
        return _MEM[key]
    ensure_dirs()
    path = CACHE_DIR / f"hist_{symbol}_{adjust}.pkl"
    if _is_fresh(path):
        df = pd.read_pickle(path)
        _MEM[key] = df
        return df

    prefixed = _sina_prefix(symbol)
    if prefixed is None:
        _MEM[key] = pd.DataFrame()
        return _MEM[key]

    import time

    import akshare as ak

    raw = pd.DataFrame()
    for attempt in range(3):                       # 偶发网络抖动,重试
        try:
            raw = ak.stock_zh_a_daily(symbol=prefixed, adjust=adjust)
            if raw is not None and not raw.empty:
                break
        except Exception:
            time.sleep(0.5 * (attempt + 1))
            raw = pd.DataFrame()

    if raw is None or raw.empty:
        _MEM[key] = pd.DataFrame()   # 不缓存空结果,下次运行可重试
        return _MEM[key]

    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"])
    if "turnover" in df.columns:
        df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce") * 100
    cutoff = pd.Timestamp(_dt.date.today() - _dt.timedelta(days=lookback_days))
    df = df[df["date"] >= cutoff].sort_values("date").reset_index(drop=True)
    keep = [c for c in ("date", "open", "close", "high", "low",
                        "volume", "amount", "turnover") if c in df.columns]
    df = df[keep]
    df.to_pickle(path)
    _MEM[key] = df
    return df


def get_fund_flow(symbol: str) -> pd.DataFrame:
    """个股资金流向历史(efinance 源,绕过被封锁的东财 akshare 接口)。

    返回列:date / main_net(主力净流入额,元)/ main_ratio(主力净流入占比,%),
    按日期升序。无数据返回空 DataFrame(不缓存空,下次可重试)。
    """
    key = f"flow_{symbol}"
    if key in _MEM:
        return _MEM[key]
    ensure_dirs()
    path = CACHE_DIR / f"flow_{symbol}.pkl"
    if _is_fresh(path):
        df = pd.read_pickle(path)
        _MEM[key] = df
        return df

    try:
        import efinance as ef
        raw = ef.stock.get_history_bill(symbol)
    except Exception:
        raw = None
    if raw is None or len(raw) == 0:
        _MEM[key] = pd.DataFrame()
        return _MEM[key]

    df = raw.rename(columns={"日期": "date", "主力净流入": "main_net",
                             "主力净流入占比": "main_ratio"})
    keep = [c for c in ("date", "main_net", "main_ratio") if c in df.columns]
    if "main_ratio" not in keep:
        _MEM[key] = pd.DataFrame()
        return _MEM[key]
    df = df[keep].copy()
    df["date"] = pd.to_datetime(df["date"])
    for c in ("main_net", "main_ratio"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    df.to_pickle(path)
    _MEM[key] = df
    return df
