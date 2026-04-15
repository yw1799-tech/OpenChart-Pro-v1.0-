"""
新浪财经 A 股数据源（A 股多源降级之备用源 1）。

用于东方财富限流/不可用时的降级。
实时行情响应快（秒级），K 线数据齐全。

接口约定：
  - K 线（日线+分钟级）:
    https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData
  - 实时: http://hq.sinajs.cn/list=sh600519

由聚合层 cn_aggregator.py 调用，不直接对外。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from backend.data.models import Candle, Interval

logger = logging.getLogger(__name__)

_BJ_TZ = timezone(timedelta(hours=8))

# 新浪 K 线周期编码
_SINA_SCALE = {
    Interval.M5: 5,
    Interval.M15: 15,
    Interval.M30: 30,
    Interval.H1: 60,
    Interval.D1: 240,  # 新浪 1D 用 240 分钟聚合；但日线专用 API 更准
}


def _get_sina_symbol(symbol: str) -> str:
    """
    转换为新浪代码格式：sh600519, sz000001
    """
    s = symbol.replace(".SH", "").replace(".SZ", "").upper()
    if s.startswith(("60", "68", "51")):
        return f"sh{s}"
    if s.startswith(("00", "30", "15", "16")):
        return f"sz{s}"
    if s.startswith(("4", "8")):
        return f"bj{s}"
    # 默认沪市
    return f"sh{s}"


class SinaCNFetcher:
    """
    新浪财经 A 股备用数据源。
    只实现 get_klines（供降级用），不做 realtime 订阅（用东财的）。
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                "Referer": "https://finance.sina.com.cn/",
            }
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_klines(
        self,
        symbol: str,
        interval: Interval,
        limit: int = 500,
        end_time_ms: Optional[int] = None,
    ) -> List[Candle]:
        """
        获取历史 K 线。
        新浪分钟级 API：https://quotes.sina.cn/cn/api/jsonp_v2.php/.../CN_MarketDataService.getKLineData
        日线 API：     https://finance.sina.com.cn/realstock/company/<code>/hisdata_klc.js
        """
        if interval == Interval.W1 or interval == Interval.MN:
            logger.debug(f"[sina] interval {interval} 不支持，降级失败")
            return []

        if interval == Interval.H4:
            # 1H 拉 4 倍再合并
            hour = await self.get_klines(symbol, Interval.H1, limit=limit * 4, end_time_ms=end_time_ms)
            return _merge_h1_to_h4(hour)[-limit:]

        sina_sym = _get_sina_symbol(symbol)
        scale = _SINA_SCALE.get(interval)
        if scale is None:
            return []

        # 新浪 K 线 API（纯 JSON 返回）
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {
            "symbol": sina_sym,
            "scale": str(scale),
            "ma": "no",
            "datalen": str(min(limit + 100 if end_time_ms else limit, 1023)),  # 新浪最多 1023
        }

        try:
            session = await self._get_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                text = await r.text()
        except Exception as e:
            logger.warning(f"[sina] 请求失败: {e}")
            return []

        # 响应为纯 JSON: [{"day":"2026-04-14 10:30:00","open":"...","high":"...","low":"...","close":"...","volume":"..."}]
        try:
            entries = json.loads(text)
            if not isinstance(entries, list) or not entries:
                logger.warning(f"[sina] 空或非列表响应: {text[:200]}")
                return []
        except json.JSONDecodeError:
            logger.warning(f"[sina] JSON 解析失败: {text[:200]}")
            return []

        candles: List[Candle] = []
        for item in entries:
            try:
                day_str = item["day"]
                # 格式: "2026-04-14 10:30:00" 或 "2026-04-14"
                if " " in day_str:
                    dt = datetime.strptime(day_str, "%Y-%m-%d %H:%M:%S")
                else:
                    dt = datetime.strptime(day_str, "%Y-%m-%d")
                dt = dt.replace(tzinfo=_BJ_TZ)
                ts = int(dt.timestamp() * 1000)

                candles.append(
                    Candle(
                        timestamp=ts,
                        open=float(item["open"]),
                        high=float(item["high"]),
                        low=float(item["low"]),
                        close=float(item["close"]),
                        volume=float(item["volume"]),
                        turnover=0.0,  # 新浪 K 线不返回成交额
                    )
                )
            except (KeyError, ValueError) as e:
                logger.debug(f"[sina] 解析一行失败: {e}, item={item}")
                continue

        # 客户端按 end_time_ms 过滤（新浪无精确 end 参数）
        if end_time_ms:
            candles = [c for c in candles if c.timestamp < end_time_ms]

        candles.sort(key=lambda x: x.timestamp)
        if len(candles) > limit:
            candles = candles[-limit:]
        return candles


def _merge_h1_to_h4(hour: List[Candle]) -> List[Candle]:
    """将 1H K 线按每 4 根合并为 4H K 线（A 股专用，因为 A 股每日 4 小时交易）。"""
    if not hour:
        return []
    out: List[Candle] = []
    for i in range(0, len(hour), 4):
        grp = hour[i : i + 4]
        if not grp:
            continue
        out.append(
            Candle(
                timestamp=grp[0].timestamp,
                open=grp[0].open,
                high=max(c.high for c in grp),
                low=min(c.low for c in grp),
                close=grp[-1].close,
                volume=sum(c.volume for c in grp),
                turnover=sum(c.turnover for c in grp),
            )
        )
    return out
