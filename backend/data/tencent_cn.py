"""
腾讯财经 A 股数据源（A 股多源降级之备用源 2）。

作为新浪之后的下一级降级。
腾讯接口稳定性好，但格式较特殊，响应速度稍慢。

接口约定：
  - K 线: https://ifzq.gtimg.cn/appstock/app/fqkline/get
  - 实时: https://qt.gtimg.cn/q=sh600519

由聚合层 cn_aggregator.py 调用。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import aiohttp

from backend.data.models import Candle, Interval

logger = logging.getLogger(__name__)

_BJ_TZ = timezone(timedelta(hours=8))

# 腾讯 K 线周期映射
_TENCENT_TYPE = {
    Interval.M5: ("m5", 5),
    Interval.M15: ("m15", 15),
    Interval.M30: ("m30", 30),
    Interval.H1: ("m60", 60),
    Interval.D1: ("day", None),
    Interval.W1: ("week", None),
    Interval.MN: ("month", None),
}


def _get_tencent_symbol(symbol: str) -> str:
    """腾讯代码: sh600519, sz000001"""
    s = symbol.replace(".SH", "").replace(".SZ", "").upper()
    if s.startswith(("60", "68", "51")):
        return f"sh{s}"
    if s.startswith(("00", "30", "15", "16")):
        return f"sz{s}"
    return f"sh{s}"


class TencentCNFetcher:
    """腾讯财经 A 股数据源。"""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                "Referer": "https://stockapp.finance.qq.com/",
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
        """获取历史 K 线。"""
        if interval == Interval.M1:
            # 腾讯不提供 1m 历史，直接失败让上层再降级
            return []

        if interval == Interval.H4:
            # 拉 1H 再合并
            hour = await self.get_klines(symbol, Interval.H1, limit=limit * 4, end_time_ms=end_time_ms)
            return _merge_h1_to_h4(hour)[-limit:]

        tencent_sym = _get_tencent_symbol(symbol)
        type_info = _TENCENT_TYPE.get(interval)
        if not type_info:
            return []
        type_str, scale = type_info
        is_minute = scale is not None  # 分钟级走 mkline 接口

        # 腾讯和新浪一样按"最近 N 根"返回，懒加载时拉最大数量再本地过滤
        if is_minute:
            # 分钟级: https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m60,,N
            url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
            fetch_count = 320 if end_time_ms else min(limit, 320)
            params = {"param": f"{tencent_sym},{type_str},,{fetch_count}"}
            data_key_candidates = [type_str]  # 分钟级返回 key = m60/m5 等
        else:
            # 日/周/月: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,N,qfq
            url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            fetch_count = 640 if end_time_ms else min(limit, 640)
            params = {"param": f"{tencent_sym},{type_str},,,{fetch_count},qfq"}
            data_key_candidates = [f"qfq{type_str}", type_str]

        try:
            session = await self._get_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"[tencent] 请求失败: {e}")
            return []

        if data.get("code") != 0:
            logger.warning(f"[tencent] 非 0 返回: {data.get('msg')}")
            return []

        try:
            inner = data["data"][tencent_sym]
        except Exception:
            logger.warning(f"[tencent] 响应结构异常")
            return []

        klines_raw = None
        for k in data_key_candidates:
            if k in inner and isinstance(inner[k], list):
                klines_raw = inner[k]
                break
        if not klines_raw:
            logger.warning(f"[tencent] 找不到 K 线字段 候选={data_key_candidates}, keys={list(inner.keys())}")
            return []

        candles: List[Candle] = []
        for row in klines_raw:
            try:
                # 分钟级: [time_str, open, close, high, low, volume, info_or_ignored]
                #   time_str 格式 "202603021030" 或 "2026-03-02 10:30"
                # 日线:   ["2026-03-02", open, close, high, low, volume, ...]
                day_str = row[0]
                o, c, h, l, v = row[1], row[2], row[3], row[4], row[5]

                # 解析时间
                if isinstance(day_str, str):
                    if len(day_str) == 12 and day_str.isdigit():
                        # 202603021030 → 分钟级
                        dt = datetime.strptime(day_str, "%Y%m%d%H%M")
                    elif " " in day_str:
                        dt = datetime.strptime(day_str, "%Y-%m-%d %H:%M")
                    elif len(day_str) >= 10:
                        dt = datetime.strptime(day_str[:10], "%Y-%m-%d")
                    else:
                        continue
                    dt = dt.replace(tzinfo=_BJ_TZ)
                    ts = int(dt.timestamp() * 1000)
                else:
                    continue

                candles.append(
                    Candle(
                        timestamp=ts,
                        open=float(o),
                        close=float(c),
                        high=float(h),
                        low=float(l),
                        volume=float(v) if v else 0.0,
                        turnover=float(row[8]) * 10000 if len(row) > 8 and row[8] else 0.0,
                    )
                )
            except Exception as e:
                logger.debug(f"[tencent] 解析一行失败: {e}, row={row}")
                continue

        if end_time_ms:
            candles = [c for c in candles if c.timestamp < end_time_ms]

        candles.sort(key=lambda x: x.timestamp)
        if len(candles) > limit:
            candles = candles[-limit:]
        return candles


def _merge_h1_to_h4(hour: List[Candle]) -> List[Candle]:
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
