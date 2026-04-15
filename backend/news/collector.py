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

    # 子类可覆盖：定制请求 headers（应对反爬）
    EXTRA_HEADERS: Dict[str, str] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            base_headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                "Accept": "application/rss+xml, application/atom+xml, application/xml, "
                          "application/json, text/html, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            base_headers.update(self.EXTRA_HEADERS)
            self._session = aiohttp.ClientSession(
                headers=base_headers,
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
                title = (entry.get("title", "") or "").strip()
                if not title:
                    continue
                # 摘要优先级: summary > description > content[0].value
                summary = entry.get("summary", "") or entry.get("description", "")
                if not summary and isinstance(entry.get("content"), list) and entry["content"]:
                    summary = entry["content"][0].get("value", "")
                # 链接：link / id (Atom 中 id 常为 URL)
                link = entry.get("link", "") or entry.get("id", "")
                # 时间 published > updated > 当前时间
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
    """OKX 公告采集器（v2 实装，按类型拉新上线公告）。"""

    def _parse_response(self, text: str) -> List[Dict[str, Any]]:
        import json
        try:
            data = json.loads(text)
        except Exception:
            return []
        items: List[Dict[str, Any]] = []
        try:
            details = data.get("data", {}).get("details") or data.get("data", []) or []
            # OKX 接口可能返回不同结构
            for entry in details if isinstance(details, list) else []:
                ann_list = entry.get("details", []) if isinstance(entry, dict) else []
                for ann in ann_list:
                    title = ann.get("title", "")
                    url = ann.get("url", "")
                    pub_ms = int(ann.get("pTime", 0))
                    n = self._normalize(title, "", url, pub_ms or None)
                    if n:
                        items.append(n)
        except Exception as e:
            logger.warning(f"[OKX] 公告解析失败: {e}")
        return items[:30]


class BinanceAnnouncementCollector(RESTCollector):
    """Binance 公告采集器。"""

    def _parse_response(self, text: str) -> List[Dict[str, Any]]:
        import json
        try:
            data = json.loads(text)
        except Exception:
            return []
        items: List[Dict[str, Any]] = []
        try:
            articles = data.get("data", {}).get("articles", [])
            for art in articles[:30]:
                title = art.get("title", "")
                code = art.get("code", "")
                # 文章链接
                url = f"https://www.binance.com/en/support/announcement/{code}" if code else ""
                pub_ms = int(art.get("releaseDate", 0))
                n = self._normalize(title, "", url, pub_ms or None)
                if n:
                    items.append(n)
        except Exception as e:
            logger.warning(f"[Binance] 公告解析失败: {e}")
        return items


class JinseFinanceCollector(RESTCollector):
    """金色财经快讯采集器（中文加密新闻）。"""

    def _parse_response(self, text: str) -> List[Dict[str, Any]]:
        import json
        try:
            data = json.loads(text)
        except Exception:
            return []
        items: List[Dict[str, Any]] = []
        try:
            for it in data.get("list", []) or data.get("data", []) or []:
                # 金色快讯的字段: extra, title, summary
                extra = it.get("extra", {}) if isinstance(it.get("extra"), dict) else {}
                title = extra.get("title") or it.get("title") or ""
                summary = extra.get("summary") or it.get("summary") or ""
                url = extra.get("topic_url") or it.get("link") or ""
                pub_sec = int(it.get("created_at", 0) or extra.get("published_at", 0))
                pub_ms = pub_sec * 1000 if pub_sec < 1e11 else pub_sec
                n = self._normalize(title, summary, url, pub_ms or None)
                if n:
                    items.append(n)
        except Exception as e:
            logger.warning(f"[金色] 解析失败: {e}")
        return items[:30]


class CailianpressCollector(RESTCollector):
    """财联社电报采集器（A 股最高时效新闻源之一）。"""

    def _parse_response(self, text: str) -> List[Dict[str, Any]]:
        import json
        try:
            data = json.loads(text)
        except Exception:
            return []
        items: List[Dict[str, Any]] = []
        try:
            for telegrams in [data.get("data", {}).get("roll_data", []), data.get("data", []) if isinstance(data.get("data"), list) else []]:
                if not telegrams:
                    continue
                for tg in telegrams:
                    title = tg.get("title") or tg.get("brief") or ""
                    if not title and tg.get("content"):
                        # 没标题用内容前 100 字作标题
                        title = tg["content"][:100]
                    content = tg.get("content", "") or tg.get("brief", "")
                    url = tg.get("shareurl", "") or f"https://www.cls.cn/detail/{tg.get('id', '')}"
                    pub_sec = int(tg.get("ctime", 0) or 0)
                    pub_ms = pub_sec * 1000 if pub_sec < 1e11 else pub_sec
                    n = self._normalize(title, content, url, pub_ms or None)
                    if n:
                        items.append(n)
        except Exception as e:
            logger.warning(f"[财联社] 解析失败: {e}")
        return items[:50]


class SinaFinanceCollector(RESTCollector):
    """新浪财经滚动新闻采集器。"""

    def _parse_response(self, text: str) -> List[Dict[str, Any]]:
        import json
        import time as _time
        # 新浪可能返回 jsonp 或 json
        try:
            # 尝试直接 json
            data = json.loads(text)
        except Exception:
            try:
                # 剥离 jsonp 包装
                import re as _re
                m = _re.search(r"\((\{.*\})\)", text, _re.DOTALL)
                if not m:
                    return []
                data = json.loads(m.group(1))
            except Exception:
                return []
        items: List[Dict[str, Any]] = []
        try:
            arr = (data.get("result", {}).get("data", []) or
                   data.get("data", []) or [])
            for it in arr:
                title = it.get("title", "")
                url = it.get("url", "")
                pub_sec = int(it.get("ctime", 0) or it.get("create_time", 0))
                pub_ms = pub_sec * 1000 if pub_sec and pub_sec < 1e11 else (pub_sec or int(_time.time() * 1000))
                summary = it.get("intro", "") or it.get("summary", "")
                n = self._normalize(title, summary, url, pub_ms)
                if n:
                    items.append(n)
        except Exception as e:
            logger.warning(f"[新浪] 解析失败: {e}")
        return items[:30]


class SECEdgarCollector(RSSCollector):
    """SEC EDGAR 必须用合规 User-Agent (含联系方式)，否则 403。"""

    EXTRA_HEADERS = {
        "User-Agent": "OpenChart Pro contact@openchartpro.local",
        "Accept-Encoding": "gzip, deflate",
    }


class BLSRSSCollector(RSSCollector):
    """美国劳工统计局 (BLS) 用 Feedly UA 才能通过反爬。"""

    EXTRA_HEADERS = {
        "User-Agent": "Feedly/1.0 (+http://feedly.com)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml",
    }


class BitcoinMagazineCollector(RSSCollector):
    """Bitcoin Magazine 需要明确的 Accept header 才不返回 403。"""

    EXTRA_HEADERS = {
        "Accept": "application/rss+xml, application/xml; q=0.9, */*; q=0.8",
    }


class Jin10FlashCollector(RESTCollector):
    """金十数据快讯（中文宏观/A 股最高时效源之一）。"""

    EXTRA_HEADERS = {
        "x-app-id": "bVBF4FyRTn5NJF5n",
        "x-version": "1.0.0",
        "Origin": "https://www.jin10.com",
        "Referer": "https://www.jin10.com/",
    }

    def _parse_response(self, text: str) -> List[Dict[str, Any]]:
        import json
        try:
            data = json.loads(text)
        except Exception:
            return []
        items: List[Dict[str, Any]] = []
        try:
            rows = data.get("data") or []
            from datetime import datetime, timezone, timedelta
            tz = timezone(timedelta(hours=8))
            for it in rows[:50]:
                # 金十快讯字段：time(yyyy-MM-dd HH:mm:ss) / type / data.title|content / data.pic
                time_str = it.get("time", "")
                d = it.get("data", {}) or {}
                title = d.get("title") or ""
                content = d.get("content") or ""
                # 没有 title 则从 content 提取首句（去掉 HTML 标签）
                if not title and content:
                    import re as _re
                    plain = _re.sub(r"<[^>]+>", "", content).strip()
                    title = plain[:80] if plain else ""
                if not title:
                    continue
                # 时间解析
                pub_ms = int(time.time() * 1000)
                if time_str:
                    try:
                        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
                        pub_ms = int(dt.timestamp() * 1000)
                    except ValueError:
                        pass
                # 链接（金十没有具体 URL，用 ID 拼站内链接）
                jid = it.get("id", "")
                url = f"https://www.jin10.com/details/{jid}" if jid else "https://www.jin10.com/"
                n = self._normalize(title, content[:500], url, pub_ms)
                if n:
                    items.append(n)
        except Exception as e:
            logger.warning(f"[金十] 解析失败: {e}")
        return items


class YicaiCollector(RESTCollector):
    """第一财经新闻采集器。"""

    def _parse_response(self, text: str) -> List[Dict[str, Any]]:
        import json
        try:
            data = json.loads(text)
        except Exception:
            return []
        items: List[Dict[str, Any]] = []
        try:
            arr = data.get("DocList", []) or data.get("data", []) or []
            for it in arr:
                title = it.get("NewsTitle", "") or it.get("title", "")
                url = it.get("WeixinShareLink", "") or it.get("url", "")
                pub_str = it.get("PublishDate", "") or it.get("publish_time", "")
                pub_ms = self._parse_iso_time(pub_str)
                summary = it.get("Source", "") or ""
                n = self._normalize(title, summary, url, pub_ms)
                if n:
                    items.append(n)
        except Exception as e:
            logger.warning(f"[第一财经] 解析失败: {e}")
        return items[:30]

    def _parse_iso_time(self, s: str) -> int:
        if not s:
            return int(time.time() * 1000)
        try:
            from datetime import datetime, timezone, timedelta
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = datetime.strptime(s.split(".")[0], fmt)
                    dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
                    return int(dt.timestamp() * 1000)
                except ValueError:
                    continue
        except Exception:
            pass
        return int(time.time() * 1000)


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
    """根据 source_config['name'] 优先用专用解析；其次按 type 走通用 RSS/REST/Scraper。"""
    typ = source_config.get("type", "rss")
    name = source_config.get("name", "")

    # 专用 collector（按 name 精确匹配）
    SPECIAL = {
        "东方财富7x24": EastmoneyFlashCollector,
        "OKX公告": OKXAnnouncementCollector,
        "Binance公告": BinanceAnnouncementCollector,
        "金色财经": JinseFinanceCollector,
        "财联社电报": CailianpressCollector,
        "金十数据": Jin10FlashCollector,
        "新浪财经": SinaFinanceCollector,
        "21财经港股": SinaFinanceCollector,  # 复用新浪 roll API
        "第一财经": YicaiCollector,
        "SEC EDGAR": SECEdgarCollector,
        "BLS就业数据": BLSRSSCollector,
        "BLS物价指数": BLSRSSCollector,
        "Bitcoin Magazine": BitcoinMagazineCollector,
    }
    if name in SPECIAL:
        return SPECIAL[name](source_config)

    if typ == "rss":
        return RSSCollector(source_config)
    if typ == "rest":
        return RESTCollector(source_config)
    if typ == "scraper":
        return ScraperCollector(source_config)
    return RSSCollector(source_config)
