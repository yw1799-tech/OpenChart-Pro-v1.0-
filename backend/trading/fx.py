"""
汇率模块 — 为自动交易提供 HKD/CNY/USD 汇率换算。

数据源：
  - 优先: Yahoo Finance 免费汇率（HKDUSD=X / CNYUSD=X）
  - 兜底: 硬编码的参考汇率（HKD→USD 0.1280, CNY→USD 0.1400）

缓存：DB 表 fx_rates，1 小时 TTL。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# 硬编码兜底（2026 年参考值）
FALLBACK_RATES = {
    "USD": 1.0,
    "HKD": 0.1280,
    "CNY": 0.1400,
}

# symbol/market → 本币
MARKET_CURRENCY = {
    "crypto": "USD",   # USDT 计价 ≈ USD
    "us": "USD",
    "hk": "HKD",
    "cn": "CNY",
}

CACHE_TTL_SEC = 3600  # 1 小时


def market_to_currency(market: str) -> str:
    return MARKET_CURRENCY.get(market, "USD")


async def _fetch_yahoo_rate(pair: str) -> Optional[float]:
    """从 Yahoo Finance 拉汇率，pair 如 'HKDUSD=X' / 'CNYUSD=X'。"""
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={pair}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                quote = (data.get("quoteResponse") or {}).get("result") or []
                if not quote:
                    return None
                price = quote[0].get("regularMarketPrice")
                return float(price) if price else None
    except Exception as e:
        logger.debug(f"[fx] Yahoo 拉汇率 {pair} 失败: {e}")
        return None


# in-flight 锁：避免同币种缓存过期时多个协程并发拉 Yahoo（之前 6 个加密 + 3 港股同时下单可能叠加打 Yahoo）
_inflight: dict = {}

async def get_rate(db, currency: str) -> float:
    """
    返回 1 单位 currency 兑 USD 的汇率。
    缓存: 1 小时内直接用。过期或无缓存则实时拉 Yahoo，失败用 fallback。
    并发安全：同币种过期刷新使用 in-flight 锁，多协程共享一次 fetch 结果。
    """
    currency = currency.upper()
    if currency == "USD":
        return 1.0
    now = int(time.time())
    # 查缓存
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT rate_to_usd, updated_at FROM fx_rates WHERE currency=?", (currency,)
            )
            row = await cur.fetchone()
        if row and (now - (row["updated_at"] or 0)) < CACHE_TTL_SEC:
            return float(row["rate_to_usd"])
    except Exception:
        pass

    # in-flight 锁：如果已有同币种刷新任务在跑，复用它的结果
    fut = _inflight.get(currency)
    if fut is not None:
        try:
            return await fut
        except Exception:
            pass  # fallthrough: 自己再拉一次

    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _inflight[currency] = fut
    try:
        rate = await _do_fetch_and_cache(db, currency, now)
        fut.set_result(rate)
        return rate
    except BaseException as e:
        # Catches both Exception and CancelledError so waiters are never left on a pending future
        if not fut.done():
            if isinstance(e, Exception):
                fut.set_exception(e)
            else:
                fut.cancel()
        raise
    finally:
        _inflight.pop(currency, None)


async def _do_fetch_and_cache(db, currency: str, now: int) -> float:
    """实际去 Yahoo 拉取 + 写缓存。从 get_rate 拆出便于 in-flight 锁包裹。"""
    pair = f"{currency}USD=X"
    rate = await _fetch_yahoo_rate(pair)
    if rate is None or rate <= 0:
        if currency not in FALLBACK_RATES:
            # 未知币种绝不默认 1:1 — 会产生严重金额误差
            raise ValueError(f"[fx] 未知币种 {currency}：Yahoo 拉取失败且无兜底汇率")
        rate = FALLBACK_RATES[currency]
        logger.info(f"[fx] {currency}→USD 使用兜底汇率 {rate}")
    else:
        logger.info(f"[fx] {currency}→USD 实时汇率 {rate:.5f}")
    # 写缓存
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO fx_rates (currency, rate_to_usd, updated_at) VALUES (?, ?, ?)",
                (currency, rate, now),
            )
            await conn.commit()
    except Exception as e:
        logger.debug(f"[fx] 写缓存失败: {e}")
    return rate


async def to_usd(db, amount: float, currency: str) -> float:
    """把某币种金额换算成 USD。"""
    rate = await get_rate(db, currency)
    return amount * rate


async def usd_to_currency(db, usd: float, currency: str) -> float:
    """USD 换本币。"""
    rate = await get_rate(db, currency)
    if rate <= 0:
        return usd
    return usd / rate
