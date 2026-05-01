"""
A 股多源聚合层（Phase 1 增强）。

主源：东方财富 (EastMoneyFetcher)
备1：新浪财经 (SinaCNFetcher)
备2：腾讯财经 (TencentCNFetcher)

工作原理：
  - 每个源维护健康度计数（连续失败次数、上次失败时间）
  - 请求按健康度顺序尝试
  - 连续失败则降权，进入冷却期（60 秒）
  - 冷却期结束后自动恢复尝试
  - 所有源失败 → 抛异常让上层（缓存层）降级到 DB

对外暴露的接口完全兼容 DataFetcher 基类（get_symbols / get_klines / subscribe_realtime / unsubscribe）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, List, Optional

from backend.data.eastmoney import EastMoneyFetcher
from backend.data.fetcher import DataFetcher
from backend.data.models import Candle, Interval, Symbol
from backend.data.sina_cn import SinaCNFetcher
from backend.data.tencent_cn import TencentCNFetcher

logger = logging.getLogger(__name__)


# 冷却期：连续失败后多久不再尝试（秒）
_COOLDOWN_SECONDS = 60
# 连续失败达到此阈值进入冷却
_FAIL_THRESHOLD = 3


class _SourceHealth:
    """单个数据源的健康度状态。"""

    def __init__(self, name: str):
        self.name = name
        self.consecutive_failures = 0
        self.cooldown_until = 0.0  # Unix 秒

    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    def record_success(self):
        self.consecutive_failures = 0
        self.cooldown_until = 0.0

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= _FAIL_THRESHOLD:
            self.cooldown_until = time.time() + _COOLDOWN_SECONDS
            logger.warning(
                f"[cn-agg] {self.name} 进入冷却: {self.consecutive_failures} 次连续失败, "
                f"冷却至 {_COOLDOWN_SECONDS}s 后"
            )

    def __repr__(self):
        status = "available" if self.is_available() else f"cooldown({int(self.cooldown_until - time.time())}s)"
        return f"<{self.name} fails={self.consecutive_failures} {status}>"


class CNAggregatorFetcher(DataFetcher):
    """
    A 股聚合源。对外 API 与 DataFetcher 完全一致。
    内部按健康度排序尝试多个源。
    """

    def __init__(self):
        # 主源 + 两个备源
        self._eastmoney = EastMoneyFetcher()
        self._sina = SinaCNFetcher()
        self._tencent = TencentCNFetcher()

        self._sources = [
            (self._eastmoney, _SourceHealth("eastmoney")),
            (self._sina, _SourceHealth("sina")),
            (self._tencent, _SourceHealth("tencent")),
        ]

    def _ordered_sources(self):
        """按可用性排序：可用的在前，冷却中的在后。同级按主备顺序。"""
        return sorted(
            self._sources,
            key=lambda x: (not x[1].is_available(), self._sources.index(x)),
        )

    async def get_symbols(self, query: str = "") -> List[Symbol]:
        """
        品种搜索只用东方财富（唯一有搜索接口的源）。
        失败时计入健康度冷却，避免在冷却期内持续重试。
        """
        em_health = self._sources[0][1]
        if not em_health.is_available():
            logger.debug("[cn-agg] get_symbols: eastmoney 冷却中，返回空")
            return []
        try:
            result = await self._eastmoney.get_symbols(query=query)
            em_health.record_success()
            return result
        except Exception as e:
            logger.warning(f"[cn-agg] get_symbols 失败: {e}")
            em_health.record_failure()
            return []

    async def get_klines(
        self,
        symbol: str,
        interval: Interval,
        limit: int = 500,
        end_time_ms: Optional[int] = None,
    ) -> List[Candle]:
        """
        按健康度顺序尝试每个源，返回第一个成功的结果。
        所有源均失败时返回空列表（上层缓存层会降级到 DB）。
        """
        sources = self._ordered_sources()
        last_exception: Optional[Exception] = None

        for fetcher, health in sources:
            if not health.is_available():
                continue
            try:
                candles = await fetcher.get_klines(
                    symbol=symbol, interval=interval, limit=limit, end_time_ms=end_time_ms
                )
                # 判定成功标准：非空（空返回视为"该源不支持此品种/周期"，尝试下一个）
                if candles:
                    health.record_success()
                    if health.name != "eastmoney":
                        logger.info(
                            f"[cn-agg] 降级成功: {health.name} 返回 {len(candles)} 根 "
                            f"(symbol={symbol} interval={interval.value})"
                        )
                    return candles
                # 空结果不标记失败，直接试下一个
                logger.debug(f"[cn-agg] {health.name} 空返回，尝试下一源")
            except Exception as e:
                last_exception = e
                logger.warning(f"[cn-agg] {health.name} 异常: {e}")
                health.record_failure()

        # 所有源都失败或返回空
        if last_exception:
            logger.error(
                f"[cn-agg] 所有 A 股数据源失败 (symbol={symbol} interval={interval.value}), "
                f"last_error={last_exception}"
            )
        return []

    async def subscribe_realtime(
        self,
        symbol: str,
        interval: Interval,
        callback: Callable,
    ) -> None:
        """
        实时订阅复用东方财富（轮询实现）。
        其他源的 realtime 未实装，此处不做多源降级（复杂度不值）。
        """
        await self._eastmoney.subscribe_realtime(symbol, interval, callback)

    async def unsubscribe(self, symbol: str) -> None:
        """取消订阅（委托给东财）。"""
        await self._eastmoney.unsubscribe(symbol)

    async def close(self):
        """清理所有源的 session。"""
        for f, _ in self._sources:
            close = getattr(f, "close", None)
            if close:
                try:
                    await close()
                except Exception:
                    pass

    def get_health(self) -> List[dict]:
        """调试用：获取各源健康度快照。"""
        return [
            {
                "name": h.name,
                "available": h.is_available(),
                "consecutive_failures": h.consecutive_failures,
                "cooldown_remaining_sec": max(0, int(h.cooldown_until - time.time())),
            }
            for _, h in self._sources
        ]
