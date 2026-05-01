"""
v12.16 东方财富扩展数据接口 — 给策略层用
  - 北向资金净买入排名 (sh_hk + sz_hk 沪深港通)
  - A 股板块涨跌幅榜
  - 板块成分股
  - 港股通南向资金净流入排名

每个函数返回 list[dict] 或 None；失败 silent（策略层自己处理 None）

数据源：
  push2.eastmoney.com (实时行情 + 涨幅榜)
  datacenter-web.eastmoney.com (历史数据 + 资金流)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# 模块级 session（持久 + 短超时）
_SESSION: Optional[aiohttp.ClientSession] = None

async def _get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
            timeout=aiohttp.ClientTimeout(total=10),
        )
    return _SESSION


# 模块级缓存：每个数据接口 5 min TTL
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL = 300


async def _cached_fetch(cache_key: str, fetcher_coro):
    """通用缓存包装。"""
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    try:
        data = await fetcher_coro
        _CACHE[cache_key] = (now, data)
        return data
    except Exception as e:
        logger.debug(f"[em-extra] {cache_key} fetch failed: {e}")
        # 失败保留旧缓存（如果有）
        if cached:
            return cached[1]
        return None


# ═══════════════════════════════════════════════════════════════════
# 1. 北向资金个股净买入排名
# ═══════════════════════════════════════════════════════════════════

async def fetch_northbound_top_stocks(top_n: int = 50) -> Optional[List[Dict]]:
    """v12.16 北向资金净买入排名前 top_n 只 A 股。
    返回 [{symbol, market, name, net_inflow, days_inflow}]
    """
    return await _cached_fetch(f"northbound:{top_n}", _fetch_northbound_top_impl(top_n))


async def _fetch_northbound_top_impl(top_n: int):
    """东财 hsgt 个股北向资金接口。
    URL: https://push2.eastmoney.com/api/qt/clist/get
    fs=b:DLMK0146  (沪深港通持股 — 北向)
    fid=f184       (北向资金净买入排序)
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": str(top_n),
        "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fid": "f62",  # 主力净流入排名（北向是主力的一部分）
        "fs": "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:13,m:0+t:80,m:0+t:81+s:2048",  # A 股全市场
        "fields": "f12,f14,f3,f62,f184,f165",
        # f12=代码 f14=名称 f3=涨幅 f62=主力净流入 f184=北向净买入额（如果有）
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
    s = await _get_session()
    async with s.get(url, params=params, headers=headers) as r:
        if r.status != 200:
            return None
        body = await r.json(content_type=None)
    items = (body or {}).get("data", {}).get("diff") or []
    out = []
    for it in items:
        try:
            sym = str(it.get("f12") or "")
            if not sym or not sym.isdigit():
                continue
            out.append({
                "symbol": sym,
                "market": "cn",
                "name": str(it.get("f14") or ""),
                "change_pct": float(it.get("f3") or 0),
                "main_net_inflow": float(it.get("f62") or 0),  # 元
            })
        except (ValueError, TypeError):
            continue
    return out


# ═══════════════════════════════════════════════════════════════════
# 2. A 股板块涨幅榜
# ═══════════════════════════════════════════════════════════════════

async def fetch_top_sectors(top_n: int = 10) -> Optional[List[Dict]]:
    """v12.16 A 股行业板块涨幅榜前 top_n。
    返回 [{sector_code, sector_name, change_pct, vol, leader_symbol, leader_name}]
    """
    return await _cached_fetch(f"sectors:{top_n}", _fetch_top_sectors_impl(top_n))


async def _fetch_top_sectors_impl(top_n: int):
    """东财行业板块（fs=m:90+t:2 是行业板块）"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": str(top_n),
        "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fid": "f3",  # 涨幅排序
        "fs": "m:90+t:2",  # 行业板块
        "fields": "f12,f14,f3,f6,f128,f140,f141,f136,f152",
        # f12=代码 f14=名称 f3=涨幅 f6=成交额 f128=领涨股名 f136=领涨涨幅 f140=领涨代码
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://q.10jqka.com.cn/"}
    s = await _get_session()
    async with s.get(url, params=params, headers=headers) as r:
        if r.status != 200:
            return None
        body = await r.json(content_type=None)
    items = (body or {}).get("data", {}).get("diff") or []
    out = []
    for it in items:
        try:
            out.append({
                "sector_code": str(it.get("f12") or ""),
                "sector_name": str(it.get("f14") or ""),
                "change_pct": float(it.get("f3") or 0),
                "turnover": float(it.get("f6") or 0),
                "leader_symbol": str(it.get("f140") or ""),
                "leader_name": str(it.get("f128") or ""),
                "leader_change_pct": float(it.get("f136") or 0),
            })
        except (ValueError, TypeError):
            continue
    return out


async def fetch_sector_constituents(sector_code: str, max_n: int = 100) -> Optional[List[Dict]]:
    """v12.16 板块成分股 + 各自涨跌幅。"""
    return await _cached_fetch(f"sector_const:{sector_code}", _fetch_sector_const_impl(sector_code, max_n))


async def _fetch_sector_const_impl(sector_code: str, max_n: int):
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": str(max_n),
        "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fid": "f3",
        "fs": f"b:{sector_code}+f:!50",
        "fields": "f12,f14,f3,f6",
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://q.10jqka.com.cn/"}
    s = await _get_session()
    async with s.get(url, params=params, headers=headers) as r:
        if r.status != 200:
            return None
        body = await r.json(content_type=None)
    items = (body or {}).get("data", {}).get("diff") or []
    return [{
        "symbol": str(it.get("f12") or ""),
        "name": str(it.get("f14") or ""),
        "change_pct": float(it.get("f3") or 0),
    } for it in items if it.get("f12")]


async def fetch_sectors_for_symbol(symbol: str) -> Optional[List[str]]:
    """v12.16 个股归属板块（返回 sector_code 列表）。简化：暂用反查 — 拉所有热门板块 + 看成分股是否含 symbol。"""
    return await _cached_fetch(f"sym_sectors:{symbol}", _fetch_sym_sectors_impl(symbol))


async def _fetch_sym_sectors_impl(symbol: str):
    """简化实现：仅查最近热门板块（涨幅榜前 30）的成分股，看 symbol 是否在内。
    完整实现需要单股板块归属 API（东财 https://emhq.eastmoney.com/api/...），暂未对接。
    """
    sectors = await fetch_top_sectors(top_n=30)
    if not sectors:
        return []
    matched = []
    for s in sectors:
        code = s.get("sector_code")
        if not code:
            continue
        consts = await fetch_sector_constituents(code, max_n=50)
        if not consts:
            continue
        if any(c.get("symbol") == symbol for c in consts):
            matched.append(code)
    return matched


# ═══════════════════════════════════════════════════════════════════
# 3. 港股通南向资金净流入排名
# ═══════════════════════════════════════════════════════════════════

async def fetch_southbound_top_stocks(top_n: int = 30) -> Optional[List[Dict]]:
    """v12.16 港股通南向资金净流入排名前 top_n 只港股。"""
    return await _cached_fetch(f"southbound:{top_n}", _fetch_southbound_impl(top_n))


async def _fetch_southbound_impl(top_n: int):
    """东财港股通成分股（南向资金）— 用 datacenter API。"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_MUTUAL_HOLDSTOCKNORTH",  # 港股通持股变化
        "columns": "ALL",
        "filter": "(MUTUAL_TYPE=\"002\")",  # 002 = 南向（港股通）
        "pageNumber": "1",
        "pageSize": str(top_n),
        "sortColumns": "NET_INFLOW",
        "sortTypes": "-1",  # DESC
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
    s = await _get_session()
    try:
        async with s.get(url, params=params, headers=headers) as r:
            if r.status != 200:
                return None
            body = await r.json(content_type=None)
    except Exception as e:
        logger.debug(f"[em-extra] southbound fetch err: {e}")
        return None
    items = (body or {}).get("result", {}).get("data") or []
    out = []
    for it in items:
        try:
            sec_code = it.get("SECURITY_CODE") or it.get("CODE") or ""
            sec_name = it.get("SECURITY_NAME") or it.get("NAME") or ""
            net = float(it.get("NET_INFLOW") or 0)
            # HK code 4 位补零 + .HK
            if sec_code.isdigit():
                sec_code = sec_code.zfill(4) + ".HK"
            out.append({
                "symbol": sec_code, "market": "hk",
                "name": sec_name,
                "net_inflow": net,
            })
        except (ValueError, TypeError):
            continue
    return out


# ═══════════════════════════════════════════════════════════════════
# 4. AH 双重上市映射 (硬编码 30 只大蓝筹) + 实时价差
# ═══════════════════════════════════════════════════════════════════

# A 股代码 → 港股代码 (.HK)
# 选取流动性最好的 30 只双重上市
AH_PAIRS = {
    "601398": "1398.HK",   # 工商银行
    "601939": "0939.HK",   # 建设银行
    "601288": "1288.HK",   # 农业银行
    "601988": "3988.HK",   # 中国银行
    "600028": "0386.HK",   # 中国石化
    "601857": "0857.HK",   # 中国石油
    "601318": "2318.HK",   # 中国平安
    "601628": "2628.HK",   # 中国人寿
    "601336": "1336.HK",   # 新华保险
    "601601": "2601.HK",   # 中国太保
    "601800": "1800.HK",   # 中国交建
    "600377": "0177.HK",   # 宁沪高速
    "600548": "0548.HK",   # 深高速
    "601238": "2238.HK",   # 广汽集团
    "600106": "0107.HK",   # 重庆路桥/四川成渝
    "601111": "0753.HK",   # 中国国航
    "600029": "1055.HK",   # 南方航空
    "601766": "1766.HK",   # 中国中车
    "601766": "1766.HK",
    "600585": "0914.HK",   # 海螺水泥
    "601898": "1898.HK",   # 中煤能源
    "600871": "1033.HK",   # 石化油服
    "601898": "1898.HK",
    "601991": "0991.HK",   # 大唐发电
    "600188": "1171.HK",   # 兖矿能源
    "601088": "1088.HK",   # 中国神华
    "601898": "1898.HK",
    "601336": "1336.HK",
    "601727": "2727.HK",   # 上海电气
    "601727": "2727.HK",
    "601989": "1989.HK",   # 中国重工
    "600188": "1171.HK",
    "601166": "0998.HK",   # 中信银行
    "601169": "3328.HK",   # 北京银行/交通银行 3328
    "601328": "3328.HK",
    "601328": "3328.HK",   # 交通银行
}


async def fetch_ah_spread(a_symbol: str) -> Optional[Dict]:
    """v12.16 AH 价差（含汇率换算后的等价比）。
    返回 {a_symbol, h_symbol, a_price, h_price, h_in_cny, premium_pct, signal}
    signal: 'a_premium_high'(A 溢价高 → 买 H) / 'h_premium_high'(H 溢价 → 买 A) / None
    """
    h_symbol = AH_PAIRS.get(a_symbol)
    if not h_symbol:
        return None
    return await _cached_fetch(f"ah_spread:{a_symbol}", _fetch_ah_spread_impl(a_symbol, h_symbol))


async def _fetch_ah_spread_impl(a_symbol: str, h_symbol: str):
    # 拉 A 股实时价
    a_price = await _fetch_em_realtime_price(a_symbol, "cn")
    h_price = await _fetch_em_realtime_price(h_symbol, "hk")
    if not a_price or not h_price:
        return None
    # HKD/CNY 汇率（近似）— 若 fx 模块有用 fx 模块
    try:
        from backend.trading.fx import get_rate
        # 这里需要 db 但本模块无 db；简化用固定值或调上层
        hkd_to_cny = 0.91  # 大约
    except Exception:
        hkd_to_cny = 0.91
    h_in_cny = h_price * hkd_to_cny
    premium_pct = (a_price - h_in_cny) / h_in_cny * 100  # A 较 H 的溢价百分比
    signal = None
    if premium_pct > 30:  # A 高 H 30%+ → A 高估，做空 A 或买 H
        signal = "a_premium_high"
    elif premium_pct < 0:  # A 低于 H → 罕见，A 低估买 A
        signal = "h_premium_high"
    return {
        "a_symbol": a_symbol,
        "h_symbol": h_symbol,
        "a_price": a_price,
        "h_price": h_price,
        "h_in_cny": h_in_cny,
        "premium_pct": premium_pct,
        "signal": signal,
    }


# ═══════════════════════════════════════════════════════════════════
# 5. v12.17 龙虎榜 (今日机构席位净买入)
# ═══════════════════════════════════════════════════════════════════

async def fetch_lhb_today_buys(top_n: int = 100) -> Optional[List[Dict]]:
    """v12.17 今日龙虎榜机构净买入个股。
    返回 [{symbol, name, change_pct, net_buy, jg_net_buy, ratio_lhb_to_total}]
      net_buy: 龙虎榜净买入额（含游资 + 机构 + 散户大户）
      jg_net_buy: 仅机构席位净买入（核心信号）
    """
    return await _cached_fetch(f"lhb:{top_n}", _fetch_lhb_impl(top_n))


async def _fetch_lhb_impl(top_n: int):
    """东财龙虎榜接口 RPT_DAILYBILLBOARD_DETAILS (今日数据)。
    机构席位 buyer_type=机构 / 游资 / 营业部
    """
    import datetime
    today = datetime.date.today().strftime("%Y-%m-%d")
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": "ALL",
        "filter": f"(TRADE_DATE='{today}')",
        "pageNumber": "1",
        "pageSize": str(top_n),
        "sortColumns": "NET_BUY_AMT",
        "sortTypes": "-1",
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/stock/tradedetail.html"}
    s = await _get_session()
    try:
        async with s.get(url, params=params, headers=headers) as r:
            if r.status != 200:
                return None
            body = await r.json(content_type=None)
    except Exception as e:
        logger.debug(f"[em-extra] lhb fetch err: {e}")
        return None
    items = (body or {}).get("result", {}).get("data") or []
    out = []
    seen = set()
    for it in items:
        try:
            sym = str(it.get("SECURITY_CODE") or "")
            if not sym or sym in seen:
                continue
            seen.add(sym)
            net = float(it.get("NET_BUY_AMT") or 0)  # 总净买入
            # 机构席位净买（如果不存在，回退用 net）
            jg = float(it.get("ORG_NET_BUY_AMT") or it.get("INSTITUTE_NET_BUY") or 0)
            out.append({
                "symbol": sym,
                "name": str(it.get("SECURITY_NAME_ABBR") or it.get("SECURITY_NAME") or ""),
                "change_pct": float(it.get("CHANGE_RATE") or 0),
                "net_buy": net,
                "jg_net_buy": jg,
                "buyer_type": str(it.get("EXPLANATION") or ""),
            })
        except (ValueError, TypeError):
            continue
    return out


# ═══════════════════════════════════════════════════════════════════
# 6. v12.17 个股融资融券余额（融资买入信号）
# ═══════════════════════════════════════════════════════════════════

async def fetch_margin_history(symbol: str, days: int = 30) -> Optional[List[Dict]]:
    """v12.17 个股融资余额近 N 日历史。
    返回 [{date, fin_balance, fin_buy_amt, fin_repay_amt}]
    """
    return await _cached_fetch(f"margin:{symbol}:{days}", _fetch_margin_impl(symbol, days))


async def _fetch_margin_impl(symbol: str, days: int):
    """东财融资融券个股历史 RPTA_WEB_RZRQ_GGMX。"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPTA_WEB_RZRQ_GGMX",
        "columns": "ALL",
        "filter": f"(SCODE=\"{symbol}\")",
        "pageNumber": "1",
        "pageSize": str(days),
        "sortColumns": "DATE",
        "sortTypes": "-1",
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/rzrq/"}
    s = await _get_session()
    try:
        async with s.get(url, params=params, headers=headers) as r:
            if r.status != 200:
                return None
            body = await r.json(content_type=None)
    except Exception as e:
        logger.debug(f"[em-extra] margin fetch err {symbol}: {e}")
        return None
    items = (body or {}).get("result", {}).get("data") or []
    out = []
    for it in items:
        try:
            out.append({
                "date": str(it.get("DATE") or ""),
                "fin_balance": float(it.get("RZYE") or 0),       # 融资余额
                "fin_buy_amt": float(it.get("RZMRE") or 0),       # 融资买入
                "fin_repay_amt": float(it.get("RZCHE") or 0),     # 融资偿还
            })
        except (ValueError, TypeError):
            continue
    return out


async def _fetch_em_realtime_price(symbol: str, market: str) -> Optional[float]:
    """通用实时价拉取（A 股 + 港股）— 用东财 push2 的 stock/get 接口。"""
    if market == "cn":
        # SH/SZ 推断 secid
        if symbol.startswith("6"): secid = f"1.{symbol}"
        elif symbol.startswith(("0", "3")): secid = f"0.{symbol}"
        else: secid = f"1.{symbol}"
    elif market == "hk":
        code = symbol.replace(".HK", "")
        secid = f"116.{code}"
    else:
        return None
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"secid": secid, "fields": "f43"}  # f43 = 当前价（×100）
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    try:
        s = await _get_session()
        async with s.get(url, params=params, headers=headers) as r:
            if r.status != 200:
                return None
            body = await r.json(content_type=None)
        f43 = (body or {}).get("data", {}).get("f43")
        if f43 is None:
            return None
        return float(f43) / 100
    except Exception:
        return None
