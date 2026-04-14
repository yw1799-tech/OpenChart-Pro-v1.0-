"""
NewsCollector - 新闻数据采集模块
支持多源新闻采集：东方财富、Finnhub、加密RSS、华尔街见闻、金十数据、Yahoo Finance、新浪财经、CoinDesk
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

# 通用请求头
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, application/xhtml+xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 单个请求超时（秒）
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8)


class NewsCollector:
    """
    多源新闻采集器。

    用法:
        collector = NewsCollector()
        news = await collector.fetch_all(market="cn")
    """

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._external_session = session is not None
        self._session = session
        self._cache: Dict[str, tuple] = {}  # source -> (timestamp, data)
        self._cache_ttl = 300  # 缓存5分钟

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout, headers=_HEADERS)
        return self._session

    async def close(self):
        if self._session and not self._external_session:
            await self._session.close()

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------

    async def fetch_all(self, market: str = None, hours: int = 24) -> List[Dict[str, Any]]:
        """
        并发采集所有源的新闻，合并去重后按时间降序返回。

        参数:
            market: 市场类型 - "cn"(A股), "hk"(港股), "us"(美股), "crypto"(加密), None(全部)
            hours: 只返回最近N小时的新闻
        """
        tasks = []

        # 通用源（所有市场都采集）
        tasks.append(self._safe_fetch("wallstreetcn", self.fetch_wallstreetcn))

        # 加密货币源
        if market in (None, "crypto"):
            tasks.append(self._safe_fetch("crypto_rss", self.fetch_crypto_news))
            tasks.append(self._safe_fetch("coindesk", self.fetch_coindesk))

        # 中国/港股源
        if market in (None, "cn", "hk"):
            tasks.append(self._safe_fetch("eastmoney", self.fetch_eastmoney_news))
            tasks.append(self._safe_fetch("sina", self.fetch_sina_finance))
            tasks.append(self._safe_fetch("jin10", self.fetch_jin10))

        # 美股/港股源
        if market in (None, "us", "hk"):
            tasks.append(self._safe_fetch("yahoo_rss", self.fetch_yahoo_rss))
            tasks.append(self._safe_fetch("finnhub", self.fetch_finnhub_news))

        results = await asyncio.gather(*tasks)
        all_news = []
        for items in results:
            all_news.extend(items)

        # 按时间过滤
        cutoff = int(time.time()) - hours * 3600
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

        # 按title去重
        seen_titles = set()
        unique_news = []
        for n in all_news:
            title = n.get("title", "").strip()
            if title and title not in seen_titles:
                seen_titles.add(title)
                unique_news.append(n)
            elif not title:
                unique_news.append(n)  # 没有标题的保留
        all_news = unique_news

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
            data = await asyncio.wait_for(func(), timeout=10)
            self._cache[source] = (time.time(), data)
            return data
        except asyncio.TimeoutError:
            logger.warning(f"采集 {source} 新闻超时")
            return []
        except Exception as e:
            logger.error(f"采集 {source} 新闻失败: {e}")
            return []

    # ------------------------------------------------------------------
    # 东方财富 7x24快讯
    # ------------------------------------------------------------------

    async def fetch_eastmoney_news(self) -> List[Dict[str, Any]]:
        """东方财富7x24快讯"""
        url = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
        params = {
            "columns": "102",
            "pageSize": "50",
            "client": "wap",
        }

        session = await self._get_session()
        async with session.get(url, params=params, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning(f"东方财富API返回 {resp.status}")
                return []

            data = await resp.json(content_type=None)

        news_list = []
        if not data or not isinstance(data, dict):
            return []
        data_inner = data.get("data")
        if not data_inner or not isinstance(data_inner, dict):
            return []
        items = data_inner.get("list", data_inner.get("items", []))

        for item in items:
            content = item.get("content", "")
            title = item.get("title", "")
            if not title and content:
                title = re.sub(r"<[^>]+>", "", content)[:80]

            clean_content = re.sub(r"<[^>]+>", "", content)

            pub_time = item.get("showtime", "")
            news_list.append(
                {
                    "source": "eastmoney",
                    "title": title.strip(),
                    "content": clean_content.strip(),
                    "url": item.get("url_w", item.get("url_m", "")),
                    "published_at": pub_time,
                    "market": "cn",
                    "category": "cn_finance",
                    "tags": _extract_tags_from_text(clean_content),
                }
            )

        logger.info(f"东方财富快讯获取 {len(news_list)} 条")
        return news_list

    # ------------------------------------------------------------------
    # Finnhub 全球新闻
    # ------------------------------------------------------------------

    async def fetch_finnhub_news(self, category: str = "general") -> List[Dict[str, Any]]:
        """Finnhub全球新闻"""
        api_key = FINNHUB_API_KEY
        # 也从数据库读取
        if not api_key:
            try:
                import sqlite3

                conn = sqlite3.connect(
                    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "openchart.db")
                )
                row = conn.execute("SELECT value FROM settings WHERE key='finnhub_api_key'").fetchone()
                if row:
                    api_key = row[0].strip('"')
                conn.close()
            except Exception:
                pass
        if not api_key:
            logger.debug("FINNHUB_API_KEY 未设置，跳过Finnhub新闻采集")
            return []

        url = "https://finnhub.io/api/v1/news"
        params = {
            "category": category,
            "token": api_key,
        }

        session = await self._get_session()
        async with session.get(url, params=params, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning(f"Finnhub API返回 {resp.status}")
                return []

            items = await resp.json(content_type=None)

        news_list = []
        for item in items:
            pub_ts = item.get("datetime", 0)
            pub_time = datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat() if pub_ts else ""

            news_list.append(
                {
                    "source": "finnhub",
                    "title": item.get("headline", ""),
                    "content": item.get("summary", ""),
                    "url": item.get("url", ""),
                    "image": item.get("image", ""),
                    "published_at": pub_time,
                    "market": "us",
                    "category": item.get("category", category),
                    "related": item.get("related", ""),
                    "tags": _extract_tags_from_text(item.get("headline", "")),
                }
            )

        logger.info(f"Finnhub新闻获取 {len(news_list)} 条")
        return news_list

    # ------------------------------------------------------------------
    # 加密新闻RSS
    # ------------------------------------------------------------------

    async def fetch_crypto_news(self) -> List[Dict[str, Any]]:
        """加密新闻RSS采集（CoinTelegraph, TheBlock, Decrypt）"""
        feeds = [
            ("cointelegraph", "https://cointelegraph.com/rss"),
            ("theblock", "https://www.theblock.co/rss.xml"),
            ("decrypt", "https://decrypt.co/feed"),
        ]

        tasks = [self._parse_rss_feed(name, url, market="crypto") for name, url in feeds]
        results = await asyncio.gather(*tasks)

        all_news = []
        for items in results:
            all_news.extend(items)

        logger.info(f"加密新闻RSS共获取 {len(all_news)} 条")
        return all_news

    # ------------------------------------------------------------------
    # CoinDesk RSS（加密货币补充源）
    # ------------------------------------------------------------------

    async def fetch_coindesk(self) -> List[Dict[str, Any]]:
        """CoinDesk RSS - 加密货币权威新闻"""
        feed_url = "https://www.coindesk.com/arc/outboundfeeds/rss/"
        items = await self._parse_rss_feed("coindesk", feed_url, market="crypto")
        logger.info(f"CoinDesk获取 {len(items)} 条")
        return items

    # ------------------------------------------------------------------
    # 华尔街见闻快讯（全球财经）
    # ------------------------------------------------------------------

    async def fetch_wallstreetcn(self) -> List[Dict[str, Any]]:
        """华尔街见闻快讯 - 全球财经实时快讯"""
        url = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
        params = {
            "channel": "global-channel",
            "limit": "30",
        }

        try:
            session = await self._get_session()
            async with session.get(url, params=params, timeout=_REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning(f"华尔街见闻API返回 {resp.status}")
                    return []
                data = await resp.json(content_type=None)

            news_list = []
            items = data.get("data", {}).get("items", [])

            for item in items:
                content = item.get("content_text", "") or item.get("content", "")
                clean_content = re.sub(r"<[^>]+>", "", content).strip()
                title = clean_content[:80] if clean_content else ""

                pub_ts = item.get("display_time", 0)
                pub_time = ""
                if pub_ts:
                    try:
                        pub_time = datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()
                    except Exception:
                        pub_time = str(pub_ts)

                news_list.append(
                    {
                        "source": "wallstreetcn",
                        "title": title,
                        "content": clean_content[:500],
                        "url": f"https://wallstreetcn.com/live/{item.get('id', '')}",
                        "published_at": pub_time,
                        "market": "global",
                        "category": "global_finance",
                        "tags": _extract_tags_from_text(clean_content),
                    }
                )

            logger.info(f"华尔街见闻快讯获取 {len(news_list)} 条")
            return news_list

        except Exception as e:
            logger.error(f"华尔街见闻采集失败: {e}")
            return []

    # ------------------------------------------------------------------
    # 金十数据快讯（宏观/外汇/加密）
    # ------------------------------------------------------------------

    async def fetch_jin10(self) -> List[Dict[str, Any]]:
        """金十数据快讯 - 宏观经济、外汇、商品"""
        url = "https://flash-api.jin10.com/get"
        params = {
            "channel": "-8200",
            "max_time": "",
            "vip": "0",
        }
        headers = {
            **_HEADERS,
            "Referer": "https://www.jin10.com/",
            "Origin": "https://www.jin10.com",
            "x-app-id": "bVBF4FyRTn5NJF5n",
            "x-version": "1.0.0",
        }

        try:
            session = await self._get_session()
            async with session.get(url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning(f"金十数据API返回 {resp.status}")
                    return []
                data = await resp.json(content_type=None)

            news_list = []
            items = data.get("data", [])
            if not isinstance(items, list):
                return []

            for item in items:
                content = item.get("data", {})
                if isinstance(content, dict):
                    text = content.get("content", "") or content.get("title", "")
                elif isinstance(content, str):
                    text = content
                else:
                    text = str(content) if content else ""

                clean_text = re.sub(r"<[^>]+>", "", text).strip()
                if not clean_text:
                    continue

                title = clean_text[:80]

                pub_time_str = item.get("time", "")
                pub_time = pub_time_str  # 保留原始时间字符串

                news_list.append(
                    {
                        "source": "jin10",
                        "title": title,
                        "content": clean_text[:500],
                        "url": "https://www.jin10.com/flash",
                        "published_at": pub_time,
                        "market": "global",
                        "category": "macro",
                        "tags": _extract_tags_from_text(clean_text),
                    }
                )

            logger.info(f"金十数据快讯获取 {len(news_list)} 条")
            return news_list

        except Exception as e:
            logger.error(f"金十数据采集失败: {e}")
            return []

    # ------------------------------------------------------------------
    # Yahoo Finance RSS（美股/港股）
    # ------------------------------------------------------------------

    async def fetch_yahoo_rss(self, symbol: str = None) -> List[Dict[str, Any]]:
        """Yahoo Finance RSS - 美股/港股新闻"""
        if symbol:
            feed_url = f"https://finance.yahoo.com/rss/headline?s={symbol}"
        else:
            feed_url = "https://finance.yahoo.com/rss/topstories"

        items = await self._parse_rss_feed("yahoo_finance", feed_url, market="us")
        logger.info(f"Yahoo Finance获取 {len(items)} 条")
        return items

    # ------------------------------------------------------------------
    # 新浪财经（A股/港股）
    # ------------------------------------------------------------------

    async def fetch_sina_finance(self) -> List[Dict[str, Any]]:
        """新浪财经滚动新闻 - A股/港股/宏观"""
        url = "https://feed.mix.sina.com.cn/api/roll/get"
        params = {
            "pageid": "153",
            "lid": "2516",
            "k": "",
            "num": "30",
            "page": "1",
        }

        try:
            session = await self._get_session()
            async with session.get(url, params=params, timeout=_REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning(f"新浪财经API返回 {resp.status}")
                    return []
                data = await resp.json(content_type=None)

            news_list = []
            result = data.get("result", {})
            items = result.get("data", [])
            if not isinstance(items, list):
                return []

            for item in items:
                title = item.get("title", "").strip()
                if not title:
                    continue

                # 清理HTML
                intro = item.get("intro", "") or item.get("summary", "")
                clean_intro = re.sub(r"<[^>]+>", "", intro).strip()

                pub_ts = item.get("ctime", 0) or item.get("mtime", 0)
                pub_time = ""
                if pub_ts:
                    try:
                        pub_ts = int(pub_ts)
                        pub_time = datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()
                    except Exception:
                        pub_time = str(pub_ts)

                news_list.append(
                    {
                        "source": "sina_finance",
                        "title": title,
                        "content": clean_intro[:500],
                        "url": item.get("url", ""),
                        "published_at": pub_time,
                        "market": "cn",
                        "category": "cn_finance",
                        "tags": _extract_tags_from_text(title + " " + clean_intro),
                    }
                )

            logger.info(f"新浪财经获取 {len(news_list)} 条")
            return news_list

        except Exception as e:
            logger.error(f"新浪财经采集失败: {e}")
            return []

    # ------------------------------------------------------------------
    # RSS 通用解析
    # ------------------------------------------------------------------

    async def _parse_rss_feed(self, source_name: str, feed_url: str, market: str = "global") -> List[Dict[str, Any]]:
        """解析单个RSS源"""
        try:
            import feedparser
        except ImportError:
            logger.warning("feedparser 未安装，无法解析RSS。请执行: pip install feedparser")
            return []

        try:
            session = await self._get_session()
            async with session.get(feed_url, timeout=_REQUEST_TIMEOUT) as resp:
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

                news_list.append(
                    {
                        "source": f"rss_{source_name}",
                        "title": entry.get("title", ""),
                        "content": clean_summary.strip(),
                        "url": entry.get("link", ""),
                        "published_at": published,
                        "market": market,
                        "category": market,
                        "tags": _extract_tags_from_text(entry.get("title", "")),
                    }
                )

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
    "原油": ["原油", "crude", "oil", "opec"],
    "黄金": ["黄金", "gold", "xau"],
    "外汇": ["外汇", "forex", "汇率", "美元", "usd"],
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
