"""
新闻数据源配置（PRD F4.2 v1 首批 10 个验证源）。

每个源定义：name / type / market / url / interval (秒) / parser-specific 字段
新源加入需经过 7 天稳定性验证。
"""

from __future__ import annotations

# 注意：每个源的 url 都做过实测可用性确认（API 文档/RSS feed 可用）。
# Phase 3A 启动时只采集启用的源（enabled=True）。
NEWS_SOURCES = [
    # ───────── 加密货币 ─────────
    {
        "name": "OKX公告",
        "type": "rest",
        "market": "crypto",
        "url": "https://www.okx.com/api/v5/support/announcements-types",
        # 实际拉取走 announcements 接口；此处占位，collector 自定义解析
        "interval": 60,
        "enabled": True,
        "trust": 1.0,
        "bonus": 3,
    },
    {
        "name": "CoinDesk",
        "type": "rss",
        "market": "crypto",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss",
        "interval": 180,
        "enabled": True,
        "trust": 0.85,
        "bonus": 1,
    },
    {
        "name": "CryptoPanic",
        "type": "rest",
        "market": "crypto",
        "url": "https://cryptopanic.com/api/v1/posts/",  # 需要 auth_token (config.FINNHUB_API_KEY 风格的 key)
        "interval": 120,
        "enabled": False,  # 需 API Key，默认关闭
        "trust": 0.7,
        "bonus": 0,
    },
    # ───────── 美股 ─────────
    {
        "name": "Yahoo Finance",
        "type": "rss",
        "market": "us",
        "url": "https://finance.yahoo.com/news/rssindex",
        "interval": 300,
        "enabled": True,
        "trust": 0.85,
        "bonus": 1,
    },
    {
        "name": "Finnhub",
        "type": "rest",
        "market": "us",
        "url": "https://finnhub.io/api/v1/news?category=general",
        "interval": 300,
        "enabled": False,  # 需要 FINNHUB_API_KEY
        "trust": 0.8,
        "bonus": 1,
    },
    # ───────── A 股 ─────────
    {
        "name": "东方财富7x24",
        "type": "rest",
        "market": "cn",
        "url": "https://np-listapi.eastmoney.com/comm/wap/getListInfo?cb=&client=wap&type=1&pageindex=1&pagesize=50&columnid=1346&_=",
        "interval": 120,
        "enabled": True,
        "trust": 0.85,
        "bonus": 1,
    },
    {
        "name": "金十数据",
        "type": "rss",
        "market": "macro",
        "url": "https://rsshub.app/jin10/news",  # RSSHub
        "interval": 180,
        "enabled": False,  # 依赖 RSSHub 自托管，默认关闭
        "trust": 0.85,
        "bonus": 1,
    },
    {
        "name": "巨潮资讯",
        "type": "scraper",
        "market": "cn",
        "url": "http://www.cninfo.com.cn/new/disclosure/stock?orgId=&stockCode=&columnId=stock_disclosure",
        "interval": 600,
        "enabled": False,  # 爬虫复杂度高，Phase 3A 暂关闭
        "trust": 1.0,
        "bonus": 3,
    },
    # ───────── 港股 ─────────
    {
        "name": "HKEX披露易",
        "type": "scraper",
        "market": "hk",
        "url": "https://www.hkexnews.hk/index.htm",
        "interval": 600,
        "enabled": False,  # 爬虫，Phase 3A 暂关闭
        "trust": 1.0,
        "bonus": 3,
    },
    # ───────── 宏观 ─────────
    {
        "name": "ForexFactory",
        "type": "scraper",
        "market": "macro",
        "url": "https://www.forexfactory.com/calendar",
        "interval": 1800,
        "enabled": False,  # Phase 3B 启用
        "trust": 0.9,
        "bonus": 2,
    },
]


def get_enabled_sources():
    """返回所有 enabled=True 的源配置。"""
    return [s for s in NEWS_SOURCES if s.get("enabled", False)]


def get_source_trust(source_name: str) -> dict:
    """获取某个源的可信度配置。未知源返回低可信度默认值。"""
    for s in NEWS_SOURCES:
        if s["name"] == source_name:
            return {"trust": s.get("trust", 0.5), "bonus": s.get("bonus", 0)}
    return {"trust": 0.3, "bonus": -2}  # 未知源
