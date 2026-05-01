"""
OpenChart Pro — FastAPI 主入口 (v3.0 Phase 1)

本文件只承载 Phase 1 + Phase 2 的基础路由：
  - 市场/品种/K 线
  - 指标
  - 自选列表
  - 设置
  - WebSocket

Phase 3A/4/5/6 的路由会在对应模块开发完成时以独立 router 方式注册到此处。

启动顺序 (TDD §11.1)：
  1. 初始化 SQLite 连接池 + 建表
  2. 加载 DB 覆盖配置到内存
  3. 加密 6 币种自动加入自选列表（PRD F1.8 首次使用引导）
  4. 加密 6 币种自动绑定全部内置策略（Phase 4 启用后生效）
  5. 启动 APScheduler（Phase 3A 新闻采集等）
  6. 启动 OKX WebSocket 订阅（Phase 1）
  7. 重置当日 LLM 成本计数器（Phase 3B）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import backend.config as config
from backend.data.cache import cached_get_klines
from backend.data.fetcher import get_fetcher
from backend.data.models import Interval, Market
from backend.db.database import DatabaseManager
from backend.indicators.registry import calculate_indicator, get_indicator_info, list_indicators
from backend.news.ai_analyzer import NewsAIAnalyzer
from backend.news.scheduler import NewsScheduler, attach_ai_analyzer
from backend.news.symbol_registry import registry as symbol_registry
from backend.portfolio.manager import PortfolioManager
from backend.portfolio.tracker import PortfolioTracker
from backend.signals.binding import StrategyBindingManager
from backend.signals.monitor import MonitorEngine
from backend.signals.strategies import list_strategies
from backend.trading.simulator import simulator as trading_simulator
from backend.watchpool.anomaly_scanner import AnomalyScanner
from backend.ws.hub import hub

# ═══════════════════════════════════════════════════════════════════
# 全局状态与日志
# ═══════════════════════════════════════════════════════════════════

logger = logging.getLogger("openchart")
logging.basicConfig(
    level=logging.INFO if not config.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# 抑制三方 DEBUG 噪音（aiosqlite 每条 SQL 都打日志）
for _noisy in ("aiosqlite", "httpcore", "httpx", "openai", "urllib3", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# 全局数据库单例
db = DatabaseManager(config.DB_PATH, pool_size=10)

# 运行时配置快照
_runtime_config: Dict[str, Any] = {}

# 应用级后台任务引用集（保 GC + lifespan 关闭时统一 cancel）
_app_bg_tasks: List[asyncio.Task] = []
# PortfolioManager 单例（避免重复创建）
_shared_portfolio_manager = None

# 订阅中的 WebSocket 任务
_ws_subscriptions: Dict[tuple, asyncio.Task] = {}

# 新闻采集调度器（Phase 3A，启动时初始化）
news_scheduler: Optional[NewsScheduler] = None

# 策略监控引擎 (Phase 4, 启动时初始化)
monitor_engine: Optional[MonitorEngine] = None

# 持仓追踪器 (Phase 5, 启动时初始化)
portfolio_tracker: Optional[PortfolioTracker] = None
portfolio_manager: Optional[PortfolioManager] = None

# 异动扫描器 (Phase 3A 通道②)
anomaly_scanner: Optional[AnomalyScanner] = None

# v11.6: AlertManager 实例 — 之前 alerts 端点写表但永不触发（孤儿管线）
alert_manager = None

# v12.3: TradeReviewer 实例 — 平仓后深度复盘
trade_reviewer = None

# LLM 新闻深度解读 (Phase 3B)
ai_analyzer: Optional[NewsAIAnalyzer] = None
auto_trader: Any = None   # 自动交易引擎（initialized in lifespan）


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════


async def _load_runtime_config():
    """从 DB 加载配置值到内存，形成 runtime 视图（DB 覆盖 config.py 默认值）。"""
    global _runtime_config
    db_config = await db.get_all_config()

    # 先把 config.py 的默认值铺底
    defaults = {k: v for k, v in vars(config).items() if not k.startswith("_") and k.isupper()}
    _runtime_config = dict(defaults)

    # DB 值覆盖（JSON 序列化存储复杂类型）
    for key, raw_value in db_config.items():
        try:
            _runtime_config[key] = json.loads(raw_value)
        except (json.JSONDecodeError, TypeError):
            _runtime_config[key] = raw_value
        # 同时反向写回 config 模块属性，让所有读 config.XXX 的代码都拿到最新值
        if key.isupper():
            setattr(config, key, _runtime_config[key])

    logger.info(f"Runtime config loaded: {len(_runtime_config)} keys")


def get_rt(key: str, default: Any = None) -> Any:
    """读取运行时配置值。"""
    return _runtime_config.get(key, default)


async def _ensure_crypto_watchlist():
    """加密 6 币种加入自选列表（PRD F1.8 首次使用引导）。"""
    existing = await db.get_watchlist(market="crypto")
    existing_symbols = {item["symbol"] for item in existing}

    for symbol in config.CRYPTO_SYMBOLS:
        if symbol not in existing_symbols:
            await db.add_to_watchlist(symbol=symbol, market="crypto", name=symbol)
            logger.info(f"Auto-added {symbol} to crypto watchlist")


def _get_pool_symbols() -> set:
    """同步获取候选池中所有股票符号（供 NewsScheduler 加分判定用）。
    简化实现：从 _runtime_config 取最新缓存（实际查询交给后台异步预热）。
    """
    return _runtime_config.get("_pool_symbols_cache", set())


def _get_holding_symbols() -> set:
    """返回当前持仓的 symbol 集合（含加密+股票）。用于新闻规则引擎加权判定。"""
    return _runtime_config.get("_holding_symbols_cache", set())


def _get_holding_records() -> list:
    """返回完整持仓记录列表 [{symbol,market,...}]。用于 scheduler 决定哪些新闻入持仓批量缓冲。"""
    return _runtime_config.get("_holding_records_cache", [])


async def _data_retention_loop():
    """
    数据保留策略：每天 03:00（北京时间）清理过期数据，防止 SQLite 撑爆。
    - flash_news: 保留 30 天
    - signals: 保留 30 天
    - llm_cost_log: 保留 90 天（成本审计）
    - alert_history: 保留 30 天
    - kline_*: 不动（缓存表，靠 LRU/容量自管）
    """
    while True:
        # 算到下一个北京时间 03:00 的秒数
        try:
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone(timedelta(hours=8)))
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
        except Exception:
            wait = 24 * 3600
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            break
        try:
            import time as _t
            now_ms = int(_t.time() * 1000)
            cutoff_30d = now_ms - 30 * 24 * 3600 * 1000
            cutoff_90d = now_ms - 90 * 24 * 3600 * 1000
            stats = {}
            async with db.acquire() as conn:
                # ai_diagnosis_history 用秒级时间戳；其余表是毫秒级
                cutoff_90d_sec = int(time.time()) - 90 * 86400
                for table, col, cutoff in [
                    ("flash_news", "collected_at", cutoff_30d),
                    ("signals", "generated_at", cutoff_30d),
                    ("llm_cost_log", "called_at", cutoff_90d),
                    ("alert_history", "triggered_at", cutoff_30d),
                    ("ai_diagnosis_history", "diagnosed_at", cutoff_90d_sec),
                    ("pool_score_history", "scored_at", cutoff_90d),
                ]:
                    try:
                        cur = await conn.execute(
                            f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)
                        )
                        stats[table] = cur.rowcount
                    except Exception as e:
                        logger.debug(f"[retention] {table} 清理失败: {e}")
                await conn.commit()
                # 每日 checkpoint：把 WAL 写回主库并截断文件（WAL 无限增长的唯一解决方案）
                # TRUNCATE 模式需要无其他 readers；凌晨跑基本能抓到空闲时刻
                try:
                    await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    logger.info("[retention] WAL checkpoint(TRUNCATE) 完成")
                except Exception as e:
                    logger.debug(f"[retention] wal_checkpoint 失败（可能有活跃事务）: {e}")
                # VACUUM 周日凌晨执行（重组文件回收空间）
                if datetime.now(timezone(timedelta(hours=8))).weekday() == 6:
                    try:
                        await conn.execute("VACUUM")
                    except Exception as e:
                        logger.debug(f"[retention] VACUUM 失败: {e}")
                # 同时触发 SQLite query planner 按最新统计选索引
                try:
                    await conn.execute("PRAGMA optimize")
                except Exception:
                    pass
            # 信号重复兜底：把同 key 同 dedup 桶里的老重复清掉（历史遗留 + 逻辑漏网）
            # 各 interval 分别去重，保留每个桶内最新
            # 1D 周期按"交易日"桶（CN/UTC+8 偏移 8h）— 否则跨夜的两次独立信号会被误判为重复
            try:
                INTERVAL_BUCKET_MS = {
                    "1D": 24*3600*1000, "4H": 4*3600*1000, "1H": 60*60*1000,
                    "30m": 30*60*1000, "15m": 15*60*1000, "5m": 5*60*1000, "1m": 60*1000,
                }
                # 1D 桶按交易日：先减去 8h 时区偏移再 / 24h，让 UTC+8 自然日聚合
                # 其它周期按 K 线周期对齐
                INTERVAL_BUCKET_OFFSET_MS = {"1D": 8 * 3600 * 1000}
                dedup_total = 0
                async with db.acquire() as conn:
                    cur = await conn.execute("SELECT DISTINCT interval FROM signals")
                    intervals = [r["interval"] or "1H" for r in await cur.fetchall()]
                    for iv in intervals:
                        bucket_ms = INTERVAL_BUCKET_MS.get(iv, 60*60*1000)
                        offset_ms = INTERVAL_BUCKET_OFFSET_MS.get(iv, 0)
                        res = await conn.execute(f"""
                            DELETE FROM signals WHERE id IN (
                              SELECT id FROM (
                                SELECT id, ROW_NUMBER() OVER (
                                  PARTITION BY symbol, market, action, strategy_name, interval,
                                               CAST((generated_at + {offset_ms}) / {bucket_ms} AS INTEGER)
                                  ORDER BY generated_at DESC
                                ) AS rn
                                FROM signals WHERE interval=?
                              ) WHERE rn > 1
                            )""", (iv,))
                        dedup_total += (res.rowcount or 0)
                    await conn.commit()
                if dedup_total > 0:
                    stats["signals_dedup"] = dedup_total
            except Exception as e:
                logger.debug(f"[retention] 信号去重失败: {e}")
            kept = ", ".join(f"{k}={v}" for k, v in stats.items() if v > 0)
            logger.info(f"[retention] 清理完成 {kept or '无可清理数据'}")
        except Exception as e:
            logger.warning(f"数据保留任务异常: {e}")


async def _pool_rescore_loop():
    """每小时对候选池做三维评分重算。启动后延迟 60s 执行首次。"""
    await asyncio.sleep(60)
    while True:
        try:
            from backend.watchpool.scorer import rescore_pool_items
            await rescore_pool_items(db)
        except Exception as e:
            logger.warning(f"候选池重评分循环异常: {e}")
        await asyncio.sleep(3600)


async def _stock_auto_bind_loop():
    """
    每 5 分钟扫一次候选池自动绑定（news/manual 走即时绑定，这里是兜底）。
    原 30 分钟间隔导致新闻入池后有黑洞期，缩短到 5 分钟 + 入池即绑路径兜底。
    """
    await asyncio.sleep(120)
    while True:
        try:
            if monitor_engine:
                await monitor_engine.auto_bind_stock_pool()
        except Exception as e:
            logger.warning(f"股票自动绑定循环异常: {e}")
        await asyncio.sleep(300)


async def _pool_pending_retry_loop():
    """每 30 分钟重试待审股票（数据源失败暂存的）。限频解除后能自动入池。"""
    await asyncio.sleep(120)
    while True:
        try:
            from backend.watchpool.quality_filter import is_eligible
            async with db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT * FROM pool_pending_review ORDER BY first_attempt_at ASC LIMIT 50"
                )
                pending = [dict(r) for r in await cur.fetchall()]
            promoted = 0
            dropped = 0
            for p in pending:
                ok, reason = await is_eligible(db, p["symbol"], p["market"])
                if ok:
                    # 通过 → 入池 + 从待审表删除
                    pool_id = await db.add_to_pool(
                        symbol=p["symbol"], market=p["market"],
                        source=p["source"] or "anomaly", score=p["score"],
                        reason=p["reason"] or "重审通过",
                    )
                    await hub.broadcast_pool_update("added", {
                        "id": pool_id, "symbol": p["symbol"], "market": p["market"],
                        "source": p["source"], "score": p["score"], "reason": "待审重试通过",
                    })
                    async with db.acquire() as conn:
                        await conn.execute(
                            "DELETE FROM pool_pending_review WHERE symbol=? AND market=?",
                            (p["symbol"], p["market"]),
                        )
                        await conn.commit()
                    promoted += 1
                elif "数据源" not in reason or (p.get("attempts") or 0) >= 10:
                    # 真不达标 OR 数据源连续失败 10 次以上 → 放弃，从待审表删除
                    async with db.acquire() as conn:
                        await conn.execute(
                            "DELETE FROM pool_pending_review WHERE symbol=? AND market=?",
                            (p["symbol"], p["market"]),
                        )
                        await conn.commit()
                    if "数据源" in reason:
                        logger.info(f"[pool-retry] 放弃 {p['symbol']} (数据源连续失败 ≥ 10 次)")
                    dropped += 1
                # 否则保留下次再试（attempts 在 _enqueue_pending_review 自增）
            if promoted or dropped:
                logger.info(f"[pool-retry] 待审 {len(pending)} 只 → 通过 {promoted} / 真不达标 {dropped}")
        except Exception as e:
            logger.warning(f"待审重试循环异常: {e}")
        await asyncio.sleep(1800)  # 30 分钟


async def _pool_fundamentals_refresh_loop():
    """
    每天 04:00（北京时间）刷新候选池所有股票的基本面缓存。
    避免 _load_cached 24h TTL 不断让数据失效，使评分能稳定拿到 fund 分。
    """
    while True:
        # 算到下一个北京时间 04:00 的秒数
        try:
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone(timedelta(hours=8)))
            target = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
        except Exception:
            wait = 24 * 3600
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            break
        try:
            from backend.watchpool.quality_filter import (
                _fetch_cn_fundamentals, _fetch_hk_fundamentals,
                _fetch_us_fundamentals, _save_cached,
            )
            items = await db.get_pool_items(limit=500)
            # 按市场分桶并发刷新（CN/HK/US 三源互不干扰，每桶内 Semaphore 限并发）
            from collections import defaultdict
            by_mkt = defaultdict(list)
            for it in items:
                if it["market"] in ("cn", "hk", "us"):
                    by_mkt[it["market"]].append(it)

            async def _fetch_one(sym: str, mkt: str):
                try:
                    if mkt == "cn":
                        return await _fetch_cn_fundamentals(sym, db=db)
                    if mkt == "hk":
                        return await _fetch_hk_fundamentals(sym)
                    if mkt == "us":
                        d = await _fetch_us_fundamentals(sym)
                        if not d:
                            try:
                                from backend.data.us_aggregator import fetch_us_fundamentals_nasdaq
                                d = await fetch_us_fundamentals_nasdaq(sym)
                            except Exception:
                                d = None
                        return d
                except Exception as e:
                    logger.debug(f"[fund-refresh] {sym}/{mkt} 异常: {e}")
                return None

            async def _run_mkt(mkt: str, mkt_items: list):
                # 每市场 3 并发 + 100ms 间隔；总速 ~30/s 远高于旧版 3.3/s，又不会触上游限频
                sem = asyncio.Semaphore(3)
                ok_m = fail_m = 0
                async def _one(it):
                    nonlocal ok_m, fail_m
                    async with sem:
                        d = await _fetch_one(it["symbol"], mkt)
                        if d:
                            await _save_cached(db, it["symbol"], mkt, d)
                            ok_m += 1
                        else:
                            fail_m += 1
                        # 礼貌间隔（在 sem 内 sleep 同时控速）
                        await asyncio.sleep(0.1)
                await asyncio.gather(*[_one(it) for it in mkt_items], return_exceptions=True)
                return ok_m, fail_m

            # 三市场并发跑
            results = await asyncio.gather(
                *[_run_mkt(m, lst) for m, lst in by_mkt.items()], return_exceptions=True
            )
            ok = sum(r[0] for r in results if isinstance(r, tuple))
            fail = sum(r[1] for r in results if isinstance(r, tuple))
            logger.info(f"[fund-refresh] 候选池基本面刷新完成 ok={ok} fail={fail} total={len(items)}")
        except Exception as e:
            logger.warning(f"基本面刷新任务异常: {e}")


async def _crypto_diagnose_loop():
    """
    6 个加密币种每 30 分钟 AI 诊断一次。
    启动 5 分钟后开始（等 LLM 客户端初始化）。

    串行跑 6 个币，每个单独超时 300 秒。
    不用 asyncio.gather + Semaphore 方案，因为股票诊断循环（8只/10min）+ news AI +
    signal_verify 会持续抢 LLM Semaphore(2)，排尾的币（BNB/XRP）会被饿死。
    串行虽然一轮耗时 6-12 分钟，但保证 6 个币全部完成。
    循环 sleep 用 1800s - 实际耗时 保证 30 分钟整周期。
    """
    await asyncio.sleep(300)
    while True:
        cycle_start = time.time()
        try:
            from backend.news import scheduler as sched
            analyzer = getattr(sched, "_ai_analyzer", None)
            if analyzer is None:
                await asyncio.sleep(600)
                continue

            # ─ rating 分层：strong_buy/buy/reduce/sell → 30min；hold → 2h；未诊断 → 立即
            # 减 ~40% 加密 LLM 成本（XRP/BNB 长期 hold 的少跑）
            now_sec = int(time.time())
            stale_thresholds = {
                "strong_buy": now_sec - 30 * 60,
                "buy":        now_sec - 30 * 60,
                "reduce":     now_sec - 30 * 60,
                "sell":       now_sec - 30 * 60,
                "hold":       now_sec - 2 * 3600,   # hold 降到 2h 一刷
            }
            # 拉每个币当前 rating + diagnosed_at
            need_refresh = []
            try:
                async with db.acquire() as conn:
                    cur = await conn.execute("SELECT symbol, rating, diagnosed_at FROM crypto_diagnosis")
                    cur_rows = {r["symbol"]: dict(r) for r in await cur.fetchall()}
            except Exception:
                cur_rows = {}
            for sym in config.CRYPTO_SYMBOLS:
                row = cur_rows.get(sym)
                if not row or not row.get("diagnosed_at"):
                    need_refresh.append(sym)  # 从未诊断 → 立即
                    continue
                rating = (row.get("rating") or "hold").lower()
                threshold = stale_thresholds.get(rating, stale_thresholds["hold"])
                if row["diagnosed_at"] < threshold:
                    need_refresh.append(sym)

            ok = fail = skip = 0
            for sym in config.CRYPTO_SYMBOLS:
                if sym not in need_refresh:
                    skip += 1
                    continue
                t0 = time.time()
                try:
                    await asyncio.wait_for(analyzer.diagnose_crypto(sym), timeout=300)
                    logger.info(f"[crypto-diag-loop] {sym} ✓ 耗时 {time.time()-t0:.1f}s")
                    ok += 1
                except asyncio.TimeoutError:
                    logger.warning(f"[crypto-diag-loop] {sym} ✗ 超时 300s 跳过")
                    fail += 1
                except Exception as e:
                    logger.debug(f"[crypto-diag-loop] {sym} ✗ 异常: {type(e).__name__}: {e}")
                    fail += 1
                await asyncio.sleep(2)  # 币间 yield

            elapsed = time.time() - cycle_start
            logger.info(f"[crypto-diag-loop] 完成一轮 ok={ok} fail={fail} skip={skip}（hold 类 2h 才刷新）总耗时 {elapsed:.1f}s")
        except Exception as e:
            logger.warning(f"加密诊断循环异常: {e}")
        # 保证 30 分钟周期（减去本轮实际耗时）
        elapsed = time.time() - cycle_start
        remaining = max(60, 1800 - elapsed)  # 至少 60s 喘息
        await asyncio.sleep(remaining)


async def _news_ai_backfill_loop():
    """
    ★4+ 新闻 AI 解读补全循环。
    场景：
      - 启动时数据库里有大量 ★4+ 但 ai_analysis IS NULL 的新闻（之前 LLM 失败/未跑）
      - 实时采集时 deep_analyze_news 也可能因网络/解析失败留下 NULL
    策略：每 15 分钟批量处理 5 条 ★4+ 未解读的（按时间倒序优先新的），单只成本 ~$0.005，
         每天最多 96 批 × 5 = 480 条，预算约 $2.4，控制在日预算内。
    """
    await asyncio.sleep(300)  # 启动 5 分钟后开始
    while True:
        try:
            from backend.news import scheduler as sched
            analyzer = getattr(sched, "_ai_analyzer", None)
            if analyzer is None:
                await asyncio.sleep(600)
                continue
            async with db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT * FROM flash_news
                       WHERE importance >= 4 AND (ai_analysis IS NULL OR ai_analysis = '')
                       ORDER BY collected_at DESC LIMIT 5"""
                )
                batch = [dict(r) for r in await cur.fetchall()]
            ok = fail = 0
            for n in batch:
                try:
                    res = await analyzer.deep_analyze_news(n)
                    if res:
                        ok += 1
                    else:
                        fail += 1
                except Exception as e:
                    logger.debug(f"[news-backfill] {n.get('id')} 异常: {e}")
                    fail += 1
                await asyncio.sleep(2)  # 限速
            if batch:
                logger.info(f"[news-backfill] AI 解读补全 batch={len(batch)} ok={ok} fail={fail}")
        except Exception as e:
            logger.warning(f"AI 解读补全循环异常: {e}")
        await asyncio.sleep(900)  # 15 分钟


async def _pool_diagnose_loop():
    """
    候选池 AI 诊断循环。
    优先扶补：从未诊断过的（ai_diagnosed_at=0）→ 7 天前诊断的（陈旧）。
    每 10 分钟批量处理 8 只（= 48/h ≈ 1200/day），
    保证大池（200+ 候选）的首轮诊断在 4-5 小时内铺完。
    """
    await asyncio.sleep(240)  # 启动 4 分钟后开始
    while True:
        try:
            from backend.news.scheduler import _ai_analyzer
            if _ai_analyzer is None:
                await asyncio.sleep(600)
                continue
            now_sec = int(time.time())
            # rating 分层新鲜度（用户调优后更激进）:
            #   strong_buy: 2h   / buy:    6h
            #   reduce:    3h   / sell:   6h
            #   hold:       3d  / manual: 12h
            #   未诊断:    立即
            stale_strong_buy = now_sec - 2 * 3600
            stale_buy        = now_sec - 6 * 3600
            stale_reduce     = now_sec - 3 * 3600
            stale_sell       = now_sec - 6 * 3600
            stale_hold       = now_sec - 3 * 24 * 3600
            stale_manual     = now_sec - 12 * 3600

            async with db.acquire() as conn:
                # 用 CTE 把 rating 提取一次，后面 6 个分支复用，避免 6 次 json_extract 全表扫
                # status 用 IN 取代 != 让 idx_pool_diag(status, ai_diagnosed_at) 真正命中
                cur = await conn.execute(
                    """WITH pool_rated AS (
                         SELECT *,
                           CASE WHEN json_valid(ai_diagnosis)
                                THEN json_extract(ai_diagnosis, '$.rating')
                                ELSE NULL END AS _rating
                         FROM watch_pool
                         WHERE status IN ('candidate','monitoring')
                       )
                       SELECT * FROM pool_rated
                       WHERE
                           ai_diagnosed_at IS NULL OR ai_diagnosed_at = 0
                           OR (source = 'manual' AND ai_diagnosed_at < ?)
                           OR (_rating = 'strong_buy' AND ai_diagnosed_at < ?)
                           OR (_rating = 'buy'        AND ai_diagnosed_at < ?)
                           OR (_rating = 'reduce'     AND ai_diagnosed_at < ?)
                           OR (_rating = 'sell'       AND ai_diagnosed_at < ?)
                           OR ((_rating IN ('hold', '') OR _rating IS NULL) AND ai_diagnosed_at < ?)
                       ORDER BY
                         CASE
                           WHEN ai_diagnosed_at IS NULL OR ai_diagnosed_at = 0 THEN 0
                           WHEN _rating = 'strong_buy' THEN 1
                           WHEN _rating IN ('buy', 'reduce') THEN 2
                           WHEN _rating = 'sell' THEN 3
                           WHEN source = 'manual' THEN 4
                           ELSE 5
                         END,
                         ai_diagnosed_at ASC, score DESC
                       LIMIT 15""",
                    (stale_manual, stale_strong_buy, stale_buy, stale_reduce, stale_sell, stale_hold),
                )
                batch = [dict(r) for r in await cur.fetchall()]
            ok = fail = 0
            for it in batch:
                try:
                    res = await _ai_analyzer.diagnose_stock(it["symbol"], it["market"], pool_item=it)
                    if res:
                        ok += 1
                        try:
                            await hub.broadcast_pool_update("diagnosed", {
                                "id": it["id"], "symbol": it["symbol"],
                                "market": it["market"], "rating": res.get("rating"),
                            })
                        except Exception:
                            pass
                    else:
                        fail += 1
                except Exception as e:
                    logger.debug(f"[diagnose] {it.get('symbol')} 异常: {e}")
                    fail += 1
                # 限速 2s/只 避免 LLM 限频
                await asyncio.sleep(2)
            if batch:
                logger.info(f"[diagnose-loop] AI 诊断 batch={len(batch)} ok={ok} fail={fail}")
        except Exception as e:
            logger.warning(f"AI 诊断循环异常: {e}")
        await asyncio.sleep(420)  # 7 分钟一批（batch=15 配合，500 只首轮 ~4h，更激进的分层刷新需要更高吞吐）


async def _signal_verify_backfill_loop():
    """
    信号 AI 验证补全循环：
      - 仅"周期内"未验证的信号：15m/30m 信号 2 小时内仍验证；1H 4 小时；4H 12 小时；1D 24 小时
      - 按 symbol+market 去重（同一股票只验证最新那条）
      - 每 60 秒扫一次；每批最多 30 条；每条间隔 0.5s（LLM 调用本身已有 semaphore 限流，这里不必再限速）
      - 超窗口的老信号才标记 stale，从队列里移除
    """
    # 周期 → stale 窗口（毫秒）。越长周期信号的"有效期"越长
    INTERVAL_STALE_MS = {
        "1m": 30 * 60 * 1000,        # 30min
        "5m": 60 * 60 * 1000,        # 1h
        "15m": 2 * 3600 * 1000,      # 2h
        "30m": 3 * 3600 * 1000,      # 3h
        "1H": 4 * 3600 * 1000,       # 4h
        "4H": 12 * 3600 * 1000,      # 12h
        "1D": 24 * 3600 * 1000,      # 24h
        "1W": 72 * 3600 * 1000,      # 3d
    }
    DEFAULT_STALE_MS = 4 * 3600 * 1000

    await asyncio.sleep(60)
    while True:
        try:
            if monitor_engine is None:
                await asyncio.sleep(300)
                continue
            now_ms = int(time.time() * 1000)
            min_conf = getattr(config, "SIGNAL_MIN_CONFIDENCE", 75)

            # 按 interval 分别标记 stale
            try:
                async with db.acquire() as conn:
                    for iv, ms in INTERVAL_STALE_MS.items():
                        cut = now_ms - ms
                        await conn.execute(
                            """UPDATE signals SET ai_verdict='stale',
                               ai_reason= '信号超过 ' || ? || ' 分钟未验证，市场已变，跳过'
                               WHERE ai_verdict = '' AND interval = ? AND generated_at < ? AND confidence >= ?""",
                            (ms // 60000, iv, cut, min_conf),
                        )
                    # 未在映射内的 interval 用默认窗口
                    cut_default = now_ms - DEFAULT_STALE_MS
                    iv_list = list(INTERVAL_STALE_MS.keys())
                    placeholders = ",".join("?" for _ in iv_list)
                    await conn.execute(
                        f"""UPDATE signals SET ai_verdict='stale',
                           ai_reason='信号超过 {DEFAULT_STALE_MS // 60000} 分钟未验证，市场已变，跳过'
                           WHERE ai_verdict = '' AND generated_at < ? AND confidence >= ?
                             AND interval NOT IN ({placeholders})""",
                        [cut_default, min_conf] + iv_list,
                    )
                    await conn.commit()
            except Exception as e:
                logger.debug(f"[verify-backfill] stale 标记失败: {e}")

            # 取待验证的最新信号（仍在各自周期窗口内）
            async with db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT s1.* FROM signals s1
                       WHERE s1.ai_verdict = ''
                         AND s1.confidence >= ?
                         AND s1.generated_at = (
                           SELECT MAX(s2.generated_at) FROM signals s2
                           WHERE s2.symbol=s1.symbol AND s2.market=s1.market AND s2.ai_verdict=''
                         )
                         AND NOT (
                           s1.market != 'crypto' AND s1.action = 'sell'
                           AND NOT EXISTS (
                             SELECT 1 FROM positions p
                             WHERE p.symbol=s1.symbol AND p.market=s1.market AND p.quantity > 0
                           )
                         )
                       ORDER BY s1.confidence DESC, s1.generated_at DESC LIMIT 30""",
                    (min_conf,),
                )
                batch = [dict(r) for r in await cur.fetchall()]

            # 并发处理（LLM Semaphore=8 自动限流；不再串行等每条）
            from backend.data.models import Signal, Market

            async def _verify_one(sig_row):
                try:
                    mkt = Market(sig_row["market"])
                    signal = Signal(
                        id=sig_row["id"],
                        symbol=sig_row["symbol"],
                        market=mkt,
                        action=sig_row["action"],
                        strategy_name=sig_row["strategy_name"],
                        confidence=int(sig_row["confidence"] or 0),
                        price=float(sig_row["price"] or 0),
                        suggested_qty=sig_row.get("suggested_qty"),
                        stop_loss=sig_row.get("stop_loss"),
                        take_profit=sig_row.get("take_profit"),
                        reason=sig_row.get("reason") or "",
                        generated_at=sig_row["generated_at"],
                    )
                    signal.interval = sig_row.get("interval") or "1H"
                    await monitor_engine._ai_verify_signal(signal)
                    return True
                except Exception as e:
                    logger.debug(f"[verify-backfill] {sig_row.get('symbol')} 异常: {e}")
                    return False

            results = await asyncio.gather(*[_verify_one(r) for r in batch], return_exceptions=True)
            ok = sum(1 for r in results if r is True)
            if batch:
                logger.info(f"[verify-backfill] 补验证 batch={len(batch)} ok={ok}")
        except Exception as e:
            logger.warning(f"验证补全循环异常: {e}")
        await asyncio.sleep(60)  # 60s 一轮（原 180s；LLM 调用本身有 semaphore，无需再限速）


async def _position_advice_loop():
    """
    持仓 AI 建议主动巡检：每 2 小时为每个持仓出一次建议（即使无新闻）。
    - 新开仓会在 _execute_open / add_position 里立即触发一次，不必等本轮
    - 本循环只为"持仓存在但尚无建议"或"上次建议已过期"的场景兜底
    用户也可在前端"🤖 建议"按钮主动触发立即生成。
    """
    await asyncio.sleep(60)  # 启动 1 分钟后第一次巡检
    while True:
        try:
            from backend.news.scheduler import _ai_analyzer
            if _ai_analyzer is None:
                await asyncio.sleep(600)
                continue
            # 优先处理"从未有过建议"的持仓，然后才是旧建议刷新
            async with db.acquire() as conn:
                cur = await conn.execute("""
                    SELECT p.id
                    FROM positions p
                    LEFT JOIN (
                        SELECT position_id, MAX(advised_at) AS last_at
                        FROM position_advices GROUP BY position_id
                    ) a ON a.position_id = p.id
                    WHERE p.quantity > 0
                    ORDER BY COALESCE(a.last_at, 0) ASC
                """)
                pos_ids = [r["id"] for r in await cur.fetchall()]
            if not pos_ids:
                await asyncio.sleep(2 * 3600)
                continue
            ok = fail = 0
            for pid in pos_ids:
                try:
                    res = await _ai_analyzer.generate_advice_for_position(pid, force=False)
                    if res: ok += 1
                    else: fail += 1
                    await asyncio.sleep(1)  # 1s 限速；LLM 自身有 semaphore
                except Exception as e:
                    logger.debug(f"[advice-loop] {pid} 异常: {e}")
                    fail += 1
            logger.info(f"[advice-loop] 持仓建议巡检完成 ok={ok} fail={fail}")
        except Exception as e:
            logger.warning(f"持仓建议循环异常: {e}")
        await asyncio.sleep(2 * 3600)  # 2 小时一轮


async def _trade_review_loop():
    """v12.3 每 4h 批量复盘当天新闭环的单（最多 10 笔/轮，避免 LLM 突发开销）"""
    await asyncio.sleep(300)  # 启动 5 分钟后开始
    while True:
        try:
            if trade_reviewer is not None:
                r = await trade_reviewer.batch_review_unreviewed(limit=10, sleep_sec=2.5)
                if r["ok"]:
                    logger.info(f"[reviewer-loop] 本轮复盘 ok={r['ok']} fail={r['fail']} skipped={r.get('skipped_budget',0)}")
        except Exception as e:
            logger.warning(f"[reviewer-loop] 异常: {e}")
        await asyncio.sleep(4 * 3600)


async def _lesson_aggregation_loop():
    """v12.5 Phase A 每 6h 聚合 trade_review.lessons → lesson_pattern。
    供 verify_signal / diagnose_* prompt 注入用。"""
    await asyncio.sleep(900)  # 启动 15 分钟后第一次（等首批 review 落库）
    while True:
        try:
            if trade_reviewer is not None:
                n = await trade_reviewer.aggregate_lessons()
                if n > 0:
                    logger.info(f"[lessons] 聚合到 {n} 个高频教训模式 — verify/diagnose prompt 已可注入")
        except Exception as e:
            logger.warning(f"[lessons] 聚合循环异常: {e}")
        await asyncio.sleep(6 * 3600)


async def _trade_review_weekly_loop():
    """v12.3 每天凌晨 03:00 检查是否需要生成上周报告"""
    await asyncio.sleep(600)
    while True:
        try:
            if trade_reviewer is not None:
                # 计算上周一 00:00 的时间戳
                now = time.time()
                t = time.localtime(now)
                # 本周一 00:00
                this_mon = int(now - (t.tm_wday * 86400 + t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec))
                last_mon = this_mon - 7 * 86400
                # 检查是否已生成
                async with db.acquire() as conn:
                    cur = await conn.execute("SELECT id FROM trade_review_weekly WHERE week_start=?", (last_mon,))
                    if not await cur.fetchone():
                        r = await trade_reviewer.generate_weekly_report(last_mon)
                        if r:
                            logger.info(f"[reviewer-weekly] 上周报告生成: {r['trades_count']} 笔, 胜率 {r['win_rate']:.1%}")
        except Exception as e:
            logger.warning(f"[reviewer-weekly-loop] 异常: {e}")
        await asyncio.sleep(24 * 3600)


async def _alert_check_loop():
    """v11.6 警报巡检：每 60s 跑一次。
    对每条活跃警报：拉该 symbol 最新 1D K 线 → 调 alert_manager.check_alerts → 触发推 WS。
    """
    await asyncio.sleep(30)
    while True:
        try:
            if alert_manager is None:
                await asyncio.sleep(60); continue
            actives = alert_manager.get_active_alerts()
            if not actives:
                await asyncio.sleep(60); continue
            # 按 (symbol, market) 分组减少 K 线查询
            from collections import defaultdict
            grouped = defaultdict(list)
            for a in actives:
                grouped[(a.get("symbol",""), a.get("market",""))].append(a)
            for (sym, mkt), alist in grouped.items():
                if not sym or mkt not in ("us","hk","cn","crypto"):
                    continue
                try:
                    # 取近 60 根 1D
                    async with db.acquire() as conn:
                        cur = await conn.execute(
                            f"SELECT timestamp, open, high, low, close, volume FROM [klines_{mkt}_1d] "
                            f"WHERE symbol=? ORDER BY timestamp DESC LIMIT 60",
                            (sym,))
                        rows = list(reversed([dict(r) for r in await cur.fetchall()]))
                    if not rows:
                        continue
                    from backend.signals.strategies import Candle
                    candles = [Candle(open=r["open"], high=r["high"], low=r["low"],
                                       close=r["close"], volume=r["volume"], timestamp=r["timestamp"])
                               for r in rows]
                    triggered = await alert_manager.check_alerts(sym, candles)
                    for t in triggered:
                        try:
                            # v12.11: hub.broadcast 不存在，改 broadcast_alert（之前 AttributeError 被吞，警报永远不推）
                            await hub.broadcast_alert({"type":"alert_triggered",
                                                       "symbol": sym, "market": mkt,
                                                       "message": t.get("message",""),
                                                       "price": t.get("price"),
                                                       "alert": t.get("alert", {})})
                        except Exception as e:
                            logger.warning(f"[alert] WS 推送失败 {sym}/{mkt}: {e}")
                except Exception as e:
                    logger.debug(f"[alert] {sym}/{mkt} 检查异常: {e}")
        except Exception as e:
            logger.warning(f"[alert] 巡检循环异常: {e}")
        await asyncio.sleep(60)


async def _holding_diagnose_loop():
    """
    v12.13: 持仓股票 AI 诊断加速循环（每 1h 跑一次）。

    背景：_pool_diagnose_loop 对 hold 评级 3 天才刷一次，对候选池合理但对持仓不合理 —
    持仓决策（加仓/减仓/平仓）需要更新鲜的 AI 视角。

    阈值（持仓收紧版）：
      strong_buy / reduce: 1h    buy / sell: 2h    hold: 6h（候选池是 3 天）

    冷启动 6 分钟后开始；预算超支不阻断（持仓诊断是核心运营任务）。
    """
    await asyncio.sleep(360)
    HOLDING_STALE = {
        "strong_buy": 1 * 3600,
        "buy":        2 * 3600,
        "reduce":     1 * 3600,
        "sell":       2 * 3600,
        "hold":       6 * 3600,
    }
    while True:
        try:
            from backend.news.scheduler import _ai_analyzer
            if _ai_analyzer is None:
                await asyncio.sleep(600)
                continue
            now_sec = int(time.time())
            async with db.acquire() as conn:
                cur = await conn.execute("""
                    SELECT p.symbol, p.market,
                           w.ai_diagnosed_at AS last_diag,
                           CASE WHEN json_valid(w.ai_diagnosis)
                                THEN json_extract(w.ai_diagnosis, '$.rating')
                                ELSE NULL END AS rating
                    FROM positions p
                    LEFT JOIN watch_pool w
                      ON p.symbol = w.symbol AND p.market = w.market AND w.status != 'archived'
                    WHERE p.market IN ('us','hk','cn') AND p.quantity > 0
                """)
                rows = [dict(r) for r in await cur.fetchall()]

            need = []
            for r in rows:
                rating = (r.get("rating") or "hold").lower()
                threshold = HOLDING_STALE.get(rating, HOLDING_STALE["hold"])
                last = r.get("last_diag") or 0
                if last == 0 or now_sec - last >= threshold:
                    need.append(r)

            if not need:
                await asyncio.sleep(3600)
                continue

            ok = fail = 0
            for r in need:
                try:
                    res = await _ai_analyzer.diagnose_stock(r["symbol"], r["market"], force=True)
                    if res:
                        ok += 1
                    else:
                        fail += 1
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.debug(f"[holding-diag] {r['symbol']}({r['market']}) 异常: {e}")
                    fail += 1
            logger.info(f"[holding-diag] 持仓诊断 刷新 {ok+fail}/{len(rows)} 个 (ok={ok} fail={fail})")
        except Exception as e:
            logger.warning(f"[holding-diag] 循环异常: {e}")
        await asyncio.sleep(3600)


async def _position_summary_loop():
    """v12.13: 持仓盈亏简报每 4h 推一次到 Telegram。
    持久化 last_push_at 到 config 表，重启不会重置计时（避免频繁部署刷屏）。
    """
    INTERVAL_SEC = 4 * 3600

    async def _get_last_push() -> int:
        try:
            async with db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT value FROM config WHERE key='telegram_last_summary_at'"
                )
                row = await cur.fetchone()
            return int(row[0]) if row and row[0] else 0
        except Exception:
            return 0

    async def _set_last_push(ts: int):
        try:
            async with db.acquire() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES ('telegram_last_summary_at', ?)",
                    (str(ts),),
                )
                await conn.commit()
        except Exception as e:
            logger.debug(f"[summary-loop] 写 last_push 失败: {e}")

    await asyncio.sleep(60)  # 启动 1 分钟后开始检查
    while True:
        try:
            if not getattr(config, "TELEGRAM_ENABLED", False):
                await asyncio.sleep(INTERVAL_SEC)
                continue
            last_push = await _get_last_push()
            now_sec = int(time.time())
            elapsed = now_sec - last_push
            if elapsed >= INTERVAL_SEC:
                from backend.notify.telegram import send_summary
                text = await _build_position_summary_text()
                if text:
                    err = await send_summary(text)
                    if not err:
                        await _set_last_push(now_sec)
                        logger.info(f"[summary-loop] 推送完成，下次 {INTERVAL_SEC//3600}h 后")
            else:
                remaining_min = (INTERVAL_SEC - elapsed) // 60
                logger.debug(f"[summary-loop] 距上次推送 {elapsed//60}min < {INTERVAL_SEC//60}min，等 {remaining_min}min")
            # 睡到下一个推送窗口（最少 5 分钟，避免 busy loop）
            wait_sec = max(300, INTERVAL_SEC - (int(time.time()) - last_push))
            await asyncio.sleep(min(wait_sec, INTERVAL_SEC))
        except Exception as e:
            logger.warning(f"[summary-loop] 异常: {e}")
            await asyncio.sleep(INTERVAL_SEC)


async def _build_position_summary_text() -> str:
    """构造 4h 持仓简报文本（池盈亏 + 持仓列表）。"""
    from datetime import datetime, timezone, timedelta
    cn_now = datetime.now(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
    lines = [f"📊 OpenChart Pro 持仓简报 ({cn_now})"]

    CCY_SYM = {"USD": "$", "CNY": "¥", "HKD": "HK$"}

    # 拉池盈亏（复用 status API 的 pools_summary）
    try:
        async with db.acquire() as conn:
            cur = await conn.execute("SELECT * FROM auto_trade_pool")
            pools = [dict(r) for r in await cur.fetchall()]
    except Exception:
        pools = []

    # 拉所有持仓 + 最新现价
    positions_by_market = {}
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT symbol, market, side, quantity, avg_cost, cost_currency "
                "FROM positions WHERE quantity > 0"
            )
            for r in await cur.fetchall():
                d = dict(r)
                positions_by_market.setdefault(d["market"], []).append(d)
    except Exception:
        pass

    # 获取最新现价
    price_map = {}
    if positions_by_market:
        async with db.acquire() as conn:
            for mkt, plist in positions_by_market.items():
                if mkt not in ("cn", "hk", "us", "crypto"):
                    continue
                syms = list({p["symbol"] for p in plist})
                if not syms:
                    continue
                placeholders = ",".join("?" for _ in syms)
                try:
                    cur = await conn.execute(
                        f"""SELECT symbol, close FROM (
                              SELECT symbol, close, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rn
                              FROM [klines_{mkt}_1d] WHERE symbol IN ({placeholders})
                            ) WHERE rn=1""",
                        syms,
                    )
                    for r in await cur.fetchall():
                        price_map[(r["symbol"], mkt)] = float(r["close"])
                except Exception:
                    pass

    POOL_OF = {"us": "us_hk", "hk": "us_hk", "cn": "cn", "crypto": "crypto"}
    POOL_NAME = {"us_hk": "港美股", "cn": "A股", "crypto": "加密"}
    if not pools:
        return ""

    # 池子顺序：us_hk → cn → crypto
    pool_map = {p["pool_id"]: p for p in pools}
    for pool_id in ("us_hk", "cn", "crypto"):
        p = pool_map.get(pool_id)
        if not p:
            continue
        ccy = p.get("currency") or "USD"
        sym = CCY_SYM.get(ccy, "")
        cash = float(p.get("cash") or 0)
        initial = float(p.get("initial_capital") or 0)

        # 该池下所有持仓 + 浮盈
        pool_positions = []
        positions_value = 0.0
        unrealized = 0.0
        for mkt, plist in positions_by_market.items():
            if POOL_OF.get(mkt) != pool_id:
                continue
            for pos in plist:
                cur_price = price_map.get((pos["symbol"], mkt))
                avg = float(pos["avg_cost"] or 0)
                qty = float(pos["quantity"] or 0)
                side = pos["side"] or "long"
                if cur_price is None or avg <= 0 or qty <= 0:
                    continue
                # 折算到池币（hk 持仓 HKD → us_hk USD 池需要 fx）
                from backend.trading.fx import market_to_currency, get_rate, FALLBACK_RATES
                local_ccy = (pos.get("cost_currency") or market_to_currency(mkt) or "USD").upper()
                if local_ccy == ccy:
                    factor = 1.0
                else:
                    try:
                        local_to_usd = await get_rate(db, local_ccy)
                        pool_to_usd = await get_rate(db, ccy) if ccy != "USD" else 1.0
                        factor = local_to_usd / pool_to_usd if pool_to_usd > 0 else 1.0
                    except Exception:
                        factor = FALLBACK_RATES.get(local_ccy, 1.0) / (FALLBACK_RATES.get(ccy, 1.0) if ccy != "USD" else 1.0)
                if side == "long":
                    mv = qty * cur_price * factor
                    cost = qty * avg * factor
                    pnl = mv - cost
                    pnl_pct = (cur_price - avg) / avg * 100
                    positions_value += mv
                    unrealized += pnl
                else:
                    margin = qty * avg * factor
                    pnl = (avg - cur_price) * qty * factor
                    positions_value += max(0.0, margin + pnl)
                    unrealized += pnl
                    pnl_pct = (avg - cur_price) / avg * 100
                pool_positions.append((pos["symbol"], pnl_pct, side))

        equity = cash + positions_value
        pnl_total = equity - initial
        rea = pnl_total - unrealized
        pnl_pct_p = (pnl_total / initial * 100) if initial else 0
        unr_s = "+" if unrealized >= 0 else ""
        rea_s = "+" if rea >= 0 else ""
        pool_emoji = "📈" if pnl_total >= 0 else "📉"
        pool_label = POOL_NAME.get(pool_id, pool_id)
        lines.append("")
        lines.append(f"{pool_emoji} {pool_label} ({ccy}): 权益 {sym}{equity:,.0f}  ({pnl_pct_p:+.2f}%)")
        lines.append(f"   浮盈 {unr_s}{sym}{unrealized:,.0f}  已实现 {rea_s}{sym}{rea:,.0f}")
        if pool_positions:
            # 按浮盈降序，最多显示 6 只
            pool_positions.sort(key=lambda x: -x[1])
            shown = pool_positions[:6]
            holds_str = "  ".join(
                f"{('多' if s == 'long' else '空')}{sym2}({pct:+.1f}%)"
                for sym2, pct, s in shown
            )
            extra = f"  ...另 {len(pool_positions)-6}" if len(pool_positions) > 6 else ""
            lines.append(f"   持仓: {holds_str}{extra}")
        else:
            lines.append(f"   持仓: 无")

    return "\n".join(lines)


async def _aged_position_advice_loop():
    """
    v11.3 + v11.4 老持仓诊断加速：
      - 持仓 ≥ 7 天的，每 4h 错峰刷 AI 诊断
      - 每轮最多 MAX_PER_CYCLE 个（按"最久没诊断的"优先）—— 避免 30+ 持仓一次性烧穿预算
      - 调用前查 budget，软超直接停本轮
    """
    await asyncio.sleep(180)  # 启动后 3 分钟
    AGE_DAYS = 7
    INTERVAL = 4 * 3600
    MAX_PER_CYCLE = 10  # 每 4h 最多刷 10 个最老的，30 持仓 → 3 轮覆盖（12h 内）
    while True:
        try:
            from backend.news.scheduler import _ai_analyzer
            if _ai_analyzer is None:
                await asyncio.sleep(600)
                continue
            cutoff = int(time.time()) - AGE_DAYS * 86400
            # 错峰：优先处理最久没新建议的持仓（last_advised_at ASC, NULL 视为最老）
            async with db.acquire() as conn:
                cur = await conn.execute("""
                    SELECT p.id
                    FROM positions p
                    LEFT JOIN (
                        SELECT position_id, MAX(advised_at) AS last_at
                        FROM position_advices GROUP BY position_id
                    ) a ON a.position_id = p.id
                    WHERE p.quantity > 0 AND p.opened_at < ?
                    ORDER BY COALESCE(a.last_at, 0) ASC
                    LIMIT ?
                """, (cutoff, MAX_PER_CYCLE))
                pids = [r["id"] for r in await cur.fetchall()]
            if not pids:
                await asyncio.sleep(INTERVAL)
                continue
            ok = fail = skipped = 0
            for pid in pids:
                # 预算软门
                try:
                    if hasattr(_ai_analyzer, "_can_call") and not await _ai_analyzer._can_call(hard_stop=False):
                        skipped = len(pids) - ok - fail
                        logger.info(f"[aged-advice] LLM 预算用尽，跳过本轮剩余 {skipped} 个")
                        break
                except Exception:
                    pass
                try:
                    r = await _ai_analyzer.generate_advice_for_position(pid, force=True)
                    if r: ok += 1
                    else: fail += 1
                    await asyncio.sleep(1.5)
                except Exception as e:
                    logger.debug(f"[aged-advice] {pid} 异常: {e}")
                    fail += 1
            logger.info(f"[aged-advice] 老持仓({AGE_DAYS}天+) 本轮刷 {ok+fail}/{len(pids)} (ok={ok} fail={fail} skipped={skipped})")
        except Exception as e:
            logger.warning(f"[aged-advice] 循环异常: {e}")
        await asyncio.sleep(INTERVAL)


async def _pending_orders_retry_loop():
    """
    待开市自动重触发循环：
      扫描近 60 分钟内 ai_verdict='confirm' + auto_trade_log status='rejected'
      且 rejected_reason 含"未到连续竞价时段"的信号；
      若当前已开市 → 重新触发 auto_trader.on_signal_verified。
    每 60 秒扫一次，覆盖 9:30 / 13:00 / 9:30ET 等开市瞬间。
    """
    await asyncio.sleep(120)
    while True:
        try:
            if auto_trader is None or not auto_trader.enabled:
                await asyncio.sleep(60)
                continue
            from backend.signals.monitor import is_market_executable
            now_ms = int(time.time() * 1000)
            cutoff_ms = now_ms - 60 * 60 * 1000
            cutoff_sec = int(time.time()) - 60 * 60

            # 找近 60min 内的 confirm 信号：有 pending 拒绝记录 + 之后没有成功执行记录
            async with db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT s.id, s.symbol, s.market FROM signals s
                       WHERE s.ai_verdict = 'confirm'
                         AND s.generated_at > ?
                         AND EXISTS (
                             SELECT 1 FROM auto_trade_log l
                             WHERE l.symbol = s.symbol AND l.market = s.market
                               AND l.status = 'rejected'
                               AND l.traded_at > ?
                               AND l.rejected_reason LIKE '%pending%'
                         )
                         AND NOT EXISTS (
                             SELECT 1 FROM auto_trade_log l2
                             WHERE l2.symbol = s.symbol AND l2.market = s.market
                               AND l2.status = 'executed'
                               AND l2.traded_at > ?
                         )
                       ORDER BY s.generated_at DESC LIMIT 30""",
                    (cutoff_ms, cutoff_sec, cutoff_sec),
                )
                pending = [dict(r) for r in await cur.fetchall()]

            if pending:
                fired = 0
                for sig in pending:
                    if not is_market_executable(sig["market"]):
                        continue
                    try:
                        await auto_trader.on_signal_verified(sig["id"])
                        fired += 1
                    except Exception as e:
                        logger.debug(f"[pending-retry] {sig['symbol']} 异常: {e}")
                if fired > 0:
                    logger.info(f"[pending-retry] 开市重触发 {fired}/{len(pending)} 个 pending 信号")
        except Exception as e:
            logger.warning(f"待开市重试循环异常: {e}")
        await asyncio.sleep(60)


async def _pool_auto_archive_loop():
    """v12.13 候选池分层 + 总上限 300 自动淘汰（每小时一次）。

    分层策略：
      Tier 1 (≥70):  上限 WATCHPOOL_TIER1_MAX (220)
      Tier 2 (50-69): 上限 WATCHPOOL_TIER2_MAX (60)
      Tier 3 (40-49): 上限 WATCHPOOL_TIER3_MAX (20)
      Tier 4 (<40):   立即 archive（不等天数阈值）

    豁免（不淘汰，不算上限）：
      - source ∈ (manual / news / news_ai / macro_theme)
      - 持仓中的股票
      - watchlist 中的股票

    超额时按 score 升序淘汰最低的几个。仍保留 30 天无新闻 archive 作为豁免来源的兜底清理。
    """
    EXEMPT_SOURCES = {"manual", "news", "news_ai", "macro_theme"}
    await asyncio.sleep(180)
    while True:
        try:
            t1_min = getattr(config, "WATCHPOOL_TIER1_MIN_SCORE", 70)
            t1_max = getattr(config, "WATCHPOOL_TIER1_MAX", 220)
            t2_min = getattr(config, "WATCHPOOL_TIER2_MIN_SCORE", 50)
            t2_max = getattr(config, "WATCHPOOL_TIER2_MAX", 60)
            t3_min = getattr(config, "WATCHPOOL_TIER3_MIN_SCORE", 40)
            t3_max = getattr(config, "WATCHPOOL_TIER3_MAX", 20)
            t4_below = getattr(config, "WATCHPOOL_TIER4_ARCHIVE_BELOW", 40)
            no_news_days = getattr(config, "WATCHPOOL_EXPIRE_NO_NEWS_DAYS", 30)
            now = int(time.time())

            # ─ 拉持仓 + watchlist 集合（豁免）─
            exempt_keys = set()
            try:
                async with db.acquire() as conn:
                    cur = await conn.execute("SELECT symbol, market FROM positions WHERE quantity > 0")
                    for r in await cur.fetchall():
                        exempt_keys.add((r["symbol"], r["market"]))
                    cur = await conn.execute("SELECT symbol, market FROM watchlist")
                    for r in await cur.fetchall():
                        exempt_keys.add((r["symbol"], r["market"]))
            except Exception as e:
                logger.debug(f"[auto-archive] 拉豁免列表失败: {e}")

            def is_exempt(it):
                if (it.get("source") or "") in EXEMPT_SOURCES:
                    return True
                if (it.get("symbol"), it.get("market")) in exempt_keys:
                    return True
                return False

            items = await db.get_pool_items(limit=2000)
            archived = {"tier4": 0, "overflow": 0, "no_news": 0}

            # ─ Step 1: Tier 4 立即 archive（score < 阈值且非豁免）─
            for it in items:
                if it.get("source") in EXEMPT_SOURCES and (it.get("symbol"), it.get("market")) not in exempt_keys:
                    # source 豁免但不在持仓/watchlist — 仍保留，但下面 no_news 兜底会处理
                    pass
                if is_exempt(it):
                    continue
                score = float(it.get("score") or 0)
                if score < t4_below:
                    try:
                        await db.archive_pool_item(it["id"], reason=f"Tier 4 score={score:.0f} < {t4_below}")
                        archived["tier4"] += 1
                        try: await hub.broadcast_pool_update("removed", {"id": it["id"]})
                        except Exception: pass
                    except Exception as e:
                        logger.debug(f"[auto-archive] tier4 archive 失败 {it.get('symbol')}: {e}")

            # ─ Step 2: Tier 1/2/3 超额按 score 升序淘最低 ─
            try:
                items = await db.get_pool_items(limit=2000)  # 重新拉
            except Exception:
                items = []
            tiers = [
                (t1_min, 1e9, t1_max, "Tier 1"),
                (t2_min, t1_min, t2_max, "Tier 2"),
                (t3_min, t2_min, t3_max, "Tier 3"),
            ]
            for tier_min, tier_max_excl, cap, label in tiers:
                bucket = [
                    it for it in items
                    if not is_exempt(it)
                    and tier_min <= float(it.get("score") or 0) < tier_max_excl
                ]
                if len(bucket) <= cap:
                    continue
                bucket.sort(key=lambda x: float(x.get("score") or 0))  # score 升序，淘最低
                to_kill = bucket[:len(bucket) - cap]
                for it in to_kill:
                    try:
                        await db.archive_pool_item(
                            it["id"],
                            reason=f"{label} 超额 (cap={cap}, 当前 score={float(it.get('score') or 0):.0f})"
                        )
                        archived["overflow"] += 1
                        try: await hub.broadcast_pool_update("removed", {"id": it["id"]})
                        except Exception: pass
                    except Exception as e:
                        logger.debug(f"[auto-archive] overflow archive 失败 {it.get('symbol')}: {e}")

            # ─ Step 3: 30 天无新闻 archive（豁免来源也适用，作为兜底清理）─
            try:
                items = await db.get_pool_items(limit=2000)
            except Exception:
                items = []
            for it in items:
                # 持仓 / watchlist 永不因"无新闻"淘汰
                if (it.get("symbol"), it.get("market")) in exempt_keys:
                    continue
                last_news = it.get("last_news_mention_at") or 0
                added_at = it.get("added_at") or 0
                if added_at and now - added_at >= no_news_days * 86400:
                    if not last_news or now - last_news >= no_news_days * 86400:
                        try:
                            await db.archive_pool_item(it["id"], reason=f"no_news (>{no_news_days} 天无新闻)")
                            archived["no_news"] += 1
                            try: await hub.broadcast_pool_update("removed", {"id": it["id"]})
                            except Exception: pass
                        except Exception as e:
                            logger.debug(f"[auto-archive] no_news archive 失败 {it.get('symbol')}: {e}")

            total = sum(archived.values())
            if total:
                logger.info(
                    f"[auto-archive] 淘汰 {total} 只 "
                    f"(Tier4 {archived['tier4']} + 超额 {archived['overflow']} + 无新闻 {archived['no_news']})"
                )
        except Exception as e:
            logger.warning(f"自动淘汰循环异常: {e}")
        await asyncio.sleep(3600)


async def _refresh_pool_symbols_cache():
    """每 5 分钟刷新候选池符号缓存 + 持仓符号缓存。
    v12.15.4 修复：之前只取 status='monitoring' 但 candidate 从未被升级 → 缓存永远空 → 池子等于死数据。
    现在 status=None（默认排除 archived）让 candidate + monitoring 都进缓存。"""
    while True:
        try:
            items = await db.get_pool_items(status=None, limit=500)
            _runtime_config["_pool_symbols_cache"] = {item["symbol"] for item in items}
        except Exception as e:
            logger.warning(f"刷新候选池缓存失败: {e}")
        try:
            # PortfolioManager 单例（避免每 5 分钟创建新实例占内存）
            global _shared_portfolio_manager
            if _shared_portfolio_manager is None:
                from backend.portfolio.manager import PortfolioManager
                _shared_portfolio_manager = PortfolioManager(db)
            positions = await _shared_portfolio_manager.get_all()
            _runtime_config["_holding_records_cache"] = positions
            _runtime_config["_holding_symbols_cache"] = {p["symbol"] for p in positions}
        except Exception as e:
            logger.warning(f"刷新持仓缓存失败: {e}")
        await asyncio.sleep(300)


async def _refresh_symbol_registry_loop():
    """
    每 3 分钟刷新 SymbolRegistry 动态词典。
    用户 watchlist / 候选池 / 持仓中的品种会被自动加入识别表，
    新闻提到这些品种就能立即识别 → categories → 自动入候选池。
    """
    # 启动时立即跑一次
    try:
        await symbol_registry.refresh_from_db(db)
    except Exception as e:
        logger.warning(f"SymbolRegistry 首次刷新失败: {e}")
    while True:
        await asyncio.sleep(180)
        try:
            await symbol_registry.refresh_from_db(db)
        except Exception as e:
            logger.warning(f"SymbolRegistry 周期刷新失败: {e}")


def _parse_market(market_str: str) -> Market:
    """
    字符串到 Market 枚举。
    兼容前端历史命名：'a' → 'cn' （A 股的旧别名）
    """
    m = market_str.lower()
    if m == "a":  # 前端历史别名
        m = "cn"
    try:
        return Market(m)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid market: {market_str}")


def _parse_interval(interval_str: str) -> Interval:
    """字符串到 Interval 枚举。"""
    try:
        return Interval(interval_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid interval: {interval_str}")


# ═══════════════════════════════════════════════════════════════════
# FastAPI 应用 + 生命周期
# ═══════════════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动 + 关闭钩子。"""
    # ─── 启动 ───
    logger.info("OpenChart Pro starting...")

    # 1. 数据库
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    await db.init_db()
    logger.info(f"Database initialized: {config.DB_PATH}")

    # 2. 加载运行时配置
    await _load_runtime_config()

    # 3. 加密 6 币种首次使用引导
    await _ensure_crypto_watchlist()

    # 4. Phase 4 策略监控引擎 + Phase 5 持仓追踪器
    global monitor_engine, portfolio_tracker, portfolio_manager
    portfolio_manager = PortfolioManager(db)

    async def _news_for_symbol(symbol: str):
        """提供给 monitor / tracker 的新闻查询函数。"""
        try:
            items = await db.get_flash_news(importance_min=2, limit=20)
            return [n for n in items if symbol in (n.get("categories") or [])]
        except Exception:
            return []

    # v12.13: 真实异步 news_provider — 30s 缓存版（之前是 lambda s: [] 导致 flash_event 永远 0 信号）
    # 拉最近 30min ★3+ 新闻 + 解析 ai_analysis 字段（FlashEventStrategy 消费 impacts）
    _news_cache = {"ts": 0.0, "rows": []}

    async def _provide_recent_news(symbol: str):
        nonlocal_now = time.time()
        if nonlocal_now - _news_cache["ts"] < 30 and _news_cache["rows"]:
            return _news_cache["rows"]
        cutoff_ms = int(nonlocal_now * 1000) - 30 * 60 * 1000
        rows = []
        try:
            async with db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT id, title, importance, sentiment, published_at,
                              categories, ai_analysis
                       FROM flash_news
                       WHERE published_at >= ? AND importance >= 3
                       ORDER BY published_at DESC LIMIT 80""",
                    (cutoff_ms,)
                )
                for r in await cur.fetchall():
                    d = dict(r)
                    try: d["categories"] = json.loads(d.get("categories") or "[]")
                    except Exception: d["categories"] = []
                    ai = d.get("ai_analysis")
                    if isinstance(ai, str) and ai:
                        try: d["ai_analysis"] = json.loads(ai)
                        except Exception: d["ai_analysis"] = None
                    rows.append(d)
            _news_cache["ts"] = nonlocal_now
            _news_cache["rows"] = rows
        except Exception as e:
            logger.debug(f"[news-provider] 拉新闻失败: {e}")
        return rows

    monitor_engine = MonitorEngine(db=db, ws_hub=hub, news_provider=_provide_recent_news)
    await monitor_engine.ensure_crypto_bindings()
    await monitor_engine.auto_bind_stock_pool()
    monitor_engine.start(check_interval_sec=60)

    # 统一管理后台任务引用（防 GC + 启动时可 cancel）
    global _app_bg_tasks
    # v11.6: 实例化 AlertManager 并加载活跃警报到内存
    global alert_manager
    try:
        from backend.alerts.manager import AlertManager
        alert_manager = AlertManager(db=db)
        await alert_manager.load_alerts()
        logger.info(f"AlertManager 已加载 {len(alert_manager.get_active_alerts())} 条活跃警报")
        # 启动后台巡检循环：每 60s 跑一次（仅交易时段）
        _app_bg_tasks.append(asyncio.create_task(_alert_check_loop()))
    except Exception as e:
        logger.warning(f"AlertManager 初始化失败: {e}")
    _app_bg_tasks.append(asyncio.create_task(_stock_auto_bind_loop()))

    # v12.3: TradeReviewer 启动
    global trade_reviewer
    try:
        from backend.trading.reviewer import TradeReviewer
        from backend.news.scheduler import _ai_analyzer as _ana
        trade_reviewer = TradeReviewer(db=db, ai_analyzer=_ana)
        _app_bg_tasks.append(asyncio.create_task(_trade_review_loop()))
        _app_bg_tasks.append(asyncio.create_task(_trade_review_weekly_loop()))
        _app_bg_tasks.append(asyncio.create_task(_lesson_aggregation_loop()))
        logger.info("[reviewer] TradeReviewer 已启动 — 每 4h 单笔复盘 + 每天周报 + 每 6h 教训聚合")
    except Exception as e:
        logger.warning(f"TradeReviewer 启动失败: {e}")
    logger.info("MonitorEngine started + 加密多周期 + 股票候选池自动绑定")

    # v11.6: 接真实 news_provider（之前永远空列表，advisor 新闻分支死代码）
    portfolio_tracker = PortfolioTracker(db=db, ws_hub=hub, news_provider=_news_for_symbol)
    portfolio_tracker.start(check_interval_sec=300)
    logger.info("PortfolioTracker started")

    # 6.5 Phase 3A 通道②：异动扫描器（每 5 分钟拉数据源涨幅榜，仅交易时段）
    global anomaly_scanner
    anomaly_scanner = AnomalyScanner(db=db, ws_hub=hub)
    anomaly_scanner.start(check_interval_sec=300)
    logger.info("AnomalyScanner started")

    # 7. Phase 7 交易模拟器（默认开启 dry-run 模式）
    await trading_simulator.connect()

    # 7.5 自动交易引擎（模拟，默认关闭，用户前端打开）
    global auto_trader
    from backend.trading.auto_trader import AutoTrader
    auto_trader = AutoTrader(db=db, portfolio_manager=portfolio_manager, ws_hub=hub)
    await auto_trader.init()
    logger.info(f"AutoTrader ready (enabled={auto_trader.enabled})")

    # 5. Phase 3B LLM 深度解读引擎（先初始化以便注入到 NewsScheduler）
    global ai_analyzer
    ai_analyzer = NewsAIAnalyzer(db=db, ws_hub=hub)
    attach_ai_analyzer(ai_analyzer)

    # 6. Phase 3A 新闻采集调度器
    global news_scheduler
    news_scheduler = NewsScheduler(
        db=db,
        ws_hub=hub,
        holding_provider=_get_holding_symbols,
        pool_provider=_get_pool_symbols,
    )
    news_scheduler.start()
    _app_bg_tasks.append(asyncio.create_task(_refresh_pool_symbols_cache()))
    _app_bg_tasks.append(asyncio.create_task(_refresh_symbol_registry_loop()))
    _app_bg_tasks.append(asyncio.create_task(_pool_rescore_loop()))
    _app_bg_tasks.append(asyncio.create_task(_pool_pending_retry_loop()))
    _app_bg_tasks.append(asyncio.create_task(_data_retention_loop()))
    _app_bg_tasks.append(asyncio.create_task(_pool_fundamentals_refresh_loop()))
    _app_bg_tasks.append(asyncio.create_task(_pool_auto_archive_loop()))
    _app_bg_tasks.append(asyncio.create_task(_pool_diagnose_loop()))
    _app_bg_tasks.append(asyncio.create_task(_news_ai_backfill_loop()))
    _app_bg_tasks.append(asyncio.create_task(_crypto_diagnose_loop()))
    _app_bg_tasks.append(asyncio.create_task(_signal_verify_backfill_loop()))
    _app_bg_tasks.append(asyncio.create_task(_pending_orders_retry_loop()))
    _app_bg_tasks.append(asyncio.create_task(_position_advice_loop()))
    _app_bg_tasks.append(asyncio.create_task(_aged_position_advice_loop()))  # v11.3 老持仓加速
    _app_bg_tasks.append(asyncio.create_task(_holding_diagnose_loop()))  # v12.13 持仓诊断加速
    _app_bg_tasks.append(asyncio.create_task(_position_summary_loop()))  # v12.13 4h 持仓简报推送
    logger.info(f"NewsScheduler + SymbolRegistry + AI Analyzer started (registry size={symbol_registry.size()})")

    # LLM 日成本：NewsAIAnalyzer._refresh_today_cost 内部按日期切换自动重置（无需额外清零任务）

    logger.info("OpenChart Pro ready on http://%s:%d", config.HOST, config.PORT)

    yield  # ← 应用运行中

    # ─── 关闭 ───
    logger.info("OpenChart Pro shutting down...")
    # 先 cancel 所有后台循环任务（防止它们在关闭过程中再访问 db）
    for task in _app_bg_tasks:
        task.cancel()
    if _app_bg_tasks:
        await asyncio.gather(*_app_bg_tasks, return_exceptions=True)
    if anomaly_scanner:
        await anomaly_scanner.stop()
    if portfolio_tracker:
        await portfolio_tracker.stop()
    if monitor_engine:
        await monitor_engine.stop()
    if news_scheduler:
        await news_scheduler.stop()
    if ai_analyzer:
        try:
            await ai_analyzer.shutdown()
        except Exception as e:
            logger.debug(f"ai_analyzer.shutdown 异常: {e}")
    await trading_simulator.close()
    for task in _ws_subscriptions.values():
        task.cancel()
    # 关闭模块级共享 aiohttp session（quality_filter / anomaly_scanner）
    # 避免 Unclosed client session 警告
    try:
        from backend.watchpool import quality_filter as _qf
        if _qf._SHARED_SESSION and not _qf._SHARED_SESSION.closed:
            await _qf._SHARED_SESSION.close()
    except Exception as e:
        logger.debug(f"关闭 quality_filter session 异常: {e}")
    try:
        from backend.watchpool import anomaly_scanner as _as
        if _as._SHARED_SESSION and not _as._SHARED_SESSION.closed:
            await _as._SHARED_SESSION.close()
    except Exception as e:
        logger.debug(f"关闭 anomaly_scanner session 异常: {e}")
    try:
        from backend.notify import telegram as _tg
        await _tg.close_session()
    except Exception as e:
        logger.debug(f"关闭 telegram session 异常: {e}")
    try:
        from backend import market_context as _mc
        await _mc.close_session()
    except Exception as e:
        logger.debug(f"关闭 market_context session 异常: {e}")
    await db.close()


app = FastAPI(title="OpenChart Pro", version="3.0.0", lifespan=lifespan)

# v12.11: CORS 修复
# - allow_origins=["*"] + allow_credentials=True 是浏览器拒绝的非法组合
# - 用 ALLOWED_ORIGINS 显式列表时才允许 credentials；wildcard 时关闭 credentials
_cors_origins = getattr(config, "ALLOWED_ORIGINS", ["*"]) or ["*"]
_cors_credentials = "*" not in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


# v12.11: 简单 Token 认证中间件
# - 环境变量 OPENCHART_API_TOKEN 设置则启用；不设置则不启用（开发兼容）
# - 启用后所有 /api/* 写操作（POST/PUT/DELETE）需 Authorization: Bearer <token>
# - GET 不强制（前端读取无负担），但所有写都强制
import os as _os
_API_TOKEN = _os.getenv("OPENCHART_API_TOKEN", "").strip()

@app.middleware("http")
async def _api_auth(request, call_next):
    if _API_TOKEN and request.url.path.startswith("/api/") and request.method in ("POST", "PUT", "DELETE"):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:].strip() != _API_TOKEN:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"detail": "未授权：写操作需要 Authorization: Bearer <token>（设置 OPENCHART_API_TOKEN 环境变量）"},
            )
    return await call_next(request)


# v12.11: 全局异常处理 - 生产模式下不向前端泄露 stack trace 和 SQL 细节
from fastapi import HTTPException as _HTTPException
@app.exception_handler(Exception)
async def _global_exception_handler(request, exc):
    from fastapi.responses import JSONResponse
    # HTTPException 已是受控错误，直接转发
    if isinstance(exc, _HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    # 其它未捕获异常 → 500
    is_debug = getattr(config, "DEBUG", False)
    detail = f"{type(exc).__name__}: {exc}" if is_debug else "服务器内部错误，请稍后重试或联系管理员"
    logger.error(f"[unhandled] {request.method} {request.url.path}: {type(exc).__name__}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": detail})


# 开发期强制禁用前端静态资源缓存（避免浏览器拿到旧版 JS/CSS 导致"改了不生效"）
@app.middleware("http")
async def _no_cache_for_static(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path.endswith((".js", ".css", ".html")) or path == "/" or path.endswith("/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# ═══════════════════════════════════════════════════════════════════
# Pydantic 请求模型
# ═══════════════════════════════════════════════════════════════════


class WatchlistAddRequest(BaseModel):
    symbol: str
    market: str
    name: str = ""


class WatchlistReorderRequest(BaseModel):
    market: str
    symbols: List[str]  # 按新顺序排列的品种代码列表


class SettingsUpdateRequest(BaseModel):
    # 部分更新，任意字段都可选
    class Config:
        extra = "allow"


class IndicatorCalcRequest(BaseModel):
    symbol: str
    market: str
    interval: str
    indicators: List[Dict[str, Any]]  # [{"name": "MACD", "params": {...}}]
    limit: int = 500


# ═══════════════════════════════════════════════════════════════════
# 路由：市场 / 品种 / K线  (Phase 1)
# ═══════════════════════════════════════════════════════════════════

market_router = APIRouter(prefix="/api", tags=["市场"])


@market_router.get("/markets")
async def get_markets():
    """返回平台支持的四大市场及默认品种。"""
    return [
        {"id": "crypto", "name": "加密货币", "default_symbol": "BTC-USDT", "currency": "USDT"},
        {"id": "us", "name": "美股", "default_symbol": "AAPL", "currency": "USD"},
        {"id": "hk", "name": "港股", "default_symbol": "0700.HK", "currency": "HKD"},
        {"id": "cn", "name": "A股", "default_symbol": "600519", "currency": "CNY"},
    ]


@market_router.get("/symbols")
async def search_symbols(market: str = Query(...), q: str = Query("", alias="q")):
    """按市场搜索品种。支持代码和名称模糊匹配。"""
    m = _parse_market(market)
    fetcher = get_fetcher(m)
    symbols = await fetcher.get_symbols(query=q)
    return [
        {
            "symbol": s.symbol,
            "name": s.name,
            "market": s.market.value,
            "exchange": s.exchange,
            "base": s.base,
            "quote": s.quote,
        }
        for s in symbols
    ]


@market_router.get("/klines")
async def get_klines(
    symbol: str = Query(...),
    interval: str = Query("1H"),
    limit: int = Query(500, ge=1, le=2000),
    market: Optional[str] = Query(None),
    end_time: Optional[int] = Query(None, description="毫秒时间戳，用于往左分页加载历史"),
    before: Optional[int] = Query(None, description="别名，等同 end_time"),
):
    """
    获取 K 线数据。
    - market 可选，不传时按 symbol 推断（BTC-USDT → crypto，600519 → cn 等）
    - end_time 参数用于前端"往左拖动自动加载"(PRD F1.9)
      - 接受毫秒时间戳，返回该时间之前的最近 limit 根 K 线
      - before 是兼容别名
    """
    # 市场推断
    if market:
        m = _parse_market(market)
    else:
        m = _infer_market(symbol)

    i = _parse_interval(interval)
    end_time_ms = end_time or before  # 兼容两种命名
    # 走缓存层：优先命中 SQLite，未命中再调上游 Fetcher，上游失败时降级返回缓存
    candles = await cached_get_klines(
        db=db, market=m, symbol=symbol, interval=i, limit=limit, end_time_ms=end_time_ms
    )

    return {
        "symbol": symbol,
        "market": m.value,
        "interval": interval,
        "candles": [
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "turnover": c.turnover,
            }
            for c in candles
        ],
    }


def _infer_market(symbol: str) -> Market:
    """根据品种代码模式推断市场（兜底用）。"""
    s = symbol.upper()
    if "-USDT" in s or "-USD" in s or "-USDC" in s:
        return Market.CRYPTO
    if s.endswith(".HK"):
        return Market.HK
    if s.isdigit() and len(s) == 6:
        return Market.CN
    return Market.US


# ═══════════════════════════════════════════════════════════════════
# 路由：指标  (Phase 2)
# ═══════════════════════════════════════════════════════════════════

indicator_router = APIRouter(prefix="/api/indicators", tags=["指标"])


@indicator_router.get("")
async def get_indicator_list():
    """返回所有内置指标列表及其参数定义。"""
    return list_indicators()


@indicator_router.post("/calculate")
async def calculate_indicators(req: IndicatorCalcRequest):
    """批量计算指标。先拉 K 线，然后逐个计算请求的指标。"""
    m = _parse_market(req.market)
    i = _parse_interval(req.interval)

    fetcher = get_fetcher(m)
    canon = _normalize_symbol(req.symbol, req.market) if req.market in ("us", "hk", "cn") else req.symbol.upper()
    candles = await fetcher.get_klines(symbol=canon, interval=i, limit=req.limit)
    if not candles:
        raise HTTPException(status_code=404, detail="No kline data")

    # 构造 OHLCV numpy 数组
    ohlcv = {
        "open": np.array([c.open for c in candles], dtype=np.float64),
        "high": np.array([c.high for c in candles], dtype=np.float64),
        "low": np.array([c.low for c in candles], dtype=np.float64),
        "close": np.array([c.close for c in candles], dtype=np.float64),
        "volume": np.array([c.volume for c in candles], dtype=np.float64),
    }

    results: Dict[str, Any] = {}
    for item in req.indicators:
        name = item.get("name", "").upper()
        params = item.get("params", {})
        info = get_indicator_info(name)
        if not info:
            results[name] = {"error": f"Unknown indicator: {name}"}
            continue
        try:
            output = calculate_indicator(name, ohlcv, params)
            # numpy array -> list / dict values -> list
            if isinstance(output, dict):
                results[name] = {k: v.tolist() if hasattr(v, "tolist") else v for k, v in output.items()}
            elif hasattr(output, "tolist"):
                results[name] = output.tolist()
            else:
                results[name] = output
        except Exception as e:
            logger.exception(f"Indicator {name} failed")
            results[name] = {"error": str(e)}

    return results


# ═══════════════════════════════════════════════════════════════════
# 路由：自选列表  (Phase 2)
# ═══════════════════════════════════════════════════════════════════

watchlist_router = APIRouter(prefix="/api/watchlist", tags=["自选列表"])


@watchlist_router.get("")
async def get_watchlist(market: Optional[str] = Query(None)):
    """获取自选列表。可按市场过滤。"""
    return await db.get_watchlist(market=market)


@watchlist_router.post("")
async def add_watchlist(req: WatchlistAddRequest):
    """添加品种到自选列表。"""
    _parse_market(req.market)  # 校验市场合法
    canon = _normalize_symbol(req.symbol, req.market) if req.market in ("us","hk","cn") else req.symbol.upper()
    item_id = await db.add_to_watchlist(symbol=canon, market=req.market, name=req.name)
    return {"id": item_id, "added": True, "symbol": canon}


@watchlist_router.delete("/{symbol}")
async def remove_watchlist(symbol: str, market: str = Query(...)):
    """从自选列表删除品种。"""
    _parse_market(market)
    canon = _normalize_symbol(symbol, market) if market in ("us","hk","cn") else symbol.upper()
    await db.remove_from_watchlist(symbol=canon, market=market)
    return {"removed": True}


@watchlist_router.put("/reorder")
async def reorder_watchlist(req: WatchlistReorderRequest):
    """重新排序自选列表（按传入的 symbols 顺序）。"""
    _parse_market(req.market)
    canonical = [
        _normalize_symbol(s, req.market) if req.market in ("us", "hk", "cn") else s.upper()
        for s in req.symbols
    ]
    items = [
        {"symbol": sym, "market": req.market, "sort_order": idx}
        for idx, sym in enumerate(canonical)
    ]
    await db.update_watchlist_order(items)
    return {"reordered": True}


# ═══════════════════════════════════════════════════════════════════
# 路由：设置  (Phase 1)
# ═══════════════════════════════════════════════════════════════════

settings_router = APIRouter(prefix="/api/settings", tags=["设置"])


# API Key 类字段在 GET 时脱敏
_SENSITIVE_KEYS = {
    "DEEPSEEK_API_KEY",
    "QWEN_API_KEY",
    "FINNHUB_API_KEY",
    "GLASSNODE_API_KEY",
    "CRYPTOQUANT_API_KEY",
    "OKX_API_KEY",
    "OKX_SECRET_KEY",
    "OKX_PASSPHRASE",
    "TELEGRAM_BOT_TOKEN",
}


def _mask_secret(value: str) -> str:
    """脱敏 API Key：仅显示前 4 + 后 4 位，中间用 **** 替代。"""
    if not value or len(value) <= 8:
        return "****" if value else ""
    return f"{value[:4]}****{value[-4:]}"


@settings_router.get("")
async def get_settings():
    """获取当前运行时配置（敏感字段脱敏）。"""
    result = {}
    for key, value in _runtime_config.items():
        if key in _SENSITIVE_KEYS and isinstance(value, str):
            result[key.lower()] = _mask_secret(value)
        else:
            result[key.lower()] = value
    return result


@settings_router.post("/telegram-test")
async def test_telegram():
    """测试 Telegram 推送配置（保存配置后用户主动点击触发）。"""
    from backend.notify.telegram import send_test_message
    result = await send_test_message()
    return result


@settings_router.put("")
async def update_settings(req: SettingsUpdateRequest):
    """
    部分更新配置。写入 DB 并热更新内存。
    敏感字段如果传入 "****" 脱敏值，视为未修改，保留原值。
    """
    updates = req.model_dump(exclude_unset=True)

    for key_lower, value in updates.items():
        key_upper = key_lower.upper()
        # 脱敏值不覆盖
        if key_upper in _SENSITIVE_KEYS and isinstance(value, str) and "****" in value:
            continue
        # 序列化复杂类型
        if isinstance(value, (dict, list, bool)):
            stored = json.dumps(value)
        else:
            stored = str(value)
        await db.set_config(key_upper, stored)
        _runtime_config[key_upper] = value
        # 同步到 config 模块属性，让 ai_analyzer 等模块立即生效
        setattr(config, key_upper, value)

    return {"success": True, "updated": list(updates.keys())}


# ═══════════════════════════════════════════════════════════════════
# WebSocket 端点  (Phase 1)
# ═══════════════════════════════════════════════════════════════════


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket 双向通信端点。
    客户端消息格式（与前端 websocket.js 保持一致）：
      {"type": "subscribe" | "unsubscribe" | "switch", "symbol": "...", "interval": "..."}
    服务端推送消息类型见 TDD §8.2（kline/flash_news/signal/...）
    """
    await hub.handle_client(websocket)


# ═══════════════════════════════════════════════════════════════════
# 路由：新闻快讯  (Phase 3A)
# ═══════════════════════════════════════════════════════════════════

news_router = APIRouter(prefix="/api/news", tags=["新闻"])


@news_router.get("/flash")
async def list_flash_news(
    market: Optional[str] = Query(None, description="过滤市场：crypto/us/hk/cn/macro"),
    importance_min: int = Query(1, ge=1, le=5),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    keyword: Optional[str] = Query(None, description="标题/正文关键词搜索"),
    symbol: Optional[str] = Query(None, description="按 categories 中的 symbol 精确过滤"),
):
    """新闻快讯列表，按发布时间倒序。支持市场/搜索/symbol 过滤。"""
    items = await db.get_flash_news(
        market=market, importance_min=importance_min,
        limit=limit, offset=offset,
        keyword=keyword, symbol=symbol,
    )
    return {"count": len(items), "items": items}


@news_router.get("/flash/{news_id}")
async def get_flash_news_detail(news_id: str):
    """单条新闻详情（含 AI 分析字段）。"""
    item = await db.get_flash_news_by_id(news_id)
    if not item:
        raise HTTPException(status_code=404, detail="News not found")
    return item


@news_router.get("/sources")
async def get_news_sources_health():
    """各采集源的健康度状态（监控用）。"""
    if not news_scheduler:
        return []
    return news_scheduler.get_health()


@news_router.post("/flash/{news_id}/analyze")
async def trigger_news_ai_analysis(news_id: str):
    """
    用户主动触发 LLM 深度解读（Phase 3B）。
    幂等：已有 ai_analysis 直接返回，否则同步调 LLM。
    """
    if not ai_analyzer:
        raise HTTPException(status_code=503, detail="AI 分析引擎未启动")
    item = await db.get_flash_news_by_id(news_id)
    if not item:
        raise HTTPException(status_code=404, detail="News not found")
    if item.get("ai_analysis"):
        return {"cached": True, "ai_analysis": item["ai_analysis"]}
    result = await ai_analyzer.deep_analyze_news(item)
    if result is None:
        raise HTTPException(
            status_code=503,
            detail="LLM 调用失败（API Key 未配置 / 网络异常 / 解析失败），请在设置中检查 LLM 配置",
        )
    return {"cached": False, "ai_analysis": result}


@news_router.get("/cost")
async def get_llm_cost_status():
    """今日 LLM 累计成本 + 预算状态 + 按路径拆分（news/signal_verify/diagnose/position_advice）。"""
    if not ai_analyzer:
        return {"status": "disabled", "today_cost_usd": 0, "daily_budget": config.LLM_DAILY_BUDGET}
    base = await ai_analyzer.get_cost_status()
    # 按 path 拆分今日成本
    try:
        day_start = int(time.time() // 86400) * 86400 * 1000
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(path, '') AS path, COUNT(*) AS n, ROUND(SUM(cost_usd), 4) AS cost "
                "FROM llm_cost_log WHERE called_at >= ? GROUP BY path",
                (day_start,),
            )
            base["by_path"] = [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        logger.debug(f"cost by_path query fail: {e}")
        base["by_path"] = []
    return base


# ═══════════════════════════════════════════════════════════════════
# 路由：候选池  (Phase 3A) — 仅股票市场
# ═══════════════════════════════════════════════════════════════════

pool_router = APIRouter(prefix="/api/pool", tags=["候选池"])


class PoolAddRequest(BaseModel):
    symbol: str
    market: str
    reason: str = ""
    score: float = 50.0


@pool_router.get("")
async def list_pool_items(
    status: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """查询候选池条目，按评分降序。"""
    items = await db.get_pool_items(status=status, market=market, limit=limit)
    return {"count": len(items), "items": items}


def _normalize_symbol(symbol: str, market: str) -> str:
    """
    标准化股票代码，确保入池/绑定/信号 symbol 统一：
      - 港股: 纯数字补足 4 位 + .HK (700 → 0700.HK, 9988 → 9988.HK)
      - A 股: 6 位数字保持不变（去除 .SH/.SZ 后缀）
      - 美股/加密: 大写
    """
    if not symbol:
        return symbol
    s = symbol.strip().upper()
    if market == "hk":
        # 去掉可能的 .HK 后缀后补零
        core = s.replace(".HK", "")
        if core.isdigit():
            return core.zfill(4) + ".HK"
        return s if s.endswith(".HK") else s + ".HK"
    if market == "cn":
        # 去掉 .SH/.SZ 后缀
        return s.replace(".SH", "").replace(".SZ", "")
    return s


@pool_router.post("")
async def add_to_pool(req: PoolAddRequest):
    """
    手动添加股票到候选池 + 启动全套监控能力：
      1) 入池（绕过质量筛选，source='manual'）
      2) 立即绑 3 个稳健策略 × 1D（不受 score 门槛限制）
      3) 异步拉基本面（最多 15s，不阻塞响应）
      4) 异步跑 AI 诊断（后台诊断循环也兜底重诊断）
    手动添加的股票享受：
      - 免自动淘汰
      - 每日基本面刷新
      - 每小时三维评分重算
      - 策略信号监控（MA/Bollinger/Volume × 1D）+ 高置信度 AI 二次验证
      - 诊断自动更新（7 天周期）
    """
    if req.market == "crypto":
        raise HTTPException(
            status_code=400,
            detail="加密货币不使用候选池：6 币种已通过 OKX WebSocket 自动监控",
        )
    if req.market not in ("us", "hk", "cn"):
        raise HTTPException(status_code=400, detail=f"Invalid market: {req.market}")
    # 标准化 symbol（港股补零 + .HK；A 股去后缀；大写）
    canonical_symbol = _normalize_symbol(req.symbol, req.market)
    if canonical_symbol != req.symbol:
        logger.info(f"[add-pool] symbol 标准化: {req.symbol!r} → {canonical_symbol!r}")
    # 预检是否已在池中（活跃 or 归档）
    existing_status = None
    existing_source = None
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT status, source FROM watch_pool WHERE symbol=? AND market=?",
                (canonical_symbol, req.market),
            )
            row = await cur.fetchone()
            if row:
                existing_status = row["status"]
                existing_source = row["source"]
    except Exception:
        pass
    try:
        item_id = await db.add_to_pool(
            symbol=canonical_symbol, market=req.market, source="manual",
            score=req.score, reason=req.reason or "用户手动添加",
        )
        await hub.broadcast_pool_update("added", {
            "id": item_id, "symbol": canonical_symbol, "market": req.market,
            "source": "manual", "score": req.score,
        })

        # 立即绑定策略（无 score 门槛）+ 异步拉基本面 + 异步 AI 诊断
        async def _post_add_init():
            # 3a 立即绑策略
            try:
                if monitor_engine:
                    for strat in monitor_engine.STOCK_AUTO_STRATEGIES:
                        for iv in monitor_engine.STOCK_INTERVALS:
                            await monitor_engine.bindings.bind(
                                symbol=canonical_symbol, market=req.market,
                                strategy_name=strat, interval=iv, enabled=True,
                            )
                    logger.info(f"[manual-add] {canonical_symbol} 已立即绑 {len(monitor_engine.STOCK_AUTO_STRATEGIES)} 策略")
            except Exception as e:
                logger.warning(f"[manual-add] {canonical_symbol} 策略绑定失败: {e}")
            # 3b 拉基本面（避免下次评分 fund=0）
            try:
                from backend.watchpool.quality_filter import (
                    _fetch_cn_fundamentals, _fetch_hk_fundamentals,
                    _fetch_us_fundamentals, _save_cached,
                )
                data = None
                if req.market == "cn":
                    data = await _fetch_cn_fundamentals(canonical_symbol, db=db)
                elif req.market == "hk":
                    data = await _fetch_hk_fundamentals(canonical_symbol)
                elif req.market == "us":
                    data = await _fetch_us_fundamentals(canonical_symbol)
                    if not data:
                        try:
                            from backend.data.us_aggregator import fetch_us_fundamentals_nasdaq
                            data = await fetch_us_fundamentals_nasdaq(canonical_symbol)
                        except Exception:
                            pass
                if data:
                    await _save_cached(db, canonical_symbol, req.market, data)
                    logger.info(f"[manual-add] {canonical_symbol} 基本面拉取成功")
            except Exception as e:
                logger.debug(f"[manual-add] {canonical_symbol} 基本面拉取异常: {e}")
            # 3c AI 诊断（可能因 LLM 预算或网络失败，后台诊断循环每 7 天兜底）
            if ai_analyzer:
                try:
                    await ai_analyzer.diagnose_stock(canonical_symbol, req.market, force=True)
                except Exception as e:
                    logger.debug(f"[manual-add] {canonical_symbol} 诊断异常: {e}")

        _app_bg_tasks.append(asyncio.create_task(_post_add_init()))
        # 区分 4 种状态
        src_label = {"news":"新闻驱动","news_ai":"AI 解读","anomaly":"异动榜","macro_theme":"宏观主题","manual":"手动"}
        if existing_status is None:
            state = "new"
            message = "新添加到候选池"
        elif existing_status == "archived":
            state = "revived"
            message = f"已从归档恢复（原来源：{src_label.get(existing_source, existing_source)}），并升级为手动添加（免自动淘汰）"
        elif existing_source == "manual":
            state = "already_exists"
            message = "该股票已是手动添加状态，只更新了入池理由"
        else:
            state = "upgraded_to_manual"
            message = f"该股票原本是「{src_label.get(existing_source, existing_source)}」入池，已升级为「手动添加」（免自动淘汰，保留原评分/诊断/绑定）"
        return {
            "id": item_id, "added": True,
            "symbol": canonical_symbol,
            "original_symbol": req.symbol,
            "normalized": canonical_symbol != req.symbol,
            "state": state,
            "message": message,
            "existing_source": existing_source,
            "existing_status": existing_status,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@pool_router.get("/{item_id}/monitoring")
async def get_pool_monitoring_status(item_id: str):
    """
    返回某只股票的"监控链路全貌"：
      - 当前绑定策略列表
      - 最近 10 条信号
      - AI 诊断时间戳和评级
    让用户看清"为什么这只股票有/没有信号"。
    """
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT id, symbol, market, source, score, status, "
            "ai_diagnosis, ai_diagnosed_at, added_at "
            "FROM watch_pool WHERE id=?", (item_id,),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Pool item not found")
    it = dict(row)
    symbol, market = it["symbol"], it["market"]
    # 绑定列表
    bindings = []
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT strategy_name, interval, enabled, created_at "
                "FROM strategy_bindings WHERE symbol=? AND market=? ORDER BY created_at DESC",
                (symbol, market),
            )
            bindings = [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        logger.debug(f"bindings query fail: {e}")
    # 最近 10 条信号
    signals = []
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT id, action, strategy_name, interval, confidence, price, "
                "ai_verdict, ai_confidence, generated_at "
                "FROM signals WHERE symbol=? AND market=? ORDER BY generated_at DESC LIMIT 10",
                (symbol, market),
            )
            signals = [dict(r) for r in await cur.fetchall()]
    except Exception:
        pass
    # 诊断评级
    rating = None
    if it.get("ai_diagnosis"):
        try:
            rating = (json.loads(it["ai_diagnosis"]) or {}).get("rating")
        except Exception:
            pass
    return {
        "symbol": symbol, "market": market,
        "source": it["source"], "score": it["score"], "status": it["status"],
        "ai_rating": rating,
        "ai_diagnosed_at": it["ai_diagnosed_at"],
        "bindings": bindings,
        "bindings_count": len(bindings),
        "recent_signals": signals,
        "signals_count": len(signals),
    }


@pool_router.get("/{item_id}/diagnosis")
async def get_pool_diagnosis(item_id: str):
    """获取某条候选池股票的 AI 诊断结果（缓存）。"""
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT symbol, market, ai_diagnosis, ai_diagnosed_at FROM watch_pool WHERE id=?",
            (item_id,),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Pool item not found")
    raw = row["ai_diagnosis"]
    if not raw:
        return {"symbol": row["symbol"], "market": row["market"], "diagnosis": None,
                "diagnosed_at": 0, "status": "pending"}
    try:
        diag = json.loads(raw)
    except Exception:
        diag = {"raw": raw}
    return {
        "symbol": row["symbol"], "market": row["market"],
        "diagnosis": diag, "diagnosed_at": row["ai_diagnosed_at"], "status": "ready",
    }


@pool_router.post("/{item_id}/diagnose")
async def trigger_pool_diagnosis(item_id: str):
    """手动触发某条候选池股票的 AI 诊断（用户主动调用，force=True 即便预算超支也会调用）。"""
    if not ai_analyzer:
        raise HTTPException(status_code=503, detail="AI 分析引擎未启动")
    async with db.acquire() as conn:
        cur = await conn.execute("SELECT * FROM watch_pool WHERE id=?", (item_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Pool item not found")
    item = dict(row)
    res = await ai_analyzer.diagnose_stock(item["symbol"], item["market"], pool_item=item, force=True)
    if res is None:
        raise HTTPException(status_code=503, detail="LLM 调用失败（预算/Key/网络/解析），请检查日志")
    return {"symbol": item["symbol"], "market": item["market"], "diagnosis": res,
            "diagnosed_at": int(time.time()), "status": "ready"}


@pool_router.get("/{item_id}/diagnosis-history")
async def get_pool_diagnosis_history(item_id: str, limit: int = Query(10, ge=1, le=50)):
    """返回某只股票的最近 N 次 AI 诊断历史（用于"上次 vs 这次"对比）。"""
    async with db.acquire() as conn:
        cur = await conn.execute("SELECT symbol, market FROM watch_pool WHERE id=?", (item_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Pool item not found")
        symbol, market = row["symbol"], row["market"]
        cur = await conn.execute(
            """SELECT diagnosis, rating, confidence, diagnosed_at
               FROM ai_diagnosis_history
               WHERE symbol=? AND market=?
               ORDER BY diagnosed_at DESC LIMIT ?""",
            (symbol, market, limit),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    history = []
    for r in rows:
        try:
            r["diagnosis"] = json.loads(r["diagnosis"])
        except Exception:
            pass
        history.append(r)
    return {"symbol": symbol, "market": market, "count": len(history), "history": history}


@pool_router.get("/{item_id}/score-history")
async def get_pool_score_history(item_id: str, limit: int = Query(50, ge=1, le=500)):
    """返回某只股票的评分历史（用于绘制趋势图）。"""
    async with db.acquire() as conn:
        cur = await conn.execute(
            """SELECT score, factors, scored_at FROM pool_score_history
               WHERE pool_item_id=? ORDER BY scored_at DESC LIMIT ?""",
            (item_id, limit),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    series = []
    for r in rows:
        try:
            r["factors"] = json.loads(r["factors"]) if r["factors"] else {}
        except Exception:
            pass
        series.append(r)
    series.reverse()  # 时间升序便于前端绘图
    return {"count": len(series), "series": series}


@pool_router.post("/{item_id}/restore")
async def restore_pool_item(item_id: str):
    """从已归档恢复为 candidate（用户主动恢复淘汰的股票）。"""
    ok = await db.restore_pool_item(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Pool item not found or not archived")
    await hub.broadcast_pool_update("restored", {"id": item_id})
    return {"restored": True, "id": item_id}


@pool_router.post("/rescore")
async def rescore_pool():
    """手动触发候选池三维评分重算（event+technical+fundamentals）。"""
    from backend.watchpool.scorer import rescore_pool_items
    stats = await rescore_pool_items(db)
    return {"success": True, **stats}


@pool_router.post("/cleanup")
async def cleanup_pool(dry_run: bool = Query(False, description="True 只返回将被清理的列表，不实际删除")):
    """
    对现有候选池非"信号驱动"条目跑质量硬筛选，移除不达标的。
    豁免来源（始终保留，与自动淘汰循环一致）：
      - manual（用户手动）
      - news / news_ai / macro_theme（明确信号驱动）
    只对 anomaly 等被动来源执行严格清理。
    """
    from backend.watchpool.quality_filter import is_eligible
    CLEANUP_EXEMPT = {"manual", "news", "news_ai", "macro_theme"}
    items = await db.get_pool_items(limit=500)
    removed = []
    kept = 0
    for it in items:
        if it.get("source") in CLEANUP_EXEMPT:
            kept += 1
            continue
        try:
            ok, reason = await is_eligible(db, it["symbol"], it["market"])
        except Exception as e:
            logger.debug(f"[pool-cleanup] is_eligible 异常 {it['symbol']}: {e}")
            kept += 1
            continue
        if ok:
            kept += 1
            continue
        removed.append({"symbol": it["symbol"], "market": it["market"], "reason": reason, "id": it["id"]})
        if not dry_run:
            await db.remove_from_pool(it["id"])
            await hub.broadcast_pool_update("removed", {"id": it["id"]})
    logger.info(f"[pool-cleanup] dry_run={dry_run} kept={kept} removed={len(removed)}")
    return {"dry_run": dry_run, "kept": kept, "removed_count": len(removed), "removed": removed}


@pool_router.delete("/{item_id}")
async def remove_pool_item(item_id: str):
    """从候选池移除条目（硬删除）。"""
    ok = await db.remove_from_pool(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Pool item not found")
    await hub.broadcast_pool_update("removed", {"id": item_id})
    return {"removed": True}


# ═══════════════════════════════════════════════════════════════════
# 路由：策略信号  (Phase 4)
# ═══════════════════════════════════════════════════════════════════

signal_router = APIRouter(prefix="/api/signals", tags=["策略信号"])


@signal_router.get("")
async def list_signals(
    symbol: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    status: Optional[str] = Query("active"),
    limit: int = Query(100, ge=1, le=500),
):
    """信号列表，按生成时间倒序。"""
    async with db.acquire() as conn:
        sql = "SELECT * FROM signals WHERE 1=1"
        params: list = []
        if symbol:
            sql += " AND symbol = ?"; params.append(symbol)
        if market:
            sql += " AND market = ?"; params.append(market)
        if status:
            sql += " AND status = ?"; params.append(status)
        sql += " ORDER BY generated_at DESC LIMIT ?"; params.append(limit)
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        items = []
        for r in rows:
            d = dict(r)
            try:
                d["triggered_by"] = json.loads(d.get("triggered_by") or "{}")
            except json.JSONDecodeError:
                d["triggered_by"] = {}
            items.append(d)
        return {"count": len(items), "items": items}


@signal_router.get("/{signal_id}")
async def get_signal_detail(signal_id: str):
    """
    信号详情，附带：
      - triggered_by JSON 解析
      - ai_news_ids 查原始新闻（AI 验证的新闻溯源）
      - 候选池诊断（如果该股票在候选池）
    """
    async with db.acquire() as conn:
        cursor = await conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Signal not found")
        d = dict(row)
        try:
            d["triggered_by"] = json.loads(d.get("triggered_by") or "{}")
        except json.JSONDecodeError:
            d["triggered_by"] = {}
        # AI 溯源：拉原始新闻
        ai_news_ids = d.get("ai_news_ids") or ""
        related_news = []
        try:
            ids = json.loads(ai_news_ids) if ai_news_ids else []
        except Exception:
            ids = []
        if ids:
            placeholders = ",".join(["?"] * len(ids))
            cur = await conn.execute(
                f"SELECT id, title, source, importance, sentiment, published_at, url "
                f"FROM flash_news WHERE id IN ({placeholders})"
                f"ORDER BY published_at DESC",
                ids,
            )
            related_news = [dict(r) for r in await cur.fetchall()]
        d["related_news"] = related_news
        # 候选池诊断
        pool_diag = None
        try:
            cur = await conn.execute(
                "SELECT ai_diagnosis, ai_diagnosed_at, score FROM watch_pool "
                "WHERE symbol=? AND market=? AND status != 'archived' LIMIT 1",
                (d.get("symbol"), d.get("market")),
            )
            prow = await cur.fetchone()
            if prow and prow["ai_diagnosis"]:
                pool_diag = {
                    "diagnosis": json.loads(prow["ai_diagnosis"]),
                    "diagnosed_at": prow["ai_diagnosed_at"],
                    "pool_score": prow["score"],
                }
        except Exception:
            pass
        d["pool_context"] = pool_diag
        return d


# ═══════════════════════════════════════════════════════════════════
# 路由：策略 + 绑定  (Phase 4)
# ═══════════════════════════════════════════════════════════════════

strategy_router = APIRouter(prefix="/api/strategies", tags=["策略"])


class StrategyBindRequest(BaseModel):
    symbol: str
    market: str
    strategy_name: str
    params: Optional[Dict[str, Any]] = None


class BatchBindRequest(BaseModel):
    strategy_name: str
    targets: List[Dict[str, str]]  # [{"symbol":..., "market":...}]
    params: Optional[Dict[str, Any]] = None


@strategy_router.get("")
async def get_strategy_list():
    """所有内置策略元信息。v12.16: 强制 utf-8 charset 防中文 description 乱码。"""
    from fastapi.responses import JSONResponse
    import json
    return JSONResponse(
        content=list_strategies(),
        media_type="application/json; charset=utf-8",
    )


@strategy_router.post("/auto-bind-now")
async def trigger_auto_bind():
    """v11.6: 立刻触发一次股票自动绑定（不等 5 分钟周期）。
    用于：v11.5 加 chanlun 后让已有候选池股票立刻补上 chanlun binding。"""
    if not monitor_engine:
        raise HTTPException(status_code=503, detail="MonitorEngine 未启动")
    try:
        await monitor_engine.auto_bind_stock_pool()
        # 统计 chanlun 绑定数量给前端
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT market, COUNT(*) AS n FROM strategy_bindings "
                "WHERE strategy_name='chanlun' GROUP BY market"
            )
            stats = {r["market"]: r["n"] for r in await cur.fetchall()}
        return {"ok": True, "chanlun_bindings": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@strategy_router.get("/bindings")
async def list_strategy_bindings(
    symbol: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    strategy_name: Optional[str] = Query(None),
):
    """查询策略绑定。"""
    if not monitor_engine:
        return []
    return await monitor_engine.bindings.get_bindings(
        symbol=symbol, market=market, strategy_name=strategy_name, enabled_only=False
    )


@strategy_router.post("/bind")
async def bind_strategy(req: StrategyBindRequest):
    """单个绑定：一只品种 + 一个策略。"""
    if not monitor_engine:
        raise HTTPException(status_code=503, detail="MonitorEngine 未启动")
    canon = _normalize_symbol(req.symbol, req.market) if req.market in ("us","hk","cn") else req.symbol.upper()
    binding_id = await monitor_engine.bindings.bind(
        symbol=canon, market=req.market,
        strategy_name=req.strategy_name, params=req.params,
    )
    return {"id": binding_id, "bound": True}


@strategy_router.post("/batch-bind")
async def batch_bind_strategy(req: BatchBindRequest):
    """批量绑定：一个策略绑多只品种。"""
    if not monitor_engine:
        raise HTTPException(status_code=503, detail="MonitorEngine 未启动")
    # 所有 target 也标准化
    norm_targets = []
    for t in req.targets:
        mkt = t.get("market", "")
        sym = t.get("symbol", "")
        if mkt in ("us", "hk", "cn"):
            sym = _normalize_symbol(sym, mkt)
        else:
            sym = sym.upper()
        norm_targets.append({"symbol": sym, "market": mkt})
    return await monitor_engine.bindings.batch_bind(
        strategy_name=req.strategy_name, targets=norm_targets, params=req.params,
    )


@strategy_router.delete("/bind")
async def unbind_strategy(
    symbol: str = Query(...), market: str = Query(...), strategy_name: str = Query(...)
):
    """解绑。"""
    if not monitor_engine:
        raise HTTPException(status_code=503, detail="MonitorEngine 未启动")
    ok = await monitor_engine.bindings.unbind(symbol, market, strategy_name)
    return {"unbound": ok}


# ═══════════════════════════════════════════════════════════════════
# 路由：持仓管理  (Phase 5)
# ═══════════════════════════════════════════════════════════════════

position_router = APIRouter(prefix="/api/positions", tags=["持仓"])


class PositionAddRequest(BaseModel):
    symbol: str
    market: str
    quantity: float
    avg_cost: float
    notes: str = ""


class PositionUpdateRequest(BaseModel):
    quantity: Optional[float] = None
    avg_cost: Optional[float] = None
    notes: Optional[str] = None


@position_router.get("")
async def list_positions():
    """返回持仓列表，附带当前价 + 浮动盈亏（按 side 长/短分别计算）。"""
    if not portfolio_manager:
        return []
    positions = await portfolio_manager.get_all()
    if not positions:
        return []
    from backend.trading.fx import get_rate, market_to_currency
    enriched = []
    for p in positions:
        symbol = p.get("symbol")
        market = p.get("market")
        # 拉当前价：先 1H（盘中实时更新），fallback 1D（日K当日盘中对 A 股/加密也实时，美股要闭市后才更新）
        # 这样 NVDA 等美股持仓在 1H K 线刷新后立刻有最新价，浮盈准确度大幅提升
        current_price = None
        if market in ("cn", "hk", "us", "crypto"):  # 白名单防 SQL 注入
            try:
                async with db.acquire() as conn:
                    # 优先 1H
                    try:
                        cur = await conn.execute(
                            f"SELECT close, timestamp FROM [klines_{market}_1h] WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                            (symbol,),
                        )
                        row = await cur.fetchone()
                        if row:
                            # 只有 1H K 线时间在 6 小时内才认为"新鲜"（否则退回 1D）
                            import time as _t
                            if (_t.time() * 1000 - row["timestamp"]) < 6 * 3600 * 1000:
                                current_price = float(row["close"])
                    except Exception:
                        pass
                    # fallback 1D
                    if current_price is None:
                        cur = await conn.execute(
                            f"SELECT close FROM [klines_{market}_1d] WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                            (symbol,),
                        )
                        row = await cur.fetchone()
                        if row:
                            current_price = float(row["close"])
            except Exception:
                pass
        avg_cost = p.get("avg_cost") or 0
        qty = p.get("quantity") or 0
        side = p.get("side") or "long"
        currency = p.get("cost_currency") or market_to_currency(market)
        try:
            fx = await get_rate(db, currency)
        except Exception:
            fx = 1.0

        pnl_local = pnl_usd = pnl_pct = 0
        market_value_usd = 0
        if current_price and avg_cost > 0 and qty > 0:
            if side == "long":
                pnl_local = (current_price - avg_cost) * qty
                market_value_usd = current_price * qty * fx
            else:
                pnl_local = (avg_cost - current_price) * qty
                market_value_usd = max(0.0, avg_cost * qty * fx + pnl_local * fx)
            pnl_usd = pnl_local * fx
            pnl_pct = (current_price - avg_cost) / avg_cost * 100
            if side == "short":
                pnl_pct = -pnl_pct

        p["current_price"] = current_price
        p["pnl_local"] = round(pnl_local, 4) if pnl_local else 0
        p["pnl_usd"] = round(pnl_usd, 2) if pnl_usd else 0
        p["pnl_pct"] = round(pnl_pct, 2) if pnl_pct else 0
        p["market_value_usd"] = round(market_value_usd, 2) if market_value_usd else 0
        enriched.append(p)
    return enriched


@position_router.post("")
async def add_position(req: PositionAddRequest):
    if not portfolio_manager:
        raise HTTPException(status_code=503, detail="PortfolioManager 未启动")
    _parse_market(req.market)
    # 持仓也标准化（港股补零+.HK，A 股去后缀）
    canon = _normalize_symbol(req.symbol, req.market) if req.market in ("us","hk","cn") else req.symbol
    pid = await portfolio_manager.add_position(
        symbol=canon, market=req.market,
        quantity=req.quantity, avg_cost=req.avg_cost, notes=req.notes,
    )
    # 手动加仓后立即触发一次 AI 建议（不等 6h 巡检）
    try:
        from backend.news.scheduler import _ai_analyzer
        if _ai_analyzer is not None and pid:
            asyncio.create_task(_ai_analyzer.generate_advice_for_position(pid, force=True))
    except Exception as e:
        logger.debug(f"[add-position] 触发 AI 建议失败: {e}")
    return {"id": pid, "added": True, "symbol": canon}


@position_router.put("/{position_id}")
async def update_position(position_id: str, req: PositionUpdateRequest):
    if not portfolio_manager:
        raise HTTPException(status_code=503, detail="PortfolioManager 未启动")
    await portfolio_manager.update_position(
        position_id, quantity=req.quantity, avg_cost=req.avg_cost, notes=req.notes,
    )
    return {"updated": True}


@position_router.delete("/{position_id}")
async def remove_position(position_id: str):
    if not portfolio_manager:
        raise HTTPException(status_code=503, detail="PortfolioManager 未启动")
    ok = await portfolio_manager.remove_position(position_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Position not found")
    return {"removed": True}


@position_router.get("/{position_id}/advices")
async def get_position_advices(position_id: str, limit: int = Query(50, ge=1, le=200)):
    if not portfolio_manager:
        return []
    return await portfolio_manager.get_advice_history(position_id, limit=limit)


@position_router.post("/{position_id}/advise")
async def trigger_position_advice(position_id: str):
    """主动触发为该持仓生成 AI 建议（即使无新闻）。前端"刷新建议"按钮调用。"""
    if ai_analyzer is None:
        raise HTTPException(status_code=503, detail="AI 分析器未启动")
    advice = await ai_analyzer.generate_advice_for_position(position_id, force=True)
    if not advice:
        raise HTTPException(status_code=400, detail="生成失败：可能持仓不存在 / 无现价 / LLM 异常")
    return {"position_id": position_id, "advice": advice}


@position_router.get("/advices/latest")
async def get_latest_position_advices():
    """返回每个持仓的最新建议，用于前端一次性渲染。"""
    async with db.acquire() as conn:
        cur = await conn.execute(
            """SELECT p.id as position_id, p.symbol, p.market,
                      a.advice, a.reason, a.triggered_by, a.advised_at
               FROM positions p
               LEFT JOIN position_advices a ON a.position_id = p.id
                   AND a.id = (SELECT MAX(id) FROM position_advices WHERE position_id = p.id)
            """
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════
# 路由：加密仪表盘  (Phase 6, 复用 crypto_dashboard 模块)
# ═══════════════════════════════════════════════════════════════════

dashboard_router = APIRouter(prefix="/api/dashboard", tags=["加密仪表盘"])


@dashboard_router.get("/fear-greed")
async def get_fear_greed():
    """恐惧贪婪指数（数据源 Alternative.me，免费）。"""
    try:
        from backend.crypto_dashboard.sentiment import SentimentData
        sd = SentimentData()
        return await sd.get_fear_greed_index()
    except Exception as e:
        return {"error": str(e), "value": None, "label": "unknown"}


@dashboard_router.get("/funding-rate")
async def get_funding_rate(symbol: str = Query("BTC-USDT-SWAP")):
    try:
        from backend.crypto_dashboard.sentiment import SentimentData
        sd = SentimentData()
        return await sd.get_funding_rate(symbol)
    except Exception as e:
        return {"error": str(e)}


@dashboard_router.get("/open-interest")
async def get_open_interest(symbol: str = Query("BTC-USDT-SWAP")):
    try:
        from backend.crypto_dashboard.sentiment import SentimentData
        sd = SentimentData()
        return await sd.get_open_interest(symbol)
    except Exception as e:
        return {"error": str(e)}


@dashboard_router.get("/long-short-ratio")
async def get_long_short_ratio(coin: str = Query("BTC")):
    try:
        from backend.crypto_dashboard.sentiment import SentimentData
        sd = SentimentData()
        return await sd.get_long_short_ratio(coin)
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════
# 路由：加密诊断 (Phase 8)
# ═══════════════════════════════════════════════════════════════════

crypto_router = APIRouter(prefix="/api/crypto", tags=["加密诊断"])


@crypto_router.get("/list")
async def list_crypto_diagnoses():
    """返回所有加密币种的最新诊断（供前端面板）。"""
    items = []
    async with db.acquire() as conn:
        for sym in config.CRYPTO_SYMBOLS:
            cur = await conn.execute(
                "SELECT symbol, diagnosis, rating, confidence, price, diagnosed_at "
                "FROM crypto_diagnosis WHERE symbol=? LIMIT 1",
                (sym,),
            )
            row = await cur.fetchone()
            if row:
                d = dict(row)
                try: d["diagnosis"] = json.loads(d["diagnosis"])
                except Exception: d["diagnosis"] = None
                items.append(d)
            else:
                items.append({
                    "symbol": sym, "diagnosis": None,
                    "rating": None, "confidence": 0,
                    "price": 0, "diagnosed_at": 0,
                })
    return {"count": len(items), "items": items}


@crypto_router.get("/{symbol}/insights")
async def get_crypto_insights(symbol: str):
    """返回某币种的原始市场数据（funding/oi/ratio/ticker/fng 等）。"""
    # 先查缓存
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT payload, updated_at FROM crypto_insights_snapshot WHERE symbol=?",
            (symbol,),
        )
        row = await cur.fetchone()
    # 缓存 1 小时内直接用
    if row and (int(time.time()) - (row["updated_at"] or 0)) < 3600:
        try:
            return {"symbol": symbol, "cached": True, **json.loads(row["payload"])}
        except Exception:
            pass
    # 实时拉（首次访问）
    try:
        from backend.crypto_dashboard.sentiment import SentimentData
        data = await SentimentData().get_insights(symbol)
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO crypto_insights_snapshot (symbol, payload, updated_at) VALUES (?, ?, ?)",
                (symbol, json.dumps(data, ensure_ascii=False), int(time.time())),
            )
            await conn.commit()
        return {"symbol": symbol, "cached": False, **data}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"获取 insights 失败: {e}")


@crypto_router.get("/{symbol}/diagnosis")
async def get_crypto_diagnosis(symbol: str):
    """返回某币种最新 AI 诊断（从 crypto_diagnosis 表）。"""
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT diagnosis, rating, confidence, price, diagnosed_at "
            "FROM crypto_diagnosis WHERE symbol=?",
            (symbol,),
        )
        row = await cur.fetchone()
    if not row:
        return {"symbol": symbol, "diagnosis": None, "status": "pending"}
    d = dict(row)
    try:
        d["diagnosis"] = json.loads(d["diagnosis"])
    except Exception:
        pass
    return {"symbol": symbol, "status": "ready", **d}


@crypto_router.post("/{symbol}/diagnose")
async def trigger_crypto_diagnose(symbol: str):
    """手动触发诊断（用户主动点击；force=True 绕过预算）。"""
    if symbol not in config.CRYPTO_SYMBOLS:
        raise HTTPException(status_code=400, detail=f"不支持的币种：{symbol}")
    if not ai_analyzer:
        raise HTTPException(status_code=503, detail="AI 分析引擎未启动")
    result = await ai_analyzer.diagnose_crypto(symbol, force=True)
    if result is None:
        raise HTTPException(status_code=503, detail="LLM 调用失败")
    return {"symbol": symbol, "diagnosis": result, "status": "ready"}


@crypto_router.get("/{symbol}/diagnosis-history")
async def get_crypto_diagnosis_history(symbol: str, limit: int = Query(10, ge=1, le=50)):
    """返回某币种诊断时序（最新在前）。"""
    async with db.acquire() as conn:
        cur = await conn.execute(
            """SELECT diagnosis, rating, confidence, diagnosed_at
               FROM ai_diagnosis_history
               WHERE symbol=? AND market='crypto'
               ORDER BY diagnosed_at DESC LIMIT ?""",
            (symbol, limit),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        try: r["diagnosis"] = json.loads(r["diagnosis"])
        except Exception: pass
    return {"symbol": symbol, "count": len(rows), "history": rows}


# ═══════════════════════════════════════════════════════════════════
# 路由：交易模拟器  (Phase 7, 仅模拟下单)
# ═══════════════════════════════════════════════════════════════════

trading_router = APIRouter(prefix="/api/trading", tags=["交易"])


class SimOrderRequest(BaseModel):
    symbol: str
    side: str  # buy / sell
    quantity: float
    price: float = 0.0
    order_type: str = "market"


@trading_router.post("/simulate-order")
async def simulate_order(req: SimOrderRequest):
    """模拟下单（dry-run，不真实下单）。"""
    order = await trading_simulator.place_order(
        symbol=req.symbol, side=req.side,
        quantity=req.quantity, price=req.price,
        order_type=req.order_type,
    )
    return order


@trading_router.get("/sim-orders")
async def get_sim_orders(limit: int = Query(50, ge=1, le=500)):
    return await trading_simulator.list_orders(limit=limit)


# ═══════════════════════════════════════════════════════════════════
# 路由：自动交易
# ═══════════════════════════════════════════════════════════════════

auto_trade_router = APIRouter(prefix="/api/auto-trade", tags=["自动交易"])


@auto_trade_router.get("/status")
async def auto_trade_status():
    """返回自动交易状态 + 账户 + 配置。"""
    if not auto_trader:
        return {"enabled": False, "error": "AutoTrader 未启动"}
    acct = await auto_trader.get_account()
    cfg = auto_trader.get_config()

    # 账户盈亏新算法：已实现 + 未实现（浮盈）
    # 旧算法 (total - initial) 在"手动录入持仓"场景会虚增盈亏——
    # 因为手动加仓不扣 cash，持仓市值凭空多出来。
    total_positions_usd = 0.0
    unrealized_pnl_usd = 0.0
    manual_position_cost_usd = 0.0
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT symbol, market, quantity, avg_cost, cost_currency, side, "
                "total_cost_usd, auto_traded FROM positions"
            )
            rows = [dict(r) for r in await cur.fetchall()]
            from backend.trading.fx import get_rate
            # ─ 批量查询 1：按市场分组一次性取所有持仓的最新现价 ─
            from collections import defaultdict
            by_mkt: Dict[str, List[str]] = defaultdict(list)
            for r in rows:
                if r["market"] in ("cn", "hk", "us", "crypto"):
                    by_mkt[r["market"]].append(r["symbol"])
            price_map: Dict[Tuple[str, str], float] = {}
            for mkt, syms in by_mkt.items():
                if not syms:
                    continue
                placeholders = ",".join("?" for _ in syms)
                sql = f"""
                    SELECT symbol, close FROM (
                        SELECT symbol, close,
                          ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rn
                        FROM [klines_{mkt}_1d] WHERE symbol IN ({placeholders})
                    ) WHERE rn = 1
                """
                try:
                    cur_px = await conn.execute(sql, syms)
                    for px in await cur_px.fetchall():
                        price_map[(px["symbol"], mkt)] = float(px["close"])
                except Exception as e:
                    logger.debug(f"[auto-trade/status] 批量现价失败 {mkt}: {e}")

            # ─ 批量查询 2：一次性统计每个 (symbol, market) 的 executed 任意操作次数 ─
            # 改进：识别"手动持仓"用"任何 executed action"判定，包括 reduce/close
            # 否则全部被 reduce 过的 auto 持仓会被误归为"手动"
            cur_cnt = await conn.execute(
                "SELECT symbol, market, COUNT(*) AS n FROM auto_trade_log "
                "WHERE status='executed' GROUP BY symbol, market"
            )
            open_add_count: Dict[Tuple[str, str], int] = {
                (c["symbol"], c["market"]): c["n"] for c in await cur_cnt.fetchall()
            }

            # ─ 遍历持仓计算浮盈 + v11.3 僵尸资金统计 ─
            zombie_capital_usd = 0.0
            zombie_count = 0
            now_ts = int(time.time())
            zombie_age_sec = (auto_trader._config.get("zombie_age_days", 14) if auto_trader else 14) * 86400
            zombie_band = (auto_trader._config.get("zombie_pnl_band_pct", 5.0) if auto_trader else 5.0)
            # 不在循环里查 advice last_at（N+1 重）—— 一次查所有 position 的最新 advice
            adv_map = {}
            try:
                # 注意 r 的 keys；我们没有 position id 列表传到外面，需要重新查
                cur_adv = await conn.execute(
                    "SELECT position_id, MAX(advised_at) AS last FROM position_advices GROUP BY position_id"
                )
                for ar in await cur_adv.fetchall():
                    adv_map[ar["position_id"]] = ar["last"] or 0
            except Exception:
                pass
            # 旧 rows 里没有 id —— 重新拉一次（轻）
            try:
                cur_id = await conn.execute(
                    "SELECT id, symbol, market, opened_at FROM positions WHERE quantity > 0"
                )
                pos_meta = {(r2["symbol"], r2["market"]): {"id": r2["id"], "opened_at": r2["opened_at"]}
                            for r2 in await cur_id.fetchall()}
            except Exception:
                pos_meta = {}

            for r in rows:
                _mkt = r["market"]
                if _mkt not in ("cn", "hk", "us", "crypto"):
                    continue
                price = price_map.get((r["symbol"], _mkt))
                if price is None or not r["quantity"]:
                    continue
                try:
                    fx = await get_rate(db, r["cost_currency"] or "USD")
                except Exception:
                    fx = 1.0
                side = (r["side"] or "long")
                qty = r["quantity"]
                avg = r["avg_cost"] or price
                cost_usd = qty * avg * fx
                if side == "long":
                    mv = qty * price * fx
                    total_positions_usd += mv
                    unrealized_pnl_usd += (mv - cost_usd)
                else:
                    margin = cost_usd
                    pnl = (avg - price) * qty * fx
                    total_positions_usd += max(0.0, margin + pnl)
                    unrealized_pnl_usd += pnl
                # 手动持仓识别：没有任何 executed open/add 记录
                if r["quantity"] and r["avg_cost"]:
                    if open_add_count.get((r["symbol"], _mkt), 0) == 0:
                        manual_position_cost_usd += cost_usd
                # v11.3 僵尸资金：持仓 ≥ N 天 + 浮盈 |%| < band
                meta = pos_meta.get((r["symbol"], _mkt)) or {}
                opened = meta.get("opened_at") or 0
                pnl_pct_pos = ((price - avg) / avg * 100) if (avg > 0 and side == "long") \
                              else ((avg - price) / avg * 100) if avg > 0 else 0
                if opened and (now_ts - opened) >= zombie_age_sec and abs(pnl_pct_pos) < zombie_band:
                    market_value = qty * price * fx if side == "long" else max(0.0, cost_usd + (avg - price) * qty * fx)
                    zombie_capital_usd += market_value
                    zombie_count += 1

            # 已实现盈亏：按 position_id 分组
            cur4 = await conn.execute(
                "SELECT action, amount_usd, position_id FROM auto_trade_log WHERE status='executed'"
            )
            rows4 = await cur4.fetchall()
    except Exception as e:
        logger.debug(f"[auto-trade/status] 持仓汇总失败: {e}")
        rows4 = []
        zombie_capital_usd = 0.0
        zombie_count = 0

    # 按 position_id 分组，识别"闭环"的单子
    groups: Dict[str, Dict[str, float]] = {}
    for r in rows4:
        pid = r["position_id"]
        if not pid:
            continue
        g = groups.setdefault(pid, {"in": 0.0, "out": 0.0, "has_close": False})
        amt = r["amount_usd"] or 0
        act = r["action"]
        if act in ("open", "add"):
            g["in"] += amt
        elif act in ("reduce", "close"):
            g["out"] += amt
            if act == "close":
                g["has_close"] = True

    # 只统计有 close 动作的单子（真正"已实现"）
    realized_pnl_usd = sum(
        g["out"] - g["in"] for g in groups.values() if g["has_close"]
    )
    # 孤儿单：有 open 但 position 已从 positions 表消失 且 没有 close log
    # → 历史手工操作造成的数据污染，不计入 pnl，但单独标记让前端能提示用户
    active_pids = set()
    try:
        async with db.acquire() as conn:
            cur5 = await conn.execute("SELECT id FROM positions")
            active_pids = set(r["id"] for r in await cur5.fetchall())
    except Exception:
        pass
    orphan_amount = sum(
        (g["in"] - g["out"]) for pid, g in groups.items()
        if (not g["has_close"]) and (pid not in active_pids)
    )

    total_value_usd = (acct.get("cash_usd") or 0) + total_positions_usd
    initial = acct.get("initial_capital_usd") or 10000
    # 总盈亏 = 总权益 - 初始本金，确保"本金+盈亏=权益"恒成立
    # 原 unrealized+realized 因 partial reduce 未计入已实现而产生系统性低估
    pnl_usd = total_value_usd - initial
    pnl_pct = pnl_usd / initial * 100 if initial else 0

    # v12.0: 三池分项汇总（港美股 / A股 / 加密）
    pools_summary = []
    try:
        from backend.trading.fx import get_rate
        all_pools = await auto_trader.get_all_pools()
        # 准备每市场已实现盈亏（按 pool 分组）
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT market, action, amount_usd, fx_rate, position_id FROM auto_trade_log "
                "WHERE status='executed'"
            )
            log_rows = await cur.fetchall()
        # 按 pool 分组计算 realized
        from collections import defaultdict
        pool_groups = defaultdict(lambda: defaultdict(lambda: {"in": 0.0, "out": 0.0, "has_close": False}))
        POOL_OF = {"us": "us_hk", "hk": "us_hk", "cn": "cn", "crypto": "crypto"}
        # v12.1 关键：池货币 = USD/CNY/USD；港股 hk 是 HKD ≠ USD，必须先 HKD→USD 再加进 us_hk 池
        # market → pool 货币的换算因子（本币 → 池币）
        POOL_CURRENCY = {"us_hk": "USD", "cn": "CNY", "crypto": "USD"}
        async def _market_to_pool_factor(market: str) -> float:
            """qty × price 是本币；返回需要乘以多少才能变成池币。"""
            from backend.trading.fx import get_rate
            from backend.trading.fx import market_to_currency
            local_ccy = market_to_currency(market)  # us→USD, hk→HKD, cn→CNY, crypto→USDT
            pool_ccy = POOL_CURRENCY[POOL_OF.get(market, "us_hk")]
            if local_ccy == pool_ccy: return 1.0
            # 都用 USD 中转
            try:
                local_to_usd = await get_rate(db, local_ccy)        # local → USD
                if pool_ccy == "USD": return local_to_usd
                pool_to_usd = await get_rate(db, pool_ccy)           # pool → USD
                return local_to_usd / pool_to_usd if pool_to_usd > 0 else 1.0
            except Exception:
                return 1.0

        # 缓存每市场的换算因子
        market_factor_cache = {}
        async def _factor(m):
            if m not in market_factor_cache:
                market_factor_cache[m] = await _market_to_pool_factor(m)
            return market_factor_cache[m]

        # 已实现 — 用 amount_usd 直接除以 fx_rate（本币）→ 再用 _factor 换算到池币
        for r in log_rows:
            pid = r["position_id"]
            if not pid: continue
            pool_id = POOL_OF.get(r["market"], "us_hk")
            fx_r = r["fx_rate"] or 1.0
            local = (r["amount_usd"] or 0) / fx_r if fx_r > 0 else (r["amount_usd"] or 0)
            factor = await _factor(r["market"])
            local_in_pool = local * factor
            g = pool_groups[pool_id][pid]
            if r["action"] in ("open", "add"): g["in"] += local_in_pool
            elif r["action"] in ("reduce", "close"):
                g["out"] += local_in_pool
                if r["action"] == "close": g["has_close"] = True
        pool_realized = {pid: 0.0 for pid in ("us_hk","cn","crypto")}
        for pool_id, groups in pool_groups.items():
            pool_realized[pool_id] = sum(g["out"] - g["in"] for g in groups.values() if g["has_close"])
        # 持仓 / 浮盈（按池币：us+hk 加进 USD，cn 用 CNY，crypto 用 USD）
        pool_pos_value = {pid: 0.0 for pid in ("us_hk","cn","crypto")}
        pool_unrealized = {pid: 0.0 for pid in ("us_hk","cn","crypto")}
        for r in rows:
            _mkt = r["market"]
            pool_id = POOL_OF.get(_mkt, "us_hk")
            price = price_map.get((r["symbol"], _mkt))
            if price is None or not r["quantity"]:
                continue
            qty = r["quantity"]; avg = r["avg_cost"] or price
            side = (r["side"] or "long")
            factor = await _factor(_mkt)
            if side == "long":
                mv_pool = qty * price * factor
                cost_pool = qty * avg * factor
                pool_pos_value[pool_id] += mv_pool
                pool_unrealized[pool_id] += (mv_pool - cost_pool)
            else:
                margin_pool = qty * avg * factor
                pnl_pool = (avg - price) * qty * factor
                pool_pos_value[pool_id] += max(0.0, margin_pool + pnl_pool)
                pool_unrealized[pool_id] += pnl_pool
        for p in all_pools:
            pid = p["pool_id"]
            ccy = p["currency"]
            cash_local = p["cash"] or 0
            init_local = p["initial_capital"] or 1
            pos_v = pool_pos_value.get(pid, 0.0)
            unr = pool_unrealized.get(pid, 0.0)
            equity_local = cash_local + pos_v
            # pnl = equity - initial（而非 unrealized+realized），确保"本金+盈亏=权益"恒成立。
            # pool_realized 只统计 has_close 的单子，partial reduce 的已收款已在 cash 里
            # 但不在 realized 里，会造成 ~$438 的账面漏洞；改用 equity-initial 消除该偏差。
            pnl_total = equity_local - init_local
            rea = pnl_total - unr  # 派生：realized = total - unrealized（含部分平仓已实现）
            pnl_pct_p = pnl_total / init_local * 100 if init_local else 0
            # USD 等值（前端汇总用）
            try:
                if ccy == "USD":
                    fx_usd = 1.0
                else:
                    fx_usd = await get_rate(db, ccy)  # local→USD
            except Exception:
                fx_usd = 1.0
            pools_summary.append({
                "pool_id": pid,
                "name": p["name"],
                "currency": ccy,
                "initial_capital": round(init_local, 2),
                "cash": round(cash_local, 2),
                "positions_value": round(pos_v, 2),
                "equity": round(equity_local, 2),
                "unrealized_pnl": round(unr, 2),
                "realized_pnl": round(rea, 2),
                "pnl": round(pnl_total, 2),
                "pnl_pct": round(pnl_pct_p, 2),
                "equity_usd": round(equity_local * fx_usd, 2),
                "pnl_usd": round(pnl_total * fx_usd, 2),
                "fx_to_usd": fx_usd,
            })
    except Exception as e:
        logger.debug(f"[auto-trade/status] 资金池汇总失败: {e}")

    return {
        "enabled": cfg.get("enabled"),
        "config": cfg,
        "account": {
            "initial_capital_usd": initial,
            "cash_usd": acct.get("cash_usd"),
            "positions_value_usd": round(total_positions_usd, 2),
            "total_value_usd": round(total_value_usd, 2),
            "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round(pnl_pct, 2),
            "unrealized_pnl_usd": round(unrealized_pnl_usd, 2),
            "realized_pnl_usd": round(realized_pnl_usd, 2),
            "manual_position_cost_usd": round(manual_position_cost_usd, 2),
            "orphan_amount_usd": round(orphan_amount, 2),
            "zombie_capital_usd": round(zombie_capital_usd, 2),
            "zombie_count": zombie_count,
        },
        # v12.0: 3 资金池分项数据
        "pools": pools_summary,
    }


@auto_trade_router.post("/toggle")
async def auto_trade_toggle(body: Dict[str, Any] = Body(...)):
    """开关自动交易: {enabled: true/false}"""
    if not auto_trader:
        raise HTTPException(status_code=503, detail="AutoTrader 未启动")
    enabled = bool(body.get("enabled"))
    await auto_trader.set_config({"enabled": enabled})
    return {"enabled": enabled, "message": "自动交易已" + ("启动" if enabled else "停止")}


@auto_trade_router.post("/summary-now")
async def trigger_summary_now():
    """v12.13 立即推送一次持仓简报到 Telegram（移动端按钮调用）。"""
    if not getattr(config, "TELEGRAM_ENABLED", False):
        return {"ok": False, "error": "TELEGRAM_ENABLED=False"}
    try:
        from backend.notify.telegram import send_summary
        text = await _build_position_summary_text()
        if not text:
            return {"ok": False, "error": "持仓简报为空"}
        err = await send_summary(text)
        if err:
            return {"ok": False, "error": err}
        # 更新 last_push 时间戳，避免 4h 自动循环又推一次
        try:
            async with db.acquire() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES ('telegram_last_summary_at', ?)",
                    (str(int(time.time())),),
                )
                await conn.commit()
        except Exception:
            pass
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@auto_trade_router.post("/config")
async def auto_trade_set_config(body: Dict[str, Any] = Body(...)):
    """更新配置（部分字段，如 initial_capital_usd / allocation / max_single_position_pct 等）。"""
    if not auto_trader:
        raise HTTPException(status_code=503, detail="AutoTrader 未启动")
    await auto_trader.set_config(body or {})
    return {"config": auto_trader.get_config()}


# v11.6: 后台 backfill 状态（避免 N×1.2s sleep 阻塞 API）
_backfill_state = {"running": False, "started_at": 0, "result": None}

@auto_trade_router.post("/backfill-targets")
async def auto_trade_backfill_targets(body: Dict[str, Any] = Body(default={})):
    """
    主动调用 AI 为缺失 SL/TP 的持仓补全（v11.2）。
    v11.6: 改成 fire-and-forget — 立即返回 {started:true}，结果通过 GET /backfill-targets/status 查
    body:
      - position_id (可选): 只处理该持仓；不传则扫所有缺失的持仓
      - force (可选, bool): True 时即使已有 SL/TP 也重新生成；默认 False
    """
    if not auto_trader:
        raise HTTPException(status_code=503, detail="AutoTrader 未启动")
    pid = body.get("position_id") if isinstance(body, dict) else None
    force = bool(body.get("force")) if isinstance(body, dict) else False
    # 单笔（指定 pid）走同步快速返回；批量走后台
    if pid:
        return await auto_trader.backfill_position_targets(position_id=pid, force=force)
    if _backfill_state["running"]:
        return {"started": False, "running": True, "started_at": _backfill_state["started_at"],
                "msg": "已有 backfill 任务在跑，请稍后查 status"}
    async def _bg():
        _backfill_state["running"] = True
        _backfill_state["started_at"] = int(time.time())
        try:
            r = await auto_trader.backfill_position_targets(position_id=None, force=force)
            _backfill_state["result"] = r
        except Exception as e:
            _backfill_state["result"] = {"error": f"{type(e).__name__}: {e}"}
        finally:
            _backfill_state["running"] = False
    asyncio.create_task(_bg())
    return {"started": True, "msg": "已在后台运行，请通过 /api/auto-trade/backfill-targets/status 查询结果"}


@auto_trade_router.get("/backfill-targets/status")
async def auto_trade_backfill_status():
    """查询最近一次 backfill 任务的状态/结果。"""
    return _backfill_state


@auto_trade_router.get("/log")
async def auto_trade_log_list(
    limit: int = Query(50, ge=1, le=500),
    symbol: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="executed | rejected"),
    position_id: Optional[str] = Query(None, description="按持仓 id 过滤，返回该单完整生命周期"),
):
    """自动交易日志（按时间倒序）。position_id 过滤时顺序改为时间正序 + 返回整单累计盈亏。"""
    sql = "SELECT * FROM auto_trade_log WHERE 1=1"
    params: list = []
    if symbol:
        sql += " AND symbol = ?"; params.append(symbol)
    if status:
        sql += " AND status = ?"; params.append(status)
    if position_id:
        sql += " AND position_id = ?"; params.append(position_id)
        sql += " ORDER BY traded_at ASC LIMIT ?"
    else:
        sql += " ORDER BY traded_at DESC LIMIT ?"
    params.append(limit)
    async with db.acquire() as conn:
        cur = await conn.execute(sql, params)
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        if r.get("trigger_detail"):
            try: r["trigger_detail"] = json.loads(r["trigger_detail"])
            except Exception: pass

    resp = {"count": len(rows), "items": rows}
    if position_id:
        exec_rows = [r for r in rows if r.get("status") == "executed"]
        total_in = sum((r.get("amount_usd") or 0) for r in exec_rows if r.get("action") in ("open", "add"))
        total_out = sum((r.get("amount_usd") or 0) for r in exec_rows if r.get("action") in ("reduce", "close"))
        qty_in = sum((r.get("quantity") or 0) for r in exec_rows if r.get("action") in ("open", "add"))
        qty_out = sum((r.get("quantity") or 0) for r in exec_rows if r.get("action") in ("reduce", "close"))
        is_closed = any(r.get("action") == "close" for r in exec_rows)

        summary: Dict[str, Any] = {
            "position_id": position_id,
            "leg_count": len(exec_rows),
            "total_bought_usd": round(total_in, 2),
            "total_sold_usd": round(total_out, 2),
            "qty_bought": round(qty_in, 6),
            "qty_sold": round(qty_out, 6),
            "is_closed": is_closed,
            "first_trade_at": exec_rows[0]["traded_at"] if exec_rows else None,
            "last_trade_at": exec_rows[-1]["traded_at"] if exec_rows else None,
        }

        if is_closed:
            # 已平仓：realized = 卖 − 买
            realized = total_out - total_in
            summary["realized_pnl_usd"] = round(realized, 2)
            summary["realized_pnl_pct"] = round((realized / total_in * 100) if total_in > 0 else 0, 2)
        else:
            # 仍持仓：计算当前浮盈（市值 − 成本）+ 部分已减仓回收
            # 加上已减仓收入和未平仓浮盈，让用户看到"如果此刻平仓会盈亏多少"
            floating_pnl = 0.0
            current_mv = 0.0
            try:
                async with db.acquire() as conn:
                    cur_pos = await conn.execute(
                        "SELECT symbol, market, quantity, avg_cost, cost_currency, side "
                        "FROM positions WHERE id=?",
                        (position_id,),
                    )
                    pos = await cur_pos.fetchone()
                if pos:
                    _mkt = pos["market"]
                    if _mkt in ("cn", "hk", "us", "crypto"):
                        async with db.acquire() as conn:
                            cur_px = await conn.execute(
                                f"SELECT close FROM [klines_{_mkt}_1d] WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                                (pos["symbol"],),
                            )
                            px_row = await cur_px.fetchone()
                        if px_row:
                            from backend.trading.fx import get_rate
                            try:
                                fx = await get_rate(db, pos["cost_currency"] or "USD")
                            except Exception:
                                fx = 1.0
                            price = float(px_row["close"])
                            qty = pos["quantity"] or 0
                            avg = pos["avg_cost"] or price
                            side = pos["side"] or "long"
                            current_mv = qty * price * fx
                            if side == "long":
                                floating_pnl = (price - avg) * qty * fx
                            else:
                                floating_pnl = (avg - price) * qty * fx
            except Exception as e:
                logger.debug(f"[log-summary] 浮盈计算失败: {e}")
            summary["current_market_value_usd"] = round(current_mv, 2)
            summary["unrealized_pnl_usd"] = round(floating_pnl, 2)
            # 全平后预估盈亏 = 已减仓收入 + 若全平收益 − 总开仓成本
            # 空仓平仓收益 = margin_release + pnl = (2*avg - price)*qty*fx（非市值）
            if pos and (pos.get("side") or "long") == "short":
                qty_rem = (pos.get("quantity") or 0)
                avg_rem = (pos.get("avg_cost") or 0)
                close_proceed = max(0.0, (2 * avg_rem - price) * qty_rem * fx) if (price and avg_rem and qty_rem) else 0.0
                projected = total_out + close_proceed - total_in
            else:
                projected = total_out + current_mv - total_in
            summary["projected_pnl_usd"] = round(projected, 2)
            summary["projected_pnl_pct"] = round((projected / total_in * 100) if total_in > 0 else 0, 2)

        resp["summary"] = summary
    return resp


@auto_trade_router.get("/trades-by-position")
async def auto_trade_trades_by_position():
    """
    按单（position_id）分组返回完整交易历史，用于"历史交易记录"视图。
    每一组包含：品种/市场/方向/状态/累计盈亏/开仓时间/最近一笔时间 + 所有分步 executed 明细（含拒绝）
    """
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT * FROM auto_trade_log ORDER BY traded_at ASC"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        cur2 = await conn.execute(
            "SELECT id, symbol, market, quantity, avg_cost, cost_currency, side, opened_at, auto_traded "
            "FROM positions"
        )
        positions = {r["id"]: dict(r) for r in await cur2.fetchall()}

    # 当前价 helper
    from backend.trading.fx import get_rate
    async def _get_price(sym, mkt):
        if mkt not in ("cn", "hk", "us", "crypto"):
            return None
        try:
            async with db.acquire() as conn:
                cur = await conn.execute(
                    f"SELECT close FROM [klines_{mkt}_1d] WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                    (sym,),
                )
                row = await cur.fetchone()
                return float(row["close"]) if row else None
        except Exception:
            return None

    for r in rows:
        if r.get("trigger_detail"):
            try: r["trigger_detail"] = json.loads(r["trigger_detail"])
            except Exception: pass

    # 按 position_id 分组；position_id 为空的（rejected 早期或异常）按 symbol+market 归到同名组的末尾展示
    groups: Dict[str, Dict[str, Any]] = {}
    orphan_reject_by_symbol: Dict[str, List[Dict]] = {}

    for r in rows:
        pid = r.get("position_id")
        if not pid:
            # 无 position_id 的记录（一般是 rejected，还没创建 position）
            key = f"{r['symbol']}|{r['market']}"
            orphan_reject_by_symbol.setdefault(key, []).append(r)
            continue
        g = groups.setdefault(pid, {
            "position_id": pid,
            "symbol": r["symbol"],
            "market": r["market"],
            "side": "long",
            "legs": [],
            "leg_count": 0,
            "total_in_usd": 0.0,
            "total_out_usd": 0.0,
            "open_at": None,
            "last_at": None,
            "is_closed": False,
            "has_any_executed": False,
        })
        g["legs"].append(r)
        if r["status"] == "executed":
            g["leg_count"] += 1
            g["has_any_executed"] = True
            amt = r.get("amount_usd") or 0
            if r["action"] in ("open", "add"):
                g["total_in_usd"] += amt
            elif r["action"] in ("reduce", "close"):
                g["total_out_usd"] += amt
            if r["action"] == "close":
                g["is_closed"] = True
            if g["open_at"] is None and r["action"] == "open":
                g["open_at"] = r["traded_at"]
            g["last_at"] = r["traded_at"]
            # side 从 trigger_detail 或 position 取
            td = r.get("trigger_detail") or {}
            if isinstance(td, dict) and td.get("side"):
                g["side"] = td["side"]

    # 归并：把 orphan rejected 拼到同品种"当前持仓"的组下，让用户看到"这一单有多少次尝试被拒"
    for key, rejects in orphan_reject_by_symbol.items():
        sym, mkt = key.split("|")
        # 找这个品种当前活跃 position
        active_group = None
        for pid, g in groups.items():
            if g["symbol"] == sym and g["market"] == mkt and (pid in positions):
                active_group = g
                break
        if active_group:
            # 按时间排进已有 legs
            active_group["legs"].extend(rejects)
            active_group["legs"].sort(key=lambda x: x.get("traded_at") or 0)
            if active_group["last_at"] is None or (rejects and rejects[-1]["traded_at"] > active_group["last_at"]):
                active_group["last_at"] = rejects[-1]["traded_at"]
        else:
            # 没有匹配 position，单独建一个"历史拒绝"组，pid 用 symbol|market 占位
            placeholder_pid = f"_orphan_{key}"
            groups[placeholder_pid] = {
                "position_id": placeholder_pid,
                "symbol": sym,
                "market": mkt,
                "side": "long",
                "legs": sorted(rejects, key=lambda x: x.get("traded_at") or 0),
                "leg_count": 0,
                "total_in_usd": 0.0,
                "total_out_usd": 0.0,
                "open_at": None,
                "last_at": rejects[-1]["traded_at"] if rejects else None,
                "is_closed": False,
                "has_any_executed": False,
                "is_reject_only": True,
            }

    # 为每组加上 pnl 和当前价
    for pid, g in groups.items():
        pos = positions.get(pid)
        g["position_still_open"] = pos is not None
        g["status_text"] = "已平仓" if g["is_closed"] else ("持仓中" if pos else ("仅拒绝记录" if g.get("is_reject_only") else "已清除"))

        if g["is_closed"]:
            realized = g["total_out_usd"] - g["total_in_usd"]
            g["realized_pnl_usd"] = round(realized, 2)
            g["realized_pnl_pct"] = round((realized / g["total_in_usd"] * 100) if g["total_in_usd"] > 0 else 0, 2)
        elif pos and g["has_any_executed"]:
            # 持仓中：算浮盈 + 若全平预估
            price = await _get_price(pos["symbol"], pos["market"])
            if price and pos.get("quantity") and pos.get("avg_cost"):
                try:
                    fx = await get_rate(db, pos.get("cost_currency") or "USD")
                except Exception:
                    fx = 1.0
                qty = pos["quantity"]; avg = pos["avg_cost"]
                side = pos.get("side") or "long"
                mv = qty * price * fx
                floating = (price - avg) * qty * fx if side == "long" else (avg - price) * qty * fx
                g["current_price"] = price
                g["current_market_value_usd"] = round(mv, 2)
                g["unrealized_pnl_usd"] = round(floating, 2)
                # 空仓：平仓收益 = (2*avg - price)*qty*fx（含归还保证金+盈亏），非市值
                if side == "short":
                    close_proceed = max(0.0, (2 * avg - price) * qty * fx)
                    projected = g["total_out_usd"] + close_proceed - g["total_in_usd"]
                else:
                    projected = g["total_out_usd"] + mv - g["total_in_usd"]
                g["projected_pnl_usd"] = round(projected, 2)
                g["projected_pnl_pct"] = round((projected / g["total_in_usd"] * 100) if g["total_in_usd"] > 0 else 0, 2)

        g["total_in_usd"] = round(g["total_in_usd"], 2)
        g["total_out_usd"] = round(g["total_out_usd"], 2)

    # 输出：按 last_at 降序（最近活动的单排最上面）
    out = sorted(groups.values(), key=lambda x: x.get("last_at") or 0, reverse=True)
    return {"count": len(out), "groups": out}


@auto_trade_router.post("/reset")
async def auto_trade_reset():
    """重置账户（清空所有自动持仓 + 重置现金 + 清日志）。仅用于测试。"""
    if not auto_trader:
        raise HTTPException(status_code=503, detail="AutoTrader 未启动")
    async with db.acquire() as conn:
        # v11.5: 取出要删的 position id，cascade 清 state
        cur = await conn.execute("SELECT id FROM positions WHERE auto_traded=1")
        pids = [r["id"] for r in await cur.fetchall()]
        await conn.execute("DELETE FROM positions WHERE auto_traded=1")
        if pids:
            placeholders = ",".join("?" for _ in pids)
            await conn.execute(f"DELETE FROM position_state WHERE position_id IN ({placeholders})", pids)
        await conn.execute("DELETE FROM auto_trade_log")
        initial = auto_trader.get_config().get("initial_capital_usd", 10000)
        await conn.execute(
            "UPDATE auto_trade_account SET cash_usd=?, updated_at=? WHERE id=1",
            (initial, int(time.time())),
        )
        await conn.commit()
    return {"reset": True, "cash_usd": initial, "cleared_states": len(pids)}


@trading_router.get("/sim-positions")
async def get_sim_positions():
    return await trading_simulator.get_positions()


# v12.3 交易复盘端点
review_router = APIRouter(prefix="/api/trade-review", tags=["交易复盘"])

@review_router.get("/{position_id}")
async def get_review(position_id: str):
    """获取单笔交易的复盘内容。"""
    async with db.acquire() as conn:
        cur = await conn.execute("SELECT * FROM trade_review WHERE position_id=?", (position_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="该单尚未复盘（如已闭环可能在排队中，等待后台 LLM 调度）")
    r = dict(row)
    # 解析 JSON 数组字段
    for k in ("turning_points", "lessons", "pros", "cons"):
        try: r[k] = json.loads(r.get(k) or "[]")
        except Exception: r[k] = []
    # 解析 JSON 对象字段
    for k in ("link_evaluations",):
        try: r[k] = json.loads(r.get(k) or "{}")
        except Exception: r[k] = {}
    return r


@review_router.post("/trigger/{position_id}")
async def trigger_review(position_id: str, force: bool = Query(False)):
    """手动触发某笔单的复盘（force=true 强制重做）。"""
    if trade_reviewer is None:
        raise HTTPException(status_code=503, detail="TradeReviewer 未启动")
    try:
        r = await trade_reviewer.review_position(position_id, force=force)
        if not r:
            return {"ok": False, "msg": "无 close 记录或已存在复盘（force=true 可强制）"}
        return {"ok": True, **r}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@review_router.post("/batch")
async def batch_review(limit: int = Query(20, ge=1, le=100)):
    """批量复盘所有未复盘的闭环单。"""
    if trade_reviewer is None:
        raise HTTPException(status_code=503, detail="TradeReviewer 未启动")
    return await trade_reviewer.batch_review_unreviewed(limit=limit)


@review_router.get("")
async def list_reviews(limit: int = Query(50, ge=1, le=500),
                      pool_id: Optional[str] = Query(None),
                      grade: Optional[str] = Query(None)):
    """列出最近复盘（可按 pool / grade 过滤）。"""
    sql = "SELECT * FROM trade_review WHERE 1=1"
    params: list = []
    if pool_id:
        sql += " AND pool_id=?"; params.append(pool_id)
    if grade:
        sql += " AND grade=?"; params.append(grade.upper())
    sql += " ORDER BY close_at DESC LIMIT ?"
    params.append(limit)
    async with db.acquire() as conn:
        cur = await conn.execute(sql, params)
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        for k in ("turning_points", "lessons", "pros", "cons"):
            try: r[k] = json.loads(r.get(k) or "[]")
            except Exception: r[k] = []
        for k in ("link_evaluations",):
            try: r[k] = json.loads(r.get(k) or "{}")
            except Exception: r[k] = {}
    return {"items": rows}


@review_router.get("/weekly/list")
async def list_weekly_reports(limit: int = Query(8, ge=1, le=52)):
    """列出最近的周报。"""
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT * FROM trade_review_weekly ORDER BY week_start DESC LIMIT ?", (limit,)
        )
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        for k in ("top_wins", "top_losses", "recurring_mistakes", "actionable_changes"):
            try: r[k] = json.loads(r.get(k) or "[]")
            except Exception: r[k] = []
    return {"items": rows}


@review_router.get("/lessons/top")
async def list_top_lessons(pool_id: Optional[str] = Query(None),
                           status: Optional[str] = Query(None, description="active / adopted / disabled / expired / all"),
                           limit: int = Query(50, ge=1, le=200)):
    """v12.5/12.7 列出教训库（默认含全部状态，前端按状态分组显示）。"""
    sql = "SELECT * FROM lesson_pattern WHERE 1=1"
    params: list = []
    if pool_id:
        sql += " AND (pool_id=? OR pool_id='all')"; params.append(pool_id)
    if status and status != 'all':
        sql += " AND status=?"; params.append(status)
    # 排序：adopted > active > expired > disabled，同状态按 occurrences 降序
    sql += (" ORDER BY CASE status "
            "WHEN 'adopted' THEN 0 WHEN 'active' THEN 1 "
            "WHEN 'expired' THEN 2 WHEN 'disabled' THEN 3 ELSE 4 END, "
            "occurrences DESC LIMIT ?")
    params.append(limit)
    async with db.acquire() as conn:
        cur = await conn.execute(sql, params)
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        try: r["sample_position_ids"] = json.loads(r.get("sample_position_ids") or "[]")
        except Exception: r["sample_position_ids"] = []
    return {"items": rows}


@review_router.post("/lessons/aggregate")
async def trigger_aggregate_lessons():
    """手动触发教训聚合。"""
    if trade_reviewer is None:
        raise HTTPException(status_code=503, detail="TradeReviewer 未启动")
    n = await trade_reviewer.aggregate_lessons()
    return {"ok": True, "patterns": n}


@review_router.post("/lessons/merge")
async def trigger_merge_similar_lessons(dry_run: bool = Query(False)):
    """v12.15 手动触发 LLM 语义合并相似教训
    - dry_run=true：仅日志输出"会合并什么"，不写库
    - dry_run=false（默认）：实际合并 + 触发后续自动采纳
    """
    if trade_reviewer is None:
        raise HTTPException(status_code=503, detail="TradeReviewer 未启动")
    result = await trade_reviewer.merge_similar_lessons(dry_run=dry_run)
    return {"ok": True, **result}


@review_router.post("/lessons/auto-adopt")
async def trigger_auto_adopt():
    """v12.15.1 手动触发自动采纳扫描（无需重跑 aggregate）。
    把当前满足条件的 active 教训自动写入 risk_rules。
    """
    if trade_reviewer is None:
        raise HTTPException(status_code=503, detail="TradeReviewer 未启动")
    n = await trade_reviewer._auto_adopt_eligible_lessons()
    return {"ok": True, "auto_adopted": n}


@review_router.post("/lessons/{lesson_id}/status")
async def update_lesson_status(lesson_id: int, body: Dict[str, Any] = Body(...)):
    """v12.7 切换教训状态：adopted / active / disabled。"""
    target = (body.get("status") or "").lower()
    if target not in ("adopted", "active", "disabled"):
        raise HTTPException(status_code=422, detail="status 必须是 adopted / active / disabled")
    from backend.trading.reviewer import set_lesson_status
    ok = await set_lesson_status(db, lesson_id, target)
    if not ok:
        raise HTTPException(status_code=500, detail="状态切换失败")
    return {"ok": True, "lesson_id": lesson_id, "status": target}


@review_router.post("/weekly/generate")
async def trigger_weekly():
    """手动触发本周周报生成。"""
    if trade_reviewer is None:
        raise HTTPException(status_code=503, detail="TradeReviewer 未启动")
    now = time.time()
    t = time.localtime(now)
    this_mon = int(now - (t.tm_wday * 86400 + t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec))
    last_mon = this_mon - 7 * 86400
    r = await trade_reviewer.generate_weekly_report(last_mon)
    if not r:
        return {"ok": False, "msg": "上周无已闭环交易"}
    return {"ok": True, **r}


# ═══ v12.15 教训采纳闭环 — adopt / translate API ═══

@review_router.post("/lessons/{lesson_id}/adopt")
async def adopt_lesson_as_rule(lesson_id: int, body: Dict[str, Any] = Body(default={})):
    """v12.15 把教训采纳为风控规则 (risk_rules 表)。
    body 可选：
      - rule_type: rsi_block / drawdown_force_close / trend_block / cooldown_override
      - params: dict 该 rule_type 的参数
      - 都不传 → 用启发式翻译；翻译失败返回 400 让用户填
    成功后：
      - INSERT risk_rules
      - UPDATE lesson_pattern SET status='adopted', adopted_at=now
    """
    async with db.acquire() as conn:
        cur = await conn.execute("SELECT * FROM lesson_pattern WHERE id=?", (lesson_id,))
        lesson = await cur.fetchone()
    if not lesson:
        raise HTTPException(status_code=404, detail="教训不存在")
    lesson = dict(lesson)
    rule_type = (body.get("rule_type") or "").strip()
    params = body.get("params")
    # 用户没指定 → 启发式翻译
    if not rule_type or not params:
        from backend.trading.reviewer import _heuristic_lesson_to_rule
        guess = _heuristic_lesson_to_rule(lesson.get("full_text", ""), lesson.get("type", ""))
        if not guess:
            raise HTTPException(
                status_code=400,
                detail="无法启发式翻译，请手动指定 rule_type + params；或先调 /translate 拿建议",
            )
        rule_type = guess["rule_type"]
        params = guess["params"]
    # 校验 rule_type 合法
    valid_types = {"rsi_block", "drawdown_force_close", "trend_block",
                   "cooldown_override", "prompt_principle"}
    if rule_type not in valid_types:
        raise HTTPException(status_code=422, detail=f"rule_type 必须 ∈ {sorted(valid_types)}")
    if not isinstance(params, dict):
        raise HTTPException(status_code=422, detail="params 必须是 JSON 对象")
    now = int(time.time())
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                """INSERT INTO risk_rules
                   (rule_type, pool_id, params, source_lesson_id, source_kind,
                    description, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'user_adopted', ?, 1, ?, ?)""",
                (rule_type, lesson.get("pool_id", "all"), json.dumps(params),
                 lesson_id, lesson.get("full_text", "")[:200], now, now)
            )
            rule_id = cur.lastrowid
            await conn.execute(
                "UPDATE lesson_pattern SET status='adopted', adopted_at=?, last_updated=? WHERE id=?",
                (now, now, lesson_id)
            )
            await conn.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"采纳失败: {e}")
    return {"ok": True, "rule_id": rule_id, "rule_type": rule_type, "params": params}


@review_router.post("/lessons/{lesson_id}/translate")
async def translate_lesson_to_rule(lesson_id: int):
    """v12.15 用启发式（无需 LLM）翻译教训为规则建议；UI 用此预填表单。
    返回 {rule_type, params, confidence: 'heuristic'} 或 404 / 400。
    """
    async with db.acquire() as conn:
        cur = await conn.execute("SELECT full_text, type, pool_id FROM lesson_pattern WHERE id=?", (lesson_id,))
        lesson = await cur.fetchone()
    if not lesson:
        raise HTTPException(status_code=404, detail="教训不存在")
    lesson = dict(lesson)
    from backend.trading.reviewer import _heuristic_lesson_to_rule
    guess = _heuristic_lesson_to_rule(lesson.get("full_text", ""), lesson.get("type", ""))
    if not guess:
        return {
            "ok": False,
            "msg": "教训文本无法启发式翻译，建议保留为 LLM prompt 软规则（不可参数化）",
            "lesson_text": lesson.get("full_text", ""),
        }
    return {
        "ok": True,
        "rule_type": guess["rule_type"],
        "params": guess["params"],
        "confidence": "heuristic",
        "lesson_text": lesson.get("full_text", ""),
        "pool_id": lesson.get("pool_id", "all"),
    }


app.include_router(review_router)


# ═══ v12.15 风控规则 CRUD API ═══

risk_rules_router = APIRouter(prefix="/api/risk-rules", tags=["风控规则"])


@risk_rules_router.get("")
async def list_risk_rules(
    enabled: Optional[bool] = Query(None),
    rule_type: Optional[str] = Query(None),
    pool_id: Optional[str] = Query(None),
):
    """列出所有风控规则（可按 enabled / rule_type / pool_id 过滤）。"""
    sql = "SELECT * FROM risk_rules WHERE 1=1"
    params: list = []
    if enabled is not None:
        sql += " AND enabled=?"; params.append(1 if enabled else 0)
    if rule_type:
        sql += " AND rule_type=?"; params.append(rule_type)
    if pool_id:
        sql += " AND pool_id=?"; params.append(pool_id)
    sql += " ORDER BY enabled DESC, created_at DESC"
    async with db.acquire() as conn:
        cur = await conn.execute(sql, params)
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        try:
            r["params"] = json.loads(r.get("params") or "{}")
        except Exception:
            r["params"] = {}
        # false_reject_rate 计算
        h = r.get("hits") or 0
        fr = r.get("false_reject_count") or 0
        r["false_reject_rate"] = round(fr / h * 100, 1) if h > 0 else 0
    return {"count": len(rows), "items": rows}


@risk_rules_router.post("/{rule_id}/toggle")
async def toggle_rule(rule_id: int, body: Dict[str, Any] = Body(...)):
    """启用/禁用风控规则。"""
    enabled = bool(body.get("enabled"))
    now = int(time.time())
    async with db.acquire() as conn:
        cur = await conn.execute(
            "UPDATE risk_rules SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, now, rule_id),
        )
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="规则不存在")
    return {"ok": True, "rule_id": rule_id, "enabled": enabled}


@risk_rules_router.put("/{rule_id}")
async def update_rule(rule_id: int, body: Dict[str, Any] = Body(...)):
    """更新规则参数（仅 params + description + enabled 可改；rule_type / pool_id 不能改）。"""
    fields = []
    params: list = []
    if "params" in body:
        if not isinstance(body["params"], dict):
            raise HTTPException(status_code=422, detail="params 必须是 JSON 对象")
        fields.append("params=?")
        params.append(json.dumps(body["params"]))
    if "description" in body:
        fields.append("description=?")
        params.append(str(body["description"])[:300])
    if "enabled" in body:
        fields.append("enabled=?")
        params.append(1 if body["enabled"] else 0)
    if not fields:
        raise HTTPException(status_code=422, detail="无可更新字段")
    fields.append("updated_at=?"); params.append(int(time.time()))
    params.append(rule_id)
    async with db.acquire() as conn:
        cur = await conn.execute(
            f"UPDATE risk_rules SET {', '.join(fields)} WHERE id=?", params
        )
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="规则不存在")
    return {"ok": True, "rule_id": rule_id}


@risk_rules_router.delete("/{rule_id}")
async def delete_rule(rule_id: int):
    """删除规则（软删除：把 enabled 改 0；如要硬删除走 admin 工具）。"""
    now = int(time.time())
    async with db.acquire() as conn:
        cur = await conn.execute(
            "UPDATE risk_rules SET enabled=0, updated_at=? WHERE id=?",
            (now, rule_id),
        )
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="规则不存在")
    return {"ok": True, "rule_id": rule_id, "soft_deleted": True}


@risk_rules_router.get("/{rule_id}/hits")
async def list_rule_hits(rule_id: int, limit: int = Query(50, ge=1, le=500)):
    """规则命中明细（用于审计 false_reject）。"""
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT * FROM risk_rule_hits WHERE rule_id=? ORDER BY hit_at DESC LIMIT ?",
            (rule_id, limit),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    return {"count": len(rows), "items": rows}


app.include_router(risk_rules_router)


# 警报端点（v11.5: 补 POST/DELETE/PUT，前端原 alerts.js / watchpool.js / signals.js 创建警报）
alerts_router = APIRouter(prefix="/api/alerts", tags=["警报"])

@alerts_router.get("")
async def list_alerts(enabled_only: bool = Query(False)):
    try:
        rows = await db.get_alerts(enabled_only=enabled_only)
        return {"alerts": rows}
    except Exception as e:
        return {"alerts": [], "error": str(e)}


VALID_CONDITION_TYPES = {
    "price_above", "price_below", "price_crossing_up", "price_crossing_down",
    "rsi_overbought", "rsi_oversold", "macd_cross_up", "macd_cross_down",
    "volume_surge",
}

def _normalize_alert_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """v11.6 兼容前端旧 alerts.js 字段：condition→condition_type，price→condition.price"""
    if not body: return {}
    if "condition_type" not in body and "condition" in body and isinstance(body["condition"], str):
        # 兼容旧前端：condition 是字符串如 "crossing_up" / "greater_than"
        legacy_map = {
            "crossing": "price_crossing_up", "crossing_up": "price_crossing_up",
            "crossing_down": "price_crossing_down",
            "greater_than": "price_above", "less_than": "price_below",
            "above": "price_above", "below": "price_below",
        }
        body["condition_type"] = legacy_map.get(body["condition"], body["condition"])
        body["condition"] = {}
    # 兼容旧前端的 price 顶级字段
    if "price" in body and "condition" not in body:
        body["condition"] = {"price": body.pop("price")}
    elif "price" in body and isinstance(body.get("condition"), dict):
        body["condition"].setdefault("price", body.pop("price"))
    # 兼容旧前端 repeat → repeat_mode
    if "repeat" in body and "repeat_mode" not in body:
        body["repeat_mode"] = body.pop("repeat")
    return body


@alerts_router.post("")
async def create_alert_endpoint(body: Dict[str, Any] = Body(...)):
    """
    创建警报。Body 示例：
      {"symbol":"NVDA","market":"us","condition_type":"price_above",
       "condition":{"price":150},"message":"NVDA 突破 150",
       "notify_methods":["browser","sound"],"repeat_mode":"once","cooldown":300}
    """
    body = _normalize_alert_body(body or {})
    if not body.get("symbol"):
        raise HTTPException(status_code=422, detail="symbol 必填")
    if not body.get("market"):
        # 容错：从 symbol 推断 market（兼容旧前端不传 market）
        from backend.news.scheduler import _infer_stock_market
        body["market"] = _infer_stock_market(body["symbol"]) or "us"
    if not body.get("condition_type"):
        raise HTTPException(status_code=422, detail="condition_type 必填（如 price_above / price_below）")
    if body["condition_type"] not in VALID_CONDITION_TYPES:
        raise HTTPException(status_code=422,
            detail=f"condition_type 必须是 {sorted(VALID_CONDITION_TYPES)} 之一")
    try:
        if alert_manager is not None:
            alert_id = await alert_manager.add_alert(body)
        else:
            alert_id = await db.create_alert(body)
        return {"id": alert_id, "ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建警报失败: {e}")


@alerts_router.put("/{alert_id}")
async def update_alert_endpoint(alert_id: str, body: Dict[str, Any] = Body(...)):
    """更新警报字段（如 enabled / message / condition）。"""
    body = _normalize_alert_body(body or {})
    if not body:
        raise HTTPException(status_code=422, detail="body 不能为空")
    if "condition_type" in body and body["condition_type"] not in VALID_CONDITION_TYPES:
        raise HTTPException(status_code=422,
            detail=f"condition_type 必须是 {sorted(VALID_CONDITION_TYPES)} 之一")
    try:
        if alert_manager is not None:
            await alert_manager.update_alert(alert_id, body)
        else:
            await db.update_alert(alert_id, body)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新警报失败: {e}")


@alerts_router.delete("/{alert_id}")
async def delete_alert_endpoint(alert_id: str):
    """删除警报。"""
    try:
        # 先检查存在性
        existing = await db.get_alert_by_id(alert_id) if hasattr(db, "get_alert_by_id") else None
        if existing is None:
            # 兜底：直接走 DB delete，rowcount 判断
            async with db.acquire() as conn:
                cur = await conn.execute("SELECT 1 FROM alerts WHERE id=?", (alert_id,))
                if not await cur.fetchone():
                    raise HTTPException(status_code=404, detail="alert not found")
        if alert_manager is not None:
            await alert_manager.delete_alert(alert_id)
        else:
            await db.delete_alert(alert_id)
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除警报失败: {e}")


# v11.5 dashboard calendar 兜底（之前 dashboard.js 调但后端无路由 → 经济日历卡死）
@app.get("/api/dashboard/calendar")
async def dashboard_calendar():
    """
    经济日历占位实现 — 返回固定的"未来 7 天" 主要事件骨架。
    后续可接 ForexFactory / TradingEconomics 真实源。
    """
    import datetime as _dt
    today = _dt.date.today()
    items = []
    # 静态周历事件（FOMC 等大概率发生在月中）
    samples = [
        {"event": "美国 PMI", "country": "US", "importance": 2, "delta_days": 1},
        {"event": "美国 CPI（月度）", "country": "US", "importance": 3, "delta_days": 3},
        {"event": "FOMC 利率决议", "country": "US", "importance": 3, "delta_days": 5},
        {"event": "中国 PMI", "country": "CN", "importance": 2, "delta_days": 2},
        {"event": "BTC 期权大额到期", "country": "Crypto", "importance": 2, "delta_days": 4},
    ]
    for s in samples:
        d = today + _dt.timedelta(days=s["delta_days"])
        items.append({
            "date": d.isoformat(),
            "country": s["country"],
            "event": s["event"],
            "importance": s["importance"],
        })
    return {"items": items, "note": "静态骨架数据，可接 TradingEconomics / Investing 实时源"}


# v11.5 backtest 占位端点 — 真正回测引擎待完善，先返 501 让前端有正确反馈而非 404
backtest_router = APIRouter(prefix="/api/backtest", tags=["回测"])

@backtest_router.post("/run")
async def backtest_run(body: Dict[str, Any] = Body(default={})):
    """
    回测占位：暂不实现完整引擎，返回 501 + 友好消息让前端 toast 提示。
    后续接入 vectorbt / 自研引擎。
    """
    raise HTTPException(
        status_code=501,
        detail="回测引擎尚未实现 — 当前版本聚焦实时信号 + 自动交易；如需历史回测请联系开发"
    )

@backtest_router.post("/stop")
async def backtest_stop():
    raise HTTPException(status_code=501, detail="回测引擎未启用")

@backtest_router.get("/list")
async def backtest_list():
    """列出已存的回测报告（取自 backtest_reports 表）。"""
    try:
        async with db.acquire() as conn:
            cur = await conn.execute("SELECT * FROM backtest_reports ORDER BY created_at DESC LIMIT 50")
            rows = [dict(r) for r in await cur.fetchall()]
        return {"reports": rows}
    except Exception:
        return {"reports": []}

app.include_router(backtest_router)


# 注册路由（必须在 StaticFiles 挂载之前）
app.include_router(alerts_router)
app.include_router(market_router)
app.include_router(indicator_router)
app.include_router(watchlist_router)
app.include_router(settings_router)
app.include_router(news_router)
app.include_router(pool_router)
app.include_router(signal_router)
app.include_router(strategy_router)
app.include_router(position_router)
app.include_router(dashboard_router)
app.include_router(crypto_router)
app.include_router(trading_router)
app.include_router(auto_trade_router)


# ═══════════════════════════════════════════════════════════════════
# 路由：缠论分析 + 艾略特波浪
# ═══════════════════════════════════════════════════════════════════

chanlun_router = APIRouter(prefix="/api/chanlun", tags=["缠论"])


@chanlun_router.post("/from-data")
async def chanlun_from_data(req: Dict[str, Any] = Body(...)):
    """前端传 K 线数据做缠论分析 → 返回 bi_list/seg_list/zs_list/bsp_list。"""
    candles = req.get("candles", [])
    if not candles or len(candles) < 30:
        return {"bi_list": [], "seg_list": [], "zs_list": [], "bsp_list": []}
    try:
        import sys as _sys, os as _os
        _engine_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "chanlun_engine")
        if _engine_dir not in _sys.path:
            _sys.path.insert(0, _engine_dir)
        from backend.chanlun_engine.chanlun_service import analyze
        return analyze(candles)
    except Exception as e:
        logger.error(f"[chanlun] 分析失败: {e}", exc_info=True)
        return {"bi_list": [], "seg_list": [], "zs_list": [], "bsp_list": [], "error": str(e)}


@chanlun_router.post("/elliott-wave/from-data")
async def elliott_wave_from_data(req: Dict[str, Any] = Body(...)):
    """前端传 K 线数据做艾略特波浪分析。"""
    candles = req.get("candles", [])
    bar_offset = int(req.get("bar_offset", 0))
    if not candles or len(candles) < 30:
        return {"patterns": [], "predictions": []}
    try:
        from backend.elliott_wave.service import analyze
        return analyze(candles, bar_offset=bar_offset)
    except Exception as e:
        logger.error(f"[elliott] 分析失败: {e}", exc_info=True)
        return {"patterns": [], "predictions": [], "error": str(e)}


app.include_router(chanlun_router)


# ═══════════════════════════════════════════════════════════════════
# 健康检查 + 静态文件
# ═══════════════════════════════════════════════════════════════════


@app.get("/api/health")
async def health():
    """简单的健康检查端点，用于监控和一键启动验证。"""
    return {
        "status": "ok",
        "version": "3.0.0",
        "phase": "1",
        "time": int(time.time()),
    }


# 前端静态文件挂载 (必须在所有 API 路由之后！)
# PRD/TDD §12 约束 #12：mount("/") 必须在所有 router 注册之后
frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")

# 前端资源版本号：取 frontend/js + frontend/css 下所有文件 mtime 最大值
# 改任何前端文件版本号立即变 —— 不需要重启后端
_BOOT_TIME = str(int(time.time()))


def _compute_asset_version() -> str:
    latest = 0
    try:
        for root_rel in ("js", "css"):
            root = os.path.join(frontend_dir, root_rel)
            if not os.path.isdir(root):
                continue
            for dp, _dn, fn in os.walk(root):
                for f in fn:
                    try:
                        m = int(os.path.getmtime(os.path.join(dp, f)))
                        if m > latest:
                            latest = m
                    except Exception:
                        pass
    except Exception:
        pass
    return str(latest) if latest else _BOOT_TIME


logger.info(f"[frontend] 资源版本号起始={_compute_asset_version()}（实际每次请求按前端文件 mtime 重算，改文件即生效）")


@app.get("/mobile", include_in_schema=False)
@app.get("/mobile/", include_in_schema=False)
async def serve_mobile_index():
    """v12.13 移动端 PWA 入口（iPhone 添加主屏幕用）。"""
    from fastapi.responses import FileResponse, HTMLResponse
    p = os.path.join(frontend_dir, "mobile", "index.html")
    if not os.path.isfile(p):
        return HTMLResponse("Mobile UI not found", status_code=404)
    return FileResponse(p, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def serve_index_with_version():
    """返回 index.html 时把 ?v=xxx 动态替换成当前前端资源版本号。"""
    from fastapi.responses import HTMLResponse, FileResponse
    import re
    index_path = os.path.join(frontend_dir, "index.html")
    if not os.path.isfile(index_path):
        raise HTTPException(status_code=404, detail="index.html not found")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()
        BOOT_ID = _compute_asset_version()
        content = re.sub(r"\?v=[a-zA-Z0-9_]+", f"?v={BOOT_ID}", content)
        return HTMLResponse(
            content,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    except Exception as e:
        logger.warning(f"serve_index_with_version 失败: {e}")
        return FileResponse(index_path)


if os.path.isdir(frontend_dir):
    # 自定义 StaticFiles 子类：所有 JS/CSS 响应加 no-store，防浏览器缓存老版本
    class NoCacheStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            try:
                if path.endswith(('.js', '.css', '.html')):
                    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
                    resp.headers['Pragma'] = 'no-cache'
                    resp.headers['Expires'] = '0'
            except Exception:
                pass
            return resp
    # html=False：不让 StaticFiles 接管 "/"（已由上面 handler 接管）
    app.mount("/", NoCacheStaticFiles(directory=frontend_dir, html=False), name="frontend")
else:
    logger.warning(f"Frontend directory not found: {frontend_dir}")
