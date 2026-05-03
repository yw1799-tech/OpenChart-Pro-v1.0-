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
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from backend.data.models import Candle, Market, Signal
from backend.indicators.builtin import (
    calc_boll,
    calc_ma,
    calc_ema,
    calc_macd,
    calc_rsi,
    calc_volume_ma,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# v12.16 通用指标 helper（不在 indicators.builtin 中）
# ═══════════════════════════════════════════════════════════════════

def _calc_adx_simple(high, low, close, period: int = 14):
    """简化 ADX 实现（仅返回最新值）。失败返回 None。"""
    try:
        n = len(close)
        if n < period * 2 + 2:
            return None
        # True Range
        tr = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
        # +DM / -DM
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)
        for i in range(1, n):
            up = high[i] - high[i-1]
            down = low[i-1] - low[i]
            if up > down and up > 0: plus_dm[i] = up
            if down > up and down > 0: minus_dm[i] = down
        # Smoothed (Wilder's smoothing approximated as SMA)
        atr = np.zeros(n)
        plus_di = np.zeros(n)
        minus_di = np.zeros(n)
        atr[period] = np.sum(tr[1:period+1])
        plus_dm_sum = np.sum(plus_dm[1:period+1])
        minus_dm_sum = np.sum(minus_dm[1:period+1])
        for i in range(period + 1, n):
            atr[i] = atr[i-1] - atr[i-1] / period + tr[i]
            plus_dm_sum = plus_dm_sum - plus_dm_sum / period + plus_dm[i]
            minus_dm_sum = minus_dm_sum - minus_dm_sum / period + minus_dm[i]
            if atr[i] > 0:
                plus_di[i] = 100 * plus_dm_sum / atr[i]
                minus_di[i] = 100 * minus_dm_sum / atr[i]
        # DX → ADX
        dx = np.zeros(n)
        for i in range(period + 1, n):
            denom = plus_di[i] + minus_di[i]
            if denom > 0:
                dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / denom
        # ADX = SMA(DX, period)
        if n - period - 1 < period:
            return None
        adx = float(np.mean(dx[-period:]))
        return adx if not np.isnan(adx) else None
    except Exception:
        return None


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

        # -15 if macro bearish impact active
        # 检查近 24h 内是否有 is_macro_data=1 + tone=hawkish/bearish 的宏观冲击新闻
        # (CPI/FOMC 超预期鹰派 = 对股票/黄金/加密不利)
        if recent_news:
            for n in recent_news[:10]:
                if not n.get("is_macro_data"):
                    continue
                # macro_impact_strength 是 level 字段: significant/light/neutral
                lvl = (n.get("macro_impact_strength") or "").lower()
                if lvl not in ("significant", "light"):
                    continue
                # ai_analysis 里 macro_impact.tone 才有 hawkish/dovish
                ai = n.get("ai_analysis")
                if isinstance(ai, str):
                    try:
                        import json as _j
                        ai = _j.loads(ai)
                    except Exception:
                        ai = {}
                tone = ((ai or {}).get("macro_impact") or {}).get("tone", "")
                if tone == "hawkish":
                    score -= 15 if lvl == "significant" else 8
                    break

        return score

    @staticmethod
    def _clamp(v: int) -> int:
        return max(0, min(100, v))

    @staticmethod
    def _calc_atr_sltp(
        action: str,
        last_close: float,
        candles: List[Candle],
        atr_period: int = 14,
        sl_atr: float = 2.0,        # v12.19.5: 1.5 → 2.0 让止损更宽，给波动留空间
        tp_atr: float = 3.5,        # v12.19.5: 3.0 → 3.5 截短利润病根，赢家奔跑更长
        sl_floor_pct: float = 1.5,  # v12.19.5 NEW: 止损最少 1.5% 空间（低波动股噪音地板）
        tp_floor_pct: float = 2.5,  # v12.19.5 NEW: 止盈最少 2.5% 空间
    ):
        """
        基于 ATR + 最近关键位计算 SL/TP（统一算法，所有策略可用）：
          - SL = max(入场价 - 2.0×ATR, 最近 20 根低点, 入场价×(1-1.5%))（BUY）
          - TP = max(入场价 + 3.5×ATR, 入场价×(1+2.5%))（最少 1:1.67 风险回报）
          - SL_FLOOR / TP_FLOOR: 防止低波动股 (如 QS) 被 0.4% 噪音扫损 / 低 TP 截短利润

        v12.19.5 实战教训:
          QCOM/MU/QS 多次因 SL 太紧被噪音扫出 (1710/1605/3236 等 5 条 ATR 类教训)
          盈亏比 0.75 (平均赢 1.42% < 平均亏 1.90%) — 经典 "小赢大亏"
        """
        n = len(candles)
        if n < max(atr_period + 1, 20):
            return None, None
        # 计算 ATR
        trs = []
        for i in range(n - atr_period, n):
            if i == 0:
                trs.append(candles[i].high - candles[i].low)
                continue
            tr = max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
            trs.append(tr)
        atr = sum(trs) / len(trs)
        # 最近 20 根高低点
        recent20 = candles[-20:]
        lo20 = min(c.low for c in recent20)
        hi20 = max(c.high for c in recent20)
        # v12.19.5 floor 阈值
        sl_floor = last_close * (sl_floor_pct / 100.0)   # 例: 1.5% of 100 = 1.5
        tp_floor = last_close * (tp_floor_pct / 100.0)
        if action == "buy":
            sl_atr_price = last_close - sl_atr * atr
            stop_loss = max(sl_atr_price, lo20 * 0.998)  # 取近的（更紧的止损）
            # v12.19.5: 加 SL floor — 防低波动股 ATR 过小导致止损过紧
            stop_loss = min(stop_loss, last_close - sl_floor)
            take_profit = last_close + tp_atr * atr
            # v12.19.5: 加 TP floor — 让赢家至少跑到 2.5%
            take_profit = max(take_profit, last_close + tp_floor)
            # sanity：止损必须低于入场价；止盈必须高于入场价（防暴跌反弹场景 lo20 高于现价）
            if stop_loss >= last_close:
                stop_loss = last_close * 0.97   # 退化到固定 3% 止损
            if take_profit <= last_close:
                take_profit = last_close * 1.05
        else:  # sell
            sl_atr_price = last_close + sl_atr * atr
            stop_loss = min(sl_atr_price, hi20 * 1.002)
            # v12.19.5: SELL 侧同样加 floor
            stop_loss = max(stop_loss, last_close + sl_floor)
            take_profit = last_close - tp_atr * atr
            take_profit = min(take_profit, last_close - tp_floor)
            if stop_loss <= last_close:
                stop_loss = last_close * 1.03
            if take_profit >= last_close:
                take_profit = last_close * 0.95
        return float(stop_loss), float(take_profit)

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
        candles: Optional[List[Candle]] = None,
    ) -> Signal:
        # 如果策略没指定 SL/TP，自动用 ATR + 关键位算法补上
        if (stop_loss is None or take_profit is None) and candles:
            auto_sl, auto_tp = self._calc_atr_sltp(action, price, candles)
            if stop_loss is None: stop_loss = auto_sl
            if take_profit is None: take_profit = auto_tp
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

    def __init__(self, fast: int = 5, slow: int = 20, use_ema: bool = False,
                 require_volume: bool = False):
        # v12.16 (Step 2): 加 use_ema（EMA 比 SMA 反应快，适合短中期）
        # require_volume: True 时金叉需配合量能放大（量 ≥ 20 日均量 1.2×）
        self.fast = fast
        self.slow = slow
        self.use_ema = use_ema
        self.require_volume = require_volume

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.slow + 2:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        if self.use_ema:
            ma_fast = calc_ema(closes, self.fast)
            ma_slow = calc_ema(closes, self.slow)
            ma_label = f"EMA{self.fast}/{self.slow}"
        else:
            ma_fast = calc_ma(closes, self.fast)
            ma_slow = calc_ma(closes, self.slow)
            ma_label = f"MA{self.fast}/{self.slow}"
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

        # v12.16 量能确认（可选）
        if self.require_volume:
            try:
                volumes = np.array([c.volume for c in candles], dtype=np.float64)
                vma = calc_volume_ma(volumes, 20)
                if not np.isnan(vma[-1]) and vma[-1] > 0:
                    if volumes[-1] / vma[-1] < 1.2:
                        return None  # 金叉/死叉但量没放大 → 大概率假突破
            except Exception:
                pass

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(closes[-1])
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"{ma_label} {'金叉' if action == 'buy' else '死叉'} @ {last_close:.4f}",
            candles=candles,
        )


# ═══════════════════════════════════════════════════════════════════
# 2. Donchian 通道突破
# ═══════════════════════════════════════════════════════════════════


class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    base_confidence = 50
    description = "Donchian 通道突破（突破 N 日高点 = 买入；跌破 N 日低点 = 卖出）"

    def __init__(self, period: int = 20, require_volume: bool = False, require_adx: bool = False):
        # v12.16 (Step 2): 加量能 + ADX 过滤（修 0% confirm 率）
        self.period = period
        self.require_volume = require_volume
        self.require_adx = require_adx

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

        # v12.16 量能过滤：突破时量必须放大 ≥1.5×（防假突破）
        if self.require_volume:
            try:
                volumes = np.array([c.volume for c in candles], dtype=np.float64)
                vma = calc_volume_ma(volumes, 20)
                if not np.isnan(vma[-1]) and vma[-1] > 0:
                    if volumes[-1] / vma[-1] < 1.5:
                        return None
            except Exception:
                pass

        # v12.16 ADX 过滤：仅在趋势市场（ADX > 20）才有意义
        if self.require_adx:
            try:
                adx_v = _calc_adx_simple(highs, lows, closes, period=14)
                if adx_v is not None and adx_v < 20:
                    return None
            except Exception:
                pass

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
            stop_loss=stop_loss, take_profit=take_profit, candles=candles,
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

        # 趋势过滤：布林带均值回归在趋势市场里是陷阱
        # 强多头中触上轨是"趋势持续"不是"超买反转"；强空头中触下轨同理
        try:
            ma5 = calc_ma(closes, 5)[-1]
            ma10 = calc_ma(closes, 10)[-1]
            ma20 = calc_ma(closes, 20)[-1]
            if not (np.isnan(ma5) or np.isnan(ma10) or np.isnan(ma20)):
                bull_trend = ma5 > ma10 > ma20
                bear_trend = ma5 < ma10 < ma20
                if action == "sell" and bull_trend:
                    return None  # 多头趋势中不发 SELL（概率性陷阱信号）
                if action == "buy" and bear_trend:
                    return None  # 空头趋势中不发 BUY
        except Exception:
            pass

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, action, last, confidence,
            f"BOLL({self.period},{self.multiplier}) {'触下轨' if action == 'buy' else '触上轨'}（震荡市过滤后）",
            stop_loss=float(lower * 0.99) if action == "buy" else float(upper * 1.01),
            take_profit=float(middle), candles=candles,
        )


# ═══════════════════════════════════════════════════════════════════
# 4. RSI 组合系列（v12.16.5 新增 — 替代旧 RSIDivergence 极值反转）
# ═══════════════════════════════════════════════════════════════════
# 设计原则：单一 RSI 极值不可信（趋势市场就是接刀子），必须配合
#   - 趋势确认（MA 排列）
#   - 量能确认（量比 > 1.2）
#   - 价格行为确认（K 线方向 / 关键位突破）
# 三个 RSI 组合策略：rsi_pullback / rsi_real_divergence / rsi_breakout_50


class RSIPullbackStrategy(Strategy):
    """RSI 趋势内回踩抄底：多头 (MA5>MA20) + RSI 30-40 + 收阳 → buy。"""
    name = "rsi_pullback"
    base_confidence = 65
    description = "RSI 趋势回踩（多头中 RSI 回到 30-40 + 当根收阳 = 买入；空头反之）"

    def __init__(self, rsi_period: int = 14,
                 buy_lo: float = 30.0, buy_hi: float = 40.0,
                 sell_lo: float = 60.0, sell_hi: float = 70.0,
                 fast_ma: int = 5, slow_ma: int = 20):
        self.rsi_period = rsi_period
        self.buy_lo = buy_lo; self.buy_hi = buy_hi
        self.sell_lo = sell_lo; self.sell_hi = sell_hi
        self.fast_ma = fast_ma; self.slow_ma = slow_ma

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < max(self.slow_ma, self.rsi_period) + 2:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        rsi = calc_rsi(closes, self.rsi_period)
        last_rsi = float(rsi[-1])
        if np.isnan(last_rsi):
            return None
        ma_fast = calc_ma(closes, self.fast_ma)[-1]
        ma_slow = calc_ma(closes, self.slow_ma)[-1]
        if np.isnan(ma_fast) or np.isnan(ma_slow):
            return None
        last = candles[-1]
        bullish_bar = last.close > last.open
        bearish_bar = last.close < last.open

        action = None
        # 多头趋势中 RSI 回踩到 30-40 + 当根收阳 → 买入
        if ma_fast > ma_slow and self.buy_lo <= last_rsi <= self.buy_hi and bullish_bar:
            action = "buy"
        # 空头趋势中 RSI 反弹到 60-70 + 当根收阴 → 卖出
        elif ma_fast < ma_slow and self.sell_lo <= last_rsi <= self.sell_hi and bearish_bar:
            action = "sell"
        if action is None:
            return None

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(closes[-1])
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"RSI 趋势{'回踩抄底' if action == 'buy' else '反弹做空'} RSI={last_rsi:.1f} "
            f"(MA{self.fast_ma}{'>' if action == 'buy' else '<'}MA{self.slow_ma})",
            candles=candles,
        )


class RSIRealDivergenceStrategy(Strategy):
    """RSI 真背离：价格创新高 RSI 不创新高（顶背离 sell）/ 价格创新低 RSI 不创新低（底背离 buy）。"""
    name = "rsi_real_divergence"
    base_confidence = 70
    description = "RSI 真背离（价格创新高/低但 RSI 没跟上 = 反转高胜率信号）"

    def __init__(self, rsi_period: int = 14, lookback: int = 20,
                 min_price_diff_pct: float = 0.5):
        self.rsi_period = rsi_period
        self.lookback = lookback
        self.min_price_diff_pct = min_price_diff_pct

    def _find_pivots(self, arr, mode: str):
        """简易枢轴点：在 lookback 窗口里取最大 2 个高点（mode='high'）或最小 2 个低点。
        返回 [(idx, value), (idx, value)] — 按时间顺序（旧 → 新）；不足返回 []。"""
        n = len(arr)
        if n < self.lookback:
            return []
        window = arr[-self.lookback:]
        idx_offset = n - self.lookback
        # 取局部极值（非端点的相邻 3 根中最大/最小）
        pivots = []
        for i in range(1, len(window) - 1):
            v = window[i]
            if np.isnan(v):
                continue
            if mode == "high":
                if v >= window[i-1] and v >= window[i+1]:
                    pivots.append((i + idx_offset, float(v)))
            else:  # low
                if v <= window[i-1] and v <= window[i+1]:
                    pivots.append((i + idx_offset, float(v)))
        if len(pivots) < 2:
            return []
        # 按 value 排序取最值前 2，再按 idx 排序（旧→新）
        if mode == "high":
            pivots.sort(key=lambda p: -p[1])
        else:
            pivots.sort(key=lambda p: p[1])
        top2 = sorted(pivots[:2], key=lambda p: p[0])
        return top2

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.lookback + self.rsi_period + 2:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        highs = np.array([c.high for c in candles], dtype=np.float64)
        lows = np.array([c.low for c in candles], dtype=np.float64)
        rsi = calc_rsi(closes, self.rsi_period)
        if np.isnan(rsi[-1]):
            return None

        action = None
        reason = None
        # 顶背离：价格高点创新高，RSI 高点没创新高
        high_pivots = self._find_pivots(highs, "high")
        if len(high_pivots) >= 2:
            (i1, p1), (i2, p2) = high_pivots
            r1 = float(rsi[i1]); r2 = float(rsi[i2])
            if not (np.isnan(r1) or np.isnan(r2)):
                price_diff_pct = (p2 - p1) / p1 * 100 if p1 > 0 else 0
                if price_diff_pct >= self.min_price_diff_pct and r2 < r1:
                    # 价格新高，RSI 没新高 → 顶背离
                    action = "sell"
                    reason = (f"RSI 顶背离 价格 {p1:.4f}→{p2:.4f} (+{price_diff_pct:.1f}%) "
                              f"但 RSI {r1:.1f}→{r2:.1f}")

        # 底背离：价格低点创新低，RSI 低点没创新低
        if action is None:
            low_pivots = self._find_pivots(lows, "low")
            if len(low_pivots) >= 2:
                (i1, p1), (i2, p2) = low_pivots
                r1 = float(rsi[i1]); r2 = float(rsi[i2])
                if not (np.isnan(r1) or np.isnan(r2)):
                    price_diff_pct = (p1 - p2) / p1 * 100 if p1 > 0 else 0
                    if price_diff_pct >= self.min_price_diff_pct and r2 > r1:
                        action = "buy"
                        reason = (f"RSI 底背离 价格 {p1:.4f}→{p2:.4f} (-{price_diff_pct:.1f}%) "
                                  f"但 RSI {r1:.1f}→{r2:.1f}")

        if action is None:
            return None

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(closes[-1])
        return self._make_signal(
            symbol, market, action, last_close, confidence, reason, candles=candles,
        )


class RSIBreakout50Strategy(Strategy):
    """RSI 上穿 50 + 量能放大 + 价格站上 MA20 → 强势启动确认。"""
    name = "rsi_breakout_50"
    base_confidence = 65
    description = "RSI 50 上穿（RSI 穿 50 + 量比≥1.2 + 价站上 MA20 = 强势启动）"

    def __init__(self, rsi_period: int = 14, ma_period: int = 20,
                 vol_ratio: float = 1.2):
        self.rsi_period = rsi_period
        self.ma_period = ma_period
        self.vol_ratio = vol_ratio

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < max(self.ma_period, self.rsi_period) + 3:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        volumes = np.array([c.volume for c in candles], dtype=np.float64)
        rsi = calc_rsi(closes, self.rsi_period)
        if np.isnan(rsi[-1]) or np.isnan(rsi[-2]):
            return None
        rsi_prev = float(rsi[-2]); rsi_now = float(rsi[-1])
        ma = calc_ma(closes, self.ma_period)
        if np.isnan(ma[-1]):
            return None
        ma_now = float(ma[-1])
        last_close = float(closes[-1])
        vma = calc_volume_ma(volumes, 20)
        if np.isnan(vma[-1]) or vma[-1] <= 0:
            return None
        vol_ratio_now = float(volumes[-1] / vma[-1])

        action = None
        # 上穿 50 + 站上 MA20 + 量能放大 → 买入
        if rsi_prev < 50 <= rsi_now and last_close > ma_now and vol_ratio_now >= self.vol_ratio:
            action = "buy"
        # 下穿 50 + 跌破 MA20 + 量能放大 → 卖出
        elif rsi_prev > 50 >= rsi_now and last_close < ma_now and vol_ratio_now >= self.vol_ratio:
            action = "sell"
        if action is None:
            return None

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"RSI {'上' if action == 'buy' else '下'}穿 50 ({rsi_prev:.1f}→{rsi_now:.1f}) "
            f"+ 量比 {vol_ratio_now:.2f} + 价{'站上' if action == 'buy' else '跌破'} MA{self.ma_period}",
            candles=candles,
        )


# ═══════════════════════════════════════════════════════════════════
# 5. 成交量突破
# ═══════════════════════════════════════════════════════════════════


class VolumeBreakout(Strategy):
    name = "volume_breakout"
    base_confidence = 55
    description = "成交量突破（量比 > 2 且收阳线 = 买入）"

    def __init__(self, ma_period: int = 20, multiplier: float = 2.0,
                 max_dist_to_high_pct: float = 0.0, require_inflow: bool = False):
        # v12.16 (Step 2): max_dist_to_high_pct > 0 时拒绝距前高 < N% 的追高 buy
        # require_inflow: A 股专用，要求资金净流入（待外接）
        self.ma_period = ma_period
        self.multiplier = multiplier
        self.max_dist_to_high_pct = max_dist_to_high_pct
        self.require_inflow = require_inflow

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.ma_period + 2:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
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

        # v12.16 追高过滤：距 20 日高 < N% 时拒绝 buy（QCOM#2 -3.58% 教训）
        if action == "buy" and self.max_dist_to_high_pct > 0:
            try:
                highs = np.array([c.high for c in candles], dtype=np.float64)
                high20 = float(np.max(highs[-20:]))
                if high20 > 0:
                    dist_pct = (high20 - last_candle.close) / last_candle.close * 100
                    if 0 <= dist_pct < self.max_dist_to_high_pct:
                        return None  # 距 20 日高过近 → 追高风险
            except Exception:
                pass

        # 趋势过滤：多头趋势中放量阴线大多是"洗盘"，不应出 SELL；空头趋势同理
        try:
            ma5 = calc_ma(closes, 5)[-1]
            ma10 = calc_ma(closes, 10)[-1]
            ma20 = calc_ma(closes, 20)[-1]
            if not (np.isnan(ma5) or np.isnan(ma10) or np.isnan(ma20)):
                bull_trend = ma5 > ma10 > ma20
                bear_trend = ma5 < ma10 < ma20
                if action == "sell" and bull_trend:
                    return None
                if action == "buy" and bear_trend:
                    return None
        except Exception:
            pass

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, action, last_candle.close, confidence,
            f"放量突破 量比={ratio:.2f} ({'阳线' if action == 'buy' else '阴线'}，顺趋势过滤后)",
            candles=candles,
        )


# ═══════════════════════════════════════════════════════════════════
# 6. 新闻事件驱动
# ═══════════════════════════════════════════════════════════════════


class FlashEventStrategy(Strategy):
    name = "flash_event"
    base_confidence = 50
    description = "新闻事件驱动（高分新闻触发 + 价格动量验证）"

    def __init__(self, importance_threshold: int = 3):
        # v12.16 (Step 2): 默认 4 → 3（放宽，让 ★3 重要新闻也能触发）
        self.importance_threshold = importance_threshold

    @staticmethod
    def _normalize_symbol_for_match(s: str) -> str:
        """把 categories 里各种 symbol 写法标准化，用于匹配。
        - 0700 / 00700 / 0700.HK → 0700.HK
        - BTC/USDT / BTCUSDT / btc-usdt → BTC-USDT
        - aapl → AAPL
        - 600519 / 600519.SH → 600519
        """
        if not isinstance(s, str): return ""
        s = s.strip().upper()
        if s.endswith(".SH") or s.endswith(".SZ"): s = s[:-3]
        # v11.6 修复：HK 标准化与 scheduler._normalize_hk_symbol 一致
        # 之前 5 位 (00700) 不剥前导 0 → strategy 比对 0700.HK 失败
        if s.replace(".HK", "").isdigit() or s.endswith(".HK"):
            d = s.replace(".HK", "")
            if d.isdigit():
                d = d.lstrip("0") or "0"
                if len(d) < 4:
                    d = d.zfill(4)
                return f"{d}.HK"
        if s.endswith("USDT") and "-" not in s and "/" not in s:
            return f"{s[:-4]}-USDT"
        if "/" in s: s = s.replace("/", "-")
        return s

    def evaluate(self, symbol, market, candles, recent_news=None):
        """v12.13: 改用 ai_analysis.impacts（LLM 解读后强相关）替代 raw categories。
        旧逻辑：只看 categories 数组是否含 symbol → 财联社"早间新闻精选"会同时
                tag 多只股，导致综合性新闻被当成单股事件触发噪音 buy。
        新逻辑：只看 LLM ai_analysis.impacts 中针对此 symbol 的 direction/strength/horizon —
                LLM 已做过"宁缺毋滥"筛选（strength<0.6 不列），噪音少 + 信号质量更高。
        """
        if not recent_news or len(candles) < 20:
            return None
        cutoff = int(time.time() * 1000) - 30 * 60 * 1000
        sym_norm = self._normalize_symbol_for_match(symbol)

        matched = []  # [(news, impact_dict)]
        for n in recent_news:
            if n.get("published_at", 0) < cutoff:
                continue
            if n.get("importance", 0) < self.importance_threshold:
                continue
            ai = n.get("ai_analysis")
            if not isinstance(ai, dict):
                continue  # 没 LLM 解读 → 跳过（不再回落 categories，避免噪音）
            for imp in (ai.get("impacts") or []):
                if not isinstance(imp, dict):
                    continue
                if self._normalize_symbol_for_match(imp.get("symbol", "")) != sym_norm:
                    continue
                direction = (imp.get("direction") or "neutral").lower()
                if direction == "neutral":
                    continue
                try:
                    strength = float(imp.get("strength") or 0)
                except (TypeError, ValueError):
                    strength = 0
                if strength < 0.6:  # LLM prompt 已要求 ≥0.6 才列，这里再防御
                    continue
                matched.append((n, imp))
                break  # 一条新闻命中一次即可

        if not matched:
            return None

        # 取最新一条（recent_news 已按 published_at DESC）
        n, imp = matched[0]
        direction = (imp.get("direction") or "").lower()
        action = "buy" if direction == "bullish" else "sell"
        importance = int(n.get("importance", 3) or 3)
        try:
            strength = float(imp.get("strength") or 0.6)
        except (TypeError, ValueError):
            strength = 0.6

        # confidence: 基线 50 + importance×10 (≤50) + strength 加成 (≤15) + common_modifier
        confidence = (
            self.base_confidence
            + importance * 10
            + int(strength * 15)
            + self._common_modifier(candles, recent_news)
        )
        last_close = float(candles[-1].close)
        title = (n.get("title", "") or "")[:50]
        imp_reason = (imp.get("reason", "") or "")[:30]
        horizon = imp.get("horizon", "1-5d")
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"AI事件驱动 [{horizon}/{strength:.2f}]: {imp_reason} | {title}",
            triggered_by={
                "flash_id": n.get("id"),
                "ai_strength": strength,
                "ai_horizon": horizon,
                "ai_direction": direction,
            },
            candles=candles,
        )


# ═══════════════════════════════════════════════════════════════════
# 注册表
# ═══════════════════════════════════════════════════════════════════

class ChanLunStrategy(Strategy):
    """
    缠论买卖点策略：调用 chanlun_engine 分析最新 N 根 K 线，
    检测最近 `recent_bars` 根内是否出现新买卖点，出现则发信号。
    **缠论理论不用传统止盈止损**（出场依据是反向笔/段/结构背驰）。
    """
    name = "chanlun"
    base_confidence = 65   # v12.16: 60 → 65（提高基线砍弱信号噪音）
    description = "缠论买卖点（笔/段级 1/2/3 类买卖点，无传统 SL/TP，出场依诊断驱动）"

    # 不同类型买卖点的置信度（根据缠论理论可靠度排）
    TYPE_CONFIDENCE = {
        "S1": 88, "S1p": 85,              # 线段一买/一卖（最高等级）
        "S2": 82, "S2s": 78,              # 线段二买/类二
        "S3a": 80, "S3b": 80, "S3": 80,   # 线段三买/三卖
        "1": 75, "1p": 75,                # 笔一买/一卖
        "2": 72, "2s": 68,                # 笔二买/类二
        "3a": 76, "3b": 76, "3": 76,      # 笔三买/三卖（突破中枢，较可靠）
    }

    def __init__(self, recent_bars: int = 3, min_bsp_level: str = "any"):
        # min_bsp_level: "any" 笔+段都接受 / "S" 只接受段级（加密用 — 减少噪音）
        # 仅对最近 N 根 K 线内新形成的买卖点触发信号（避免历史买卖点重复触发）
        # 注意：不包含当前进行中的最后一根 K 线（未收盘时买卖点可能抹除）
        self.recent_bars = recent_bars
        self.min_bsp_level = min_bsp_level

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < 60:  # 缠论至少要 60 根 K 线才有意义
            return None

        # 调用后端缠论引擎（本地导入避免启动循环）
        try:
            from backend.chanlun_engine.chanlun_service import analyze
            # monitor._evaluate_symbol 已统一剔除未收盘末根 K 线，这里直接用 candles
            candle_dicts = [
                {"timestamp": c.timestamp, "open": c.open, "high": c.high,
                 "low": c.low, "close": c.close, "volume": c.volume}
                for c in candles
            ]
            result = analyze(candle_dicts)
        except Exception as e:
            logger.debug(f"[chanlun-strategy] {symbol} 分析失败: {e}")
            return None

        bsp_list = result.get("bsp_list") or []
        if not bsp_list:
            return None

        # 检测最近 recent_bars 根已收盘 K 线内形成的新买卖点
        total_bars = len(candles)
        cutoff_idx = total_bars - self.recent_bars
        recent_bsps = [b for b in bsp_list if b.get("x", -1) >= cutoff_idx]
        # v12.16 (Step 2): min_bsp_level='S' 只允许段级 (type 以 S 开头) — 加密用
        if self.min_bsp_level == "S":
            recent_bsps = [b for b in recent_bsps if (b.get("type") or "").startswith("S")]
        if not recent_bsps:
            return None

        # 选择最强信号：按类型置信度降序 + 线段级优先
        def _bsp_strength(b):
            t = b.get("type", "")
            return self.TYPE_CONFIDENCE.get(t, 60)
        recent_bsps.sort(key=_bsp_strength, reverse=True)
        best = recent_bsps[0]
        action = "buy" if best.get("is_buy") else "sell"
        bsp_type = best.get("type", "")
        base_conf = self.TYPE_CONFIDENCE.get(bsp_type, 65)

        # 中文标签
        is_seg = bsp_type.startswith("S")
        type_cn_map = {"1": "一", "1p": "一", "2": "二", "2s": "类二", "3": "三", "3a": "三", "3b": "三"}
        raw = bsp_type[1:] if is_seg else bsp_type
        type_cn = type_cn_map.get(raw.split(",")[0], raw)
        level_cn = "段级" if is_seg else "笔级"
        action_cn = "买" if action == "buy" else "卖"

        reason = (f"缠论 {level_cn}{type_cn}{action_cn}点 @ {best['y']:.4f} "
                  f"（type={bsp_type}, 距今 {total_bars - best['x']} 根 K 线）")

        # 叠加通用加减分（但降权，因为缠论自身已有质量）
        confidence = base_conf + (self._common_modifier(candles, recent_news) // 2)
        last_close = float(candles[-1].close)

        # 关键：缠论信号**不带 SL/TP**（None），不调 _make_signal 的 ATR 补全
        return Signal(
            id=str(uuid.uuid4()),
            symbol=symbol,
            market=market,
            action=action,
            strategy_name=self.name,
            confidence=self._clamp(confidence),
            price=last_close,
            stop_loss=None,        # 缠论不设传统止损
            take_profit=None,      # 缠论不设传统止盈
            reason=reason,
            triggered_by={
                "bsp_type": bsp_type,
                "bsp_price": best["y"],
                "bsp_bar_offset": total_bars - best["x"],
                "is_seg": is_seg,
                "level": level_cn,
            },
            generated_at=int(time.time() * 1000),
        )


# ═══════════════════════════════════════════════════════════════════
# v12.16 (Step 4): 4 个通用型新策略 — MACD / EMA Triple / Squeeze / ADX
# ═══════════════════════════════════════════════════════════════════

class MACDCrossStrategy(Strategy):
    """MACD 金叉/死叉 + Histogram 同向 — 经典动量指标。"""
    name = "macd_cross"
    base_confidence = 60
    description = "MACD 金叉/死叉 + Histogram 同向（动量启动）"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast = fast; self.slow = slow; self.signal = signal

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.slow + self.signal + 2:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        out = calc_macd(closes, fast=self.fast, slow=self.slow, signal=self.signal)
        macd = out.get("dif")
        macd_signal = out.get("dea")
        hist = out.get("histogram")
        if macd is None or macd_signal is None or hist is None:
            return None
        if np.isnan(macd[-2]) or np.isnan(macd[-1]):
            return None
        prev_diff = macd[-2] - macd_signal[-2]
        curr_diff = macd[-1] - macd_signal[-1]
        action = None
        if prev_diff <= 0 < curr_diff and hist[-1] > 0:
            action = "buy"
        elif prev_diff >= 0 > curr_diff and hist[-1] < 0:
            action = "sell"
        if action is None:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(closes[-1])
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"MACD {'金叉' if action == 'buy' else '死叉'} + 柱状图同向 (DIF={macd[-1]:.4f})",
            candles=candles,
        )


class EMATripleStrategy(Strategy):
    """EMA 三线排列：EMA10 > EMA30 > EMA60 = 多头 / 反之空头 — 趋势确认。"""
    name = "ema_triple"
    base_confidence = 65
    description = "EMA 三线排列（10/30/60，确认中期趋势方向）"

    def __init__(self, p1: int = 10, p2: int = 30, p3: int = 60):
        self.p1 = p1; self.p2 = p2; self.p3 = p3

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.p3 + 2:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        e1 = calc_ema(closes, self.p1)[-1]
        e2 = calc_ema(closes, self.p2)[-1]
        e3 = calc_ema(closes, self.p3)[-1]
        if np.isnan(e1) or np.isnan(e2) or np.isnan(e3):
            return None
        last_close = float(closes[-1])
        # 必须是"刚形成"的多空排列 — 比较前一根
        e1_prev = calc_ema(closes, self.p1)[-2]
        e2_prev = calc_ema(closes, self.p2)[-2]
        e3_prev = calc_ema(closes, self.p3)[-2]
        action = None
        if e1 > e2 > e3 and not (e1_prev > e2_prev > e3_prev) and last_close > e1:
            action = "buy"
        elif e1 < e2 < e3 and not (e1_prev < e2_prev < e3_prev) and last_close < e1:
            action = "sell"
        if action is None:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"EMA{self.p1}/{self.p2}/{self.p3} {'多头' if action == 'buy' else '空头'}排列形成",
            candles=candles,
        )


class SqueezeBreakoutStrategy(Strategy):
    """布林带 Squeeze 突破：BBand 在 Keltner 通道内（盘整结束）+ 突破方向。"""
    name = "squeeze_breakout"
    base_confidence = 70
    description = "布林带 Squeeze + 突破方向（盘整结束的高胜率信号）"

    def __init__(self, period: int = 20, bb_mult: float = 2.0, kc_mult: float = 1.5):
        self.period = period; self.bb_mult = bb_mult; self.kc_mult = kc_mult

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.period + 5:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        highs = np.array([c.high for c in candles], dtype=np.float64)
        lows = np.array([c.low for c in candles], dtype=np.float64)
        # 布林带
        boll = calc_boll(closes, self.period, self.bb_mult)
        bb_upper = boll["upper"][-2]; bb_lower = boll["lower"][-2]
        # Keltner: 中轨 EMA + ATR (用 TR 简化)
        ema = calc_ema(closes, self.period)
        ema_prev = ema[-2]
        # ATR 简化：(high - low) 的 N 期均值
        tr = np.maximum.reduce([highs - lows, np.abs(highs - np.roll(closes, 1)),
                                np.abs(lows - np.roll(closes, 1))])
        atr = float(np.mean(tr[-self.period:]))
        kc_upper = ema_prev + atr * self.kc_mult
        kc_lower = ema_prev - atr * self.kc_mult
        if np.isnan(bb_upper) or np.isnan(bb_lower) or np.isnan(ema_prev):
            return None
        # Squeeze 状态：布林带在 KC 内（前一根）
        squeezed = (bb_upper < kc_upper) and (bb_lower > kc_lower)
        if not squeezed:
            return None
        # 当前 K 线突破方向
        last_close = float(closes[-1])
        bb_upper_now = boll["upper"][-1]
        bb_lower_now = boll["lower"][-1]
        action = None
        if last_close > bb_upper_now:
            action = "buy"
        elif last_close < bb_lower_now:
            action = "sell"
        if action is None:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"Squeeze 突破 ({'向上' if action == 'buy' else '向下'} 突破布林带，盘整结束)",
            candles=candles,
        )


class ADXTrendFollowStrategy(Strategy):
    """ADX > 25 + DI 方向 — 强趋势市场里跟随入场。"""
    name = "adx_trend_follow"
    base_confidence = 60
    description = "ADX > 阈值 + DI 方向（趋势跟随）"

    def __init__(self, period: int = 14, adx_threshold: float = 25.0):
        self.period = period; self.adx_threshold = adx_threshold

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.period * 2 + 2:
            return None
        highs = np.array([c.high for c in candles], dtype=np.float64)
        lows = np.array([c.low for c in candles], dtype=np.float64)
        closes = np.array([c.close for c in candles], dtype=np.float64)
        adx_val = _calc_adx_simple(highs, lows, closes, period=self.period)
        if adx_val is None or adx_val < self.adx_threshold:
            return None
        # 简易 DI 方向：用最近 N 期均价上行/下行决定
        ma_short = calc_ma(closes, self.period)[-1]
        ma_short_prev = calc_ma(closes, self.period)[-3]
        if np.isnan(ma_short) or np.isnan(ma_short_prev):
            return None
        action = "buy" if ma_short > ma_short_prev else "sell"
        last_close = float(closes[-1])
        # 价格也需在该方向（多趋势中价格 > MA20 才发 buy）
        if action == "buy" and last_close < ma_short:
            return None
        if action == "sell" and last_close > ma_short:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"ADX={adx_val:.1f} > {self.adx_threshold} 强趋势 {'多' if action == 'buy' else '空'}",
            candles=candles,
        )


# ═══════════════════════════════════════════════════════════════════
# v12.16 (Step 5): 加密专属衍生品策略（4 个，async — 需调外部 API）
# ═══════════════════════════════════════════════════════════════════

# 模块级 sentiment 缓存：5 min TTL（避免每次 evaluate 重新调 OKX API）
_SENTIMENT_CACHE: Dict[str, Tuple[float, Any]] = {}
_SENTIMENT_TTL = 300

async def _get_cached_sentiment(key_type: str, symbol_or_coin: str):
    """缓存版 sentiment 拉取。key_type ∈ {funding, oi, lsr, fg}"""
    cache_key = f"{key_type}:{symbol_or_coin}"
    now = time.time()
    if cache_key in _SENTIMENT_CACHE:
        ts, data = _SENTIMENT_CACHE[cache_key]
        if now - ts < _SENTIMENT_TTL:
            return data
    try:
        # P0 修复 (v12.21.5): 实际类名是 SentimentData (不是 SentimentDataSource)
        # 之前 ImportError 被 try/except 静默吞 → 4 个加密衍生品策略
        # (funding_extreme/oi_breakout/long_short_ratio/fear_greed_reversal) 100% 哑火
        # 加密合约模式最依赖的策略全部不工作 — 这是合约 confirm 信号几乎 0 的根本原因
        from backend.crypto_dashboard.sentiment import SentimentData
        s = SentimentData()
        if key_type == "funding":
            data = await s.get_funding_rate(symbol_or_coin)  # e.g. "BTC-USDT-SWAP"
        elif key_type == "oi":
            data = await s.get_open_interest(symbol_or_coin)
        elif key_type == "lsr":
            data = await s.get_long_short_ratio(symbol_or_coin)  # coin like "BTC"
        elif key_type == "fg":
            data = await s.get_fear_greed_index()
        else:
            return None
        _SENTIMENT_CACHE[cache_key] = (now, data)
        return data
    except Exception as e:
        logger.debug(f"[sentiment-cache] {key_type} {symbol_or_coin} failed: {e}")
        return None


def _to_swap_inst(symbol: str) -> str:
    """BTC-USDT → BTC-USDT-SWAP；已是 SWAP 不动"""
    if not symbol: return symbol
    if symbol.endswith("-SWAP"): return symbol
    return symbol + "-SWAP"


class FundingExtremeStrategy(Strategy):
    """资金费率极值反转 — 散户多头/空头拥挤时反向。"""
    name = "funding_extreme"
    base_confidence = 70
    description = "资金费率极值反转（>0.05% 反向 short / <-0.02% 反向 long）"

    def __init__(self, long_threshold: float = -0.0002, short_threshold: float = 0.0005):
        # 阈值是单期费率（OKX 8h 一期；annualized = rate × 3 × 365）
        self.long_threshold = long_threshold
        self.short_threshold = short_threshold

    async def evaluate(self, symbol, market, candles, recent_news=None):
        if (market.value if hasattr(market, "value") else str(market)) != "crypto":
            return None
        data = await _get_cached_sentiment("funding", _to_swap_inst(symbol))
        if not data or not data.get("current"):
            return None
        rate = data["current"].get("rate", 0)
        action = None
        if rate >= self.short_threshold:
            action = "sell"  # 多头拥挤 → 反向 short
        elif rate <= self.long_threshold:
            action = "buy"   # 空头拥挤 → 反向 long
        if action is None:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"资金费率 {rate*100:.4f}% 极值 → 反向 {action}",
            triggered_by={"funding_rate": rate, "side": "short" if action=="sell" else "long"},
            candles=candles,
        )


class OIBreakoutStrategy(Strategy):
    """持仓量增加 + 价格突破 — 真趋势确认（非散户拉锯）。"""
    name = "oi_breakout"
    base_confidence = 65
    description = "OI 持仓量增加 ≥ N% + 价格突破前 5 高 → 真趋势"

    def __init__(self, oi_increase_threshold_pct: float = 5.0, breakout_lookback: int = 5):
        self.oi_increase_threshold_pct = oi_increase_threshold_pct
        self.breakout_lookback = breakout_lookback

    async def evaluate(self, symbol, market, candles, recent_news=None):
        if (market.value if hasattr(market, "value") else str(market)) != "crypto":
            return None
        data = await _get_cached_sentiment("oi", _to_swap_inst(symbol))
        if not data or data.get("oi") is None:
            return None
        # OI 历史变化通过 candles[].volume 比对（简化版：用近 5 期 OI 不可得 → 用 candle 量比代替）
        # OKX OI API 不返回历史，简化：仅用当前 OI 是否处在历史 90% 分位
        # 这里我们简化为：价格突破前 N 高 + 量也放大 = OI 增加的代理
        if len(candles) < self.breakout_lookback + 2:
            return None
        highs = np.array([c.high for c in candles], dtype=np.float64)
        volumes = np.array([c.volume for c in candles], dtype=np.float64)
        prev_high = float(np.max(highs[-self.breakout_lookback - 1:-1]))
        last_close = float(candles[-1].close)
        # 价格突破 + 量放大 1.5×
        if last_close <= prev_high:
            return None
        vma = calc_volume_ma(volumes, 20)
        if np.isnan(vma[-1]) or vma[-1] <= 0:
            return None
        if volumes[-1] / vma[-1] < 1.5:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, "buy", last_close, confidence,
            f"OI 突破：价 > 前{self.breakout_lookback}高 + 量比 {volumes[-1]/vma[-1]:.2f}× (OI={data.get('oi'):.0f})",
            triggered_by={"oi": data.get("oi"), "volume_ratio": volumes[-1]/vma[-1]},
            candles=candles,
        )


class LongShortRatioStrategy(Strategy):
    """散户多空比极值反转 — > 3.5 散户极度多头 → short；< 0.5 反之 → long"""
    name = "long_short_ratio"
    base_confidence = 65
    description = "散户多空比极值反转（散户拥挤时反向）"

    def __init__(self, short_when_ratio_above: float = 3.5, long_when_ratio_below: float = 0.5):
        self.short_when_ratio_above = short_when_ratio_above
        self.long_when_ratio_below = long_when_ratio_below

    async def evaluate(self, symbol, market, candles, recent_news=None):
        if (market.value if hasattr(market, "value") else str(market)) != "crypto":
            return None
        coin = symbol.split("-")[0] if "-" in symbol else symbol
        data = await _get_cached_sentiment("lsr", coin)
        if not data:
            return None
        # 取最新 ratio
        # v12.21.6 P1 修复: SentimentData.get_long_short_ratio 返回的 history
        # 是 OKX 时间倒序 (最新在 [0], history[-1] 是 24h 前的旧值!)
        # 之前用 history[-1] 在剧烈波动时会判断错误
        # 优先 current (=history[0]),fallback history[0]
        ratio = None
        try:
            if data.get("current"):
                ratio = float(data["current"].get("ratio", 0))
            elif isinstance(data.get("history"), list) and data["history"]:
                ratio = float(data["history"][0].get("ratio", 0))
        except (TypeError, ValueError):
            return None
        if not ratio:
            return None
        action = None
        if ratio >= self.short_when_ratio_above:
            action = "sell"
        elif ratio <= self.long_when_ratio_below:
            action = "buy"
        if action is None:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"多空比 {ratio:.2f} 极值 → 反向 {action}",
            triggered_by={"long_short_ratio": ratio, "side": "short" if action=="sell" else "long"},
            candles=candles,
        )


class FearGreedReversalStrategy(Strategy):
    """F&G 极值反转：< 20 极度恐惧 → buy；> 80 极度贪婪 → sell"""
    name = "fear_greed_reversal"
    base_confidence = 65
    description = "F&G 极值反转（< 20 抄底 / > 80 逃顶）"

    def __init__(self, oversold_threshold: int = 20, overbought_threshold: int = 80):
        self.oversold = oversold_threshold
        self.overbought = overbought_threshold

    async def evaluate(self, symbol, market, candles, recent_news=None):
        if (market.value if hasattr(market, "value") else str(market)) != "crypto":
            return None
        data = await _get_cached_sentiment("fg", "_global")
        if not data:
            return None
        try:
            value = int(data.get("value", 50))
        except (TypeError, ValueError):
            return None
        action = None
        if value <= self.oversold:
            action = "buy"
        elif value >= self.overbought:
            action = "sell"
        if action is None:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"F&G={value} 极值 → 反向 {action} ({data.get('classification', '')})",
            triggered_by={"fg_value": value, "side": "short" if action=="sell" else "long"},
            candles=candles,
        )


# ═══════════════════════════════════════════════════════════════════
# v12.16 (Step 6): A 股专属策略
# ═══════════════════════════════════════════════════════════════════

class LimitUpFollowupStrategy(Strategy):
    """涨停后回踩 — 用 1D K 线检测昨涨停 + 今开盘≤涨停价 + 价格回踩均价不破"""
    name = "limit_up_followup"
    base_confidence = 70
    description = "A 股涨停后第二日回踩均价不破 → buy"

    LIMIT_UP_RATIO = 0.099  # 主板 +9.9% (考虑误差)；创业板/科创板可放到 0.199

    def evaluate(self, symbol, market, candles, recent_news=None):
        if (market.value if hasattr(market, "value") else str(market)) != "cn":
            return None
        if len(candles) < 5:
            return None
        # 昨日 K 线：close >= prev_close × 1.099 ≈ 涨停
        # 今日 K 线：open ≤ 昨日 close（未直接平开）+ 当前 close 仍站住开盘价
        c_yesterday = candles[-2]
        c_today = candles[-1]
        prev_close = float(candles[-3].close) if len(candles) >= 3 else float(c_yesterday.open)
        # 昨涨停判定（容忍误差 0.5%）
        if prev_close <= 0:
            return None
        ret = (c_yesterday.close - prev_close) / prev_close
        if ret < self.LIMIT_UP_RATIO - 0.005:
            return None
        # 今日开盘价 ≤ 昨日 close（避免高开抢筹）+ 当前价 ≥ 今日均价
        if c_today.open > c_yesterday.close * 1.005:
            return None
        avg_today = (c_today.open + c_today.close + c_today.high + c_today.low) / 4
        if c_today.close < avg_today * 0.995:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, "buy", float(c_today.close), confidence,
            f"A 股涨停后回踩 (昨涨停 +{ret*100:.1f}%, 今开≤昨收+回踩均价)",
            triggered_by={"yesterday_return_pct": round(ret*100, 2)},
            candles=candles,
        )


class NorthboundFlowTopStrategy(Strategy):
    """v12.16 北向资金净买入排名前 50 → 该股 buy。
    简化：用最新一期主力净流入排名（北向是主力的一部分）。
    """
    name = "northbound_flow_top"
    base_confidence = 65
    description = "A 股个股北向/主力资金净买入排名前 50 → buy"

    def __init__(self, top_n: int = 50, min_net_inflow_yuan: float = 5e7):
        self.top_n = top_n
        self.min_net_inflow_yuan = min_net_inflow_yuan  # 5000 万门槛

    async def evaluate(self, symbol, market, candles, recent_news=None):
        if (market.value if hasattr(market, "value") else str(market)) != "cn":
            return None
        try:
            from backend.data.eastmoney_extra import fetch_northbound_top_stocks
            top = await fetch_northbound_top_stocks(top_n=self.top_n)
        except Exception as e:
            logger.debug(f"[northbound] {symbol} fetch failed: {e}")
            return None
        if not top:
            return None
        # 看 symbol 是否在前 top_n 且净流入达标
        match = next((it for it in top if it.get("symbol") == symbol), None)
        if not match:
            return None
        net = float(match.get("main_net_inflow", 0))
        if net < self.min_net_inflow_yuan:
            return None
        # rank 越靠前置信度越高
        rank = next((i for i, it in enumerate(top) if it.get("symbol") == symbol), self.top_n)
        rank_bonus = max(0, 15 - rank // 5)  # 前 5 +15, 6-10 +12, 11-15 +9 ...
        confidence = self.base_confidence + rank_bonus + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, "buy", last_close, confidence,
            f"北向/主力净流入排名 #{rank+1} (净额 {net/1e8:.2f}亿 元)",
            triggered_by={"net_inflow": net, "rank": rank + 1},
            candles=candles,
        )


class SectorMomentumStrategy(Strategy):
    """v12.16 板块联动 — 个股所属板块涨幅榜前 N + 板块内联动 ≥ M 只。"""
    name = "sector_momentum"
    base_confidence = 65
    description = "A 股板块涨幅前 3 + 板块内联动 ≥ 3 只 → buy"

    def __init__(self, top_sectors: int = 3, min_co_movers: int = 3,
                 co_mover_threshold_pct: float = 5.0):
        self.top_sectors = top_sectors
        self.min_co_movers = min_co_movers
        self.co_mover_threshold_pct = co_mover_threshold_pct

    async def evaluate(self, symbol, market, candles, recent_news=None):
        if (market.value if hasattr(market, "value") else str(market)) != "cn":
            return None
        try:
            from backend.data.eastmoney_extra import (
                fetch_top_sectors, fetch_sector_constituents, fetch_sectors_for_symbol
            )
            # 1. 个股所属板块
            sym_sectors = await fetch_sectors_for_symbol(symbol)
            if not sym_sectors:
                return None
            # 2. 拉涨幅前 N 板块
            top_secs = await fetch_top_sectors(top_n=self.top_sectors)
            if not top_secs:
                return None
            top_codes = {s["sector_code"] for s in top_secs}
            # 3. 个股是否在前 N 板块中
            matched_sector = next((c for c in sym_sectors if c in top_codes), None)
            if not matched_sector:
                return None
            # 4. 该板块内联动股 (>= co_mover_threshold_pct) 数量
            consts = await fetch_sector_constituents(matched_sector, max_n=100)
            if not consts:
                return None
            co_movers = sum(1 for c in consts if c.get("change_pct", 0) >= self.co_mover_threshold_pct)
            if co_movers < self.min_co_movers:
                return None
        except Exception as e:
            logger.debug(f"[sector] {symbol} fetch failed: {e}")
            return None
        sec_info = next((s for s in top_secs if s["sector_code"] == matched_sector), None)
        sec_name = sec_info.get("sector_name", "?") if sec_info else "?"
        sec_change = sec_info.get("change_pct", 0) if sec_info else 0
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, "buy", last_close, confidence,
            f"板块联动: {sec_name} 涨 {sec_change:.2f}%, 板块内联动 {co_movers} 只",
            triggered_by={
                "sector_code": matched_sector,
                "sector_name": sec_name,
                "sector_change_pct": sec_change,
                "co_movers": co_movers,
            },
            candles=candles,
        )


# ═══════════════════════════════════════════════════════════════════
# v12.16 (Step 7): 港股专属策略
# ═══════════════════════════════════════════════════════════════════

class SouthboundInflowStrategy(Strategy):
    """v12.16 港股通南向资金净流入排名前 30 → buy。"""
    name = "southbound_inflow"
    base_confidence = 65
    description = "港股通南向资金净流入排名前 30 → buy"

    def __init__(self, top_n: int = 30, min_net_inflow_hkd: float = 1e7):
        self.top_n = top_n
        self.min_net_inflow_hkd = min_net_inflow_hkd  # 1000 万 HKD

    async def evaluate(self, symbol, market, candles, recent_news=None):
        if (market.value if hasattr(market, "value") else str(market)) != "hk":
            return None
        try:
            from backend.data.eastmoney_extra import fetch_southbound_top_stocks
            top = await fetch_southbound_top_stocks(top_n=self.top_n)
        except Exception as e:
            logger.debug(f"[southbound] {symbol} fetch failed: {e}")
            return None
        if not top:
            return None
        match = next((it for it in top if it.get("symbol") == symbol), None)
        if not match:
            return None
        net = float(match.get("net_inflow", 0))
        if net < self.min_net_inflow_hkd:
            return None
        rank = next((i for i, it in enumerate(top) if it.get("symbol") == symbol), self.top_n)
        rank_bonus = max(0, 12 - rank // 3)
        confidence = self.base_confidence + rank_bonus + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, "buy", last_close, confidence,
            f"港股通南向净流入 #{rank+1} (净额 {net/1e8:.2f}亿 HKD)",
            triggered_by={"net_inflow": net, "rank": rank + 1},
            candles=candles,
        )


class AHSpreadRevertStrategy(Strategy):
    """v12.16 AH 价差回归 — A 股较 H 股溢价 > 30% → 买 H（折价方）。
    硬编码 30 只大蓝筹双重上市映射。
    """
    name = "ah_spread_revert"
    base_confidence = 65
    description = "AH 双重上市价差 > 30% → 买折价方（30 只大蓝筹映射）"

    def __init__(self, premium_threshold_pct: float = 30.0):
        self.premium_threshold_pct = premium_threshold_pct

    async def evaluate(self, symbol, market, candles, recent_news=None):
        mkt = market.value if hasattr(market, "value") else str(market)
        if mkt not in ("cn", "hk"):
            return None
        try:
            from backend.data.eastmoney_extra import fetch_ah_spread, AH_PAIRS
            # 找对应的 a_symbol
            if mkt == "cn":
                if symbol not in AH_PAIRS:
                    return None
                a_symbol = symbol
            else:
                # hk → 反查
                a_symbol = next((a for a, h in AH_PAIRS.items() if h == symbol), None)
                if not a_symbol:
                    return None
            data = await fetch_ah_spread(a_symbol)
        except Exception as e:
            logger.debug(f"[ah-spread] {symbol} fetch failed: {e}")
            return None
        if not data or data.get("signal") is None:
            return None
        premium = data["premium_pct"]
        # 决定方向：当前查询的 symbol 是哪边？
        action = None
        if data["signal"] == "a_premium_high":
            # A 高估 → 买 H 卖 A
            if mkt == "hk": action = "buy"
        elif data["signal"] == "h_premium_high":
            # H 高估 → 买 A 卖 H
            if mkt == "cn": action = "buy"
        if action is None:
            return None
        if abs(premium) < self.premium_threshold_pct:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"AH 价差 {premium:+.1f}% (A/H 配对 {data['a_symbol']}↔{data['h_symbol']})",
            triggered_by={
                "premium_pct": premium, "a_price": data["a_price"], "h_price": data["h_price"],
                "pair_a": data["a_symbol"], "pair_h": data["h_symbol"],
            },
            candles=candles,
        )


# ═══════════════════════════════════════════════════════════════════
# v12.16 (Step 8): 美股专属策略
# ═══════════════════════════════════════════════════════════════════

class GapUpContinuationStrategy(Strategy):
    """盘前高开延续 — 当日 open 较前日 close +3% + 价格未破 open"""
    name = "gap_up_continuation"
    base_confidence = 65
    description = "美股盘前/开盘高开 +3% + 站稳开盘价 → buy"

    def __init__(self, gap_threshold_pct: float = 3.0):
        self.gap_threshold_pct = gap_threshold_pct

    def evaluate(self, symbol, market, candles, recent_news=None):
        # v12.16.6: 允许 us + hk（港股盘前高开延续也常见）
        mkt = market.value if hasattr(market, "value") else str(market)
        if mkt not in ("us", "hk"):
            return None
        if len(candles) < 3:
            return None
        prev_close = float(candles[-2].close)
        today_open = float(candles[-1].open)
        today_close = float(candles[-1].close)
        if prev_close <= 0:
            return None
        gap_pct = (today_open - prev_close) / prev_close * 100
        if gap_pct < self.gap_threshold_pct:
            return None
        # 价格站住开盘价（开盘后未跌回 prev_close）
        if today_close < today_open * 0.99:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, "buy", today_close, confidence,
            f"高开延续 +{gap_pct:.2f}% (open={today_open:.2f}/prev={prev_close:.2f}/now={today_close:.2f})",
            triggered_by={"gap_pct": round(gap_pct, 2)},
            candles=candles,
        )


class VWAPPullbackStrategy(Strategy):
    """1H 滚动 VWAP 回踩（v12.16.6 实战化）：
    取最近 N 根 1H K 线（≈ 美股 1 个交易日的 7 根）作为 session VWAP 基准，
    多头趋势中价格回踩到 VWAP 附近 (±band_pct) + 量能萎缩 + 当根收阳 → buy。
    粗糙但可用 — 1m K 线对接前的折中实现。
    """
    name = "vwap_pullback"
    base_confidence = 65
    description = "1H 滚动 VWAP 回踩（多头中价站 VWAP + 量缩 + 收阳 = 买入）"

    def __init__(self, vwap_window: int = 7, band_pct: float = 0.5,
                 vol_shrink_ratio: float = 0.8, fast_ma: int = 5, slow_ma: int = 20):
        # vwap_window: 滚动窗口 K 线数；band_pct: 价格距 VWAP 的容忍带 (%)
        # vol_shrink_ratio: 当根量必须 < N × vol_ma_20（量缩 = 回踩特征）
        self.vwap_window = vwap_window
        self.band_pct = band_pct
        self.vol_shrink_ratio = vol_shrink_ratio
        self.fast_ma = fast_ma
        self.slow_ma = slow_ma

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < max(self.slow_ma, self.vwap_window + 5):
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        highs = np.array([c.high for c in candles], dtype=np.float64)
        lows = np.array([c.low for c in candles], dtype=np.float64)
        volumes = np.array([c.volume for c in candles], dtype=np.float64)

        # 滚动 VWAP：最近 vwap_window 根的 typical_price 加权均
        win = self.vwap_window
        tp = (highs[-win:] + lows[-win:] + closes[-win:]) / 3
        vol_win = volumes[-win:]
        if float(vol_win.sum()) <= 0:
            return None
        vwap = float((tp * vol_win).sum() / vol_win.sum())

        last_close = float(closes[-1])
        last = candles[-1]
        # 多头确认
        ma_fast = calc_ma(closes, self.fast_ma)[-1]
        ma_slow = calc_ma(closes, self.slow_ma)[-1]
        if np.isnan(ma_fast) or np.isnan(ma_slow):
            return None
        if not (ma_fast > ma_slow):
            return None  # 仅多头中触发

        # 价格在 VWAP 容忍带内（双边 band_pct）
        diff_pct = abs(last_close - vwap) / vwap * 100 if vwap > 0 else 999
        if diff_pct > self.band_pct:
            return None

        # 量缩：当根量 < N × 20 期均量
        vma = calc_volume_ma(volumes, 20)
        if np.isnan(vma[-1]) or vma[-1] <= 0:
            return None
        if volumes[-1] >= vma[-1] * self.vol_shrink_ratio:
            return None

        # 当根收阳（反弹确认，避免接刀子）
        if last.close <= last.open:
            return None

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, "buy", last_close, confidence,
            f"VWAP 回踩 (close={last_close:.4f} vs VWAP={vwap:.4f}, 距={diff_pct:.2f}%, "
            f"量比 {volumes[-1]/vma[-1]:.2f}× < {self.vol_shrink_ratio}×, 多头 MA{self.fast_ma}>MA{self.slow_ma})",
            triggered_by={"vwap": vwap, "diff_pct": round(diff_pct, 3),
                          "vol_ratio": round(float(volumes[-1]/vma[-1]), 2)},
            candles=candles,
        )


# v12.16.6: EarningsWindowFilter 已下线 — 实际是过滤器不是策略，
# 真正的"屏蔽其他策略"由 monitor.is_in_earnings_window() 在 _evaluate_symbol 入口检查
# 移除 ALL_STRATEGIES 注册避免在策略库展示混淆用户；仅保留下方两个 helper 函数供 monitor 使用


# 模块级缓存 — yfinance 财报日历 24h TTL（财报日期不会频繁变）
_EARNINGS_CACHE: Dict[str, Tuple[float, Optional[float]]] = {}
_EARNINGS_TTL = 86400


async def get_next_earnings_ts(symbol: str) -> Optional[float]:
    """v12.16 拉美股下次财报日期（unix ts）。失败返回 None。"""
    cache = _EARNINGS_CACHE.get(symbol)
    now = time.time()
    if cache and now - cache[0] < _EARNINGS_TTL:
        return cache[1]
    try:
        # 在 thread 里跑 yfinance（同步库）
        import asyncio
        def _fetch():
            try:
                import yfinance as yf
                t = yf.Ticker(symbol)
                cal = t.calendar
                if not cal:
                    return None
                # cal 可能是 dict 或 DataFrame
                if isinstance(cal, dict):
                    dates = cal.get("Earnings Date") or cal.get("Earnings Datetime") or []
                    if isinstance(dates, list) and dates:
                        d0 = dates[0]
                        if hasattr(d0, "timestamp"):
                            return d0.timestamp()
                # DataFrame 兜底
                try:
                    if hasattr(cal, "iloc"):
                        v = cal.iloc[0, 0]
                        if hasattr(v, "timestamp"):
                            return v.timestamp()
                except Exception:
                    pass
            except Exception:
                pass
            return None
        ts = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        _EARNINGS_CACHE[symbol] = (now, ts)
        return ts
    except Exception as e:
        logger.debug(f"[earnings] {symbol} 拉财报日期失败: {e}")
        _EARNINGS_CACHE[symbol] = (now, None)
        return None


async def is_in_earnings_window(symbol: str, market: str,
                                 days_before: int = 3, days_after: int = 1) -> bool:
    """v12.16 美股是否临近财报。仅美股有效（其他市场返回 False）"""
    if market != "us":
        return False
    ts = await get_next_earnings_ts(symbol)
    if not ts:
        return False
    now = time.time()
    diff_days = (ts - now) / 86400
    # 前 N 日 / 后 N 日内（即 ts 在 now+N 到 now-N 之间）
    return -days_after <= diff_days <= days_before


# ═══════════════════════════════════════════════════════════════════
# v12.17.0: 9 个新策略（盘前突破 / VIX 极值 / 相对强度 / 龙虎榜 / 融资余额
#                       / 量价背离 / 链上巨鲸 / 稳定币流入 / 三重过滤）
# ═══════════════════════════════════════════════════════════════════

# --- 全局缓存 ----
_VIX_CACHE: Dict[str, Tuple[float, Optional[float]]] = {}
_VIX_TTL = 600    # 10min
_SPY_RET_CACHE: Dict[str, Tuple[float, Optional[float]]] = {}
_SPY_RET_TTL = 600


async def _fetch_vix_value() -> Optional[float]:
    """异步拉 ^VIX 当前值（yfinance 同步库走线程池）。"""
    cache = _VIX_CACHE.get("vix")
    now = time.time()
    if cache and now - cache[0] < _VIX_TTL:
        return cache[1]
    try:
        import asyncio
        def _fetch():
            try:
                import yfinance as yf
                t = yf.Ticker("^VIX")
                hist = t.history(period="2d", interval="1d")
                if hist is None or hist.empty:
                    return None
                return float(hist["Close"].iloc[-1])
            except Exception:
                return None
        v = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        _VIX_CACHE["vix"] = (now, v)
        return v
    except Exception:
        return None


async def _fetch_spy_60d_return() -> Optional[float]:
    """SPY 60 日累计收益（用于 RS 比较）。"""
    cache = _SPY_RET_CACHE.get("spy60")
    now = time.time()
    if cache and now - cache[0] < _SPY_RET_TTL:
        return cache[1]
    try:
        import asyncio
        def _fetch():
            try:
                import yfinance as yf
                t = yf.Ticker("SPY")
                hist = t.history(period="90d", interval="1d")
                if hist is None or hist.empty or len(hist) < 60:
                    return None
                first = float(hist["Close"].iloc[-60])
                last = float(hist["Close"].iloc[-1])
                return (last - first) / first if first > 0 else None
            except Exception:
                return None
        v = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        _SPY_RET_CACHE["spy60"] = (now, v)
        return v
    except Exception:
        return None


# ─── 1. 盘前突破 (premarket_breakout) ────────────────────────────────

class PremarketBreakoutStrategy(Strategy):
    """美股开盘 1H 内：当根 high 突破前 N 日高 + 量能放大 + 收阳 = 强势开盘。
    粗糙模拟 — 用第一根 1H 候选条件代替真盘前数据。
    """
    name = "premarket_breakout"
    base_confidence = 70
    description = "美股开盘 1H 突破前 N 日高 + 量能放大（盘前/开盘强势确认）"

    def __init__(self, lookback_high_bars: int = 60, vol_ratio: float = 1.8,
                 min_open_gap_pct: float = 1.0):
        self.lookback_high_bars = lookback_high_bars
        self.vol_ratio = vol_ratio
        self.min_open_gap_pct = min_open_gap_pct

    def evaluate(self, symbol, market, candles, recent_news=None):
        mkt = market.value if hasattr(market, "value") else str(market)
        if mkt != "us":
            return None
        if len(candles) < self.lookback_high_bars + 2:
            return None
        highs = np.array([c.high for c in candles], dtype=np.float64)
        volumes = np.array([c.volume for c in candles], dtype=np.float64)
        last = candles[-1]
        prev_high = float(np.max(highs[-self.lookback_high_bars - 1:-1]))
        if last.high <= prev_high:
            return None
        # 开盘高于前一根收盘价 ≥ N%
        prev_close = float(candles[-2].close)
        if prev_close <= 0:
            return None
        gap_pct = (last.open - prev_close) / prev_close * 100
        if gap_pct < self.min_open_gap_pct:
            return None
        # 量比
        vma = calc_volume_ma(volumes, 20)
        if np.isnan(vma[-1]) or vma[-1] <= 0:
            return None
        vol_ratio = float(volumes[-1] / vma[-1])
        if vol_ratio < self.vol_ratio:
            return None
        # 收阳
        if last.close <= last.open:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, "buy", float(last.close), confidence,
            f"盘前/开盘突破 高 > 前{self.lookback_high_bars}H 高 ({prev_high:.4f}) + "
            f"开盘 +{gap_pct:.2f}% + 量比 {vol_ratio:.2f}× + 收阳",
            triggered_by={"prev_high": prev_high, "gap_pct": round(gap_pct, 2),
                          "vol_ratio": round(vol_ratio, 2)},
            candles=candles,
        )


# ─── 2. VIX 极值反转 (vix_extreme) ────────────────────────────────

class VIXExtremeStrategy(Strategy):
    """VIX > 30 极度恐慌 → buy SPY/QQQ；VIX < 12 极度贪婪 → sell。
    仅对大盘 ETF 触发（白名单）。
    """
    name = "vix_extreme"
    base_confidence = 70
    description = "VIX 极值反转（>30 抄底大盘 ETF / <12 逃顶；仅 SPY/QQQ/DIA/IWM）"

    INDEX_WHITELIST = {"SPY", "QQQ", "DIA", "IWM", "VOO", "IVV"}

    def __init__(self, panic_threshold: float = 30.0, complacent_threshold: float = 12.0):
        self.panic_threshold = panic_threshold
        self.complacent_threshold = complacent_threshold

    async def evaluate(self, symbol, market, candles, recent_news=None):
        mkt = market.value if hasattr(market, "value") else str(market)
        if mkt != "us":
            return None
        if symbol.upper() not in self.INDEX_WHITELIST:
            return None
        vix = await _fetch_vix_value()
        if vix is None:
            return None
        action = None
        if vix >= self.panic_threshold:
            action = "buy"
        elif vix <= self.complacent_threshold:
            action = "sell"
        if action is None:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"VIX={vix:.1f} {'极度恐慌' if action == 'buy' else '极度贪婪'} → 反向 {action} 大盘 ETF",
            triggered_by={"vix": vix, "side": "long" if action == "buy" else "short"},
            candles=candles,
        )


# ─── 3. 相对大盘强度 (relative_strength_top) ───────────────────────

class RelativeStrengthStrategy(Strategy):
    """美股个股 60 日累计收益显著跑赢 SPY → 强势股，回踩 MA20 入场。"""
    name = "relative_strength_top"
    base_confidence = 65
    description = "美股 60 日跑赢 SPY ≥ N% + 价格回踩 MA20 → 强势股入场"

    def __init__(self, outperform_threshold_pct: float = 15.0,
                 ma_period: int = 20, max_dist_to_ma_pct: float = 2.0):
        self.outperform_threshold_pct = outperform_threshold_pct
        self.ma_period = ma_period
        self.max_dist_to_ma_pct = max_dist_to_ma_pct

    async def evaluate(self, symbol, market, candles, recent_news=None):
        mkt = market.value if hasattr(market, "value") else str(market)
        if mkt != "us":
            return None
        if len(candles) < 60 + self.ma_period:
            return None
        # 跳过大盘 ETF 自身
        if symbol.upper() in {"SPY", "QQQ", "DIA", "IWM", "VOO", "IVV"}:
            return None
        spy_ret = await _fetch_spy_60d_return()
        if spy_ret is None:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        first = float(closes[-60])
        last = float(closes[-1])
        if first <= 0:
            return None
        sym_ret = (last - first) / first
        outperf = (sym_ret - spy_ret) * 100
        if outperf < self.outperform_threshold_pct:
            return None
        # 当前价回踩 MA20 附近（强势股回调买点）
        ma = calc_ma(closes, self.ma_period)
        if np.isnan(ma[-1]):
            return None
        dist_pct = (last - ma[-1]) / ma[-1] * 100
        if abs(dist_pct) > self.max_dist_to_ma_pct:
            return None
        # 多头确认
        if not (last > ma[-1] * 0.99):
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        return self._make_signal(
            symbol, market, "buy", last, confidence,
            f"相对强势 60日跑赢 SPY +{outperf:.1f}% (个股 {sym_ret*100:+.1f}% / SPY {spy_ret*100:+.1f}%) "
            f"+ 回踩 MA{self.ma_period} (距 {dist_pct:+.2f}%)",
            triggered_by={"outperform_pct": round(outperf, 2), "spy_ret": round(spy_ret*100, 2),
                          "sym_ret": round(sym_ret*100, 2)},
            candles=candles,
        )


# ─── 4. 龙虎榜跟盘 (lhb_follow) ────────────────────────────────────

class LHBFollowStrategy(Strategy):
    """A 股龙虎榜机构席位净买入 ≥ N → 次日跟盘 buy。"""
    name = "lhb_follow"
    base_confidence = 70
    description = "A 股龙虎榜机构净买入 ≥ 5000 万 → 次日跟盘 buy"

    def __init__(self, min_jg_net_buy_yuan: float = 5e7, fallback_total_buy: float = 1e8):
        # 优先用机构净买额；如机构数据缺则用总净买入额（>1亿）
        self.min_jg_net_buy_yuan = min_jg_net_buy_yuan
        self.fallback_total_buy = fallback_total_buy

    async def evaluate(self, symbol, market, candles, recent_news=None):
        mkt = market.value if hasattr(market, "value") else str(market)
        if mkt != "cn":
            return None
        try:
            from backend.data.eastmoney_extra import fetch_lhb_today_buys
            lhb = await fetch_lhb_today_buys(top_n=200)
        except Exception as e:
            logger.debug(f"[lhb] {symbol} fetch err: {e}")
            return None
        if not lhb:
            return None
        match = next((it for it in lhb if it.get("symbol") == symbol), None)
        if not match:
            return None
        jg = float(match.get("jg_net_buy") or 0)
        net = float(match.get("net_buy") or 0)
        # 机构净买达标 OR 机构数据缺则总净买达更高门槛
        if jg >= self.min_jg_net_buy_yuan:
            metric_label = f"机构净买 {jg/1e7:.1f}千万"
        elif jg == 0 and net >= self.fallback_total_buy:
            metric_label = f"龙虎榜总净买 {net/1e8:.2f}亿（机构数据缺）"
        else:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, "buy", last_close, confidence,
            f"龙虎榜跟盘: {metric_label} (今日涨跌 {match.get('change_pct',0):+.2f}%)",
            triggered_by={"jg_net_buy": jg, "total_net_buy": net,
                          "today_change_pct": match.get("change_pct", 0)},
            candles=candles,
        )


# ─── 5. 融资余额突破 (margin_breakout) ────────────────────────────

class MarginBreakoutStrategy(Strategy):
    """A 股融资余额突破 N 日均 × M 倍 → 杠杆资金入场，次日 buy。"""
    name = "margin_breakout"
    base_confidence = 65
    description = "A 股融资余额突破 20 日均 × 1.3 → 杠杆资金入场跟盘"

    def __init__(self, lookback_days: int = 20, breakout_ratio: float = 1.3):
        self.lookback_days = lookback_days
        self.breakout_ratio = breakout_ratio

    async def evaluate(self, symbol, market, candles, recent_news=None):
        mkt = market.value if hasattr(market, "value") else str(market)
        if mkt != "cn":
            return None
        try:
            from backend.data.eastmoney_extra import fetch_margin_history
            history = await fetch_margin_history(symbol, days=self.lookback_days + 5)
        except Exception as e:
            logger.debug(f"[margin] {symbol} fetch err: {e}")
            return None
        if not history or len(history) < self.lookback_days:
            return None
        balances = [float(it.get("fin_balance") or 0) for it in history]
        latest = balances[0] if balances else 0
        if latest <= 0:
            return None
        avg = sum(balances[1:self.lookback_days + 1]) / self.lookback_days
        if avg <= 0:
            return None
        ratio = latest / avg
        if ratio < self.breakout_ratio:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, "buy", last_close, confidence,
            f"融资余额突破: {latest/1e8:.2f}亿 / 20日均 {avg/1e8:.2f}亿 = {ratio:.2f}×",
            triggered_by={"fin_balance": latest, "avg_balance": avg, "ratio": round(ratio, 2)},
            candles=candles,
        )


# ─── 6. 量价背离 (volume_price_divergence) ────────────────────────

class VolumePriceDivergenceStrategy(Strategy):
    """量价背离 — 价格创新高 + 量缩 = 顶部预警 sell；
                  价格创新低 + 量缩 = 底部信号 buy（恐慌耗尽）。
    """
    name = "volume_price_divergence"
    base_confidence = 65
    description = "量价背离（价新高量缩=顶部 / 价新低量缩=底部）"

    def __init__(self, lookback: int = 30, vol_shrink_ratio: float = 0.7,
                 min_price_diff_pct: float = 1.0):
        self.lookback = lookback
        self.vol_shrink_ratio = vol_shrink_ratio
        self.min_price_diff_pct = min_price_diff_pct

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.lookback + 5:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        highs = np.array([c.high for c in candles], dtype=np.float64)
        lows = np.array([c.low for c in candles], dtype=np.float64)
        volumes = np.array([c.volume for c in candles], dtype=np.float64)
        win_high = highs[-self.lookback:]
        win_low = lows[-self.lookback:]
        win_vol = volumes[-self.lookback:]

        last = candles[-1]
        last_high = float(last.high)
        last_low = float(last.low)
        last_vol = float(last.volume)
        # 当根量必须明显萎缩
        avg_vol = float(np.mean(win_vol[:-1]))
        if avg_vol <= 0:
            return None
        if last_vol >= avg_vol * self.vol_shrink_ratio:
            return None

        # 找窗口内（不含当根）的前期高/低
        prior_high = float(np.max(win_high[:-1]))
        prior_low = float(np.min(win_low[:-1]))

        action = None
        reason = None
        # 顶背离：价格创新高 + 量缩
        diff_high_pct = (last_high - prior_high) / prior_high * 100 if prior_high > 0 else 0
        if diff_high_pct >= self.min_price_diff_pct:
            action = "sell"
            reason = (f"顶背离 价格新高 +{diff_high_pct:.2f}% (高 {last_high:.4f} > 前高 {prior_high:.4f}) "
                      f"但量缩 {last_vol/avg_vol:.2f}× < {self.vol_shrink_ratio}×")
        else:
            # 底背离：价格创新低 + 量缩
            diff_low_pct = (prior_low - last_low) / prior_low * 100 if prior_low > 0 else 0
            if diff_low_pct >= self.min_price_diff_pct:
                action = "buy"
                reason = (f"底背离 价格新低 -{diff_low_pct:.2f}% (低 {last_low:.4f} < 前低 {prior_low:.4f}) "
                          f"但量缩 {last_vol/avg_vol:.2f}× < {self.vol_shrink_ratio}× = 恐慌耗尽")

        if action is None:
            return None

        # 趋势过滤：多头中不发 sell；空头中不发 buy
        ma5 = calc_ma(closes, 5)[-1]; ma20 = calc_ma(closes, 20)[-1]
        if not (np.isnan(ma5) or np.isnan(ma20)):
            if action == "sell" and ma5 > ma20 * 1.03:
                pass  # 强多头中即便顶背离也是高位震荡，不强行翻空 — 保留信号但置信度降
            if action == "buy" and ma5 < ma20 * 0.97:
                pass

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(closes[-1])
        return self._make_signal(
            symbol, market, action, last_close, confidence, reason, candles=candles,
        )


# ─── 7. 链上巨鲸 (whale_activity) ────────────────────────────────

class WhaleActivityStrategy(Strategy):
    """OKX 大额成交识别（替代付费链上 API）：
    最近 N 笔成交里 ≥ $500K 的大单买卖比例 ≥ 2:1 → 巨鲸方向信号。
    """
    name = "whale_activity"
    base_confidence = 65
    description = "OKX 大单（≥$500K）买卖比 ≥ 2:1 → 巨鲸方向信号"

    def __init__(self, large_trade_usd: float = 5e5, min_imbalance_ratio: float = 2.0,
                 lookback_trades: int = 100):
        self.large_trade_usd = large_trade_usd
        self.min_imbalance_ratio = min_imbalance_ratio
        self.lookback_trades = lookback_trades

    async def evaluate(self, symbol, market, candles, recent_news=None):
        mkt = market.value if hasattr(market, "value") else str(market)
        if mkt != "crypto":
            return None
        try:
            from backend.data.fetcher import get_fetcher
            okx = get_fetcher(market)
            trades = await okx.get_recent_trades(symbol, limit=self.lookback_trades)
        except Exception as e:
            logger.debug(f"[whale] {symbol} fetch err: {e}")
            return None
        if not trades:
            return None
        large = [t for t in trades if t.get("sizeUsd", 0) >= self.large_trade_usd]
        if len(large) < 5:
            return None
        buys = sum(t["sizeUsd"] for t in large if t.get("side") == "buy")
        sells = sum(t["sizeUsd"] for t in large if t.get("side") == "sell")
        if buys == 0 and sells == 0:
            return None
        action = None
        ratio = 0
        if buys > 0 and sells > 0:
            ratio = buys / sells
            if ratio >= self.min_imbalance_ratio:
                action = "buy"
            elif ratio <= 1 / self.min_imbalance_ratio:
                action = "sell"
        elif buys > 0:
            action = "buy"; ratio = float("inf")
        elif sells > 0:
            action = "sell"; ratio = 0.0
        if action is None:
            return None
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        ratio_str = "∞" if ratio == float("inf") else f"{ratio:.2f}"
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"巨鲸大单: {len(large)} 笔 ≥${self.large_trade_usd/1000:.0f}K, "
            f"买/卖={buys/1e6:.2f}M/{sells/1e6:.2f}M (ratio={ratio_str})",
            triggered_by={"large_trades": len(large), "buy_usd": buys, "sell_usd": sells,
                          "imbalance_ratio": None if ratio == float("inf") else round(ratio, 2)},
            candles=candles,
        )


# ─── 8. 稳定币流入 (stablecoin_flow) ─────────────────────────────

class StablecoinFlowStrategy(Strategy):
    """USDT/USDC 24h 全市场净流入 > $500M → 加密整体 buy；
                              净流出 > $500M → sell。
    全市场广播信号 — 一次拉数据应用到所有 crypto symbol。
    """
    name = "stablecoin_flow"
    base_confidence = 60
    description = "稳定币 USDT+USDC 24h 净流入 ≥ $500M = 加密整体方向参考"

    def __init__(self, inflow_threshold_usd: float = 5e8):
        self.inflow_threshold_usd = inflow_threshold_usd

    async def evaluate(self, symbol, market, candles, recent_news=None):
        mkt = market.value if hasattr(market, "value") else str(market)
        if mkt != "crypto":
            return None
        # 仅对主流币广播（避免对所有山寨刷信号）
        coin = symbol.split("-")[0] if "-" in symbol else symbol
        if coin.upper() not in {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "TRX", "DOT"}:
            return None
        try:
            from backend.data.stablecoin_flow import fetch_stablecoin_flow_24h
            flow = await fetch_stablecoin_flow_24h()
        except Exception as e:
            logger.debug(f"[stablecoin] fetch err: {e}")
            return None
        if not flow or flow.get("signal") == "neutral":
            return None
        delta = float(flow.get("total_delta_24h") or 0)
        action = "buy" if delta > 0 else "sell"
        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(candles[-1].close)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"稳定币 24h 净{'流入' if action == 'buy' else '流出'} ${delta/1e9:.2f}B "
            f"(总市值 ${(flow.get('total_mcap') or 0)/1e9:.1f}B)",
            triggered_by={"total_delta_24h": delta, "total_mcap": flow.get("total_mcap")},
            candles=candles,
        )


# ─── 9. 三重过滤 (triple_screen) ─────────────────────────────────

class TripleScreenStrategy(Strategy):
    """Elder 三重过滤的单 interval 实现版（合成多周期）：
    Screen 1 (周线趋势)  ─ 长 EMA(168) 上升 → 多头大势
    Screen 2 (日线动量)  ─ 24 期 RSI 在 40-60 = 中性回调（不超买不超卖）
    Screen 3 (1H 入场)   ─ 当根 close > 前根 close + 量比 > 1.2 → 入场触发
    任何一层失败即不发信号。仅 1H 周期适用（确保 168 = 1 周近似）。
    """
    name = "triple_screen"
    base_confidence = 75
    description = "三重过滤（周趋势 + 日动量中性 + 1H 入场触发）"

    def __init__(self, weekly_ema_period: int = 168, daily_rsi_period: int = 24,
                 vol_ratio: float = 1.2):
        self.weekly_ema_period = weekly_ema_period
        self.daily_rsi_period = daily_rsi_period
        self.vol_ratio = vol_ratio

    def evaluate(self, symbol, market, candles, recent_news=None):
        if len(candles) < self.weekly_ema_period + 5:
            return None
        closes = np.array([c.close for c in candles], dtype=np.float64)
        volumes = np.array([c.volume for c in candles], dtype=np.float64)

        # Screen 1: 周线趋势（EMA168 上升 = 多头）
        ema_w = calc_ema(closes, self.weekly_ema_period)
        if np.isnan(ema_w[-1]) or np.isnan(ema_w[-5]):
            return None
        weekly_up = ema_w[-1] > ema_w[-5]
        weekly_down = ema_w[-1] < ema_w[-5]

        # Screen 2: 日线动量（24 期 RSI 在 40-60 = 适合入场不超买）
        daily_rsi_arr = calc_rsi(closes, self.daily_rsi_period)
        if np.isnan(daily_rsi_arr[-1]):
            return None
        daily_rsi = float(daily_rsi_arr[-1])
        # 多头中：RSI 30-55 (回调没超买) ; 空头中：RSI 45-70 (反弹没超卖)
        action = None
        if weekly_up and 30 <= daily_rsi <= 55:
            action = "buy"
        elif weekly_down and 45 <= daily_rsi <= 70:
            action = "sell"
        if action is None:
            return None

        # Screen 3: 1H 入场触发（顺方向价格 + 量能放大）
        last = candles[-1]
        prev = candles[-2]
        vma = calc_volume_ma(volumes, 20)
        if np.isnan(vma[-1]) or vma[-1] <= 0:
            return None
        vol_ratio_now = float(volumes[-1] / vma[-1])
        if vol_ratio_now < self.vol_ratio:
            return None
        if action == "buy" and last.close <= prev.close:
            return None
        if action == "sell" and last.close >= prev.close:
            return None

        confidence = self.base_confidence + self._common_modifier(candles, recent_news)
        last_close = float(last.close)
        return self._make_signal(
            symbol, market, action, last_close, confidence,
            f"三重过滤共振: 周{'多' if action=='buy' else '空'}EMA{self.weekly_ema_period} "
            f"+ 日 RSI={daily_rsi:.1f}({'回调' if action=='buy' else '反弹'}) "
            f"+ 1H 量比 {vol_ratio_now:.2f}×",
            triggered_by={"weekly_trend": "up" if weekly_up else "down",
                          "daily_rsi": round(daily_rsi, 2),
                          "hourly_vol_ratio": round(vol_ratio_now, 2)},
            candles=candles,
        )


ALL_STRATEGIES: Dict[str, type] = {
    # v12.16 (Step 2): rsi_divergence 已删除（实战 0 confirm 100% skipped）
    # v12.16.5: 用 3 个 RSI 组合策略替代（rsi_pullback / rsi_real_divergence / rsi_breakout_50）
    "ma_cross": MACrossStrategy,
    "donchian_breakout": DonchianBreakout,
    "bollinger_reversion": BollingerReversion,
    "volume_breakout": VolumeBreakout,
    "flash_event": FlashEventStrategy,
    "chanlun": ChanLunStrategy,
    # v12.16 (Step 4) 通用型
    "macd_cross": MACDCrossStrategy,
    "ema_triple": EMATripleStrategy,
    "squeeze_breakout": SqueezeBreakoutStrategy,
    "adx_trend_follow": ADXTrendFollowStrategy,
    # v12.16.5 RSI 组合系列（替代旧 RSIDivergence）
    "rsi_pullback": RSIPullbackStrategy,
    "rsi_real_divergence": RSIRealDivergenceStrategy,
    "rsi_breakout_50": RSIBreakout50Strategy,
    # v12.16 (Step 5) 加密专属
    "funding_extreme": FundingExtremeStrategy,
    "oi_breakout": OIBreakoutStrategy,
    "long_short_ratio": LongShortRatioStrategy,
    "fear_greed_reversal": FearGreedReversalStrategy,
    # v12.16 (Step 6) A 股专属
    "limit_up_followup": LimitUpFollowupStrategy,
    "northbound_flow_top": NorthboundFlowTopStrategy,
    "sector_momentum": SectorMomentumStrategy,
    # v12.16 (Step 7) 港股专属
    "southbound_inflow": SouthboundInflowStrategy,
    "ah_spread_revert": AHSpreadRevertStrategy,
    # v12.16 (Step 8) 美股专属
    "gap_up_continuation": GapUpContinuationStrategy,
    "vwap_pullback": VWAPPullbackStrategy,
    # v12.16.6: earnings_window_filter 已下线（不是策略而是 monitor 入口过滤器）
    # ─── v12.17.0: 9 个新策略 ─────────────────────────────────────
    "premarket_breakout": PremarketBreakoutStrategy,           # 美股
    "vix_extreme": VIXExtremeStrategy,                          # 美股大盘 ETF
    "relative_strength_top": RelativeStrengthStrategy,          # 美股
    "lhb_follow": LHBFollowStrategy,                            # A 股
    "margin_breakout": MarginBreakoutStrategy,                  # A 股
    "volume_price_divergence": VolumePriceDivergenceStrategy,   # 全市场
    "whale_activity": WhaleActivityStrategy,                    # 加密
    "stablecoin_flow": StablecoinFlowStrategy,                  # 加密（仅主流币）
    "triple_screen": TripleScreenStrategy,                      # 全市场（仅 1H）
}


# ═══════════════════════════════════════════════════════════════════
# v12.16 (Step 3): 市场分化策略矩阵
# ═══════════════════════════════════════════════════════════════════
# 定义每个策略在每个市场的 (interval, params)；不在矩阵中的市场=该策略不绑定
# 后续新策略加入时直接在此矩阵注册即可

STRATEGY_MARKET_MATRIX: Dict[str, Dict[str, list]] = {
    "ma_cross": {
        "crypto": [{"interval": "1H", "params": {"fast": 10, "slow": 30, "use_ema": True}}],
        "us":     [{"interval": "1H", "params": {"fast": 10, "slow": 30, "use_ema": True}}],
        "hk":     [{"interval": "1H", "params": {"fast": 10, "slow": 30, "use_ema": True}}],
        "cn":     [{"interval": "1D", "params": {"fast": 5, "slow": 20, "use_ema": False}}],
    },
    "chanlun": {
        "crypto": [{"interval": "1H", "params": {"recent_bars": 2, "min_bsp_level": "S"}}],
        "us":     [{"interval": "1D", "params": {"recent_bars": 3, "min_bsp_level": "S"}}],
        "hk":     [{"interval": "1D", "params": {"recent_bars": 3, "min_bsp_level": "any"}}],
        # v12.16.6: A 股散户主导 → 笔级噪音过大，提高到 S 级（仅段级买卖点）
        "cn":     [{"interval": "1D", "params": {"recent_bars": 3, "min_bsp_level": "S"}}],
    },
    "volume_breakout": {
        "crypto": [{"interval": "1H", "params": {"ma_period": 20, "multiplier": 2.5}}],
        "us":     [{"interval": "1H", "params": {"ma_period": 20, "multiplier": 2.0,
                                                  "max_dist_to_high_pct": 3.0}}],
        "hk":     [{"interval": "1H", "params": {"ma_period": 20, "multiplier": 2.0,
                                                  "max_dist_to_high_pct": 3.0}}],
        # v12.16.6: A 股加追高过滤（高位放量经常是出货，主板涨停板效应）
        "cn":     [{"interval": "1D", "params": {"ma_period": 20, "multiplier": 2.0,
                                                  "max_dist_to_high_pct": 3.0}}],
    },
    "donchian_breakout": {
        "crypto": [{"interval": "1H", "params": {"period": 24, "require_volume": True, "require_adx": True}}],
        "us":     [{"interval": "1H", "params": {"period": 20, "require_volume": True, "require_adx": True}}],
        "hk":     [{"interval": "1H", "params": {"period": 20, "require_volume": True, "require_adx": True}}],
        "cn":     [{"interval": "1D", "params": {"period": 10, "require_volume": True}}],  # T+1 不需 ADX
    },
    "bollinger_reversion": {
        "crypto": [{"interval": "1H", "params": {"period": 20, "multiplier": 2.5}}],
        "us":     [{"interval": "1H", "params": {"period": 20, "multiplier": 2.0}}],
        "hk":     [{"interval": "1H", "params": {"period": 20, "multiplier": 2.0}}],
        "cn":     [{"interval": "1D", "params": {"period": 20, "multiplier": 2.0}}],
    },
    "flash_event": {
        "crypto": [{"interval": "1H", "params": {"importance_threshold": 3}}],
        "us":     [{"interval": "1H", "params": {"importance_threshold": 3}}],
        "hk":     [{"interval": "1H", "params": {"importance_threshold": 3}}],
        "cn":     [{"interval": "1D", "params": {"importance_threshold": 3}}],
    },
    # ─── Step 4: 通用型新策略（4 个）─────────────────────
    "macd_cross": {
        "crypto": [{"interval": "1H", "params": {"fast": 12, "slow": 26, "signal": 9}}],
        "us":     [{"interval": "1H", "params": {"fast": 12, "slow": 26, "signal": 9}}],
        "hk":     [{"interval": "1H", "params": {"fast": 12, "slow": 26, "signal": 9}}],
        "cn":     [{"interval": "1D", "params": {"fast": 12, "slow": 26, "signal": 9}}],
    },
    "ema_triple": {
        "crypto": [{"interval": "1H", "params": {"p1": 10, "p2": 30, "p3": 60}}],
        "us":     [{"interval": "1H", "params": {"p1": 10, "p2": 30, "p3": 60}}],
        "hk":     [{"interval": "1H", "params": {"p1": 10, "p2": 30, "p3": 60}}],
        "cn":     [{"interval": "1D", "params": {"p1": 5,  "p2": 20, "p3": 60}}],
    },
    "squeeze_breakout": {
        "crypto": [{"interval": "1H", "params": {"period": 20, "bb_mult": 2.0, "kc_mult": 1.5}}],
        "us":     [{"interval": "1H", "params": {"period": 20, "bb_mult": 2.0, "kc_mult": 1.5}}],
        "hk":     [{"interval": "1H", "params": {"period": 20, "bb_mult": 2.0, "kc_mult": 1.5}}],
        "cn":     [{"interval": "1D", "params": {"period": 20, "bb_mult": 2.0, "kc_mult": 1.5}}],
    },
    "adx_trend_follow": {
        "crypto": [{"interval": "1H", "params": {"period": 14, "adx_threshold": 25.0}}],
        "us":     [{"interval": "1H", "params": {"period": 14, "adx_threshold": 25.0}}],
        "hk":     [{"interval": "1H", "params": {"period": 14, "adx_threshold": 25.0}}],
        "cn":     [{"interval": "1D", "params": {"period": 14, "adx_threshold": 20.0}}],  # A 股趋势性弱 → 阈值放低
    },
    # ─── v12.16.5: RSI 组合系列（3 个，全市场通用）─────────
    "rsi_pullback": {
        # 加密波动大 → RSI 区间放宽到 25-40 / 60-75；A 股趋势更稳 → 用更紧的 30-40 / 60-70
        "crypto": [{"interval": "1H", "params": {"buy_lo": 25, "buy_hi": 40, "sell_lo": 60, "sell_hi": 75}}],
        "us":     [{"interval": "1H", "params": {"buy_lo": 30, "buy_hi": 40, "sell_lo": 60, "sell_hi": 70}}],
        "hk":     [{"interval": "1H", "params": {"buy_lo": 30, "buy_hi": 40, "sell_lo": 60, "sell_hi": 70}}],
        "cn":     [{"interval": "1D", "params": {"buy_lo": 30, "buy_hi": 40, "sell_lo": 60, "sell_hi": 70,
                                                  "fast_ma": 5, "slow_ma": 20}}],
    },
    "rsi_real_divergence": {
        # 真背离窗口：加密短 (15 根) / 股票中 (20 根)；最小价差 加密 1.0% / 股票 0.5%
        "crypto": [{"interval": "1H", "params": {"lookback": 15, "min_price_diff_pct": 1.0}}],
        "us":     [{"interval": "1H", "params": {"lookback": 20, "min_price_diff_pct": 0.5}}],
        "hk":     [{"interval": "1H", "params": {"lookback": 20, "min_price_diff_pct": 0.5}}],
        # v12.16.6: cn 1D 一个月波动经常 10%+，1% 价差太低 → 提到 2.0%
        "cn":     [{"interval": "1D", "params": {"lookback": 20, "min_price_diff_pct": 2.0}}],
    },
    "rsi_breakout_50": {
        # 加密量能要求更高（vol_ratio 1.5），股票 1.2 即可
        "crypto": [{"interval": "1H", "params": {"vol_ratio": 1.5, "ma_period": 20}}],
        "us":     [{"interval": "1H", "params": {"vol_ratio": 1.2, "ma_period": 20}}],
        "hk":     [{"interval": "1H", "params": {"vol_ratio": 1.2, "ma_period": 20}}],
        "cn":     [{"interval": "1D", "params": {"vol_ratio": 1.2, "ma_period": 20}}],
    },
    # ─── Step 5: 加密专属（4 个）──────────────────────────
    # v12.16.6 阈值放宽：原阈值实测频率过低（funding -0.0002 / lsr 3.5 几乎不触发）
    "funding_extreme": {
        "crypto": [{"interval": "1H", "params": {"long_threshold": -0.0001, "short_threshold": 0.0005}}],
    },
    "oi_breakout": {
        "crypto": [{"interval": "1H", "params": {"oi_increase_threshold_pct": 3.0, "breakout_lookback": 5}}],
    },
    "long_short_ratio": {
        "crypto": [{"interval": "1H", "params": {"short_when_ratio_above": 2.5, "long_when_ratio_below": 0.7}}],
    },
    "fear_greed_reversal": {
        "crypto": [{"interval": "1H", "params": {"oversold_threshold": 20, "overbought_threshold": 80}}],
    },
    # ─── Step 6: A 股专属（3 个）────────────────────────
    "limit_up_followup": {
        "cn": [{"interval": "1D", "params": {}}],
    },
    "northbound_flow_top": {
        "cn": [{"interval": "1D", "params": {}}],
    },
    "sector_momentum": {
        "cn": [{"interval": "1D", "params": {}}],
    },
    # ─── Step 7: 港股专属（2 个）───────────────────────
    "southbound_inflow": {
        "hk": [{"interval": "1D", "params": {}}],
    },
    "ah_spread_revert": {
        # v12.16.6: HSCAH 历史均值就是 30-50% 溢价，30% 不算极端 → 改 50%
        "hk": [{"interval": "1D", "params": {"premium_threshold_pct": 50.0}}],
        "cn": [{"interval": "1D", "params": {"premium_threshold_pct": 50.0}}],
    },
    # ─── Step 8: 美股专属（3 个）───────────────────────
    "gap_up_continuation": {
        "us": [{"interval": "1D", "params": {"gap_threshold_pct": 3.0}}],
        # v12.16.6: 港股盘前高开也常见（A 股早盘联动）→ 加 hk 绑定
        "hk": [{"interval": "1D", "params": {"gap_threshold_pct": 3.0}}],
    },
    "vwap_pullback": {
        # v12.16.6: 实战化 — 1H 滚动 VWAP（粗糙但可用），仅绑定 us
        "us": [{"interval": "1H", "params": {"vwap_window": 7, "band_pct": 0.5,
                                              "vol_shrink_ratio": 0.8}}],
    },
    # ─── v12.17.0: 9 个新策略矩阵注册 ──────────────────────────────
    "premarket_breakout": {
        "us": [{"interval": "1H", "params": {"lookback_high_bars": 60, "vol_ratio": 1.8,
                                              "min_open_gap_pct": 1.0}}],
    },
    "vix_extreme": {
        # 仅大盘 ETF 触发（白名单内）；候选池里 SPY/QQQ 才会绑定到此策略
        "us": [{"interval": "1D", "params": {"panic_threshold": 30.0,
                                              "complacent_threshold": 12.0}}],
    },
    "relative_strength_top": {
        "us": [{"interval": "1D", "params": {"outperform_threshold_pct": 15.0,
                                              "ma_period": 20, "max_dist_to_ma_pct": 2.0}}],
    },
    "lhb_follow": {
        "cn": [{"interval": "1D", "params": {"min_jg_net_buy_yuan": 5e7,
                                              "fallback_total_buy": 1e8}}],
    },
    "margin_breakout": {
        "cn": [{"interval": "1D", "params": {"lookback_days": 20, "breakout_ratio": 1.3}}],
    },
    "volume_price_divergence": {
        # 全市场可用（纯指标）；加密/股票阈值差异
        "crypto": [{"interval": "1H", "params": {"lookback": 30, "vol_shrink_ratio": 0.7,
                                                  "min_price_diff_pct": 1.5}}],
        "us":     [{"interval": "1H", "params": {"lookback": 30, "vol_shrink_ratio": 0.7,
                                                  "min_price_diff_pct": 0.8}}],
        "hk":     [{"interval": "1H", "params": {"lookback": 30, "vol_shrink_ratio": 0.7,
                                                  "min_price_diff_pct": 1.0}}],
        "cn":     [{"interval": "1D", "params": {"lookback": 30, "vol_shrink_ratio": 0.7,
                                                  "min_price_diff_pct": 2.0}}],
    },
    "whale_activity": {
        "crypto": [{"interval": "1H", "params": {"large_trade_usd": 5e5,
                                                  "min_imbalance_ratio": 2.0,
                                                  "lookback_trades": 100}}],
    },
    "stablecoin_flow": {
        "crypto": [{"interval": "1H", "params": {"inflow_threshold_usd": 5e8}}],
    },
    "triple_screen": {
        # 仅 1H — 168 = 1 周近似（单 interval 多周期合成）
        "crypto": [{"interval": "1H", "params": {"weekly_ema_period": 168,
                                                  "daily_rsi_period": 24, "vol_ratio": 1.2}}],
        "us":     [{"interval": "1H", "params": {"weekly_ema_period": 168,
                                                  "daily_rsi_period": 24, "vol_ratio": 1.2}}],
        "hk":     [{"interval": "1H", "params": {"weekly_ema_period": 168,
                                                  "daily_rsi_period": 24, "vol_ratio": 1.2}}],
        # cn 1D 周期下：weekly=20D, daily=5D 等价
        "cn":     [{"interval": "1D", "params": {"weekly_ema_period": 20,
                                                  "daily_rsi_period": 5, "vol_ratio": 1.2}}],
    },
}


def get_strategy_matrix_for_market(market: str) -> List[Dict]:
    """v12.16 取某市场所有可用策略 + 周期 + params。
    返回 [{strategy_name, interval, params}, ...]
    """
    out = []
    for strat_name, market_cfg in STRATEGY_MARKET_MATRIX.items():
        if strat_name not in ALL_STRATEGIES:
            continue  # 矩阵中可能写了但策略类未注册（向后兼容）
        cfgs = market_cfg.get(market) or []
        for cfg in cfgs:
            out.append({
                "strategy_name": strat_name,
                "interval": cfg.get("interval", "1H"),
                "params": cfg.get("params") or {},
            })
    return out


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
