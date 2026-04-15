"""
新闻数据源配置 (PRD F4.2 扩充版 v2)。

优先级原则：
  - 官方公告 > 权威媒体 > 聚合源 > 社交
  - 高时效源（财联社、SEC EDGAR、官方公告）轮询间隔短
  - 各市场 4-6 个源，覆盖中英文、不同视角
  - 默认启用所有不需要 API Key 的源

关键提升点（vs v1）：
  - 加密：4 → 9 个源（含 Cointelegraph / Decrypt / TheBlock / 金色财经）
  - 美股：2 → 7 个源（含 SEC EDGAR / Reuters / CNBC / MarketWatch / PR Newswire）
  - A 股：2 → 6 个源（含 财联社 / 新浪财经 / 第一财经 / 同花顺）
  - 港股：1 → 2 个源
  - 宏观：2 → 4 个源（含 Fed / BLS / 央行）
  - 总计 11 → 28+ 个源

每个源定义：name / type / market / url / interval (秒) / enabled / trust / bonus
"""

from __future__ import annotations

NEWS_SOURCES = [
    # ═══════════════════════════════════════════════════════════════
    # 加密货币 (9 个源)
    # ═══════════════════════════════════════════════════════════════
    {
        "name": "OKX公告",
        "type": "rest",
        "market": "crypto",
        "url": "https://www.okx.com/api/v5/support/announcements?annType=announcements-new-listings",
        "interval": 60,
        "enabled": True,
        "trust": 1.0,
        "bonus": 3,
    },
    {
        "name": "Binance公告",
        "type": "rest",
        "market": "crypto",
        "url": "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=20",
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
        "interval": 90,
        "enabled": True,
        "trust": 0.9,
        "bonus": 2,
    },
    {
        "name": "Cointelegraph",
        "type": "rss",
        "market": "crypto",
        "url": "https://cointelegraph.com/rss",
        "interval": 90,
        "enabled": True,
        "trust": 0.85,
        "bonus": 2,
    },
    {
        "name": "Decrypt",
        "type": "rss",
        "market": "crypto",
        "url": "https://decrypt.co/feed",
        "interval": 120,
        "enabled": True,
        "trust": 0.85,
        "bonus": 2,
    },
    {
        "name": "TheBlock",
        "type": "rss",
        "market": "crypto",
        "url": "https://www.theblock.co/rss.xml",
        "interval": 120,
        "enabled": True,
        "trust": 0.9,
        "bonus": 2,
    },
    {
        "name": "Bitcoin Magazine",
        "type": "rss",
        "market": "crypto",
        "url": "https://bitcoinmagazine.com/.rss/full/",
        "interval": 180,
        "enabled": True,
        "trust": 0.8,
        "bonus": 1,
    },
    {
        "name": "金色财经",
        "type": "rest",
        "market": "crypto",
        "url": "https://api.jinse.cn/v6/information/list?catelogue_key=news&limit=20&_source=web",
        "interval": 60,
        "enabled": True,
        "trust": 0.8,
        "bonus": 1,
    },
    {
        "name": "CryptoPanic",
        "type": "rest",
        "market": "crypto",
        "url": "https://cryptopanic.com/api/v1/posts/?public=true",
        "interval": 120,
        "enabled": False,  # 需 API Key 才有完整功能
        "trust": 0.7,
        "bonus": 0,
    },

    # ═══════════════════════════════════════════════════════════════
    # 美股 (7 个源)
    # ═══════════════════════════════════════════════════════════════
    {
        "name": "SEC EDGAR",
        "type": "rss",
        "market": "us",
        "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&company=&dateb=&owner=include&count=40&output=atom",
        "interval": 120,
        "enabled": True,
        "trust": 1.0,
        "bonus": 3,
    },
    {
        "name": "Yahoo Finance",
        "type": "rss",
        "market": "us",
        "url": "https://finance.yahoo.com/news/rssindex",
        "interval": 180,
        "enabled": True,
        "trust": 0.85,
        "bonus": 1,
    },
    {
        "name": "Reuters Business",
        "type": "rss",
        "market": "us",
        "url": "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best",
        "interval": 120,
        "enabled": True,
        "trust": 0.95,
        "bonus": 2,
    },
    {
        "name": "CNBC",
        "type": "rss",
        "market": "us",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",
        "interval": 120,
        "enabled": True,
        "trust": 0.85,
        "bonus": 2,
    },
    {
        "name": "MarketWatch",
        "type": "rss",
        "market": "us",
        "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "interval": 120,
        "enabled": True,
        "trust": 0.85,
        "bonus": 2,
    },
    {
        "name": "PR Newswire",
        "type": "rss",
        "market": "us",
        "url": "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
        "interval": 120,
        "enabled": True,
        "trust": 0.8,
        "bonus": 1,
    },
    {
        "name": "Finnhub",
        "type": "rest",
        "market": "us",
        "url": "https://finnhub.io/api/v1/news?category=general",
        "interval": 300,
        "enabled": False,  # 需 FINNHUB_API_KEY
        "trust": 0.8,
        "bonus": 1,
    },

    # ═══════════════════════════════════════════════════════════════
    # A 股 (6 个源)
    # ═══════════════════════════════════════════════════════════════
    {
        "name": "财联社电报",
        "type": "rest",
        "market": "cn",
        "url": "https://www.cls.cn/nodeapi/updateTelegraphList?app=CailianpressWeb&category=&hasFirstVipArticle=1&lastTime=0&os=web&rn=20&subscribedColumnIds=&sv=8.4.6",
        "interval": 30,  # 财联社快讯，秒级时效
        "enabled": True,
        "trust": 0.9,
        "bonus": 2,
    },
    {
        "name": "东方财富7x24",
        "type": "rest",
        "market": "cn",
        "url": "https://np-listapi.eastmoney.com/comm/wap/getListInfo?cb=&client=wap&type=1&pageindex=1&pagesize=50&columnid=1346",
        "interval": 60,
        "enabled": True,
        "trust": 0.85,
        "bonus": 1,
    },
    {
        "name": "新浪财经",
        "type": "rss",
        "market": "cn",
        "url": "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&num=50&versionNumber=1.2.4&page=1&encode=utf-8",
        "interval": 120,
        "enabled": True,
        "trust": 0.8,
        "bonus": 1,
    },
    {
        "name": "第一财经",
        "type": "rss",
        "market": "cn",
        "url": "https://www.yicai.com/api/ajax/getlistdatabycid?page=1&cid=15&pagesize=20",
        "interval": 120,
        "enabled": True,
        "trust": 0.85,
        "bonus": 1,
    },
    {
        "name": "同花顺财经",
        "type": "rss",
        "market": "cn",
        "url": "https://news.10jqka.com.cn/today_list/index.shtml",
        "interval": 180,
        "enabled": False,  # 需爬虫，Phase 3B 启用
        "trust": 0.8,
        "bonus": 1,
    },
    {
        "name": "巨潮资讯",
        "type": "rest",
        "market": "cn",
        "url": "http://www.cninfo.com.cn/new/disclosure?stock=&searchkey=&category=category_ndbg_szsh&plate=&column=szse_main&tabName=fulltext&pageSize=30&pageNum=1",
        "interval": 300,
        "enabled": False,  # 需 POST 请求 + 复杂解析，Phase 3B 启用
        "trust": 1.0,
        "bonus": 3,
    },

    # ═══════════════════════════════════════════════════════════════
    # 港股 (2 个源)
    # ═══════════════════════════════════════════════════════════════
    {
        "name": "AAStocks",
        "type": "rss",
        "market": "hk",
        "url": "http://www.aastocks.com/tc/rss/news/CompanyNews.xml",
        "interval": 180,
        "enabled": True,
        "trust": 0.85,
        "bonus": 1,
    },
    {
        "name": "HKEX披露易",
        "type": "scraper",
        "market": "hk",
        "url": "https://www1.hkexnews.hk/listedco/listconews/mainindex/SEHK_LISTEDCO_DOCUMENTS_TODAY.HTM",
        "interval": 600,
        "enabled": False,  # 需 HTML 爬取，Phase 3B 启用
        "trust": 1.0,
        "bonus": 3,
    },

    # ═══════════════════════════════════════════════════════════════
    # 宏观 (4 个源)
    # ═══════════════════════════════════════════════════════════════
    {
        "name": "Federal Reserve",
        "type": "rss",
        "market": "macro",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "interval": 300,
        "enabled": True,
        "trust": 1.0,
        "bonus": 3,
    },
    {
        "name": "BLS就业数据",
        "type": "rss",
        "market": "macro",
        "url": "https://www.bls.gov/feed/news_release/empsit.rss",
        "interval": 600,
        "enabled": True,
        "trust": 1.0,
        "bonus": 3,
    },
    {
        "name": "BLS物价指数",
        "type": "rss",
        "market": "macro",
        "url": "https://www.bls.gov/feed/news_release/cpi.rss",
        "interval": 600,
        "enabled": True,
        "trust": 1.0,
        "bonus": 3,
    },
    {
        "name": "ForexFactory",
        "type": "scraper",
        "market": "macro",
        "url": "https://www.forexfactory.com/calendar",
        "interval": 1800,
        "enabled": False,  # Phase 3B 实装爬虫
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
    return {"trust": 0.3, "bonus": -2}


def stats() -> dict:
    """启动时打印的统计信息。"""
    by_market = {}
    enabled_count = 0
    for s in NEWS_SOURCES:
        m = s["market"]
        by_market.setdefault(m, {"total": 0, "enabled": 0})
        by_market[m]["total"] += 1
        if s.get("enabled"):
            by_market[m]["enabled"] += 1
            enabled_count += 1
    return {
        "total": len(NEWS_SOURCES),
        "enabled": enabled_count,
        "by_market": by_market,
    }
