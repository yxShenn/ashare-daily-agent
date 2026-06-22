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
_LIVE_SPOT: dict = {}  # {"df": DataFrame, "ts": float(epoch)}


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


def get_live_spot(max_age_seconds: int = 45) -> pd.DataFrame:
    """全市场实时快照(新浪 stock_zh_a_spot),短 TTL 内存缓存。

    与 get_spot() 的「当日磁盘缓存、盘中不刷新」不同;供 Agent 大盘广度统计。
    拉取失败时回退 get_spot() 并尽量标注为陈旧数据。
    """
    import time

    now = time.time()
    cached = _LIVE_SPOT.get("df")
    if cached is not None and now - float(_LIVE_SPOT.get("ts", 0)) < max_age_seconds:
        return cached

    try:
        import akshare as ak

        raw = ak.stock_zh_a_spot()
        df = raw.rename(columns=_SINA_RENAME)
        df["exchange"] = df["symbol_raw"].str[:2]
        df["symbol"] = df["symbol_raw"].str[2:]
        for c in ("close", "pct", "amount", "volume", "open", "prev_close",
                  "high", "low"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        _LIVE_SPOT["df"] = df
        _LIVE_SPOT["ts"] = now
        _LIVE_SPOT["source"] = "sina_live"
        return df
    except Exception:
        if cached is not None:
            return cached
        df = get_spot().copy()
        _LIVE_SPOT["df"] = df
        _LIVE_SPOT["ts"] = now
        _LIVE_SPOT["source"] = "spot_disk_fallback"
        return df


def market_breadth(df: pd.DataFrame | None = None) -> dict:
    """从全市场快照计算涨跌家数/均涨跌幅等广度指标。"""
    spot = df if df is not None else get_live_spot()
    board = spot[spot["exchange"] != "bj"]
    pct = board["pct"].dropna()
    up = int((pct > 0).sum())
    down = int((pct < 0).sum())
    total = int(len(pct))
    return {
        "total": total,
        "up": up,
        "down": down,
        "flat": int(total - up - down),
        "limit_up_approx": int((pct >= 9.8).sum()),
        "avg_pct": round(float(pct.mean()), 2) if len(pct) else None,
        "median_pct": round(float(pct.median()), 2) if len(pct) else None,
        "total_amount_yi": round(float(board["amount"].sum()) / 1e8, 1)
        if "amount" in board.columns else None,
    }


def live_spot_meta() -> dict:
    """最近一次 live spot 的元信息(source/as_of)。"""
    import datetime as dt

    ts = _LIVE_SPOT.get("ts")
    as_of = None
    if ts:
        as_of = dt.datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    return {"source": _LIVE_SPOT.get("source", "unknown"), "as_of": as_of}


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


def get_live_quotes(symbols: list[str]) -> dict[str, dict]:
    """与成交 tick 同源的新浪实时报价(hq.sinajs.cn);缺失时回退 spot 并标记 source。

    返回 {symbol: {name, price, prev_close, open, high, low, source}}。
    source: sina_realtime | spot_fallback
    """
    syms = list(dict.fromkeys(s for s in symbols if s))
    if not syms:
        return {}
    out = get_realtime(syms)
    missing = [s for s in syms if s not in out]
    if missing:
        spot = get_spot().set_index("symbol")
        for s in missing:
            if s not in spot.index:
                continue
            row = spot.loc[s]
            price = float(row.get("close") or 0)
            if price <= 0:
                continue
            pc = float(row.get("prev_close") or price)
            out[s] = {
                "name": str(row.get("name", "")),
                "price": price,
                "prev_close": pc,
                "open": float(row.get("open") or price),
                "high": float(row.get("high") or price),
                "low": float(row.get("low") or price),
                "source": "spot_fallback",
            }
    for q in out.values():
        q.setdefault("source", "sina_realtime")
    return out


def get_live_price(symbol: str) -> float | None:
    q = get_live_quotes([symbol]).get(symbol)
    return None if not q else float(q["price"])


def fund_flow_recent(symbol: str, days: int = 5) -> list[dict]:
    """最近 N 日主力资金流向(efinance),字段带明确单位。"""
    ff = get_fund_flow(symbol)
    if ff.empty:
        return []
    rows: list[dict] = []
    for _, r in ff.tail(int(days)).iterrows():
        d = r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"])
        net = r.get("main_net")
        ratio = r.get("main_ratio")
        net_f = None if pd.isna(net) else float(net)
        ratio_f = None if pd.isna(ratio) else float(ratio)
        rows.append({
            "date": d,
            "main_net_yuan": net_f,
            "main_net_yi": round(net_f / 1e8, 2) if net_f is not None else None,
            "main_ratio_pct": round(ratio_f, 2) if ratio_f is not None else None,
        })
    return rows


def agent_field_legend(cfg: dict | None = None) -> dict:
    """Agent 工具返回字段的单位说明(防误读)。"""
    mf_days = int((cfg or {}).get("run", {}).get("mainflow_days", 5))
    return {
        "current_price": "新浪实时价(元/股),与成交 tick 同源;决策现价以此为准",
        "last_daily_close": "最近一根日K收盘价(元);可能滞后于 current_price",
        "pct": "相对昨收的涨跌幅(%)",
        "amount_yi": "成交额(亿元)",
        "turnover": "换手率(%)",
        "score": "多因子横截面 z-score 加权综合分(无量纲),不是涨跌幅",
        "mainflow_ratio_nd_avg_pct": (
            f"近{mf_days}日「主力净流入占比」的算术平均,单位百分点(%),不是亿元"
        ),
        "fund_flow_recent": "逐日主力净流入:main_net_yi=亿元,main_ratio_pct=占成交额%",
        "price_source": "sina_realtime=实时接口;spot_fallback=实时不可用时的快照兜底",
        "up_down": "get_market_overview 涨跌家数,新浪全市场实时快照,字段 as_of 为抓取时刻",
        "avg_pct": "全市场均涨跌幅(%),来自 get_market_overview 实时快照",
        "board_pct": "所属东财行业板块当日涨跌幅(%)",
        "vs_board_pct": "个股涨跌幅减板块涨跌幅(百分点),正=强于板块",
        "board_vs_market_pct": "板块涨跌幅减全市场均涨跌幅(百分点),正=板块强于大盘",
    }


def get_industry_boards() -> pd.DataFrame:
    """东财行业板块当日快照(akshare);按日缓存。列:name/code/pct/up/down。"""
    key = f"boards_{_today()}"
    if key in _MEM:
        return _MEM[key]
    ensure_dirs()
    path = CACHE_DIR / f"{key}.pkl"
    if _is_fresh(path):
        df = pd.read_pickle(path)
        _MEM[key] = df
        return df
    import akshare as ak

    try:
        raw = ak.stock_board_industry_name_em()
    except Exception:
        _MEM[key] = pd.DataFrame()
        return _MEM[key]
    rename = {"板块名称": "name", "板块代码": "code", "涨跌幅": "pct",
              "上涨家数": "up", "下跌家数": "down"}
    df = raw.rename(columns={k: v for k, v in rename.items() if k in raw.columns})
    for c in ("pct", "up", "down"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    keep = [c for c in ("name", "code", "pct", "up", "down") if c in df.columns]
    df = df[keep] if keep else pd.DataFrame()
    if not df.empty:
        df.to_pickle(path)
    _MEM[key] = df
    return df


def _match_industry_board(industry: str, board_code: str | None,
                          boards: pd.DataFrame) -> pd.Series | None:
    if boards.empty:
        return None
    if board_code:
        hit = boards[boards["code"].astype(str) == str(board_code)]
        if not hit.empty:
            return hit.iloc[0]
    ind = (industry or "").strip()
    if not ind:
        return None
    hit = boards[boards["name"].astype(str) == ind]
    if not hit.empty:
        return hit.iloc[0]
    hit = boards[boards["name"].astype(str).str.contains(ind, na=False, regex=False)]
    if not hit.empty:
        return hit.iloc[0]
    hit = boards[boards["name"].astype(str).apply(lambda n: ind in str(n) if n else False)]
    return hit.iloc[0] if not hit.empty else None


def _sector_decision_hint(ctx: dict, market_avg_pct: float | None) -> str:
    bpct = ctx.get("board_pct")
    spct = ctx.get("stock_pct")
    bvm = ctx.get("board_vs_market_pct")
    if bpct is None:
        return "未匹配到行业板块数据;卖出/买入除大盘外请尽量 get_sector_context 核对板块"
    parts = [f"板块{ctx.get('board_name')}当日{bpct:+.2f}%"]
    if market_avg_pct is not None and bvm is not None:
        parts.append(f"板块较全市场{'强' if bvm > 0 else '弱'}{abs(bvm):.2f}pct")
    if spct is not None and ctx.get("vs_board_pct") is not None:
        vs = ctx["vs_board_pct"]
        parts.append(f"个股较板块{'强' if vs > 0 else '弱'}{abs(vs):.2f}pct")
    if market_avg_pct is not None and market_avg_pct < -1 and bvm is not None and bvm > 0:
        parts.append("大盘弱但板块强,不宜仅凭大盘弱就止盈/清仓")
    elif market_avg_pct is not None and market_avg_pct < -1 and bvm is not None and bvm < -1:
        parts.append("大盘与板块均弱,风控减仓更合理")
    return "；".join(parts)


def get_sector_context(symbol: str, market_avg_pct: float | None = None) -> dict:
    """个股所属行业板块行情 + 相对大盘/板块强弱(东财行业板块 + 新浪实时价)。"""
    base = get_base_info(symbol)
    live = get_live_quotes([symbol]).get(symbol, {})
    stock_pct = None
    if live:
        pc = float(live.get("prev_close") or 0)
        if pc > 0 and live.get("price"):
            stock_pct = round((float(live["price"]) / pc - 1) * 100, 2)
    industry = (base.get("industry") or "").strip()
    boards = get_industry_boards()
    row = _match_industry_board(industry, base.get("board_code"), boards)
    out: dict = {
        "symbol": symbol,
        "name": base.get("name") or live.get("name"),
        "industry": industry or None,
        "stock_pct": stock_pct,
    }
    if row is not None:
        bpct = row.get("pct")
        bpct_f = None if pd.isna(bpct) else float(bpct)
        out.update({
            "board_name": str(row.get("name", "")),
            "board_code": str(row.get("code", "")),
            "board_pct": round(bpct_f, 2) if bpct_f is not None else None,
            "board_up": int(row["up"]) if pd.notna(row.get("up")) else None,
            "board_down": int(row["down"]) if pd.notna(row.get("down")) else None,
        })
        if stock_pct is not None and bpct_f is not None:
            out["vs_board_pct"] = round(stock_pct - bpct_f, 2)
        if market_avg_pct is not None and bpct_f is not None:
            out["board_vs_market_pct"] = round(bpct_f - market_avg_pct, 2)
        if stock_pct is not None and market_avg_pct is not None:
            out["stock_vs_market_pct"] = round(stock_pct - market_avg_pct, 2)
    out["decision_hint"] = _sector_decision_hint(out, market_avg_pct)
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


def get_base_info(symbol: str) -> dict:
    """个股基础信息(efinance 源):返回 {industry: 所处行业, name}。失败返回 {}。

    用于 Serenity 卡脖子赛道的板块层加权(按行业/名称匹配主题)。按日缓存。
    """
    key = f"base_{symbol}"
    if key in _MEM:
        return _MEM[key]
    ensure_dirs()
    path = CACHE_DIR / f"base_{symbol}.pkl"
    if _is_fresh(path):
        info = pd.read_pickle(path)
        if "board_code" in info:
            _MEM[key] = info
            return info

    try:
        import efinance as ef
        s = ef.stock.get_base_info(symbol)
    except Exception:
        s = None
    if s is None:
        _MEM[key] = {}
        return _MEM[key]
    d = s.to_dict() if hasattr(s, "to_dict") else dict(s)
    board_code = None
    for v in d.values():
        if v is not None and str(v).strip().upper().startswith("BK"):
            board_code = str(v).strip().upper()
            break
    info = {
        "industry": d.get("所处行业"),
        "name": d.get("股票名称"),
        "board_code": board_code,
    }
    pd.to_pickle(info, path)
    _MEM[key] = info
    return info
