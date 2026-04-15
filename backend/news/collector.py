"""
新闻采集器（PRD F4 / TDD §6.3.1）。

三种适配器：
  - RSSCollector:     通用 RSS feed
  - RESTCollector:    REST API 接口
  - ScraperCollector: HTML 爬取兜底（Phase 3B 启用）

每个 collector 实现 fetch() → 返回 List[RawNews]
RawNews 是 dict: { id, title, content, source, url, published_at }

由 scheduler.py 按 source.interval 定时调用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from backend.news.dedup import content_hash as compute_content_hash
from backend.news.dedup import make_news_id, url_hash

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 基类
# ═══════════════════════════════════════════════════════════════════


class NewsCollector(ABC):
    """采集器基类。"""

    def __init__(self, source_config: Dict[str, Any]):
        self.config = source_config
        self.name = source_config["name"]
        self.market = source_config.get("market", "global")
        self.url = source_config["url"]
        self.interval = source_config.get("interval", 300)
        self._session: Optional[aiohttp.ClientSession] = None
        self._stats = {
            "total_fetches": 0,
            "successful_fetches": 0,
            "consecutive_failures": 0,
            "last_success_at": 0,
            "last_error": None,
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                    ),
                },
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @abstractmethod
    async def fetch(self) -> List[Dict[str, Any]]:
        """返回原始新闻列表（已规范化为通用 dict 格式）。"""
        ...

    def get_stats(self) -> Dict[str, Any]:
        return {**self._stats, "name": self.name, "market": self.market}

    def _normalize(
        self,
        title: str,
        content: str = "",
        url: str = "",
        published_at: Optional[int] = None,
    ) -> Dict[str, Any]:
        """生成标准化的新闻 dict（供 fetch 子类调用）。"""
        if not title:
            return None
        ts = published_at or int(time.time() * 1000)
        return {
            "id": make_news_id(self.name, title, ts),
            "title": title.strip(),
            "content": (content or "").strip()[:5000],
            "source": self.name,
            "url": url or "",
            "published_at": ts,
            "collected_at": int(time.time() * 1000),
            "content_hash": compute_content_hash(title, content),
        }


# ═══════════════════════════════════════════════════════════════════
# RSS 采集器
# ═══════════════════════════════════════════════════════════════════


class RSSCollector(NewsCollector):
    """通用 RSS feed 采集器（基于 feedparser）。"""

    async def fetch(self) -> List[Dict[str, Any]]:
        self._stats["total_fetches"] += 1
        try:
            session = await self._get_session()
            async with session.get(self.url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                text = await resp.text()
        except Exception as e:
            self._stats["consecutive_failures"] += 1
            self._stats["last_error"] = str(e)
            logger.warning(f"[{self.name}] RSS 拉取失败: {e}")
            return []

        # feedparser 是同步库，放到线程池
        try:
            import feedparser
            parsed = await asyncio.to_thread(feedparser.parse, text)
        except Exception as e:
            self._stats["consecutive_failures"] += 1
            self._stats["last_error"] = str(e)
            logger.warning(f"[{self.name}] RSS 解析失败: {e}")
            return []

        items: List[Dict[str, Any]] = []
        for entry in parsed.entries[:50]:  # 单次最多 50 条
            try:
                title = entry.get("title", "")
                summary = entry.get("summary", "") or entry.get("description", "")
                link = entry.get("link", "")
                # published_parsed 是 time.struct_time
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub:
                    pub_ms = int(time.mktime(pub) * 1000)
                else:
                    pub_ms = int(time.time() * 1000)
                normalized = self._normalize(title, summary, link, pub_ms)
                if normalized:
                    items.append(normalized)
            except Exception as e:
                logger.debug(f"[{self.name}] 解析单条 RSS 项失败: {e}")

        if items:
            self._stats["successful_fetches"] += 1
            self._stats["consecutive_failures"] = 0
            self._stats["last_success_at"] = int(time.time())
        return items


# ═══════════════════════════════════════════════════════════════════
# REST API 采集器
# ═══════════════════════════════════════════════════════════════════


class RESTCollector(NewsCollector):
    """REST JSON API 采集器。需要为每个源单独实现 _parse_response。"""

    async def fetch(self) -> List[Dict[str, Any]]:
        self._stats["total_fetches"] += 1
        try:
            session = await self._get_session()
            url = self.url
            if "{ts}" in url:
                url = url.replace("{ts}", str(int(time.time() * 1000)))
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                # 部分接口返回 jsonp/text，统一先取 text
                text = await resp.text()
        except Exception as e:
            self._stats["consecutive_failures"] += 1
            self._stats["last_error"] = str(e)
            logger.warning(f"[{self.name}] REST 拉取失败: {e}")
            return []

        items = self._parse_response(text)
        if items:
            self._stats["successful_fetches"] += 1
            self._stats["consecutive_failures"] = 0
            self._stats["last_success_at"] = int(time.time())
        return items

    def _parse_response(self, text: str) -> List[Dict[str, Any]]:
        """子类（或具体源 collector）实现解析逻辑。"""
        return []


class EastmoneyFlashCollector(RESTCollector):
    """东方财富 7×24 快讯采集器（中文新闻主源）。"""

    def _parse_response(self, text: str) -> List[Dict[str, Any]]:
        import json
        import re as _re
        try:
            # 东财接口可能返回 jsonp，剥离回调
            m = _re.search(r"\{.*\}", text, _re.DOTALL)
            if not m:
                return []
            data = json.loads(m.group(0))
        except Exception as e:
            logger.warning(f"[{self.name}] JSON 解析失败: {e}")
            return []

        items: List[Dict[str, Any]] = []
        try:
            list_data = (
                data.get("data", {}).get("list")
                or data.get("LivesList")
                or []
            )
            for item in list_data[:50]:
                # 字段名兼容多种格式
                title = item.get("title") or item.get("Title") or ""
                content = item.get("digest") or item.get("Digest") or item.get("summary") or ""
                url = item.get("url") or item.get("Url") or ""
                # 时间字段
                pub_str = item.get("showTime") or item.get("ShowTime") or item.get("publishtime") or ""
                pub_ms = self._parse_time(pub_str)
                normalized = self._normalize(title, content, url, pub_ms)
                if normalized:
                    items.append(normalized)
        except Exception as e:
            logger.warning(f"[{self.name}] 数据结构解析失败: {e}")
        return items

    def _parse_time(self, pub_str: str) -> int:
        """东财时间字段格式：'2024-01-02 10:30:00' 或 时间戳。"""
        if not pub_str:
            return int(time.time() * 1000)
        try:
            if pub_str.isdigit():
                return int(pub_str) * (1 if len(pub_str) > 11 else 1000)
            from datetime import datetime, timezone, timedelta
            dt = datetime.strptime(pub_str, "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return int(dt.timestamp() * 1000)
        except Exception:
            return int(time.time() * 1000)


class OKXAnnouncementCollector(RESTCollector):
    """OKX 公告采集器（占位简化实现：返回空，Phase 3B 完善）。"""

    def _parse_response(self, text: str) -> List[Dict[str, Any]]:
        # OKX 公告需要先拿类型再按类型拉详情，复杂度较高
        # Phase 3A 暂返回空，避免阻塞调度器
        return []


# ═══════════════════════════════════════════════════════════════════
# Scraper 采集器（Phase 3B 启用）
# ═══════════════════════════════════════════════════════════════════


class ScraperCollector(NewsCollector):
    """爬虫采集器（HTML 解析）。Phase 3A 占位，Phase 3B 完善。"""

    async def fetch(self) -> List[Dict[str, Any]]:
        return []


# ═══════════════════════════════════════════════════════════════════
# 工厂
# ═══════════════════════════════════════════════════════════════════


def create_collector(source_config: Dict[str, Any]) -> NewsCollector:
    """根据 source_config['type'] 和 name 创建对应的 collector。"""
    typ = source_config.get("type", "rss")
    name = source_config.get("name", "")

    if name == "东方财富7x24":
        return EastmoneyFlashCollector(source_config)
    if name == "OKX公告":
        return OKXAnnouncementCollector(source_config)

    if typ == "rss":
        return RSSCollector(source_config)
    if typ == "rest":
        return RESTCollector(source_config)
    if typ == "scraper":
        return ScraperCollector(source_config)

    return RSSCollector(source_config)  # 默认 RSS
