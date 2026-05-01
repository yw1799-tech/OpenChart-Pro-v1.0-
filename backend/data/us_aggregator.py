"""
美股数据多源聚合（Phase 3B 扩展）。

K 线主源：Yahoo（yfinance），失败时回落到 Stooq。
基本面主源：Yahoo quote endpoint，失败时回落到 NASDAQ 公开 API。

NASDAQ API 免费、无 Key、稳定：
  https://api.nasdaq.com/api/quote/AAPL/summary?assetclass=stocks
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import aiohttp

from backend.data.models import Candle, Interval

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# NASDAQ 基本面（市值 / 均量 / 价格 / 名称）
# ═══════════════════════════════════════════════════════════════════


async def fetch_us_fundamentals_nasdaq(symbol: str) -> Optional[dict]:
    """
    NASDAQ 公开 API。返回 None 表示失败。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    base = "https://api.nasdaq.com/api/quote"
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            # 1. 摘要（含市值、均量、行业等）
            async with s.get(
                f"{base}/{symbol}/summary",
                params={"assetclass": "stocks"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                summary_body = await r.json(content_type=None)
            # 2. 实时行情（含价格、交易所）
            async with s.get(
                f"{base}/{symbol}/info",
                params={"assetclass": "stocks"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                info_body = await r.json(content_type=None)
    except Exception as e:
        logger.debug(f"[us-nasdaq] {symbol} 拉取失败: {e}")
        return None

    sd = (summary_body or {}).get("data", {}).get("summaryData", {}) or {}
    primary = (info_body or {}).get("data", {}).get("primaryData", {}) or {}
    company = (info_body or {}).get("data", {}) or {}

    if not sd and not primary:
        return None

    market_cap = _parse_money(sd.get("MarketCap", {}).get("value", "") if sd else "")
    avg_vol = _parse_int(sd.get("AverageVolume", {}).get("value", "") if sd else "")
    price = _parse_money(primary.get("lastSalePrice", "") if primary else "")
    pe_str = sd.get("PERatio", {}).get("value", "") if sd else ""
    pe = _parse_money(pe_str)
    name = company.get("companyName", "") or ""
    exchange = company.get("exchange", "") or company.get("primaryExchange", "") or ""
    is_otc = 1 if "OTC" in exchange.upper() or "PNK" in exchange.upper() else 0

    if market_cap == 0 and price == 0:
        return None

    return {
        "name": name,
        "price": price,
        "market_cap": market_cap,
        "avg_turnover": 0,
        "avg_volume": avg_vol,
        "listed_days": 9999,
        "is_st": 0,
        "is_gem": 0,
        "is_otc": is_otc,
        "pe": pe,
        "pb": 0,
        "turnover_rate": 0,
    }


def _parse_money(s) -> float:
    """'$267.96' / '3,934,821,525,432' / '16.29B' / '$3.94T' / 'N/A' → float。"""
    if s is None:
        return 0.0
    s = str(s).strip().replace("$", "").replace(",", "").replace("%", "")
    if not s or s.upper() in ("N/A", "NA", "--"):
        return 0.0
    multipliers = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    upper = s.upper()
    for suffix, mult in multipliers.items():
        if upper.endswith(suffix):
            try:
                return float(s[:-1]) * mult
            except ValueError:
                return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_int(s) -> int:
    return int(_parse_money(s))


# ═══════════════════════════════════════════════════════════════════
# Stooq K 线（兜底）
# ═══════════════════════════════════════════════════════════════════

# Stooq 周期映射
_STOOQ_INTERVAL = {
    Interval.D1: "d",
    Interval.W1: "w",
    Interval.MN: "m",
}


async def fetch_us_klines_stooq(
    symbol: str,
    interval: Interval,
    limit: int = 500,
) -> List[Candle]:
    """
    Stooq US 日/周/月线。免费但需要 API Key（自 2024 年后）；当前作为占位/可选。
    返回空列表表示失败/不支持。
    """
    period = _STOOQ_INTERVAL.get(interval)
    if not period:
        return []
    sym_lower = symbol.lower() + ".us"
    url = f"https://stooq.com/q/d/l/?s={sym_lower}&i={period}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                text = await r.text()
    except Exception as e:
        logger.debug(f"[us-stooq] {symbol} 失败: {e}")
        return []

    if "apikey" in text.lower() or text.strip().startswith("<"):
        # Stooq 现需要 apikey
        return []

    candles: List[Candle] = []
    lines = text.strip().split("\n")
    if len(lines) < 2:
        return []
    # 跳过 header: Date,Open,High,Low,Close,Volume
    for line in lines[1:][-limit:]:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            dt = datetime.strptime(parts[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            candles.append(Candle(
                timestamp=int(dt.timestamp() * 1000),
                open=float(parts[1]),
                high=float(parts[2]),
                low=float(parts[3]),
                close=float(parts[4]),
                volume=float(parts[5]),
                turnover=0.0,
            ))
        except (ValueError, IndexError):
            continue
    return candles
