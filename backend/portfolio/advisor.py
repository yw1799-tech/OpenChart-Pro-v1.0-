"""
持仓建议引擎（PRD F8 / TDD §6.6）。

输入：单个 position + 最近新闻 + 当前 K 线 + 指标
输出：建议 ('hold' | 'reduce' | 'add' | 'close') + 理由 + 紧急度

触发场景：
- 涉及持仓的 ★★★+ 利空新闻 → reduce/close
- 利好新闻 → add (谨慎)
- RSI > 80 → reduce (超买)
- RSI < 20 → add (超卖反弹机会)
- 价格接近用户设定止损位 → 'close' 提醒（Phase 5 简化：无止损位字段，跳过）
- 浮亏超 -5% + 利空新闻 → close

同一 position 同一建议 1 小时内不重复推送（去重在 tracker 层）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import numpy as np

from backend.indicators.builtin import calc_rsi

logger = logging.getLogger(__name__)


class PositionAdvisor:
    """
    根据当前指标 + 新闻给出持仓建议。
    """

    def evaluate(
        self,
        position: Dict[str, Any],
        candles: Optional[List] = None,
        recent_news: Optional[List[Dict]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        返回 {advice, reason, urgency} 或 None（无建议）。
        urgency: 'low' | 'medium' | 'high'
        """
        symbol = position["symbol"]
        avg_cost = position.get("avg_cost") or 0
        current_price = self._infer_current_price(candles)

        # 浮动盈亏比例
        pnl_pct = 0.0
        if avg_cost > 0 and current_price > 0:
            pnl_pct = (current_price - avg_cost) / avg_cost * 100

        # 1. 新闻利空判定
        bearish_news = []
        bullish_news = []
        if recent_news:
            for n in recent_news[:20]:
                if symbol not in (n.get("categories") or []):
                    continue
                if n.get("importance", 0) < 3:
                    continue
                if n.get("sentiment") == "bearish":
                    bearish_news.append(n)
                elif n.get("sentiment") == "bullish":
                    bullish_news.append(n)

        # 2. 技术指标判定
        rsi_value = None
        if candles and len(candles) >= 20:
            try:
                closes = np.array([c.close for c in candles], dtype=np.float64)
                rsi = calc_rsi(closes, 14)
                if not np.isnan(rsi[-1]):
                    rsi_value = float(rsi[-1])
            except Exception:
                pass

        # 3. 综合决策
        # 情况 1: 严重利空（多条 ★★★+ bearish + 技术超买） → close
        if len(bearish_news) >= 2 and rsi_value and rsi_value > 70:
            return {
                "advice": "close",
                "reason": f"多条利空新闻 ({len(bearish_news)} 条 ★★★+) + RSI={rsi_value:.0f} 超买",
                "urgency": "high",
                "pnl_pct": round(pnl_pct, 2),
                "current_price": current_price,
            }
        # 情况 2: 利空新闻 + 浮亏 → reduce
        if bearish_news and pnl_pct < -3:
            n = bearish_news[0]
            return {
                "advice": "reduce",
                "reason": f"利空新闻 + 浮亏 {pnl_pct:.1f}%: {n.get('title', '')[:60]}",
                "urgency": "high",
                "pnl_pct": round(pnl_pct, 2),
                "current_price": current_price,
            }
        # 情况 3: 单条利空 ★★★+ → reduce 提示
        if bearish_news:
            n = bearish_news[0]
            return {
                "advice": "reduce",
                "reason": f"利空新闻 ★{n.get('importance', 3)}: {n.get('title', '')[:60]}",
                "urgency": "medium",
                "pnl_pct": round(pnl_pct, 2),
                "current_price": current_price,
            }
        # 情况 4: 技术超买严重 → reduce
        if rsi_value and rsi_value > 80:
            return {
                "advice": "reduce",
                "reason": f"RSI={rsi_value:.0f} 严重超买，注意回调",
                "urgency": "medium",
                "pnl_pct": round(pnl_pct, 2),
                "current_price": current_price,
            }
        # 情况 5: 多条利好 + RSI 合理 → add
        if len(bullish_news) >= 2 and rsi_value and 30 < rsi_value < 70:
            return {
                "advice": "add",
                "reason": f"多条利好新闻 ({len(bullish_news)} 条) + 技术面合理 RSI={rsi_value:.0f}",
                "urgency": "low",
                "pnl_pct": round(pnl_pct, 2),
                "current_price": current_price,
            }
        # 情况 6: 浮盈较大 + 技术过热 → reduce
        if pnl_pct > 20 and rsi_value and rsi_value > 70:
            return {
                "advice": "reduce",
                "reason": f"浮盈 {pnl_pct:.1f}% + RSI={rsi_value:.0f}，可考虑部分止盈",
                "urgency": "low",
                "pnl_pct": round(pnl_pct, 2),
                "current_price": current_price,
            }
        # 默认：继续持有
        return None  # 无明显信号则不推送，避免噪音

    @staticmethod
    def _infer_current_price(candles) -> float:
        if not candles:
            return 0.0
        return float(candles[-1].close)
