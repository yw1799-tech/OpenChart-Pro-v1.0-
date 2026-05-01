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
    # 加密货币
    # ═══════════════════════════════════════════════════════════════
    # 注：OKX/Binance 官方公告 API endpoint 经过测试均返回 404/异常，
    # 改用 CoinDesk/Cointelegraph 等专业媒体覆盖（已包含交易所重要公告）。
    {
        "name": "Binance公告",
        "type": "rest",
        "market": "crypto",
        "url": "https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query?catalogId=48&pageNo=1&pageSize=20",
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
        "enabled": False,  # Cloudflare 反爬严格 (403)，CoinDesk/Cointelegraph 已覆盖
        "trust": 0.9,
        "bonus": 2,
    },
    {
        "name": "Bitcoin Magazine",
        "type": "rss",
        "market": "crypto",
        "url": "https://bitcoinmagazine.com/.rss/full/",
        "interval": 180,
        "enabled": False,  # Cloudflare 反爬严格 (403)，CoinDesk/Cointelegraph 已覆盖
        "trust": 0.8,
        "bonus": 1,
    },
    # 金色财经 jinse.com/jinse.cn 域名在云服务器（东京）无法解析（实测 HTTP 000），已从启用列表移除
    # 巴比特 8btc.com 同 — DNS/网络层不通，需要换网络或代理才能用
    {
        "name": "CryptoPanic",
        "type": "rest",
        "market": "crypto",
        "url": "https://cryptopanic.com/api/v1/posts/?public=true",
        "interval": 120,
        "enabled": False,
        "trust": 0.7,
        "bonus": 0,
    },
    {
        "name": "OKX公告",
        "type": "rest",
        "market": "crypto",
        "url": "https://www.okx.com/api/v5/support/announcements",
        "interval": 300,
        "enabled": True,  # v12.13: 新 endpoint 实测 HTTP 200 + 标准 JSON
        "trust": 1.0,
        "bonus": 3,
    },
    {
        "name": "TheDefiant",
        "type": "rss",
        "market": "crypto",
        "url": "https://thedefiant.io/feed",
        "interval": 300,
        "enabled": True,  # v12.13: DeFi 专题视角（强 BTC/ETH 互补）
        "trust": 0.85,
        "bonus": 1,
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
    # Reuters reuters.com 反爬严格 (401)，feeds.reuters.com 已停服。
    # CNBC + MarketWatch + PR Newswire + Yahoo + SEC EDGAR 已能覆盖美股权威新闻。
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
        "name": "金十数据",
        "type": "rest",
        "market": "cn",  # 中文宏观 + A 股快讯
        "url": "https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1&t=1",
        "interval": 30,  # 金十快讯，秒级时效
        "enabled": True,
        "trust": 0.9,
        "bonus": 2,
    },
    {
        "name": "新浪财经",
        "type": "rest",
        "market": "cn",
        "url": "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&num=50&versionNumber=1.2.4&page=1&encode=utf-8",
        "interval": 120,
        "enabled": True,
        "trust": 0.8,
        "bonus": 1,
    },
    # 东方财富 7x24 / 第一财经 / 同花顺：API 接口反爬严格 / 需要登录 cookie。
    # 财联社电报 + 金十数据 已是 A股最高时效双源，单独覆盖足够。
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
    # 港股 (4 个源)
    # ═══════════════════════════════════════════════════════════════
    {
        "name": "Yahoo Finance HK",
        "type": "rss",
        "market": "hk",
        "url": "https://hk.finance.yahoo.com/rss/",
        # v12.13: 120→600（云服务器 IP 被 Yahoo 严格反爬，2min 高频导致持续 429）
        # 港股新闻不需要 2min 实时；SCMP+金十+财联社已覆盖时效需求
        "interval": 600,
        "enabled": True,
        "trust": 0.85,
        "bonus": 1,
    },
    {
        "name": "SCMP Business",
        "type": "rss",
        "market": "hk",
        "url": "https://www.scmp.com/rss/92/feed",
        "interval": 180,
        "enabled": True,
        "trust": 0.9,
        "bonus": 2,
    },
    {
        "name": "SCMP 中国财经",
        "type": "rss",
        "market": "hk",
        "url": "https://www.scmp.com/rss/2/feed",
        "interval": 180,
        "enabled": True,
        "trust": 0.85,
        "bonus": 1,
    },
    {
        "name": "21财经港股",
        "type": "rest",
        "market": "hk",
        "url": "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2510&num=30",
        "interval": 120,
        "enabled": False,  # 2026-04: 最后采集 2025-05，来源已死，关闭
        "trust": 0.8,
        "bonus": 1,
    },
    {
        "name": "HKEX披露易",
        "type": "rest",
        "market": "hk",
        "url": "https://www1.hkexnews.hk/ncms/today/today.json",
        "interval": 300,
        "enabled": False,  # 2026-04: today.json 持续 JSON 解析失败（接口改/反爬），关闭止损刷屏
        "trust": 1.0,
        "bonus": 3,
    },
    {
        "name": "经济通etnet",
        "type": "scraper",
        "market": "hk",
        "url": "https://www.etnet.com.hk/www/tc/news/index.php",
        "interval": 600,
        "enabled": True,  # v12.13: HTML scraper 实测 1 页 ~60 条港股财经，含 (xxxxx) 港股代码自动 tag
        "trust": 0.85,
        "bonus": 2,
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
        "url": "https://www.bls.gov/feed/empsit.rss",  # 不带 news_release 前缀
        "interval": 600,
        "enabled": True,
        "trust": 1.0,
        "bonus": 3,
    },
    {
        "name": "BLS物价指数",
        "type": "rss",
        "market": "macro",
        "url": "https://www.bls.gov/feed/cpi.rss",
        "interval": 600,
        "enabled": True,
        "trust": 1.0,
        "bonus": 3,
    },
    {
        "name": "ECB欧洲央行",
        "type": "rss",
        "market": "macro",
        "url": "https://www.ecb.europa.eu/rss/press.xml",
        "interval": 600,
        "enabled": True,  # v12.13: 实测 HTTP 200 + 标准 RSS（含 ECB 政策声明 + 行长讲话）
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
