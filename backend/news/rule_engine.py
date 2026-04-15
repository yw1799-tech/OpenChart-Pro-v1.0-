"""
新闻规则引擎（PRD F5 / TDD §6.3.3）。

核心：用免费的关键词权重 + 来源可信度，把原始新闻评分并打标签。
覆盖 70%+ 场景，无需 LLM。

输入：原始新闻 dict（来自 collector.normalize 的输出）
输出：FlashNews 字段 dict (importance, sentiment, categories, impact_tags, l2_score, ...)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from backend.news.sources import get_source_trust
from backend.news.symbol_registry import registry as _symbol_registry


# ═══════════════════════════════════════════════════════════════════
# 关键词权重表（PRD §2.5 节选）
# ═══════════════════════════════════════════════════════════════════

KEYWORD_WEIGHTS: Dict[int, List[str]] = {
    5: [
        # 极重要：宏观/系统性事件
        "FOMC", "加息", "降息", "rate decision", "利率决议",
        "CPI", "PPI", "通胀", "inflation",
        "非农", "nonfarm", "NFP",
        "黑客", "hack", "exploit", "被盗",
        "ETF批准", "ETF approval", "SEC approved", "现货 ETF",
        "退市", "delist", "破产", "bankruptcy",
        "停牌", "suspended",
    ],
    3: [
        # 重要：公司/行业事件
        "财报", "earnings", "业绩预告",
        "收购", "并购", "M&A", "acquisition", "merger",
        "监管", "regulator", "SEC", "CFTC", "证监会",
        "降级", "downgrade", "升级评级", "upgrade rating",
        "重组", "restructure",
        "解禁", "lockup",
    ],
    1: [
        # 一般：常规公告
        "增持", "减持", "回购", "buyback",
        "分红", "dividend",
        "高管变动", "CEO", "CFO",
    ],
    -2: [
        # 噪音：广告/推广
        "推广", "广告", "赞助", "sponsored",
        "促销", "活动", "campaign",
    ],
}


# ═══════════════════════════════════════════════════════════════════
# 情绪关键词（用于 sentiment 判定）
# ═══════════════════════════════════════════════════════════════════

BULLISH_PATTERNS = [
    "突破", "大涨", "暴涨", "利好", "看好", "上涨", "新高",
    "buy", "bullish", "surge", "rally", "soar", "jump", "beat",
    "approved", "批准", "通过", "盈利超预期", "增长", "扩张",
]

BEARISH_PATTERNS = [
    "下跌", "暴跌", "大跌", "利空", "看空", "新低", "崩盘",
    "sell", "bearish", "plunge", "tumble", "crash", "miss",
    "下调", "downgrade", "rejected", "拒绝", "亏损", "缩水",
    "破产", "退市", "停牌",
]


# ═══════════════════════════════════════════════════════════════════
# 事件类型标签匹配
# ═══════════════════════════════════════════════════════════════════

EVENT_TYPE_PATTERNS = {
    "earnings": ["财报", "earnings", "业绩"],
    "regulation": ["监管", "regulator", "SEC", "CFTC", "证监会", "compliance"],
    "rate_decision": ["FOMC", "加息", "降息", "rate decision", "利率"],
    "inflation": ["CPI", "PPI", "通胀", "inflation"],
    "employment": ["非农", "NFP", "失业率", "unemployment"],
    "etf": ["ETF"],
    "hack": ["黑客", "hack", "exploit", "被盗"],
    "merger": ["收购", "并购", "M&A", "acquisition", "merger"],
    "delisting": ["退市", "delist"],
    "bankruptcy": ["破产", "bankruptcy"],
    "buyback": ["回购", "buyback"],
    "dividend": ["分红", "dividend"],
}


# ═══════════════════════════════════════════════════════════════════
# 加密 6 币种 + 常见股票符号识别
# ═══════════════════════════════════════════════════════════════════

CRYPTO_SYMBOL_PATTERNS = {
    "BTC-USDT": [r"\bBTC\b", r"\bbitcoin\b", r"比特币"],
    "ETH-USDT": [r"\bETH\b", r"\bethereum\b", r"以太坊", r"以太币"],
    "SOL-USDT": [r"\bSOL\b", r"\bsolana\b"],
    "DOGE-USDT": [r"\bDOGE\b", r"\bdogecoin\b", r"狗狗币"],
    "BNB-USDT": [r"\bBNB\b", r"\bbinance coin\b"],
    "XRP-USDT": [r"\bXRP\b", r"\bripple\b", r"瑞波"],
}

# 常见美股/A股符号识别正则（前 50 个常见标的，可按需扩展）
US_STOCK_PATTERNS = {
    "AAPL": [r"\bAAPL\b", r"\bApple\b", r"苹果公司"],
    "NVDA": [r"\bNVDA\b", r"\bNVIDIA\b", r"英伟达"],
    "TSLA": [r"\bTSLA\b", r"\bTesla\b", r"特斯拉"],
    "MSFT": [r"\bMSFT\b", r"\bMicrosoft\b", r"微软"],
    "GOOGL": [r"\bGOOGL?\b", r"\bGoogle\b", r"\bAlphabet\b", r"谷歌"],
    "META": [r"\bMETA\b", r"\bFacebook\b", r"Meta Platforms"],
    "AMZN": [r"\bAMZN\b", r"\bAmazon\b", r"亚马逊"],
    "AMD": [r"\bAMD\b"],
}

CN_STOCK_PATTERNS = {
    "600519": [r"\b600519\b", r"贵州茅台", r"茅台"],
    "601318": [r"\b601318\b", r"中国平安"],
    "000001": [r"\b000001\b", r"平安银行"],
    "002594": [r"\b002594\b", r"比亚迪"],
}


# ═══════════════════════════════════════════════════════════════════
# 主评分函数
# ═══════════════════════════════════════════════════════════════════


def score_news(
    raw: Dict[str, Any],
    holding_symbols: Optional[Set[str]] = None,
    pool_symbols: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    规则引擎主入口：把原始新闻 dict 转为 FlashNews 字段 dict。

    raw 必须包含: title, source。其余字段可选: content, url, published_at。
    holding_symbols / pool_symbols: 用于"持仓快速通道"加分。

    返回新增字段：
      importance: 1-5 星
      sentiment:  bullish/bearish/neutral
      categories: 关联品种代码列表
      impact_tags: 事件类型标签
      keywords:   命中的关键词
      l2_score:   规则引擎原始分（保留用于调试）
      is_holding_related: 是否涉及持仓
      impact_on_crypto: 对加密 6 币种的影响（如果新闻提到加密）
    """
    title = raw.get("title", "") or ""
    content = raw.get("content", "") or ""
    source = raw.get("source", "") or ""
    full_text = (title + " " + content).lower()
    full_text_orig = title + " " + content

    holding_symbols = holding_symbols or set()
    pool_symbols = pool_symbols or set()

    # 1. 关键词命中评分
    keyword_score = 0
    matched_keywords: List[str] = []
    for weight, words in KEYWORD_WEIGHTS.items():
        for word in words:
            if word.lower() in full_text:
                keyword_score += weight
                matched_keywords.append(word)

    # 2. 来源可信度
    trust_info = get_source_trust(source)
    trust = trust_info["trust"]
    bonus = trust_info["bonus"]

    # 3. 品种关联识别（使用 SymbolRegistry，含 200+ 静态 + 动态 watchlist/pool/positions）
    categories: List[str] = _symbol_registry.find_matches(full_text_orig)

    # 兜底：保留旧硬编码以防 SymbolRegistry 未初始化
    if not categories:
        def _match_patterns(patterns_dict, text):
            out = []
            for sym, patterns in patterns_dict.items():
                for p in patterns:
                    if re.search(p, text, re.IGNORECASE):
                        out.append(sym)
                        break
            return out

        categories.extend(_match_patterns(CRYPTO_SYMBOL_PATTERNS, full_text_orig))
        categories.extend(_match_patterns(US_STOCK_PATTERNS, full_text_orig))
        categories.extend(_match_patterns(CN_STOCK_PATTERNS, full_text_orig))

    # 4. 持仓加分（PRD F5.10 持仓快速通道）
    is_holding_related = any(c in holding_symbols for c in categories)
    is_pool_related = any(c in pool_symbols for c in categories)
    holding_boost = 0
    if is_holding_related:
        holding_boost = 3
    elif is_pool_related:
        holding_boost = 1

    # 5. 综合评分
    final_score = (keyword_score + bonus) * trust + holding_boost

    # 6. 重要性映射
    if final_score < 0:
        importance = 0  # 丢弃
    elif final_score < 2:
        importance = 1
    elif final_score < 4:
        importance = 2
    elif final_score < 6:
        importance = 3
    elif final_score < 8:
        importance = 4
    else:
        importance = 5

    # 7. 情绪判定
    bullish_hits = sum(1 for p in BULLISH_PATTERNS if p.lower() in full_text)
    bearish_hits = sum(1 for p in BEARISH_PATTERNS if p.lower() in full_text)
    if bullish_hits > bearish_hits + 1:
        sentiment = "bullish"
    elif bearish_hits > bullish_hits + 1:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    # 8. 事件类型标签
    impact_tags: List[str] = []
    for tag, patterns in EVENT_TYPE_PATTERNS.items():
        if any(p.lower() in full_text for p in patterns):
            impact_tags.append(tag)

    # 9. 加密影响（涉及加密币种的新闻）
    crypto_categories = [c for c in categories if c.endswith("-USDT")]
    impact_on_crypto = None
    if crypto_categories:
        impact_on_crypto = []
        for sym in crypto_categories:
            impact_on_crypto.append({
                "symbol": sym,
                "direction": sentiment,
                "strength": importance,
                "reason": f"新闻提及 {sym}，情绪判定 {sentiment}",
            })

    return {
        "importance": importance,
        "sentiment": sentiment,
        "categories": list(set(categories)),
        "impact_tags": impact_tags,
        "keywords": matched_keywords,
        "is_holding_related": is_holding_related,
        "l2_score": float(final_score),
        "impact_on_crypto": impact_on_crypto,
    }
