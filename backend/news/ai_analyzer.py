"""
LLM 深度解读引擎（PRD F5.7 / TDD §6.3.6 Phase 3B）。

设计原则：
  - 不做选股决策（决策已在规则引擎完成）
  - 只对 ★★★★+ 新闻 + 用户主动点击的新闻 做深度解读
  - 异步回填 flash_news.ai_analysis，不阻塞主流程
  - 日预算硬上限 (config.LLM_DAILY_BUDGET, 默认 $5)
  - 超支只告警，不降级（按用户要求）

支持的 LLM:
  - DeepSeek (deepseek-chat)
  - 通义千问 (qwen-turbo / qwen-plus)
  统一用 openai 库的兼容模式调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import backend.config as config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Prompt 模板
# ═══════════════════════════════════════════════════════════════════

DEEP_ANALYSIS_PROMPT = """你是一个专业的金融分析师。请对下面这条财经新闻做深度解读，输出严格的 JSON 格式（不要解释/不要 markdown 包裹）：

新闻标题：{title}
新闻来源：{source}
新闻正文：{content}
关联品种：{categories}

请输出 JSON：
{{
  "overall_view": "bullish|bearish|neutral",
  "summary": "30 字以内的核心摘要",
  "impacts": [
    {{"symbol": "品种代码", "direction": "bullish|bearish|neutral", "horizon": "1d|1-5d|1-3m", "strength": 0.0-1.0, "reason": "一句话原因"}}
  ],
  "reasons": ["利好/利空理由 1", "理由 2"],
  "risks": ["潜在风险 1", "风险 2"],
  "key_levels": {{"support": 数字 or null, "resistance": 数字 or null}},
  "historical_analogies": ["类似事件 1（如有）"]
}}

要求：
- impacts 数组只列实际有影响的品种，可为空
- 价格相关字段没把握就用 null
- 不要编造数据
"""


POSITION_ADVICE_PROMPT = """你是一个专业的金融顾问。基于下列信息，对用户的持仓给出操作建议（输出严格 JSON）：

持仓品种：{symbol} ({market})
数量：{quantity}  成本价：{avg_cost}  当前价：{current_price}
浮动盈亏：{pnl_pct}%

最近 24 小时相关新闻 ({news_count} 条)：
{news_summary}

请输出 JSON：
{{
  "advice": "hold|reduce|add|close",
  "urgency": "low|medium|high",
  "reason": "100 字以内的核心理由",
  "key_factors": ["因素 1", "因素 2"],
  "suggested_action": {{"qty_pct": 0-100, "target_price": 数字 or null, "stop_loss": 数字 or null}}
}}

只对"明确利好/利空"才给 add/close 建议，不确定时给 hold。
"""


# ═══════════════════════════════════════════════════════════════════
# 价格预估（输入/输出 token × 单价）
# ═══════════════════════════════════════════════════════════════════

# 美元 / 1K tokens（截至文档时点公开价格，可能变动）
PROVIDER_PRICING = {
    "deepseek": {"input": 0.00027, "output": 0.0011},  # deepseek-chat
    "qwen": {"input": 0.0002, "output": 0.0006},        # qwen-turbo
}


def _estimate_cost(provider: str, input_tokens: int, output_tokens: int) -> float:
    p = PROVIDER_PRICING.get(provider, {"input": 0.001, "output": 0.002})
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1000


# ═══════════════════════════════════════════════════════════════════
# 主分析器
# ═══════════════════════════════════════════════════════════════════


class NewsAIAnalyzer:
    """
    LLM 深度解读 + 持仓建议。共用日预算池。
    """

    def __init__(self, db):
        self.db = db
        self._client = None
        self._provider = None
        self._model = None
        # 当日成本快照 (避免每次都查 DB)
        self._today_cost: float = 0.0
        self._today_date: str = ""
        # 并发限制 (LLM 单账户限速)
        self._semaphore = asyncio.Semaphore(2)

    # ───────── 客户端懒初始化 ─────────

    def _ensure_client(self) -> bool:
        """读取运行时配置初始化 OpenAI 兼容客户端。返回是否可用。"""
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("openai 库未安装，LLM 不可用")
            return False

        provider = config.LLM_PROVIDER or "deepseek"
        if provider == "deepseek":
            api_key = config.DEEPSEEK_API_KEY
            base_url = config.DEEPSEEK_BASE_URL
            model = config.DEEPSEEK_MODEL
        elif provider == "qwen":
            api_key = config.QWEN_API_KEY
            base_url = config.QWEN_BASE_URL
            model = config.QWEN_MODEL
        else:
            logger.warning(f"未知 LLM 提供商: {provider}")
            return False

        if not api_key:
            logger.debug(f"LLM provider={provider} API Key 未配置，跳过深度解读")
            return False

        # 配置变化时重新创建客户端
        if (self._client is None or
                self._provider != provider or
                getattr(self._client, "_base_url_str", None) != base_url):
            self._client = OpenAI(api_key=api_key, base_url=base_url)
            self._client._base_url_str = base_url
            self._provider = provider
            self._model = model
            logger.info(f"LLM 客户端初始化: provider={provider}, model={model}")

        return True

    # ───────── 预算控制 ─────────

    async def _refresh_today_cost(self):
        """从 DB 加载当日累计成本（每次调用前快速查）。"""
        today = time.strftime("%Y-%m-%d")
        if self._today_date != today:
            # 新一天，重置
            self._today_date = today
            self._today_cost = 0.0

        try:
            day_start_ms = int(time.mktime(time.strptime(today, "%Y-%m-%d")) * 1000)
            async with self.db.acquire() as conn:
                cursor = await conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) as total FROM llm_cost_log WHERE called_at >= ?",
                    (day_start_ms,),
                )
                row = await cursor.fetchone()
                self._today_cost = float(row["total"]) if row else 0.0
        except Exception as e:
            logger.debug(f"读取当日 LLM 成本失败: {e}")

    async def _can_call(self) -> bool:
        """检查日预算（按 PRD F5.11：超支只告警，不降级）。"""
        await self._refresh_today_cost()
        budget = config.LLM_DAILY_BUDGET
        if self._today_cost >= budget:
            logger.warning(
                f"⚠️ LLM 日预算 ${budget:.2f} 已超支 (当前 ${self._today_cost:.2f})，"
                f"按 PRD F5.11 仍继续调用，请关注成本"
            )
        if self._today_cost >= budget * 0.8:
            logger.warning(
                f"⚠️ LLM 日成本接近预算: ${self._today_cost:.2f} / ${budget:.2f}"
            )
        return True  # 永远允许调用，仅告警

    async def _record_cost(
        self,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
        news_id: Optional[str] = None,
    ):
        """成本入库 llm_cost_log。"""
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO llm_cost_log
                    (called_at, model, input_tokens, output_tokens, cost_usd, news_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(time.time() * 1000),
                        self._model or "unknown",
                        input_tokens,
                        output_tokens,
                        cost_usd,
                        news_id,
                    ),
                )
                await conn.commit()
            self._today_cost += cost_usd
        except Exception as e:
            logger.debug(f"成本入库失败: {e}")

    # ───────── LLM 调用核心 ─────────

    async def _call_llm(
        self,
        prompt: str,
        news_id: Optional[str] = None,
        max_tokens: int = 800,
    ) -> Optional[Dict[str, Any]]:
        """
        统一 LLM 调用入口。返回解析后的 JSON dict 或 None。
        """
        if not self._ensure_client():
            return None
        await self._can_call()

        async with self._semaphore:
            try:
                # OpenAI 客户端是同步的，放线程池
                resp = await asyncio.to_thread(
                    self._client.chat.completions.create,
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=max_tokens,
                    timeout=30,
                )
            except Exception as e:
                logger.warning(f"LLM 调用失败: {type(e).__name__}: {e}")
                return None

        # 解析返回
        try:
            content = resp.choices[0].message.content or ""
            # 去除可能的 ```json ... ``` 包裹
            content = content.strip()
            if content.startswith("```"):
                # 找到第一个 { 和最后一个 }
                start = content.find("{")
                end = content.rfind("}")
                if start >= 0 and end > start:
                    content = content[start:end + 1]
            result = json.loads(content)
        except (json.JSONDecodeError, IndexError, AttributeError) as e:
            logger.warning(f"LLM 返回解析失败: {e}, raw={resp.choices[0].message.content[:200] if resp.choices else 'no choices'}")
            return None

        # 记录成本
        try:
            usage = resp.usage
            input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0
            cost = _estimate_cost(self._provider, input_tokens, output_tokens)
            await self._record_cost(cost, input_tokens, output_tokens, news_id)
            logger.info(
                f"💡 LLM 调用成功 ({self._provider}/{self._model}): "
                f"in={input_tokens} out={output_tokens} cost=${cost:.4f}"
            )
        except Exception as e:
            logger.debug(f"成本统计异常: {e}")

        return result

    # ───────── 新闻深度解读 ─────────

    async def deep_analyze_news(self, news: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        对单条新闻做深度解读。
        news 字段: id, title, source, content, categories, importance
        """
        title = news.get("title", "")
        if not title:
            return None
        prompt = DEEP_ANALYSIS_PROMPT.format(
            title=title[:200],
            source=news.get("source", ""),
            content=(news.get("content") or "")[:1500],
            categories=", ".join(news.get("categories") or []) or "无",
        )
        result = await self._call_llm(prompt, news_id=news.get("id"))
        if result:
            # 入库回填
            try:
                async with self.db.acquire() as conn:
                    await conn.execute(
                        "UPDATE flash_news SET ai_analysis = ? WHERE id = ?",
                        (json.dumps(result, ensure_ascii=False), news["id"]),
                    )
                    await conn.commit()
            except Exception as e:
                logger.debug(f"AI 分析回填失败: {e}")
        return result

    # ───────── 持仓建议（增强 PortfolioAdvisor） ─────────

    async def deep_position_advice(
        self,
        position: Dict[str, Any],
        recent_news: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """
        对持仓做 LLM 深度建议（PortfolioAdvisor 规则版的增强版）。
        """
        if not position:
            return None
        news_summary = "\n".join(
            f"- ★{n.get('importance',1)} [{n.get('sentiment','neutral')}] {n.get('title','')[:80]}"
            for n in recent_news[:8]
        ) or "无相关新闻"

        cur_price = float(position.get("current_price", 0))
        avg = float(position.get("avg_cost", 0))
        pnl_pct = ((cur_price - avg) / avg * 100) if avg > 0 else 0

        prompt = POSITION_ADVICE_PROMPT.format(
            symbol=position["symbol"],
            market=position.get("market", ""),
            quantity=position.get("quantity", 0),
            avg_cost=avg,
            current_price=cur_price,
            pnl_pct=f"{pnl_pct:.2f}",
            news_count=len(recent_news),
            news_summary=news_summary,
        )
        return await self._call_llm(prompt, max_tokens=600)

    # ───────── 成本查询 ─────────

    async def get_cost_status(self) -> Dict[str, Any]:
        """前端"今日成本"查询用。"""
        await self._refresh_today_cost()
        budget = config.LLM_DAILY_BUDGET
        return {
            "today_cost_usd": round(self._today_cost, 4),
            "daily_budget": budget,
            "budget_remaining_pct": max(0, round((1 - self._today_cost / budget) * 100, 1)),
            "provider": self._provider,
            "model": self._model,
            "status": "ok" if self._today_cost < budget * 0.8
            else ("warning" if self._today_cost < budget else "exceeded"),
        }
