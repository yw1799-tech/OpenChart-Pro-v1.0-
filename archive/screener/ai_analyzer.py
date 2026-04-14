"""
AIAnalyzer - AI新闻情绪分析与推荐模块
使用 OpenAI 兼容接口，支持 DeepSeek / 通义千问 / GPT 等模型
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from openai import AsyncOpenAI

    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    logger.warning("openai 库未安装，AI分析功能不可用。请执行: pip install openai")


# ======================================================================
# 配置
# ======================================================================

# 支持通过环境变量切换不同的 LLM 提供商
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.deepseek.com")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "deepseek-chat")
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "2048"))

# 情绪分析提示词
SENTIMENT_SYSTEM_PROMPT = """你是一个专业的金融新闻分析师。请分析以下新闻列表，对每条新闻给出：

1. sentiment_score: 情绪评分 (0-100，50为中性，>50看多，<50看空)
2. impact_level: 影响等级 (high/medium/low)
3. affected_sectors: 受影响行业列表
4. affected_symbols: 受影响品种代码列表
5. summary: 一句话摘要

请以JSON数组格式返回，每个元素对应一条新闻。"""

RECOMMENDATION_SYSTEM_PROMPT = """你是一个量化投资顾问。基于以下新闻情绪分析和技术面数据，给出投资建议。

请返回JSON数组，每个推荐包含：
1. symbol: 品种代码
2. action: 建议操作 (buy/sell/hold/watch)
3. confidence: 置信度 (0-100)
4. reason: 推荐理由（简短）
5. risk_level: 风险等级 (high/medium/low)

只推荐置信度>60的品种，按置信度降序排列。"""


class AIAnalyzer:
    """
    AI新闻情绪分析与智能推荐。

    支持的 LLM 后端:
    - DeepSeek: AI_BASE_URL=https://api.deepseek.com
    - 通义千问: AI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    - OpenAI: AI_BASE_URL=https://api.openai.com/v1
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.base_url = base_url or AI_BASE_URL
        self.api_key = api_key or AI_API_KEY
        self.model = model or AI_MODEL

        self._client: Optional[Any] = None
        self._analysis_cache: Dict[str, tuple] = {}  # hash -> (timestamp, result)
        self._cache_ttl = 600  # 缓存10分钟

    def _get_client(self):
        """懒加载OpenAI客户端"""
        if not HAS_OPENAI:
            raise RuntimeError("openai 库未安装，请执行: pip install openai")
        if not self.api_key:
            raise RuntimeError("AI_API_KEY 未设置，请设置环境变量")
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
            )
        return self._client

    # ------------------------------------------------------------------
    # 新闻情绪分析
    # ------------------------------------------------------------------

    async def analyze_news(self, news_list: List[Dict]) -> List[Dict[str, Any]]:
        """
        使用LLM分析新闻情绪。

        参数:
            news_list: 新闻列表，每条含 title, content 字段

        返回:
            每条新闻附加 sentiment_score, impact_level, affected_sectors,
            affected_symbols, summary 字段
        """
        if not news_list:
            return []

        # 分批处理，每批最多15条（避免token超限）
        batch_size = 15
        all_results = []

        for i in range(0, len(news_list), batch_size):
            batch = news_list[i : i + batch_size]
            analyzed = await self._analyze_batch(batch)
            all_results.extend(analyzed)

        return all_results

    async def _analyze_batch(self, batch: List[Dict]) -> List[Dict]:
        """分析一批新闻"""
        # 构建输入文本
        news_text_parts = []
        for idx, item in enumerate(batch):
            title = item.get("title", "")
            content = item.get("content", "")[:200]  # 截断避免过长
            news_text_parts.append(f"[{idx + 1}] {title}\n{content}")

        news_input = "\n\n".join(news_text_parts)

        try:
            client = self._get_client()
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SENTIMENT_SYSTEM_PROMPT},
                        {"role": "user", "content": news_input},
                    ],
                    max_tokens=AI_MAX_TOKENS,
                    temperature=0.3,
                ),
                timeout=30,
            )

            result_text = (response.choices[0].message.content or "").strip()
            # DeepSeek可能返回markdown代码块，提取JSON
            if result_text.startswith("```"):
                # 去掉 ```json ... ```
                lines = result_text.split("\n")
                json_lines = []
                in_block = False
                for line in lines:
                    if line.strip().startswith("```") and not in_block:
                        in_block = True
                        continue
                    elif line.strip() == "```" and in_block:
                        break
                    elif in_block:
                        json_lines.append(line)
                result_text = "\n".join(json_lines).strip()
            # 如果不是以{或[开头，尝试找JSON部分
            if result_text and result_text[0] not in ("{", "["):
                start = result_text.find("{")
                if start == -1:
                    start = result_text.find("[")
                if start != -1:
                    result_text = result_text[start:]
            logger.debug(f"LLM返回内容(截取): {result_text[:200]}")
            parsed = json.loads(result_text)

            # 兼容: 结果可能是 {"results": [...]} 或直接是 [...]
            if isinstance(parsed, dict):
                items = parsed.get("results", parsed.get("data", []))
            elif isinstance(parsed, list):
                items = parsed
            else:
                items = []

            # 合并分析结果到原始新闻
            enriched = []
            for idx, news_item in enumerate(batch):
                merged = {**news_item}
                if idx < len(items):
                    analysis = items[idx]
                    merged["sentiment_score"] = analysis.get("sentiment_score", 50)
                    merged["impact_level"] = analysis.get("impact_level", "low")
                    merged["affected_sectors"] = analysis.get("affected_sectors", [])
                    merged["affected_symbols"] = analysis.get("affected_symbols", [])
                    merged["ai_summary"] = analysis.get("summary", "")
                else:
                    merged["sentiment_score"] = 50
                    merged["impact_level"] = "low"
                    merged["affected_sectors"] = []
                    merged["affected_symbols"] = []
                    merged["ai_summary"] = ""
                enriched.append(merged)

            return enriched

        except json.JSONDecodeError as e:
            logger.error(f"LLM返回的JSON解析失败: {e}")
            return [
                {
                    **item,
                    "sentiment_score": 50,
                    "impact_level": "low",
                    "affected_sectors": [],
                    "affected_symbols": [],
                    "ai_summary": "",
                }
                for item in batch
            ]
        except Exception as e:
            logger.error(f"AI情绪分析失败: {e}")
            return [
                {
                    **item,
                    "sentiment_score": 50,
                    "impact_level": "low",
                    "affected_sectors": [],
                    "affected_symbols": [],
                    "ai_summary": "",
                }
                for item in batch
            ]

    # ------------------------------------------------------------------
    # 综合推荐
    # ------------------------------------------------------------------

    async def get_recommendations(
        self,
        market: str,
        hours: int = 24,
        min_score: int = 60,
    ) -> List[Dict[str, Any]]:
        """
        综合新闻情绪+技术面，生成推荐列表。

        评分权重:
            sentiment_score × 0.40
          + technical_score × 0.35
          + news_volume_score × 0.15
          + impact_score × 0.10

        参数:
            market: 市场 (A股/US/crypto)
            hours: 取最近多少小时的新闻
            min_score: 最低综合评分阈值

        返回:
            推荐列表，按综合评分降序
        """
        # 1) 采集新闻
        from backend.screener.news import NewsCollector

        collector = NewsCollector()
        try:
            all_news = await collector.fetch_all(hours=hours)
        finally:
            await collector.close()

        # 过滤时间范围
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        recent_news = []
        for item in all_news:
            pub = item.get("published_at", "")
            if pub:
                try:
                    pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    if pub_dt >= cutoff:
                        recent_news.append(item)
                except (ValueError, TypeError):
                    recent_news.append(item)  # 解析失败则保留
            else:
                recent_news.append(item)

        if not recent_news:
            return []

        # 2) AI情绪分析（限制最多20条，避免LLM调用过多导致超时）
        news_to_analyze = recent_news[:20]
        logger.info(f"AI分析: 共{len(recent_news)}条新闻, 分析前{len(news_to_analyze)}条")
        analyzed = await self.analyze_news(news_to_analyze)

        # 3) 聚合：按品种汇总
        symbol_data: Dict[str, Dict] = {}
        for item in analyzed:
            for sym in item.get("affected_symbols", []):
                if sym not in symbol_data:
                    symbol_data[sym] = {
                        "sentiment_scores": [],
                        "impact_scores": [],
                        "news_count": 0,
                        "news_items": [],
                    }
                symbol_data[sym]["sentiment_scores"].append(item.get("sentiment_score", 50))
                impact_map = {"high": 90, "medium": 60, "low": 30}
                symbol_data[sym]["impact_scores"].append(impact_map.get(item.get("impact_level", "low"), 30))
                symbol_data[sym]["news_count"] += 1
                symbol_data[sym]["news_items"].append(item.get("title", ""))

        # 4) 加载技术面评分
        tech_scores = await self._get_technical_scores(list(symbol_data.keys()), market)

        # 5) 计算综合评分
        recommendations = []
        max_news_count = max((d["news_count"] for d in symbol_data.values()), default=1)

        for sym, data in symbol_data.items():
            # 情绪评分 (均值)
            sentiment = sum(data["sentiment_scores"]) / len(data["sentiment_scores"])

            # 技术评分
            technical = tech_scores.get(sym, 50)

            # 新闻量评分 (归一化到0-100)
            news_volume = min(data["news_count"] / max(max_news_count, 1) * 100, 100)

            # 影响力评分 (均值)
            impact = sum(data["impact_scores"]) / len(data["impact_scores"])

            # 综合评分
            total_score = sentiment * 0.40 + technical * 0.35 + news_volume * 0.15 + impact * 0.10

            if total_score >= min_score:
                # 根据评分判断动作
                if total_score >= 75:
                    action = "buy"
                elif total_score >= 60:
                    action = "watch"
                elif total_score <= 30:
                    action = "sell"
                else:
                    action = "hold"

                recommendations.append(
                    {
                        "symbol": sym,
                        "action": action,
                        "total_score": round(total_score, 2),
                        "sentiment_score": round(sentiment, 2),
                        "technical_score": round(technical, 2),
                        "news_volume_score": round(news_volume, 2),
                        "impact_score": round(impact, 2),
                        "news_count": data["news_count"],
                        "recent_headlines": data["news_items"][:5],
                        "market": market,
                    }
                )

        # 按综合评分降序
        recommendations.sort(key=lambda x: x["total_score"], reverse=True)
        return recommendations

    # ------------------------------------------------------------------
    # 技术面评分
    # ------------------------------------------------------------------

    async def _get_technical_scores(self, symbols: List[str], market: str) -> Dict[str, float]:
        """
        获取品种的技术面评分 (0-100)。

        评分逻辑:
        - 价格在MA20上方: +15
        - 价格在MA60上方: +10
        - RSI在30-70正常区间: +10, 超买(>70): -5, 超卖(<30): +20
        - MACD金叉: +15, 死叉: -10
        - 成交量放大: +10
        - 基础分: 50
        """
        scores = {}
        try:
            from backend.db.database import get_database

            db = await get_database()

            for symbol in symbols:
                try:
                    collection_name = f"kline_{symbol.lower().replace('/', '_')}_1d"
                    cursor = (
                        db[collection_name]
                        .find({}, {"_id": 0, "close": 1, "volume": 1, "high": 1, "low": 1})
                        .sort("timestamp", -1)
                        .limit(100)
                    )

                    records = await cursor.to_list(length=None)
                    if not records or len(records) < 30:
                        scores[symbol] = 50
                        continue

                    close = [r["close"] for r in reversed(records)]
                    volume = [r["volume"] for r in reversed(records)]
                    close_arr = __import__("numpy").array(close, dtype=float)
                    vol_arr = __import__("numpy").array(volume, dtype=float)

                    score = 50.0  # 基础分

                    # MA20
                    if len(close_arr) >= 20:
                        ma20 = float(__import__("numpy").mean(close_arr[-20:]))
                        if close_arr[-1] > ma20:
                            score += 15

                    # MA60
                    if len(close_arr) >= 60:
                        ma60 = float(__import__("numpy").mean(close_arr[-60:]))
                        if close_arr[-1] > ma60:
                            score += 10

                    # RSI
                    if len(close_arr) >= 15:
                        delta = __import__("numpy").diff(close_arr[-15:])
                        gain = float(__import__("numpy").mean(delta[delta > 0])) if any(delta > 0) else 0
                        loss = float(__import__("numpy").mean(-delta[delta < 0])) if any(delta < 0) else 1e-10
                        rs = gain / loss if loss > 0 else 100
                        rsi = 100 - 100 / (1 + rs)
                        if rsi < 30:
                            score += 20  # 超卖看多
                        elif rsi > 70:
                            score -= 5  # 超买
                        else:
                            score += 10  # 正常

                    # 成交量
                    if len(vol_arr) >= 20:
                        vol_ma = float(__import__("numpy").mean(vol_arr[-20:]))
                        if vol_ma > 0 and vol_arr[-1] > vol_ma * 1.5:
                            score += 10

                    scores[symbol] = min(max(score, 0), 100)

                except Exception as e:
                    logger.debug(f"计算 {symbol} 技术评分失败: {e}")
                    scores[symbol] = 50

        except Exception as e:
            logger.error(f"技术面评分批量计算失败: {e}")
            for sym in symbols:
                scores.setdefault(sym, 50)

        return scores
