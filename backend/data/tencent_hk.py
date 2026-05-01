"""
腾讯港股 K 线数据源（兜底用）。

当 Yahoo Finance 不支持某只港股（尤其是新上市的 5 位代码小盘股）时调用。
接口：https://ifzq.gtimg.cn/appstock/app/hkfqkline/get?param=hk{code},day|60|15|5,,,N,qfq

支持周期：day / week / month / 60(1H) / 30 / 15 / 5。
不支持 1m / 4H（4H 可由 1H 合并）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import aiohttp

from backend.data.models import Candle, Interval

logger = logging.getLogger(__name__)

_BJ_TZ = timezone(timedelta(hours=8))

# Interval → Tencent 周期代码 映射
_TX_PERIOD = {
    Interval.M5: ("m5", False),      # 分钟线返回的 key 不带 qfq 后缀
    Interval.M15: ("m15", False),
    Interval.M30: ("m30", False),
    Interval.H1: ("m60", False),
    Interval.D1: ("day", True),
    Interval.W1: ("week", True),
    Interval.MN: ("month", True),
}


def _hk_param(symbol: str) -> str:
    """'06193.HK' → 'hk06193'（5 位补零）。"""
    code = symbol.strip().upper()
    if code.endswith(".HK"):
        code = code[:-3]
    return f"hk{code.zfill(5)}"


def _parse_ts(date_str: str) -> int:
    """日线格式 YYYY-MM-DD → 毫秒；分钟线格式 YYYYMMDDHHMM → 毫秒。"""
    s = date_str.strip()
    if "-" in s:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=_BJ_TZ)
    elif len(s) == 12:
        dt = datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=_BJ_TZ)
    else:
        raise ValueError(f"无法解析时间格式: {s}")
    return int(dt.timestamp() * 1000)


async def fetch_hk_klines(
    symbol: str,
    interval: Interval,
    limit: int = 500,
    end_time_ms: Optional[int] = None,
) -> List[Candle]:
    """
    腾讯港股 K 线拉取。返回按时间升序的 Candle 列表。
    end_time_ms 暂不支持（腾讯接口只能取最近 N 根），传入时仅用作客户端过滤。
    """
    period_tuple = _TX_PERIOD.get(interval)
    if not period_tuple:
        logger.debug(f"tencent_hk 不支持的周期: {interval}")
        return []
    period, is_qfq = period_tuple

    param = f"{_hk_param(symbol)},{period},,,{max(limit, 320)},qfq"
    url = "https://ifzq.gtimg.cn/appstock/app/hkfqkline/get"
    if interval in (Interval.M5, Interval.M15, Interval.M30, Interval.H1):
        # 分钟线用另一个接口
        url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
        param = f"{_hk_param(symbol)},{period},,{max(limit, 320)}"

    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(url, params={"param": param}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                body = await r.json(content_type=None)
    except Exception as e:
        logger.warning(f"tencent_hk 请求失败 {symbol}/{interval.value}: {e}")
        return []

    data = (body or {}).get("data") or {}
    sym_data = data.get(_hk_param(symbol)) or {}
    # 日/周/月用 qfq{period}；分钟线 key 为 'm5' 等
    klines_raw = (
        sym_data.get(f"qfq{period}")
        or sym_data.get(period)
        or []
    )
    if not klines_raw:
        logger.debug(f"tencent_hk 无数据 {symbol}/{interval.value}")
        return []

    candles: List[Candle] = []
    for row in klines_raw:
        # 格式：[time, open, close, high, low, volume, ...]
        if len(row) < 6:
            continue
        try:
            ts = _parse_ts(str(row[0]))
            if end_time_ms is not None and ts >= end_time_ms:
                continue
            candles.append(Candle(
                timestamp=ts,
                open=float(row[1]),
                close=float(row[2]),
                high=float(row[3]),
                low=float(row[4]),
                volume=float(row[5]),
                turnover=float(row[8]) if len(row) > 8 else 0.0,
            ))
        except (ValueError, IndexError) as e:
            logger.debug(f"tencent_hk 解析单行失败 {row}: {e}")
            continue

    # 升序 + 截断到 limit
    candles.sort(key=lambda c: c.timestamp)
    if len(candles) > limit:
        candles = candles[-limit:]
    return candles
