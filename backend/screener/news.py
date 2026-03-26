"""
NewsCollector - 新闻数据采集模块
支持东方财富快讯、Finnhub全球新闻、加密新闻RSS
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Finnhub API Key (从环境变量读取)
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")


class NewsCollector:
    """
    多源新闻采集器。

    用法:
        collector = NewsCollector()
        news = await collector.fetch_all()
    """

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._external_session = session is not None
        self._session = session
        self._cache: Dict[str, tuple] = {}  # source -> (timestamp, data)
        self._cache_ttl = 300  # 缓存5分钟

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._external_session:
            await self._session.close()

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------

    async def fetch_all(self, hours: int = 24) -> List[Dict[str, Any]]:
        """并发采集所有源的新闻，合并去重后按时间降序返回"""
        tasks = [
            self._safe_fetch("eastmoney", self.fetch_eastmoney_news),
            self._safe_fetch("finnhub", self.fetch_finnhub_news),
            self._safe_fetch("crypto_rss", self.fetch_crypto_news),
        ]
        results = await asyncio.gather(*tasks)
        all_news = []
        for items in results:
            all_news.extend(items)

        # 按时间过滤
        import time as _time
        cutoff = int(_time.time()) - hours * 3600
        filtered = []
        for n in all_news:
            pa = n.get("published_at", 0)
            try:
                if isinstance(pa, (int, float)) and pa > cutoff:
                    filtered.append(n)
                elif isinstance(pa, str):
                    filtered.append(n)  # 无法判断时间的保留
                else:
                    filtered.append(n)
            except Exception:
                filtered.append(n)
        all_news = filtered

        # 按时间降序
        all_news.sort(key=lambda x: x.get("published_at", ""), reverse=True)
        return all_news

    async def _safe_fetch(self, source: str, func) -> List[Dict]:
        """带缓存和异常保护的采集"""
        # 检查缓存
        if source in self._cache:
            ts, data = self._cache[source]
            if time.time() - ts < self._cache_ttl:
                return data

        try:
            data = await func()
            self._cache[source] = (time.time(), data)
            return data
        except Exception as e:
            logger.error(f"采集 {source} 新闻失败: {e}")
            return []

    # ------------------------------------------------------------------
    # 东方财富 7x24快讯
    # ------------------------------------------------------------------

    async def fetch_eastmoney_news(self) -> List[Dict[str, Any]]:
        """
        东方财富7x24快讯

        API: https://np-listapi.eastmoney.com/comm/web/getNewsByColumns
        参数: columns=102, pageSize=50, client=wap
        """
        url = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
        params = {
            "columns": "102",
            "pageSize": "50",
            "client": "wap",
        }

        session = await self._get_session()
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                logger.warning(f"东方财富API返回 {resp.status}")
                return []

            data = await resp.json(content_type=None)

        news_list = []
        items = data.get("data", {}).get("list", [])

        for item in items:
            content = item.get("content", "")
            title = item.get("title", "")
            if not title and content:
                # 快讯通常没有title，取content前50字
                title = re.sub(r"<[^>]+>", "", content)[:80]

            # 清理HTML标签
            clean_content = re.sub(r"<[^>]+>", "", content)

            pub_time = item.get("showtime", "")
            news_list.append({
                "source": "eastmoney",
                "title": title.strip(),
                "content": clean_content.strip(),
                "url": item.get("url_w", item.get("url_m", "")),
                "published_at": pub_time,
                "category": "cn_finance",
                "tags": _extract_tags_from_text(clean_content),
            })

        logger.info(f"东方财富快讯获取 {len(news_list)} 条")
        return news_list

    # ------------------------------------------------------------------
    # Finnhub 全球新闻
    # ------------------------------------------------------------------

    async def fetch_finnhub_news(self, category: str = "general") -> List[Dict[str, Any]]:
        """
        Finnhub全球新闻

        API: https://finnhub.io/api/v1/news
        参数: category=general, token=API_KEY
        """
        if not FINNHUB_API_KEY:
            logger.warning("FINNHUB_API_KEY 未设置，跳过Finnhub新闻采集")
            return []

        url = "https://finnhub.io/api/v1/news"
        params = {
            "category": category,
            "token": FINNHUB_API_KEY,
        }

        session = await self._get_session()
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                logger.warning(f"Finnhub API返回 {resp.status}")
                return []

            items = await resp.json(content_type=None)

        news_list = []
        for item in items:
            pub_ts = item.get("datetime", 0)
            pub_time = datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat() if pub_ts else ""

            news_list.append({
                "source": "finnhub",
                "title": item.get("headline", ""),
                "content": item.get("summary", ""),
                "url": item.get("url", ""),
                "image": item.get("image", ""),
                "published_at": pub_time,
                "category": item.get("category", category),
                "related": item.get("related", ""),
                "tags": _extract_tags_from_text(item.get("headline", "")),
            })

        logger.info(f"Finnhub新闻获取 {len(news_list)} 条")
        return news_list

    # ------------------------------------------------------------------
    # 加密新闻RSS
    # ------------------------------------------------------------------

    async def fetch_crypto_news(self) -> List[Dict[str, Any]]:
        """
        加密新闻RSS采集

        来源:
        - CoinTelegraph: https://cointelegraph.com/rss
        - TheBlock: https://www.theblock.co/rss.xml
        - Decrypt: https://decrypt.co/feed
        """
        feeds = [
            ("cointelegraph", "https://cointelegraph.com/rss"),
            ("theblock", "https://www.theblock.co/rss.xml"),
            ("decrypt", "https://decrypt.co/feed"),
        ]

        tasks = [self._parse_rss_feed(name, url) for name, url in feeds]
        results = await asyncio.gather(*tasks)

        all_news = []
        for items in results:
            all_news.extend(items)

        logger.info(f"加密新闻RSS共获取 {len(all_news)} 条")
        return all_news

    async def _parse_rss_feed(self, source_name: str, feed_url: str) -> List[Dict[str, Any]]:
        """解析单个RSS源"""
        try:
            import feedparser
        except ImportError:
            logger.warning("feedparser 未安装，无法解析RSS。请执行: pip install feedparser")
            return []

        try:
            session = await self._get_session()
            async with session.get(feed_url) as resp:
                if resp.status != 200:
                    logger.warning(f"RSS {source_name} 返回 {resp.status}")
                    return []
                content = await resp.text()

            feed = feedparser.parse(content)
            news_list = []

            for entry in feed.entries[:30]:  # 每个源最多取30条
                published = entry.get("published", entry.get("updated", ""))
                # 尝试解析时间
                pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub_parsed:
                    pub_dt = datetime(*pub_parsed[:6], tzinfo=timezone.utc)
                    published = pub_dt.isoformat()

                # 清理HTML
                summary = entry.get("summary", "")
                clean_summary = re.sub(r"<[^>]+>", "", summary)[:500]

                news_list.append({
                    "source": f"crypto_{source_name}",
                    "title": entry.get("title", ""),
                    "content": clean_summary.strip(),
                    "url": entry.get("link", ""),
                    "published_at": published,
                    "category": "crypto",
                    "tags": _extract_tags_from_text(entry.get("title", "")),
                })

            return news_list

        except Exception as e:
            logger.error(f"解析RSS {source_name} 失败: {e}")
            return []


# ======================================================================
# 辅助函数
# ======================================================================

# 关键词标签映射
_TAG_KEYWORDS = {
    "BTC": ["bitcoin", "btc", "比特币"],
    "ETH": ["ethereum", "eth", "以太坊", "以太"],
    "SOL": ["solana", "sol"],
    "XRP": ["ripple", "xrp"],
    "AI": ["ai", "人工智能", "artificial intelligence", "chatgpt", "openai"],
    "DeFi": ["defi", "去中心化金融", "uniswap", "aave"],
    "NFT": ["nft", "opensea"],
    "美联储": ["fed", "美联储", "federal reserve", "fomc"],
    "降息": ["rate cut", "降息"],
    "加息": ["rate hike", "加息"],
    "CPI": ["cpi", "通胀", "inflation"],
    "GDP": ["gdp"],
    "就业": ["employment", "nonfarm", "就业", "非农"],
    "A股": ["a股", "沪深", "上证", "深证", "创业板"],
    "港股": ["港股", "恒生", "hang seng"],
    "美股": ["美股", "纳斯达克", "标普", "道琼斯", "s&p", "nasdaq", "dow"],
}


def _extract_tags_from_text(text: str) -> List[str]:
    """从文本中提取标签关键词"""
    if not text:
        return []
    text_lower = text.lower()
    tags = []
    for tag, keywords in _TAG_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                tags.append(tag)
                break
    return tags
