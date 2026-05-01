"""
持仓监控器（Phase 5）。

定时遍历所有持仓，调用 PositionAdvisor 评估，
触发建议时入库 + WebSocket 推送。

去重：同 position 同 advice 1 小时内不重复推送。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

from backend.data.cache import cached_get_klines
from backend.data.models import Interval, Market
from backend.portfolio.advisor import PositionAdvisor
from backend.portfolio.manager import PortfolioManager

logger = logging.getLogger(__name__)


class PortfolioTracker:
    def __init__(self, db, ws_hub, news_provider=None):
        self.db = db
        self.ws_hub = ws_hub
        self.manager = PortfolioManager(db)
        self.advisor = PositionAdvisor()
        self.news_provider = news_provider or (lambda symbol: [])
        # 去重: { (position_id, advice): last_emit_ts_sec }
        self._dedupe: Dict[tuple, int] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self, check_interval_sec: int = 300):
        """每 5 分钟检查所有持仓。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(check_interval_sec))
        logger.info("PortfolioTracker started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("PortfolioTracker stopped")

    async def _loop(self, interval_sec: int):
        while self._running:
            try:
                await self.check_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"持仓监控异常: {e}")
            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                break

    async def check_all(self):
        """遍历所有持仓，评估建议。"""
        positions = await self.manager.get_all()
        for pos in positions:
            try:
                await self._check_one(pos)
            except Exception as e:
                logger.exception(f"持仓 {pos['symbol']} 检查异常: {e}")

    async def _check_one(self, position: Dict):
        symbol = position["symbol"]
        market_str = position["market"]
        try:
            market = Market(market_str)
        except ValueError:
            return

        # 拉 K 线
        try:
            candles = await cached_get_klines(
                db=self.db, market=market, symbol=symbol,
                interval=Interval.H1, limit=200,
            )
        except Exception:
            return
        if not candles:
            return

        # 拉新闻（v11.6: 支持 sync 或 async news_provider）
        import inspect
        recent_news = []
        try:
            res = self.news_provider(symbol)
            if inspect.isawaitable(res):
                res = await res
            recent_news = res or []
        except Exception:
            recent_news = []

        advice = self.advisor.evaluate(position, candles, recent_news)
        if not advice:
            return

        # 去重: 同 position 同 advice 1 小时内不重复
        key = (position["id"], advice["advice"])
        last_ts = self._dedupe.get(key, 0)
        now = int(time.time())
        if now - last_ts < 3600:
            return
        self._dedupe[key] = now

        # 入库
        async with self.db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO position_advices (position_id, symbol, advice, reason, triggered_by, advised_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    position["id"],
                    symbol,
                    advice["advice"],
                    advice["reason"],
                    json.dumps({"urgency": advice.get("urgency"), "pnl_pct": advice.get("pnl_pct")}),
                    now,
                ),
            )
            await conn.commit()

        # WebSocket 推送
        try:
            await self.ws_hub.broadcast_position_advice({
                "position_id": position["id"],
                "symbol": symbol,
                "market": market_str,
                "advice": advice["advice"],
                "reason": advice["reason"],
                "urgency": advice.get("urgency", "medium"),
                "pnl_pct": advice.get("pnl_pct"),
                "current_price": advice.get("current_price"),
                "advised_at": now,
            })
        except Exception as e:
            logger.warning(f"持仓建议推送失败: {e}")

        logger.info(
            f"💼 持仓建议 {symbol}: {advice['advice']} (urgency={advice.get('urgency')}, "
            f"pnl={advice.get('pnl_pct')}%) - {advice['reason'][:60]}"
        )
