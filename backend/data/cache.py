"""
K 线缓存层（Phase 1 增强）。

设计原则（参考 TDD §5 klines_{market}_{interval} 表）：
  - 所有 K 线先查 SQLite，未命中再调上游 Fetcher
  - 历史 K 线（非当前进行中）永久缓存
  - 最新一根 K 线（未收盘）不可靠，每次强制从上游取
  - 上游失败时优雅降级：返回已有缓存数据

使用方式：
  from backend.data.cache import cached_get_klines
  candles = await cached_get_klines(db, market, symbol, interval, limit, end_time_ms)

语义等价于 fetcher.get_klines(...)，但大部分请求命中本地缓存。
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from backend.data.fetcher import get_fetcher
from backend.data.models import Candle, Interval, Market


# v12.11: 标的"退市/无效"负向缓存：连续失败 3 次 → 1h 内不再尝试上游（避免浪费配额 + 限流）
_NEG_CACHE: dict = {}  # (market_str, symbol) -> (fail_count, last_fail_ts)
_NEG_FAIL_THRESHOLD = 3       # 连续失败次数门槛
_NEG_BLACKLIST_SEC = 3600     # 拉黑时长 1h
_NEG_RESET_SEC = 600          # 失败计数 10 分钟无新失败则重置（避免长期累积假退市）

def _neg_is_blacklisted(market_str: str, symbol: str) -> bool:
    rec = _NEG_CACHE.get((market_str, symbol))
    if not rec: return False
    fails, ts = rec
    age = time.time() - ts
    if age > _NEG_BLACKLIST_SEC:
        _NEG_CACHE.pop((market_str, symbol), None)
        return False
    return fails >= _NEG_FAIL_THRESHOLD

def _neg_record_failure(market_str: str, symbol: str):
    rec = _NEG_CACHE.get((market_str, symbol))
    now = time.time()
    if rec and (now - rec[1]) < _NEG_RESET_SEC:
        _NEG_CACHE[(market_str, symbol)] = (rec[0] + 1, now)
    else:
        _NEG_CACHE[(market_str, symbol)] = (1, now)

def _neg_record_success(market_str: str, symbol: str):
    _NEG_CACHE.pop((market_str, symbol), None)


async def _try_upstream(market: Market, symbol: str, interval: Interval, limit: int, end_time_ms):
    """
    带港股兜底的上游拉取：
      - HK 市场：Yahoo 空/异常 → 回落到 Eastmoney(116.xxxxx)
      - 其他：按默认 fetcher
    v12.11: 退市标的负向缓存（连续 3 次失败 → 1h 不再尝试）
    v12.11: 返回 (candles, is_primary) — is_primary=False 时调用方不应缓存（避免兜底源不同复权污染主源数据）
    """
    market_str = market.value
    if _neg_is_blacklisted(market_str, symbol):
        logger.debug(f"[neg-cache] {market_str}/{symbol} 在黑名单内，跳过上游拉取")
        return [], True  # 当 primary 处理（缓存它的"空"也合理）
    fresh = []
    is_primary = True
    try:
        fetcher = get_fetcher(market)
        fresh = await fetcher.get_klines(symbol, interval, limit=limit, end_time_ms=end_time_ms)
    except Exception as e:
        # Too Many Requests / Server disconnected 是预期降级，降为 debug 减少噪音
        err_str = str(e)
        if any(k in err_str for k in ("Too Many Requests", "Rate limited", "Server disconnected", "429")):
            logger.debug(f"主数据源降级 {market.value}/{symbol}: {e}")
        else:
            logger.warning(f"主数据源失败 {market.value}/{symbol}: {e}")
    if fresh:
        _neg_record_success(market_str, symbol)
        return fresh, True
    # US 兜底：Yahoo 限频时回落 Stooq
    if market == Market.US:
        try:
            from backend.data.us_aggregator import fetch_us_klines_stooq
            logger.info(f"[us-fallback] {symbol} Yahoo 无数据，尝试 Stooq")
            fresh = await fetch_us_klines_stooq(symbol, interval, limit=limit)
            if fresh:
                is_primary = False  # Stooq 不复权，与 Yahoo 主源不一致 — 不写缓存
        except Exception as e:
            logger.debug(f"Stooq US 兜底失败 {symbol}: {e}")
    # HK 兜底：Yahoo 对部分小盘港股无数据 → 先腾讯、再东财
    if market == Market.HK:
        try:
            from backend.data.tencent_hk import fetch_hk_klines
            logger.info(f"[hk-fallback] {symbol} Yahoo 无数据，回落腾讯港股")
            fresh = await fetch_hk_klines(symbol, interval, limit=limit, end_time_ms=end_time_ms)
            if fresh:
                # 腾讯港股是前复权数据，质量与 Yahoo 相当，允许写缓存
                # （云端 Yahoo 被限速时腾讯即为事实主源）
                is_primary = True
        except Exception as e:
            logger.debug(f"腾讯港股兜底失败 {symbol}: {e}")
        if not fresh:
            try:
                from backend.data.eastmoney import EastMoneyFetcher
                em = EastMoneyFetcher()
                logger.info(f"[hk-fallback] {symbol} 腾讯无数据，再回落东财")
                fresh = await em.get_klines(symbol, interval, limit=limit, end_time_ms=end_time_ms)
                if fresh:
                    is_primary = False
            except Exception as e:
                logger.warning(f"东财港股兜底失败 {symbol}: {e}")
    # v12.11: 记录上游结果到负向缓存
    if fresh:
        _neg_record_success(market_str, symbol)
    else:
        _neg_record_failure(market_str, symbol)
        rec = _NEG_CACHE.get((market_str, symbol))
        if rec and rec[0] >= _NEG_FAIL_THRESHOLD:
            logger.warning(f"[neg-cache] {market_str}/{symbol} 连续 {rec[0]} 次失败 → 拉黑 {_NEG_BLACKLIST_SEC//60}min")
    return fresh, is_primary

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 周期时长映射（毫秒）— 用于判断最后一根 K 线是否已收盘
# ═══════════════════════════════════════════════════════════════════

_INTERVAL_MS = {
    Interval.M1: 60 * 1000,
    Interval.M5: 5 * 60 * 1000,
    Interval.M15: 15 * 60 * 1000,
    Interval.M30: 30 * 60 * 1000,
    Interval.H1: 60 * 60 * 1000,
    Interval.H4: 4 * 60 * 60 * 1000,
    Interval.D1: 24 * 60 * 60 * 1000,
    Interval.W1: 7 * 24 * 60 * 60 * 1000,
    Interval.MN: 30 * 24 * 60 * 60 * 1000,  # 粗略估计
}


def _is_closed(candle_ts: int, interval: Interval, now_ms: Optional[int] = None) -> bool:
    """判断某根 K 线是否已收盘。
    v12.11 修：之前 `*2` 让 1D K 要等 48h 才算收盘，缓存几乎不命中。
    正确语义：candle_ts 是该 K 的开盘时间，开盘 + 周期 = 该 K 的收盘时刻；
    即 now_ms >= ts + period 时该 K 已收盘。
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    period = _INTERVAL_MS.get(interval, 60 * 60 * 1000)
    return now_ms >= candle_ts + period


def _candle_dict_to_obj(d: dict) -> Candle:
    """DB 行（dict）→ Candle 对象。"""
    return Candle(
        timestamp=d["timestamp"],
        open=d["open"],
        high=d["high"],
        low=d["low"],
        close=d["close"],
        volume=d["volume"],
        turnover=d.get("turnover", 0.0),
    )


def _candle_obj_to_dict(c: Candle) -> dict:
    """Candle 对象 → DB 写入用 dict。"""
    return {
        "timestamp": c.timestamp,
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": c.volume,
        "turnover": c.turnover,
    }


# ═══════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════


async def cached_get_klines(
    db,
    market: Market,
    symbol: str,
    interval: Interval,
    limit: int = 500,
    end_time_ms: Optional[int] = None,
) -> List[Candle]:
    """
    带缓存的 K 线查询。
    行为等价于 fetcher.get_klines(symbol, interval, limit, end_time_ms)，
    但优先命中 SQLite，减少 90%+ 的上游请求。

    三种场景：

    1. 懒加载历史（end_time_ms 不为 None）
       - 通常用户在浏览器"往左拖"。查找 ts < end_time_ms 的数据。
       - 本地有足够数据 → 直接返回
       - 不够 → 向上游拉 end_time_ms 之前的 limit 根，存库，返回

    2. 实时最新（end_time_ms 为 None）
       - 用户打开图表获取最新 N 根。
       - 本地有"最新一根且已收盘"且总量 ≥ limit → 直接返回最后 limit 根
       - 否则 → 向上游拉最新 limit 根，存库，返回

    3. 上游失败时 → 降级返回本地缓存（即使数据略陈旧，也比空图好）
    """
    market_str = market.value
    interval_str = interval.value
    now_ms = int(time.time() * 1000)

    # ═══ 场景 1：懒加载历史（end_time_ms）═══
    if end_time_ms is not None:
        # 查 DB：取 ts < end_time_ms 的最后 limit 根（降序取，再升序返回）
        cached = await db.get_klines(
            market=market_str,
            interval=interval_str,
            symbol=symbol,
            end_ts=end_time_ms - 1,  # 严格更早
            limit=limit,
            order_desc=True,
        )
        if len(cached) >= limit:
            # 本地足够，直接返回（升序）
            cached.sort(key=lambda x: x["timestamp"])
            logger.debug(
                f"[cache-hit] {market_str}/{symbol}/{interval_str} historical, "
                f"n={len(cached)} end_time={end_time_ms}"
            )
            return [_candle_dict_to_obj(x) for x in cached]

        # 本地不够 → 向上游拉
        logger.info(
            f"[cache-miss] {market_str}/{symbol}/{interval_str} historical, "
            f"cached={len(cached)}/{limit}, fetching upstream..."
        )
        try:
            fresh, is_primary = await _try_upstream(market, symbol, interval, limit, end_time_ms)
            # v12.11: 仅主源结果写缓存（兜底源不同复权会污染主源数据）
            if fresh and is_primary:
                await db.save_klines(
                    market=market_str,
                    interval=interval_str,
                    symbol=symbol,
                    candles=[_candle_obj_to_dict(c) for c in fresh],
                )
            elif fresh:
                logger.info(f"[cache-skip-write] {market_str}/{symbol}/{interval_str} 来自兜底源，不写缓存（避免复权污染）")
            return fresh
        except Exception as e:
            logger.warning(
                f"[cache-fallback] {market_str}/{symbol}/{interval_str} upstream failed ({e}), "
                f"returning stale cached={len(cached)}"
            )
            # 降级：返回本地已有的（即使不足 limit）
            cached.sort(key=lambda x: x["timestamp"])
            return [_candle_dict_to_obj(x) for x in cached]

    # ═══ 场景 2：实时最新（end_time_ms=None）═══
    # 检查本地 K 线情况
    kline_range = await db.get_kline_range(
        market=market_str, interval=interval_str, symbol=symbol
    )

    # 策略：本地最新 K 线是"已收盘的前一根"时可以直接用
    # 但"当前进行中的 K 线"必须从上游拿（因为还在变）
    # 简化处理：如果本地 max_ts 的 K 线已确定收盘（距今超过 2 个周期），
    # 且本地总量够 limit，直接返回最新 limit 根
    if (
        kline_range
        and kline_range["count"] >= limit
        and _is_closed(kline_range["max_ts"], interval, now_ms)
    ):
        # 这种情况意味着用户近期没刷新图表过，本地 K 线已经落后了
        # 但仍需至少尝试拉"最新一小段"补上
        # 为保证实时性，此路径只在启动时、或确实本地没上游支持时走
        # 默认都走上游获取最新
        pass

    # 默认策略：最新 K 线必须从上游取（保证实时）
    try:
        fresh, is_primary = await _try_upstream(market, symbol, interval, limit, None)
        # v12.11: 仅主源结果写缓存
        if fresh and is_primary:
            await db.save_klines(
                market=market_str,
                interval=interval_str,
                symbol=symbol,
                candles=[_candle_obj_to_dict(c) for c in fresh],
            )
        elif fresh:
            logger.info(f"[cache-skip-write] {market_str}/{symbol}/{interval_str} 来自兜底源，不写缓存")
        logger.debug(
            f"[cache-write] {market_str}/{symbol}/{interval_str} latest, "
            f"n={len(fresh)}"
        )
        return fresh
    except Exception as e:
        # 上游失败 → 降级返回本地缓存
        logger.warning(
            f"[cache-fallback] {market_str}/{symbol}/{interval_str} upstream failed ({e}), "
            f"returning cached"
        )
        cached = await db.get_klines(
            market=market_str,
            interval=interval_str,
            symbol=symbol,
            limit=limit,
            order_desc=True,
        )
        cached.sort(key=lambda x: x["timestamp"])
        return [_candle_dict_to_obj(x) for x in cached]
