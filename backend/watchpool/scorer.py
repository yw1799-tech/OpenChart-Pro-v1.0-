"""
候选池三维评分（Phase 6 扩展）。

total = event (0-50) + technical (0-30) + fundamentals (0-20)

- event_score  入池时产生，保留取 max
- technical_score  每小时后台批量重算（基于 60 根日 K）
- fundamentals_score  24h 缓存（与 symbol_fundamentals 同周期）
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from backend.data.cache import cached_get_klines
from backend.data.models import Interval, Market

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 事件分（0-50）—— 入池时直接传入，下面提供 3 个压缩公式供各入口复用
# ═══════════════════════════════════════════════════════════════════


def event_score_anomaly(change_pct: float, rank: int) -> float:
    """涨幅榜：涨幅 + 排名靠前加分。"""
    return min(50.0, max(0.0, change_pct * 1.0 + (30 - min(rank, 30)) * 0.25))


def event_score_news(importance: int) -> float:
    """
    新闻事件分：
      ★1=16 / ★2=22 / ★3=28 / ★4=34 / ★5=40
    公式：10 + importance × 6
    过去版本 importance × 4 让 ★3 只得 12 分，加上 tech+fund 仍难过 40 阈值 → 大量被淘汰。
    新公式让 ★3 就有 28 分，配合技术+基本面能轻松过门槛。
    """
    return min(50.0, max(0.0, 10 + importance * 6.0))


def event_score_ai(importance: int, strength: float) -> float:
    """AI 解读：importance × 2 + strength × 10"""
    return min(50.0, max(0.0, importance * 2.0 + strength * 10.0))


# ═══════════════════════════════════════════════════════════════════
# 技术分（0-30）
# ═══════════════════════════════════════════════════════════════════


def _ma(values, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _ema_series(values, period: int) -> list:
    if not values:
        return []
    k = 2 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def _rsi(closes, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def _macd(closes) -> Optional[Dict]:
    if len(closes) < 35:
        return None
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema_series(dif, 9)
    hist = [(d - e) * 2 for d, e in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "hist": hist}


async def compute_technical_score(db, market: str, symbol: str) -> float:
    """
    根据最近 60 根日 K 计算技术分。失败返回 0。
    """
    try:
        m = Market(market)
        candles = await cached_get_klines(db=db, market=m, symbol=symbol, interval=Interval.D1, limit=60)
    except Exception as e:
        logger.debug(f"[scorer-tech] {symbol}/{market} K线获取失败: {e}")
        return 0.0
    if not candles or len(candles) < 25:
        return 0.0

    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]

    score = 0.0
    # ── MACD 金叉/死叉 ──
    macd = _macd(closes)
    if macd and len(macd["hist"]) >= 2:
        last = macd["hist"][-1]
        prev = macd["hist"][-2]
        if prev < 0 and last > 0:
            score += 10   # 金叉
        elif prev > 0 and last < 0:
            score -= 5    # 死叉
        elif last > 0:
            score += 4    # 柱在零轴上
        elif last < 0:
            score -= 2

    # ── RSI 区间 ──
    rsi = _rsi(closes, 14)
    if rsi is not None:
        if 30 <= rsi < 40:
            score += 8    # 底部反弹机会
        elif 40 <= rsi <= 60:
            score += 0    # 中性
        elif rsi < 30:
            score += 5    # 超卖
        elif 60 < rsi <= 70:
            score -= 3    # 顶部
        elif rsi > 70:
            score -= 5    # 超买

    # ── 均线排列 ──
    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            score += 8   # 多头排列
        elif ma5 < ma10 < ma20:
            score -= 5   # 空头排列

    # ── 放量 ──
    if len(volumes) >= 20:
        recent_5 = sum(volumes[-5:]) / 5
        avg_20 = sum(volumes[-20:]) / 20
        if avg_20 > 0 and recent_5 >= avg_20 * 1.5:
            score += 4
        elif avg_20 > 0 and recent_5 < avg_20 * 0.5:
            score -= 2   # 缩量

    return max(0.0, min(30.0, score))


# ═══════════════════════════════════════════════════════════════════
# 基本面分（0-20）
# ═══════════════════════════════════════════════════════════════════


def compute_fundamentals_score(market: str, fund: Dict) -> float:
    """
    根据 symbol_fundamentals 记录算基本面分。
    没有数据返回 0。
    """
    if not fund:
        return 0.0

    score = 0.0
    cap = float(fund.get("market_cap") or 0)
    pe = float(fund.get("pe") or 0)
    avg_turnover = float(fund.get("avg_turnover") or 0)

    # ── 市值档位 ──
    if market == "cn":
        if cap > 50_000_000_000:     # >500 亿
            score += 10
        elif cap > 10_000_000_000:   # 100-500 亿
            score += 7
        elif cap > 0:
            score += 3
    elif market == "hk":
        if cap > 50_000_000_000:     # >500 亿 HKD
            score += 10
        elif cap > 10_000_000_000:   # 100-500 亿 HKD
            score += 7
        elif cap > 0:
            score += 3
    elif market == "us":
        if cap > 10_000_000_000:     # >$10B
            score += 10
        elif cap > 2_000_000_000:    # $2-10B
            score += 7
        elif cap > 0:
            score += 3

    # ── 流动性（成交活跃）──
    # 用各市场的阈值判断：高于筛选阈值 2 倍 +5，高于 1 倍 +3
    import backend.config as config
    thresholds = {
        "cn": config.POOL_CN_MIN_AVG_TURNOVER,
        "hk": config.POOL_HK_MIN_AVG_TURNOVER,
    }
    if market in thresholds and avg_turnover > 0:
        t = thresholds[market]
        if avg_turnover >= t * 2:
            score += 5
        elif avg_turnover >= t:
            score += 3
    elif market == "us":
        vol = float(fund.get("avg_volume") or 0)
        if vol >= config.POOL_US_MIN_AVG_VOLUME * 2:
            score += 5
        elif vol >= config.POOL_US_MIN_AVG_VOLUME:
            score += 3

    # ── PE 合理区间 ──
    # 盈利公司且 PE 在 5-40 之间算合理
    if 5 <= pe <= 40:
        score += 5
    elif 40 < pe <= 80:
        score += 2   # 偏高
    elif pe <= 0 or pe > 80:
        score += 0   # 亏损或过高

    return max(0.0, min(20.0, score))


# ═══════════════════════════════════════════════════════════════════
# 批量重算（后台每小时跑）
# ═══════════════════════════════════════════════════════════════════


async def _batch_prefetch_klines(db, items: List[Dict]) -> Dict[Tuple[str, str], List]:
    """
    批量预取所有候选股票近 60 根日 K。按市场分组，每市场一次 SELECT。
    返回 {(symbol, market): [candle_dict, ...]} (按 timestamp ASC)
    """
    from collections import defaultdict
    by_mkt: Dict[str, List[str]] = defaultdict(list)
    for it in items:
        mkt = it.get("market")
        sym = it.get("symbol")
        if mkt in ("cn", "hk", "us", "crypto") and sym:
            by_mkt[mkt].append(sym)

    result: Dict[Tuple[str, str], List] = {}
    for mkt, syms in by_mkt.items():
        if not syms:
            continue
        tbl = f"klines_{mkt}_1d"
        placeholders = ",".join("?" for _ in syms)
        # 每只股票取最近 60 根（用窗口函数）
        sql = f"""
            SELECT symbol, timestamp, open, high, low, close, volume
            FROM (
              SELECT symbol, timestamp, open, high, low, close, volume,
                ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rn
              FROM [{tbl}]
              WHERE symbol IN ({placeholders})
            )
            WHERE rn <= 60
            ORDER BY symbol, timestamp ASC
        """
        try:
            async with db.acquire() as conn:
                cur = await conn.execute(sql, syms)
                rows = await cur.fetchall()
            # 按 symbol 分组
            for r in rows:
                key = (r["symbol"], mkt)
                result.setdefault(key, []).append({
                    "timestamp": r["timestamp"],
                    "open": r["open"], "high": r["high"], "low": r["low"],
                    "close": r["close"], "volume": r["volume"],
                })
        except Exception as e:
            logger.warning(f"[scorer] 批量 K 线拉取 {mkt} 失败: {e}")
    return result


def _compute_tech_from_candles(candles: List[Dict]) -> float:
    """纯计算版：给定 60 根日 K dict 数组（timestamp ASC），返回 tech_score。"""
    if not candles or len(candles) < 25:
        return 0.0
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    score = 0.0
    macd = _macd(closes)
    if macd and len(macd["hist"]) >= 2:
        last = macd["hist"][-1]; prev = macd["hist"][-2]
        if prev < 0 and last > 0: score += 10
        elif prev > 0 and last < 0: score -= 5
        elif last > 0: score += 4
        elif last < 0: score -= 2

    rsi = _rsi(closes, 14)
    if rsi is not None:
        if 30 <= rsi < 40: score += 8
        elif 40 <= rsi <= 60: score += 0
        elif rsi < 30: score += 5
        elif 60 < rsi <= 70: score -= 3
        elif rsi > 70: score -= 5

    ma5 = _ma(closes, 5); ma10 = _ma(closes, 10); ma20 = _ma(closes, 20)
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20: score += 8
        elif ma5 < ma10 < ma20: score -= 5
        elif ma5 > ma20: score += 3

    if len(volumes) >= 20:
        recent_5 = sum(volumes[-5:]) / 5
        avg_20 = sum(volumes[-20:]) / 20
        if avg_20 > 0 and recent_5 >= avg_20 * 1.5: score += 4
        elif avg_20 > 0 and recent_5 < avg_20 * 0.5: score -= 2

    return max(0.0, min(30.0, score))


async def rescore_pool_items(db) -> Dict[str, int]:
    """
    扫候选池所有条目：
      - 批量预取所有股票的 60 根日 K（按市场一次 SELECT，消除 N 次查询）
      - 批量按 50 只一 commit（避免 N 次 fsync 撑爆 WAL）
      - 复用缓存 fundamentals（24h TTL 内不会重复请求上游）
      - 算 technical_score + fundamentals_score
      - total = event_score + tech + fund
    返回 {updated, failed} 统计。
    """
    import json as _json
    from backend.watchpool.quality_filter import _load_any

    items = await db.get_pool_items(status='all', limit=500)
    items = [it for it in items if it.get('status') != 'archived']
    updated = failed = 0
    now_ms = int(time.time() * 1000)

    # 1) 批量预取所有候选 60 根日 K（原来是 500 次 cached_get_klines，现在 4 次 SELECT）
    t0 = time.time()
    kline_cache = await _batch_prefetch_klines(db, items)
    logger.info(f"[scorer] 批量预取 K 线 {len(kline_cache)} 只 耗时 {time.time()-t0:.1f}s")

    # 2) 分批事务处理（50 只一 commit）
    BATCH = 50
    pending: List[Tuple] = []   # (update_tuple, insert_tuple)

    async def _flush_batch():
        if not pending:
            return
        async with db.acquire() as conn:
            for upd, ins in pending:
                await conn.execute(
                    """UPDATE watch_pool
                       SET event_score=?, technical_score=?, fundamentals_score=?, score=?,
                           last_scored_at=?
                       WHERE id=? AND status != 'archived'""",
                    upd,
                )
                await conn.execute(
                    """INSERT INTO pool_score_history (pool_item_id, score, factors, scored_at)
                       VALUES (?, ?, ?, ?)""",
                    ins,
                )
            await conn.commit()
        pending.clear()

    for it in items:
        try:
            market = it["market"]; symbol = it["symbol"]
            candles = kline_cache.get((symbol, market), [])
            tech = _compute_tech_from_candles(candles)
            fund_row = await _load_any(db, symbol, market, max_stale_days=30)
            fund = compute_fundamentals_score(market, fund_row or {})
            event = float(it.get("event_score") or 0)
            if event == 0:
                event = min(50.0, float(it.get("score") or 0) * 0.5)
            total = min(100.0, event + tech + fund)
            factors = _json.dumps(
                {"event": round(event, 1), "tech": round(tech, 1), "fund": round(fund, 1)},
                ensure_ascii=False,
            )
            pending.append((
                (round(event, 1), round(tech, 1), round(fund, 1), round(total, 1), now_ms, it["id"]),
                (it["id"], round(total, 1), factors, now_ms),
            ))
            if len(pending) >= BATCH:
                await _flush_batch()
            updated += 1
        except Exception as e:
            logger.warning(f"[scorer] 重算失败 {it.get('symbol')}/{it.get('market')}: {e}")
            failed += 1

    await _flush_batch()  # 收尾
    logger.info(f"[scorer] 候选池批量重算完成 updated={updated} failed={failed}")
    return {"updated": updated, "failed": failed}
