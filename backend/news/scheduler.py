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


def _infer_stock_market(symbol: str) -> str:
    """
    根据 symbol 推断市场。
    返回 'us' / 'hk' / 'cn' / 'crypto' / 'unknown'
    候选池只接受 us/hk/cn（加密 6 币种走专属监控通道）。
    """
    if not symbol:
        return "unknown"
    s = symbol.upper()
    # 加密 USDT 对
    if s.endswith("-USDT") or s.endswith("-USD") or s.endswith("-USDC"):
        return "crypto"
    # 港股: 如 0700.HK / 9988.HK
    if s.endswith(".HK"):
        return "hk"
    # A 股: 6 位纯数字 (主板/科创板/创业板)
    if s.isdigit() and len(s) == 6:
        return "cn"
    # 美股: 1-5 位纯字母
    if s.isalpha() and 1 <= len(s) <= 5:
        return "us"
    return "unknown"


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
        pool_added_count = 0
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
                    # PRD F6.1: ★★★+ 新闻涉及的股票自动推入候选池（加密 6 币种跳过）
                    if score_result["importance"] >= 3:
                        added = await self._auto_add_to_pool(news_record, score_result)
                        pool_added_count += added
            except Exception as e:
                logger.exception(f"[{collector.name}] 处理单条新闻异常: {e}")

        if saved_count > 0 or pool_added_count > 0:
            logger.info(
                f"[{collector.name}] 入库 {saved_count} 条新闻 (共 {len(new_items)} 条新)"
                + (f", 新闻驱动入池 {pool_added_count} 只" if pool_added_count else "")
            )

    async def _auto_add_to_pool(
        self, news: Dict[str, Any], score_result: Dict[str, Any]
    ) -> int:
        """
        PRD F6.1 新闻事件驱动入池：
        ★★★+ 新闻涉及的股票自动推入候选池。加密 6 币种跳过。
        返回新增入池数量。
        """
        cats = score_result.get("categories") or []
        if not cats:
            return 0
        importance = score_result.get("importance", 0)
        # 评分公式：基础 50 + (importance - 2) × 8 = 58 (★★★) / 66 (★★★★) / 74 (★★★★★)
        score = 50 + (importance - 2) * 8
        added = 0
        for sym in cats:
            market = _infer_stock_market(sym)
            if market not in ("us", "hk", "cn"):
                continue  # 加密币种跳过 (DB CHECK 也会拒绝)
            try:
                pool_id = await self.db.add_to_pool(
                    symbol=sym,
                    market=market,
                    source="news",
                    score=score,
                    reason=f"新闻 ★{importance}: {news.get('title', '')[:80]}",
                )
                # WebSocket 推送 pool_update
                await self.ws_hub.broadcast_pool_update(
                    "added",
                    {
                        "id": pool_id,
                        "symbol": sym,
                        "market": market,
                        "source": "news",
                        "score": score,
                        "reason": f"新闻 ★{importance}",
                    },
                )
                added += 1
            except ValueError:
                # market CHECK 拒绝（理论上 _infer_stock_market 已过滤）
                pass
            except Exception as e:
                logger.debug(f"自动入池 {sym}/{market} 失败: {e}")
        return added

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
