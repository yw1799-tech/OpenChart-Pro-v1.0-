"""
v12.17 稳定币市值流入流出数据源 — DefiLlama (free, no key).

DefiLlama 公开接口 https://stablecoins.llama.fi/stablecoins
返回所有稳定币的 (peg, price, circulating) 历史。

用法：每 1h 全局拉一次 (model: 1 fetch 给所有 crypto symbol 共用)
信号：USDT + USDC 24h 净流入 / 净流出对加密整体方向参考
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

_SESSION: Optional[aiohttp.ClientSession] = None
_CACHE: Dict[str, tuple] = {}
_TTL = 3600  # 1h，稳定币流入是慢变量


async def _get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=5, ttl_dns_cache=300),
            timeout=aiohttp.ClientTimeout(total=15),
        )
    return _SESSION


async def fetch_stablecoin_flow_24h() -> Optional[Dict[str, Any]]:
    """v12.17 拉 USDT + USDC 24h 净流入（合计 supply 变化）。
    返回 {usdt_delta, usdc_delta, total_delta, total_mcap, signal}
      signal: 'inflow' / 'outflow' / 'neutral' (绝对值 < $200M)
    """
    cache = _CACHE.get("flow")
    now = time.time()
    if cache and now - cache[0] < _TTL:
        return cache[1]
    try:
        data = await _fetch_impl()
        _CACHE["flow"] = (now, data)
        return data
    except Exception as e:
        logger.debug(f"[stablecoin] fetch failed: {e}")
        if cache:
            return cache[1]
        return None


async def _fetch_impl():
    """DefiLlama: GET /stablecoincharts/all 返回历史，最后两项就是今天/昨天 supply"""
    url = "https://stablecoins.llama.fi/stablecoincharts/all"
    s = await _get_session()
    async with s.get(url) as r:
        if r.status != 200:
            return None
        rows = await r.json(content_type=None)
    if not rows or len(rows) < 2:
        return None
    # rows: [{date: unix, totalCirculating: {peggedUSD: X}}, ...]
    last = rows[-1]
    prev = rows[-2]
    last_total = float((last.get("totalCirculating") or {}).get("peggedUSD") or 0)
    prev_total = float((prev.get("totalCirculating") or {}).get("peggedUSD") or 0)
    total_delta = last_total - prev_total
    # 单独拉 USDT/USDC 当前 mcap
    usdt = await _fetch_single("tether")
    usdc = await _fetch_single("usd-coin")
    out = {
        "total_mcap": last_total,
        "total_delta_24h": total_delta,
        "usdt_delta": usdt or 0,
        "usdc_delta": usdc or 0,
    }
    if total_delta > 5e8:        # 净流入 > $500M
        out["signal"] = "inflow"
    elif total_delta < -5e8:     # 净流出 > $500M
        out["signal"] = "outflow"
    else:
        out["signal"] = "neutral"
    return out


async def _fetch_single(stable_id: str) -> Optional[float]:
    """单个稳定币 24h supply delta（亿美元单位）。"""
    url = f"https://stablecoins.llama.fi/stablecoin/{stable_id}"
    try:
        s = await _get_session()
        async with s.get(url) as r:
            if r.status != 200:
                return None
            body = await r.json(content_type=None)
    except Exception:
        return None
    chain_balances = body.get("chainBalances") or {}
    # Sum across chains for current vs prev
    total_now, total_prev = 0.0, 0.0
    for chain, info in chain_balances.items():
        tokens = info.get("tokens") or []
        if len(tokens) >= 2:
            total_now += float((tokens[-1].get("circulating") or {}).get("peggedUSD") or 0)
            total_prev += float((tokens[-2].get("circulating") or {}).get("peggedUSD") or 0)
    return total_now - total_prev if total_now and total_prev else None
