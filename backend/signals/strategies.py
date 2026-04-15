"""
策略实现（PRD F7.1 / TDD §6.5.1）。

6 个内置策略：
  1. MACrossStrategy        均线金叉死叉      (基础分 55)
  2. DonchianBreakout       Donchian 通道突破 (基础分 50)
  3. BollingerReversion     布林带均值回归    (基础分 50)
  4. RSIDivergence          RSI 背离          (基础分 50)
  5. VolumeBreakout         成交量突破        (基础分 55)
  6. FlashEventStrategy     新闻事件驱动      (基础分 50 + importance × 10)

通用置信度计算（PRD F7.5）：
  confidence = base
    + 10 if RSI in 30-70 (合理区间)
    + 15 if price > MA200 (上升趋势)
    + 10 if volume > 1.5 × MA20_volume (有量配合)
    - 10 if recent bearish news (★★★+)
    - 15 if macro bearish impact active
  → clamp(0, 100)
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import numpy as np

from backend.data.models import Candle, Market, Signal
from backend.indicators.builtin import (
    calc_boll,
    calc_ma,
    calc_macd,
    calc_rsi,
    calc_volume_ma,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 基类
# ═══════════════════════════════════════════════════════════════════


class Strategy(ABC):
    """策略基类。"""

    name: str = "abstract"
    base_confidence: int = 50
    description: str = ""

    @abstractmethod
    def evaluate(
        self,
        symbol: str,
        market: Market,
        candles: List[Candle],
        recent_news: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Signal]:
        """评估当前数据，返回 Signal 或 None。"""

    def _common_modifier(
        self,
        candles: List[Candle],
        recent_news: Optional[List[Dict[str, Any]]],
    ) -> int:
        """通用加减分项（PRD F7.5）。"""
        if len(candles) < 200:
            return 0
        closes = np.array([c.close for c in candles], dtype=np.float64)
        volumes = np.array([c.volume for c in candles], dtype=np.float64)
        score = 0

        # +10 if RSI in 30-70
        try:
            rsi = calc_rsi(closes, 14)
            last_rsi = rsi[-1]
            if not np.isnan(last_rsi) and 30 < last_rsi < 70:
                score += 10
        except Exception:
            pass

        # +15 if price > MA200
        try:
            ma200 = calc_ma(closes, 200)
            if not np.isnan(ma200[-1]) and closes[-1] > ma200[-1]:
                score += 15
        except Exception:
            pass

        # +10 if volume > 1.5 × MA20_volume
        try:
            vma = calc_volume_ma(volumes, 20)
            if not np.isnan(vma[-1]) and volumes[-1] > 1.5 * vma[-1]:
                score += 10
        except Exception:
            pass

        # -10 if recent bearish news (★★★+)
        if recent_news:
            for n in recent_news[:10]:
                if n.get("importance", 0) >= 3 and n.get("sentiment") == "bearish":
                    score -= 10
                    break

        # -15 if macro bearish impact active (Phase 3B 集成时启用)
        # TODO: 接入 macro impact analyzer

        return score

    @staticmethod
    def _clamp(v: int) -> int:
        return max(0, min(100, v))

    def _make_signal(
        self,
        symbol: str,
        market: Market,
        action: str,
        price: float,
        confidence: int,
        reason: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        triggered_by: Optional[Dict] = None,
    ) -> Signal:
        return Signal(
            id=str(uuid.uuid4()),
            symbol=symbol,
            market=market,
            action=action,
            strategy_name=self.name,
            confidence=self._clamp(confidence),
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=reason,
            triggered_by=triggered_by or {},
            generated_at=int(time.time() * 1000),
        )


# ═══════════════════════════════════════════════════════════════════
# 1. 均线金叉死叉（短均线穿越长均线）
# ═══════════════════════════════════════════════════════════════════


class MACrossStrategy(Strategy):
    name = "ma_cross"
    base_confidence = 55
    description = "均线金叉死叉（5 上穿 20 = 买入；5 下穿 20 = 卖出）"

    def __init__(self, fast: int = 5, slow: int = 20):
        self.fast = fast
        self.slow = slow

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.slow + 2:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        ma_fast = calc_ma(closes, self.fast)
        ma_slow = calc_ma(closes, self.slow)
        # 检查最后两根 K 线的相对关系
        prev_diff = ma_fast[-2] - ma_slow[-2]
        curr_diff = ma_fast[-1] - ma_slow[-1]
        if np.isnan(prev_diff) or np.isnan(curr_diff):
            return None

        action = None
        if prev_diff <= 0 < curr_diff:
            action = "buy"
        elif prev_diff >= 0 > curr_diff:
            action = "sell"
        if action is None:
            return None

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(closes[-1])
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"MA{self.fast}/{self.slow} {'金叉' if action == 'buy' else '死叉'} @ {last_close:.4f}",
        )


# ═══════════════════════════════════════════════════════════════════
# 2. Donchian 通道突破
# ═══════════════════════════════════════════════════════════════════


class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    base_confidence = 50
    description = "Donchian 通道突破（突破 N 日高点 = 买入；跌破 N 日低点 = 卖出）"

    def __init__(self, period: int = 20):
        self.period = period

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.period + 2:
            return None
        highs = np.array([c.high for c in candles], dtype=np.float64)
        lows = np.array([c.low for c in candles], dtype=np.float64)
        closes = np.array([c.close for c in candles], dtype=np.float64)
        # 用 [-period-1:-1] 作为前 N 根（不含当前）
        prev_high = float(np.max(highs[-self.period - 1: -1]))
        prev_low = float(np.min(lows[-self.period - 1: -1]))
        last_close = float(closes[-1])

        action = None
        if last_close > prev_high:
            action = "buy"
        elif last_close < prev_low:
            action = "sell"
        if action is None:
            return None

        # 通道宽度作为止损参考
        atr_proxy = (prev_high - prev_low) * 0.3
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        if action == "buy":
            stop_loss = last_close - atr_proxy
            take_profit = last_close + atr_proxy * 2
        else:
            stop_loss = last_close + atr_proxy
            take_profit = last_close - atr_proxy * 2

        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"Donchian({self.period}) {'突破' if action == 'buy' else '跌破'} {prev_high:.4f}/{prev_low:.4f}",
            stop_loss=stop_loss, take_profit=take_profit,
        )


# ═══════════════════════════════════════════════════════════════════
# 3. 布林带均值回归
# ═══════════════════════════════════════════════════════════════════


class BollingerReversion(Strategy):
    name = "bollinger_reversion"
    base_confidence = 50
    description = "布林带均值回归（触下轨 = 买入；触上轨 = 卖出）"

    def __init__(self, period: int = 20, multiplier: float = 2.0):
        self.period = period
        self.multiplier = multiplier

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.period + 2:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        boll = calc_boll(closes, self.period, self.multiplier)
        upper = boll["upper"][-1]
        middle = boll["middle"][-1]
        lower = boll["lower"][-1]
        last = float(closes[-1])
        if np.isnan(upper) or np.isnan(lower):
            return None

        action = None
        if last <= lower:
            action = "buy"
        elif last >= upper:
            action = "sell"
        if action is None:
            return None

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, action, last, confidence,
            f"BOLL({self.period},{self.multiplier}) {'触下轨' if action == 'buy' else '触上轨'}",
            stop_loss=float(lower * 0.99) if action == "buy" else float(upper * 1.01),
            take_profit=float(middle),
        )


# ═══════════════════════════════════════════════════════════════════
# 4. RSI 背离（简化版：极值反转）
# ═══════════════════════════════════════════════════════════════════


class RSIDivergence(Strategy):
    name = "rsi_divergence"
    base_confidence = 50
    description = "RSI 极值反转（RSI<30 = 买入；RSI>70 = 卖出）"

    def __init__(self, period: int = 14, oversold: float = 30, overbought: float = 70):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.period + 2:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        rsi = calc_rsi(closes, self.period)
        last_rsi = float(rsi[-1])
        if np.isnan(last_rsi):
            return None

        action = None
        if last_rsi < self.oversold:
            action = "buy"
        elif last_rsi > self.overbought:
            action = "sell"
        if action is None:
            return None

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(closes[-1])
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"RSI({self.period}) = {last_rsi:.1f} {'超卖' if action == 'buy' else '超买'}",
        )


# ═══════════════════════════════════════════════════════════════════
# 5. 成交量突破
# ═══════════════════════════════════════════════════════════════════


class VolumeBreakout(Strategy):
    name = "volume_breakout"
    base_confidence = 55
    description = "成交量突破（量比 > 2 且收阳线 = 买入）"

    def __init__(self, ma_period: int = 20, multiplier: float = 2.0):
        self.ma_period = ma_period
        self.multiplier = multiplier

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.ma_period + 2:
            return None
        volumes = np.array([c.volume for c in candles], dtype=np.float64)
        vma = calc_volume_ma(volumes, self.ma_period)
        last_vol = float(volumes[-1])
        last_vma = float(vma[-1])
        if np.isnan(last_vma) or last_vma <= 0:
            return None
        ratio = last_vol / last_vma
        if ratio < self.multiplier:
            return None

        last_candle = candles[-1]
        action = "buy" if last_candle.close > last_candle.open else "sell"
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, action, last_candle.close, confidence,
            f"放量突破 量比={ratio:.2f} ({'阳线' if action == 'buy' else '阴线'})",
        )


# ═══════════════════════════════════════════════════════════════════
# 6. 新闻事件驱动
# ═══════════════════════════════════════════════════════════════════


class FlashEventStrategy(Strategy):
    name = "flash_event"
    base_confidence = 50
    description = "新闻事件驱动（高分新闻触发 + 价格动量验证）"

    def __init__(self, importance_threshold: int = 4):
        self.importance_threshold = importance_threshold

    def evaluate(self, symbol, market, candles, recent_news=None):
        if not recent_news or len(candles) < 20:
            return None
        # 找出 5 分钟内、importance >= threshold 且涉及该 symbol 的新闻
        cutoff = int(time.time() * 1000) - 5 * 60 * 1000
        matched = [
            n for n in recent_news
            if n.get("published_at", 0) >= cutoff
            and n.get("importance", 0) >= self.importance_threshold
            and symbol in (n.get("categories") or [])
        ]
        if not matched:
            return None

        n = matched[0]  # 最新一条
        sentiment = n.get("sentiment", "neutral")
        if sentiment == "neutral":
            return None
        action = "buy" if sentiment == "bullish" else "sell"

        importance = n.get("importance", 3)
        confidence = self.base_confidence + importance * 10 + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"事件驱动: {n.get('title', '')[:60]} (★{importance})",
            triggered_by={"flash_id": n.get("id")},
        )


# ═══════════════════════════════════════════════════════════════════
# 注册表
# ═══════════════════════════════════════════════════════════════════

ALL_STRATEGIES: Dict[str, type] = {
    "ma_cross": MACrossStrategy,
    "donchian_breakout": DonchianBreakout,
    "bollinger_reversion": BollingerReversion,
    "rsi_divergence": RSIDivergence,
    "volume_breakout": VolumeBreakout,
    "flash_event": FlashEventStrategy,
}


def get_strategy(name: str, **params) -> Optional[Strategy]:
    """实例化策略。未知名称返回 None。"""
    cls = ALL_STRATEGIES.get(name)
    if not cls:
        return None
    try:
        return cls(**params)
    except TypeError:
        # 参数不匹配，用默认参数
        return cls()


def list_strategies() -> List[Dict[str, Any]]:
    """返回所有策略的元信息（供前端展示选择）。"""
    return [
        {
            "name": cls.name,
            "base_confidence": cls.base_confidence,
            "description": cls.description,
        }
        for cls in ALL_STRATEGIES.values()
    ]
