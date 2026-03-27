"""
自动斐波那契回撤与扩展 (Auto Fibonacci Retracement & Extension)

完全复现 TradingView 内置 "Auto Fib Retracement" 和 "Auto Fib Extension" 指标的核心算法。

核心算法要点：
    1. ZigZag锚点检测：使用动态deviation阈值 = ATR(10)/close*100*multiplier
       （与TradingView官方ZigZag库 TradingView/ZigZag/7 完全一致）
    2. 回撤(Retracement)：取最新2个pivot，在之间画Fib水平
    3. 扩展(Extension)：取最新3个连续pivot(A->B->C)，从C投射扩展目标

参考来源：
    - TradingView 内置 Auto Fib Retracement 指标
    - TradingView 内置 Auto Fib Extension 指标
    - TradingView/ZigZag/7 官方库 (deviation + depth 参数)
    - GitHub: toz-panzmoravy/Fibonacci_Pro (Pine Script v6 开源复现)
"""

import numpy as np
import pandas as pd
from typing import List, Tuple, Optional, Dict, Literal
from dataclasses import dataclass, field
from enum import Enum


# ============================================================
# 经典斐波那契水平常量
# ============================================================

# 回撤水平 (Retracement)
FIB_RETRACEMENT_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]

# 扩展水平 (Extension)
FIB_EXTENSION_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618, 2.0, 2.618, 4.236]


class TrendDirection(Enum):
    UP = "up"
    DOWN = "down"


@dataclass
class PivotPoint:
    """ZigZag枢轴点"""
    index: int              # 在原始数据中的索引
    price: float            # 价格
    is_high: bool           # True=高点, False=低点
    bar_time: Optional[pd.Timestamp] = None


@dataclass
class FibLevel:
    """单个斐波那契水平"""
    ratio: float
    price: float
    label: str = ""

    def __post_init__(self):
        if not self.label:
            if self.ratio in (0, 0.5, 1.0, 2.0):
                self.label = f"{self.ratio:.1f}"
            else:
                self.label = f"{self.ratio:.3f}"


@dataclass
class FibRetracementResult:
    """斐波那契回撤结果"""
    trend: TrendDirection
    start_point: PivotPoint     # 趋势起点
    end_point: PivotPoint       # 趋势终点
    levels: List[FibLevel]
    price_range: float

    def get_price_at_level(self, ratio: float) -> float:
        if self.trend == TrendDirection.UP:
            return self.end_point.price - self.price_range * ratio
        else:
            return self.end_point.price + self.price_range * ratio

    def to_dict(self) -> Dict:
        return {
            "trend": self.trend.value,
            "start_index": self.start_point.index,
            "start_price": self.start_point.price,
            "end_index": self.end_point.index,
            "end_price": self.end_point.price,
            "price_range": self.price_range,
            "levels": {lv.ratio: lv.price for lv in self.levels},
        }


@dataclass
class FibExtensionResult:
    """斐波那契扩展结果 (三点法)"""
    trend: TrendDirection
    point_a: PivotPoint         # 趋势起点
    point_b: PivotPoint         # 趋势终点
    point_c: PivotPoint         # 回撤终点
    levels: List[FibLevel]
    trend_range: float          # |B - A|
    retracement_ratio: float    # C回撤了多少

    def get_price_at_level(self, ratio: float) -> float:
        if self.trend == TrendDirection.UP:
            return self.point_c.price + self.trend_range * ratio
        else:
            return self.point_c.price - self.trend_range * ratio

    def to_dict(self) -> Dict:
        return {
            "trend": self.trend.value,
            "point_a": {"index": self.point_a.index, "price": self.point_a.price},
            "point_b": {"index": self.point_b.index, "price": self.point_b.price},
            "point_c": {"index": self.point_c.index, "price": self.point_c.price},
            "trend_range": self.trend_range,
            "retracement_ratio": self.retracement_ratio,
            "levels": {lv.ratio: lv.price for lv in self.levels},
        }


# ============================================================
# ATR 计算 (复现 Pine Script ta.atr)
# ============================================================

def _calc_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
              period: int = 10) -> np.ndarray:
    """
    计算ATR序列，使用RMA（与Pine Script ta.atr一致）。

    Pine Script的ta.atr使用RMA(tr, period)，RMA是指数移动平均的变种：
        rma[0] = SMA(tr, period)   # 前period根用简单平均初始化
        rma[i] = (rma[i-1] * (period-1) + tr[i]) / period
    """
    n = len(highs)
    atr = np.zeros(n)

    if n < 2:
        return atr

    # True Range
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )

    # RMA (Wilder's Moving Average)
    if n < period:
        atr[:] = np.mean(tr[:n])
        return atr

    # 初始值：前period根的SMA
    atr[period - 1] = np.mean(tr[:period])

    # 递推
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    # 填充前period-1根
    atr[:period - 1] = atr[period - 1]

    return atr


# ============================================================
# ZigZag 算法 —— 完全复现 TradingView/ZigZag/7
# ============================================================

def _find_pivot_highs(highs: np.ndarray, depth: int) -> Dict[int, float]:
    """
    等效于 Pine Script: ta.pivothigh(high, depth, depth)

    pivot high = 在 [i-depth, i+depth] 范围内，highs[i] 是最大值
    需要等待右侧depth根K线确认，因此有depth根K线的滞后。
    """
    n = len(highs)
    result = {}
    for i in range(depth, n - depth):
        is_pivot = True
        for j in range(1, depth + 1):
            if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                is_pivot = False
                break
        if is_pivot:
            result[i] = float(highs[i])
    return result


def _find_pivot_lows(lows: np.ndarray, depth: int) -> Dict[int, float]:
    """等效于 Pine Script: ta.pivotlow(low, depth, depth)"""
    n = len(lows)
    result = {}
    for i in range(depth, n - depth):
        is_pivot = True
        for j in range(1, depth + 1):
            if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                is_pivot = False
                break
        if is_pivot:
            result[i] = float(lows[i])
    return result


def zigzag_pivots(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    deviation: float = 3.0,
    depth: int = 10,
    timestamps: Optional[np.ndarray] = None,
    dynamic_deviation: bool = True,
    atr_period: int = 10,
) -> List[PivotPoint]:
    """
    ZigZag枢轴点检测 —— 完全复现TradingView/ZigZag/7库。

    TradingView的关键算法细节：
    1. 用 ta.pivothigh/ta.pivotlow (左右各depth根) 找候选pivot
    2. 动态deviation阈值 = ATR(10) / close * 100 * deviation_multiplier
       （不是固定百分比！每根K线的阈值不同）
    3. 新候选pivot从上一个确认pivot的偏离 > 动态阈值才被确认
    4. 同方向连续pivot取更极端值
    5. 高低点严格交替

    参数:
        highs, lows, closes: OHLC数据
        deviation: 偏差乘数（TradingView默认3.0）
                   注意：这是乘数，不是百分比！
                   实际阈值 = ATR(atr_period) / close * 100 * deviation
        depth: pivot检测的左右回看深度（TradingView默认10）
        timestamps: 时间戳数组（可选）
        dynamic_deviation: True=使用ATR动态阈值（TV标准），False=固定百分比
        atr_period: ATR计算周期（TradingView内部用10）
    """
    n = len(highs)
    if n < depth * 2 + 1:
        return []

    # 计算动态deviation阈值序列
    if dynamic_deviation:
        atr = _calc_atr(highs, lows, closes, period=atr_period)
        # TradingView: settings.devThreshold := ta.atr(10) / close * 100 * threshold_multiplier
        dev_thresholds = np.where(
            closes > 0,
            atr / closes * 100.0 * deviation,
            deviation  # fallback
        )
    else:
        dev_thresholds = np.full(n, deviation)

    # 第一步：找候选pivot
    candidate_highs = _find_pivot_highs(highs, depth)
    candidate_lows = _find_pivot_lows(lows, depth)

    # 第二步：合并并按索引排序
    all_candidates = []
    for idx, price in candidate_highs.items():
        all_candidates.append((idx, price, True))
    for idx, price in candidate_lows.items():
        all_candidates.append((idx, price, False))
    all_candidates.sort(key=lambda x: x[0])

    if not all_candidates:
        return []

    # 第三步：应用动态deviation过滤 + 高低交替
    pivots: List[PivotPoint] = []

    for idx, price, is_high in all_candidates:
        ts = timestamps[idx] if timestamps is not None else None

        if not pivots:
            pivots.append(PivotPoint(index=idx, price=price, is_high=is_high, bar_time=ts))
            continue

        last = pivots[-1]

        if is_high == last.is_high:
            # 同方向：取更极端值（更高的high或更低的low）
            if is_high and price > last.price:
                pivots[-1] = PivotPoint(index=idx, price=price, is_high=is_high, bar_time=ts)
            elif not is_high and price < last.price:
                pivots[-1] = PivotPoint(index=idx, price=price, is_high=is_high, bar_time=ts)
        else:
            # 反方向：检查动态deviation
            # 使用当前bar位置的deviation阈值
            threshold = dev_thresholds[idx]
            price_change_pct = abs(price - last.price) / last.price * 100.0
            if price_change_pct >= threshold:
                pivots.append(PivotPoint(index=idx, price=price, is_high=is_high, bar_time=ts))

    return pivots


# ============================================================
# 斐波那契回撤
# ============================================================

def calc_fibonacci_retracement(
    pivots: List[PivotPoint],
    levels: Optional[List[float]] = None,
    reverse: bool = False,
) -> Optional[FibRetracementResult]:
    """
    计算斐波那契回撤 —— 复现TradingView "Auto Fib Retracement"。

    算法：
    1. 取最后2个ZigZag pivot
    2. 判断趋势方向
    3. 按Fib比率在两点间计算各水平

    TradingView的计算公式：
        startPrice = reverse ? lastP.start.price : lastP.end.price
        height = (startPrice > endPrice ? -1 : 1) * abs(startPrice - endPrice)
        level_price = startPrice + height * ratio

    参数:
        pivots: 至少2个pivot
        levels: 自定义水平，默认标准回撤水平
        reverse: 反转水平顺序（对应TV的"Reverse"参数）
    """
    if len(pivots) < 2:
        return None

    if levels is None:
        levels = FIB_RETRACEMENT_LEVELS

    # 取最后两个pivot（与TV一致）
    p1 = pivots[-2]  # 趋势起点
    p2 = pivots[-1]  # 趋势终点

    if reverse:
        p1, p2 = p2, p1

    # 判断趋势
    if p2.price > p1.price:
        trend = TrendDirection.UP
    else:
        trend = TrendDirection.DOWN

    price_range = abs(p2.price - p1.price)

    # TradingView的计算方式：
    # startPrice = p2.price (趋势终点/最新点)
    # endPrice = p1.price (趋势起点)
    # height = (startPrice > endPrice ? -1 : 1) * abs(startPrice - endPrice)
    # level = startPrice + height * ratio
    #
    # 上升趋势: startPrice(高) > endPrice(低), height = -range
    #   level = high - range * ratio  (ratio=0 => high, ratio=1 => low)
    # 下降趋势: startPrice(低) < endPrice(高), height = +range
    #   level = low + range * ratio   (ratio=0 => low, ratio=1 => high)

    fib_levels = []
    for ratio in sorted(levels):
        if trend == TrendDirection.UP:
            price = p2.price - price_range * ratio
        else:
            price = p2.price + price_range * ratio
        fib_levels.append(FibLevel(ratio=ratio, price=round(price, 8)))

    return FibRetracementResult(
        trend=trend,
        start_point=p1,
        end_point=p2,
        levels=fib_levels,
        price_range=price_range,
    )


# ============================================================
# 斐波那契扩展 (三点法)
# ============================================================

def calc_fibonacci_extension(
    pivots: List[PivotPoint],
    levels: Optional[List[float]] = None,
) -> Optional[FibExtensionResult]:
    """
    计算斐波那契扩展 —— 复现TradingView "Auto Fib Extension" 三点锚定法。

    算法（与TradingView一致）：
    1. 取最后3个ZigZag pivot: A, B, C
    2. A->B 定义趋势方向和幅度 (trend_range = |B - A|)
    3. B->C 是回撤/修正
    4. 从C点投射扩展目标：
       上升趋势: level = C + trend_range * ratio
       下降趋势: level = C - trend_range * ratio

    参数:
        pivots: 至少3个pivot
        levels: 自定义扩展水平
    """
    if len(pivots) < 3:
        return None

    if levels is None:
        levels = FIB_EXTENSION_LEVELS

    a = pivots[-3]
    b = pivots[-2]
    c = pivots[-1]

    # A->B方向 = 趋势方向
    if b.price > a.price:
        trend = TrendDirection.UP
    else:
        trend = TrendDirection.DOWN

    trend_range = abs(b.price - a.price)

    # 回撤比率
    retracement = abs(b.price - c.price) / trend_range if trend_range > 0 else 0.0

    # 扩展水平
    fib_levels = []
    for ratio in sorted(levels):
        if trend == TrendDirection.UP:
            price = c.price + trend_range * ratio
        else:
            price = c.price - trend_range * ratio
        fib_levels.append(FibLevel(ratio=ratio, price=round(price, 8)))

    return FibExtensionResult(
        trend=trend,
        point_a=a,
        point_b=b,
        point_c=c,
        levels=fib_levels,
        trend_range=trend_range,
        retracement_ratio=round(retracement, 4),
    )


# ============================================================
# 高层API
# ============================================================

class AutoFibonacci:
    """
    自动斐波那契分析器

    完全复现TradingView Auto Fib Retracement + Auto Fib Extension。

    用法:
        >>> fib = AutoFibonacci(deviation=3.0, depth=10)
        >>> fib.fit(df)
        >>> ret = fib.get_retracement()
        >>> ext = fib.get_extension()
    """

    def __init__(
        self,
        deviation: float = 3.0,
        depth: int = 10,
        retracement_levels: Optional[List[float]] = None,
        extension_levels: Optional[List[float]] = None,
        dynamic_deviation: bool = True,
        atr_period: int = 10,
    ):
        """
        参数:
            deviation: ZigZag偏差乘数（TV默认3.0）
                      实际阈值 = ATR(atr_period)/close*100 * deviation
            depth: pivot检测回看深度（TV默认10）
            retracement_levels: 自定义回撤水平
            extension_levels: 自定义扩展水平
            dynamic_deviation: True=ATR动态阈值(TV标准)，False=固定百分比
            atr_period: ATR周期（TV内部用10）
        """
        self.deviation = deviation
        self.depth = depth
        self.retracement_levels = retracement_levels or FIB_RETRACEMENT_LEVELS
        self.extension_levels = extension_levels or FIB_EXTENSION_LEVELS
        self.dynamic_deviation = dynamic_deviation
        self.atr_period = atr_period

        self._pivots: List[PivotPoint] = []
        self._fitted = False

    def fit(
        self,
        df: pd.DataFrame,
        high_col: str = "high",
        low_col: str = "low",
        close_col: str = "close",
        time_col: Optional[str] = None,
    ) -> "AutoFibonacci":
        """拟合数据，检测ZigZag pivot点。"""
        highs = df[high_col].values.astype(float)
        lows = df[low_col].values.astype(float)
        closes = df[close_col].values.astype(float)

        timestamps = None
        if time_col and time_col in df.columns:
            timestamps = df[time_col].values
        elif isinstance(df.index, pd.DatetimeIndex):
            timestamps = df.index.values

        self._pivots = zigzag_pivots(
            highs, lows, closes,
            deviation=self.deviation,
            depth=self.depth,
            timestamps=timestamps,
            dynamic_deviation=self.dynamic_deviation,
            atr_period=self.atr_period,
        )
        self._fitted = True
        return self

    def fit_arrays(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        timestamps: Optional[np.ndarray] = None,
    ) -> "AutoFibonacci":
        """用numpy数组拟合。"""
        self._pivots = zigzag_pivots(
            highs, lows, closes,
            deviation=self.deviation,
            depth=self.depth,
            timestamps=timestamps,
            dynamic_deviation=self.dynamic_deviation,
            atr_period=self.atr_period,
        )
        self._fitted = True
        return self

    @property
    def pivots(self) -> List[PivotPoint]:
        return self._pivots

    def get_retracement(
        self,
        pivot_index: int = -1,
        reverse: bool = False,
    ) -> Optional[FibRetracementResult]:
        """
        获取斐波那契回撤。

        参数:
            pivot_index: 终点pivot索引（-1=最新）
            reverse: 反转水平（对应TV的Reverse参数）
        """
        if not self._fitted or len(self._pivots) < 2:
            return None
        try:
            end_idx = pivot_index if pivot_index >= 0 else len(self._pivots) + pivot_index
            start_idx = end_idx - 1
            if start_idx < 0:
                return None
            selected = [self._pivots[start_idx], self._pivots[end_idx]]
        except IndexError:
            return None
        return calc_fibonacci_retracement(selected, self.retracement_levels, reverse=reverse)

    def get_extension(
        self,
        pivot_index: int = -1,
    ) -> Optional[FibExtensionResult]:
        """
        获取斐波那契扩展（三点法）。

        参数:
            pivot_index: C点pivot索引（-1=最新）
        """
        if not self._fitted or len(self._pivots) < 3:
            return None
        try:
            end_idx = pivot_index if pivot_index >= 0 else len(self._pivots) + pivot_index
            start_idx = end_idx - 2
            if start_idx < 0:
                return None
            selected = [self._pivots[start_idx], self._pivots[start_idx + 1], self._pivots[end_idx]]
        except IndexError:
            return None
        return calc_fibonacci_extension(selected, self.extension_levels)

    def get_all_retracements(self) -> List[FibRetracementResult]:
        """获取所有相邻pivot对的回撤"""
        results = []
        for i in range(1, len(self._pivots)):
            ret = calc_fibonacci_retracement(
                [self._pivots[i - 1], self._pivots[i]],
                self.retracement_levels,
            )
            if ret:
                results.append(ret)
        return results

    def get_all_extensions(self) -> List[FibExtensionResult]:
        """获取所有连续三点的扩展"""
        results = []
        for i in range(2, len(self._pivots)):
            ext = calc_fibonacci_extension(
                [self._pivots[i - 2], self._pivots[i - 1], self._pivots[i]],
                self.extension_levels,
            )
            if ext:
                results.append(ext)
        return results

    def find_nearest_level(
        self,
        current_price: float,
        mode: Literal["retracement", "extension"] = "retracement",
    ) -> Optional[Tuple[FibLevel, FibLevel]]:
        """
        找到当前价格最近的支撑位和阻力位。

        返回: (下方支撑, 上方阻力) 元组
        """
        result = self.get_retracement() if mode == "retracement" else self.get_extension()
        if result is None:
            return None

        levels = sorted(result.levels, key=lambda x: x.price)
        support = None
        resistance = None
        for lv in levels:
            if lv.price <= current_price:
                support = lv
            elif resistance is None:
                resistance = lv

        if support is None:
            support = levels[0]
        if resistance is None:
            resistance = levels[-1]
        return (support, resistance)

    def summary(self) -> str:
        """打印分析摘要"""
        lines = [
            f"=== 自动斐波那契分析 ===",
            f"参数: deviation={self.deviation}, depth={self.depth}, "
            f"dynamic={'ATR' if self.dynamic_deviation else '固定'}",
            f"检测到 {len(self._pivots)} 个ZigZag枢轴点",
        ]

        if self._pivots:
            lines.append(f"\n--- ZigZag Pivots ---")
            for i, p in enumerate(self._pivots):
                ptype = "H" if p.is_high else "L"
                time_str = f" [{p.bar_time}]" if p.bar_time is not None else ""
                lines.append(f"  [{i}] idx={p.index} {ptype} {p.price:.4f}{time_str}")

        ret = self.get_retracement()
        if ret:
            trend_cn = "上升" if ret.trend == TrendDirection.UP else "下降"
            lines.append(f"\n--- 回撤 ({trend_cn}) ---")
            lines.append(f"  {ret.start_point.price:.4f} -> {ret.end_point.price:.4f}  "
                         f"幅度={ret.price_range:.4f}")
            for lv in ret.levels:
                lines.append(f"  {lv.ratio:>7.3f} => {lv.price:.4f}")

        ext = self.get_extension()
        if ext:
            trend_cn = "上升" if ext.trend == TrendDirection.UP else "下降"
            lines.append(f"\n--- 扩展 ({trend_cn} A->B->C) ---")
            lines.append(f"  A={ext.point_a.price:.4f} B={ext.point_b.price:.4f} "
                         f"C={ext.point_c.price:.4f}")
            lines.append(f"  趋势幅度={ext.trend_range:.4f}  回撤={ext.retracement_ratio:.1%}")
            for lv in ext.levels:
                lines.append(f"  {lv.ratio:>7.3f} => {lv.price:.4f}")

        return "\n".join(lines)


# ============================================================
# 便捷函数
# ============================================================

def auto_fib_retracement(
    df: pd.DataFrame,
    deviation: float = 3.0,
    depth: int = 10,
    levels: Optional[List[float]] = None,
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
    reverse: bool = False,
) -> Optional[FibRetracementResult]:
    """
    一键计算自动斐波那契回撤。

    >>> result = auto_fib_retracement(df)
    >>> for lv in result.levels:
    ...     print(f"{lv.label}: {lv.price:.2f}")
    """
    fib = AutoFibonacci(deviation=deviation, depth=depth, retracement_levels=levels)
    fib.fit(df, high_col=high_col, low_col=low_col, close_col=close_col)
    return fib.get_retracement(reverse=reverse)


def auto_fib_extension(
    df: pd.DataFrame,
    deviation: float = 3.0,
    depth: int = 10,
    levels: Optional[List[float]] = None,
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
) -> Optional[FibExtensionResult]:
    """
    一键计算自动斐波那契扩展。

    >>> result = auto_fib_extension(df)
    >>> for lv in result.levels:
    ...     print(f"{lv.label}: {lv.price:.2f}")
    """
    fib = AutoFibonacci(deviation=deviation, depth=depth, extension_levels=levels)
    fib.fit(df, high_col=high_col, low_col=low_col, close_col=close_col)
    return fib.get_extension()


# ============================================================
# 主程序示例
# ============================================================

if __name__ == "__main__":
    import os

    data_files = ["btc_usdt_1d.csv", "btc_usdt_4h.csv", "btc_usdt_1h.csv"]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)

    for fname in data_files:
        fpath = os.path.join(parent_dir, fname)
        if os.path.exists(fpath):
            print(f"\n{'='*60}")
            print(f"分析文件: {fname}")
            print(f"{'='*60}")

            df = pd.read_csv(fpath)

            # 自动检测列名
            col_map = {}
            for col in df.columns:
                cl = col.lower()
                if "high" in cl:
                    col_map["high"] = col
                elif "low" in cl:
                    col_map["low"] = col
                elif "close" in cl:
                    col_map["close"] = col

            if len(col_map) < 3:
                print(f"  无法识别OHLC列: {df.columns.tolist()}")
                continue

            # 使用TV标准参数
            fib = AutoFibonacci(deviation=3.0, depth=10, dynamic_deviation=True)
            fib.fit(df, high_col=col_map["high"], low_col=col_map["low"],
                    close_col=col_map["close"])
            print(fib.summary())

            # 查找当前价格的支撑/阻力
            current = df[col_map["close"]].iloc[-1]
            sr = fib.find_nearest_level(current, mode="retracement")
            if sr:
                print(f"\n当前价格 {current:.4f}")
                print(f"  支撑: {sr[0].label} => {sr[0].price:.4f}")
                print(f"  阻力: {sr[1].label} => {sr[1].price:.4f}")
            break
    else:
        # 模拟数据
        print("未找到数据文件，使用模拟数据")
        np.random.seed(42)
        n = 200
        t = np.arange(n)
        price = 100 + 0.5 * t + 15 * np.sin(t / 20) + np.random.randn(n) * 3
        df = pd.DataFrame({
            "high": price + np.abs(np.random.randn(n)) * 2,
            "low": price - np.abs(np.random.randn(n)) * 2,
            "close": price + np.random.randn(n) * 0.5,
        })

        fib = AutoFibonacci(deviation=3.0, depth=5, dynamic_deviation=True)
        fib.fit(df)
        print(fib.summary())
