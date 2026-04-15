"""
新闻采集调度器（PRD F4.1 / TDD §1.3 后台任务）。

随 FastAPI 启动，按每个源 config['interval'] 定时拉取，
拉到的新闻经规则引擎评分后入库 + WebSocket 推送给前端。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from backend.news.collector import NewsCollector, create_collector
from backend.news.dedup import url_hash
from backend.news.rule_engine import score_news
from backend.news.sources import get_enabled_sources

logger = logging.getLogger(__name__)


class NewsScheduler:
    """
    后台采集调度器。

    每个启用的源开一个独立 asyncio 任务，按其 interval 循环拉取。
    新拉到的新闻：
      1. URL hash 去重
      2. content_hash 去重（DB 查询）
      3. 规则引擎评分
      4. importance >= 1 才入库
      5. importance >= 3 通过 WebSocket 推送前端
    """

    def __init__(self, db, ws_hub, holding_provider=None, pool_provider=None):
        """
        db:          DatabaseManager 实例
        ws_hub:      WebSocketHub 实例
        holding_provider: 可选 callable() -> Set[str] 持仓品种
        pool_provider:    可选 callable() -> Set[str] 候选池品种
        """
        self.db = db
        self.ws_hub = ws_hub
        self.holding_provider = holding_provider or (lambda: set())
        self.pool_provider = pool_provider or (lambda: set())
        self._tasks: List[asyncio.Task] = []
        self._collectors: Dict[str, NewsCollector] = {}
        self._running = False
        # 内存级 URL 去重集（每个源最多保留最近 500 个 URL hash，简单 LRU）
        self._url_seen: Dict[str, List[str]] = {}

    def start(self):
        """启动所有启用源的采集循环。"""
        if self._running:
            logger.warning("NewsScheduler 已在运行")
            return

        sources = get_enabled_sources()
        logger.info(f"NewsScheduler 启动，启用源: {len(sources)} 个")
        for src in sources:
            collector = create_collector(src)
            self._collectors[src["name"]] = collector
            task = asyncio.create_task(self._run_loop(collector))
            self._tasks.append(task)
        self._running = True

    async def stop(self):
        """停止所有采集任务。"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for collector in self._collectors.values():
            await collector.close()
        self._tasks.clear()
        self._collectors.clear()
        logger.info("NewsScheduler 已停止")

    def get_health(self) -> List[Dict[str, Any]]:
        """各源健康度快照（供 /api/news/sources 端点）。"""
        return [c.get_stats() for c in self._collectors.values()]

    async def _run_loop(self, collector: NewsCollector):
        """单个 collector 的采集循环。"""
        while self._running:
            try:
                await self._fetch_once(collector)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[{collector.name}] 采集循环异常: {e}")
            # 等到下次拉取
            try:
                await asyncio.sleep(collector.interval)
            except asyncio.CancelledError:
                break

    async def _fetch_once(self, collector: NewsCollector):
        """单次采集：拉取 → 去重 → 规则评分 → 入库 → 推送。"""
        items = await collector.fetch()
        if not items:
            return

        # URL hash 去重（内存级）
        seen = self._url_seen.setdefault(collector.name, [])
        seen_set = set(seen)
        new_items = []
        for it in items:
            uh = url_hash(it.get("url", "") or it["id"])
            if uh in seen_set:
                continue
            new_items.append((it, uh))
            seen.append(uh)
        # 简单 LRU：限制大小
        if len(seen) > 500:
            self._url_seen[collector.name] = seen[-500:]

        if not new_items:
            return

        # 规则引擎评分 + 入库
        try:
            holdings = self.holding_provider()
            pool_syms = self.pool_provider()
        except Exception:
            holdings = set()
            pool_syms = set()

        saved_count = 0
        for raw_news, _ in new_items:
            try:
                # 内容 hash 去重（DB 查）
                if raw_news.get("content_hash"):
                    if await self.db.is_news_duplicate(raw_news["content_hash"]):
                        continue

                # 规则引擎评分
                score_result = score_news(raw_news, holdings, pool_syms)
                # importance=0 直接丢弃
                if score_result["importance"] == 0:
                    continue

                # 合并字段
                news_record = {**raw_news, **score_result}

                inserted = await self.db.save_flash_news(news_record)
                if inserted:
                    saved_count += 1
                    # 高分（★★★+）实时推送给前端
                    if score_result["importance"] >= 3:
                        await self._broadcast_news(news_record)
            except Exception as e:
                logger.exception(f"[{collector.name}] 处理单条新闻异常: {e}")

        if saved_count > 0:
            logger.info(f"[{collector.name}] 入库 {saved_count} 条新闻 (共 {len(new_items)} 条新)")

    async def _broadcast_news(self, news: Dict[str, Any]):
        """通过 WebSocket Hub 推送高价值新闻到前端。"""
        try:
            payload = {
                "type": "flash_news",
                "data": {
                    "id": news["id"],
                    "title": news["title"],
                    "source": news["source"],
                    "importance": news["importance"],
                    "sentiment": news.get("sentiment", "neutral"),
                    "categories": news.get("categories", []),
                    "url": news.get("url", ""),
                    "published_at": news["published_at"],
                    "impact_on_crypto": news.get("impact_on_crypto"),
                },
            }
            # WebSocketHub 没有 broadcast_news 方法时降级到 broadcast_alert
            broadcast = getattr(self.ws_hub, "broadcast_news", None)
            if broadcast:
                await broadcast(payload)
            else:
                # 最简单的兜底：用现有 broadcast_alert API（前端要做适配）
                logger.debug(f"WebSocketHub 缺少 broadcast_news，跳过实时推送: {news['id']}")
        except Exception as e:
            logger.warning(f"推送新闻失败: {e}")
