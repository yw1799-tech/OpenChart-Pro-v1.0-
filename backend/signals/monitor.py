"""
监控引擎（PRD F7 / TDD §6.5.3）。

加密 6 币种：系统启动时自动绑定全部内置策略，每根 K 线收盘时检查（实装时连 OKX WebSocket 即可）。
股票候选池：候选池 monitoring 状态的品种，按其绑定的策略评估。

信号去重：同品种同方向 SIGNAL_DEDUP_WINDOW (默认 10s) 内不重复触发。
信号阈值：confidence < SIGNAL_MIN_CONFIDENCE (默认 60) 不触发。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import backend.config as config
from backend.data.cache import cached_get_klines
from backend.data.models import Interval, Market, Signal
from backend.signals.binding import StrategyBindingManager
from backend.signals.strategies import ALL_STRATEGIES, Strategy, get_strategy

logger = logging.getLogger(__name__)


class MonitorEngine:
    """
    策略信号监控引擎。

    工作模式：
    - 加密 6 币种：定时（每个周期）拉一次 K 线，跑全部策略
    - 股票候选池：定时拉 K 线，跑各自绑定的策略
    """

    def __init__(self, db, ws_hub, news_provider=None):
        self.db = db
        self.ws_hub = ws_hub
        self.news_provider = news_provider or (lambda symbol: [])
        self.bindings = StrategyBindingManager(db)
        # 信号去重缓存: { (symbol, action): last_emit_ts_ms }
        self._dedupe: Dict[tuple, int] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ───────── 启动时初始化加密 6 币种绑定 ─────────

    async def ensure_crypto_bindings(self):
        """系统启动时为加密 6 币种自动绑定全部内置策略（PRD F7.2）。"""
        for symbol in config.CRYPTO_SYMBOLS:
            for strategy_name in ALL_STRATEGIES.keys():
                try:
                    await self.bindings.bind(
                        symbol=symbol,
                        market="crypto",
                        strategy_name=strategy_name,
                        enabled=True,
                    )
                except Exception as e:
                    logger.debug(f"绑定 {symbol}/{strategy_name} 失败: {e}")
        logger.info(
            f"加密 6 币种自动绑定完成: {len(config.CRYPTO_SYMBOLS)} 币种 × {len(ALL_STRATEGIES)} 策略"
        )

    # ───────── 后台监控循环 ─────────

    def start(self, check_interval_sec: int = 60):
        """启动后台监控（默认每 60 秒检查一次所有绑定）。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(check_interval_sec))
        logger.info("MonitorEngine started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MonitorEngine stopped")

    async def _monitor_loop(self, interval_sec: int):
        while self._running:
            try:
                await self.check_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"监控循环异常: {e}")
            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                break

    # ───────── 主检查逻辑 ─────────

    async def check_all(self):
        """遍历所有启用绑定，按 (symbol,market) 分组运行策略。"""
        bindings = await self.bindings.get_bindings(enabled_only=True)
        if not bindings:
            return

        # 按 (symbol, market) 聚合
        by_symbol: Dict[tuple, List[Dict]] = {}
        for b in bindings:
            key = (b["symbol"], b["market"])
            by_symbol.setdefault(key, []).append(b)

        # 并发评估每个品种（每个品种内部串行跑各策略）
        await asyncio.gather(
            *[self._evaluate_symbol(sym, mkt, bs) for (sym, mkt), bs in by_symbol.items()],
            return_exceptions=True,
        )

    async def _evaluate_symbol(self, symbol: str, market_str: str, bindings: List[Dict]):
        """对单个品种跑所有绑定的策略。"""
        try:
            market = Market(market_str)
        except ValueError:
            return

        # 取该品种最新 K 线（默认 1H 周期，足够通用策略用）
        try:
            candles = await cached_get_klines(
                db=self.db, market=market, symbol=symbol, interval=Interval.H1, limit=300
            )
        except Exception as e:
            logger.debug(f"拉取 K 线失败 {symbol}: {e}")
            return
        if not candles or len(candles) < 30:
            return

        # 取该品种最近新闻（供 FlashEventStrategy 和通用 modifier 使用）
        recent_news = []
        try:
            recent_news = self.news_provider(symbol) or []
        except Exception:
            pass

        for binding in bindings:
            try:
                strat_name = binding["strategy_name"]
                params = binding.get("params") or {}
                strategy = get_strategy(strat_name, **params)
                if not strategy:
                    continue
                signal = strategy.evaluate(symbol, market, candles, recent_news)
                if not signal:
                    continue
                # 阈值过滤
                if signal.confidence < config.SIGNAL_MIN_CONFIDENCE:
                    continue
                # 去重过滤
                key = (signal.symbol, signal.action)
                last = self._dedupe.get(key, 0)
                now_ms = int(time.time() * 1000)
                if now_ms - last < config.SIGNAL_DEDUP_WINDOW * 1000:
                    continue
                self._dedupe[key] = now_ms
                # 入库 + 推送
                await self._save_and_broadcast(signal)
            except Exception as e:
                logger.exception(f"策略 {strat_name} 评估异常: {e}")

    async def _save_and_broadcast(self, signal: Signal):
        """保存信号到 DB + WebSocket 推送。"""
        async with self.db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO signals (
                    id, symbol, market, action, strategy_name, confidence,
                    price, suggested_qty, stop_loss, take_profit,
                    reason, triggered_by, status, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    signal.id,
                    signal.symbol,
                    signal.market.value,
                    signal.action,
                    signal.strategy_name,
                    signal.confidence,
                    signal.price,
                    signal.suggested_qty,
                    signal.stop_loss,
                    signal.take_profit,
                    signal.reason,
                    json.dumps(signal.triggered_by or {}),
                    signal.generated_at,
                ),
            )
            await conn.commit()

        # WebSocket 推送
        try:
            await self.ws_hub.broadcast_signal({
                "id": signal.id,
                "symbol": signal.symbol,
                "market": signal.market.value,
                "action": signal.action,
                "strategy_name": signal.strategy_name,
                "confidence": signal.confidence,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "reason": signal.reason,
                "generated_at": signal.generated_at,
            })
        except Exception as e:
            logger.warning(f"信号推送失败: {e}")

        logger.info(
            f"📡 信号触发 {signal.symbol} {signal.action} "
            f"@{signal.price:.4f} confidence={signal.confidence} "
            f"strategy={signal.strategy_name}"
        )
