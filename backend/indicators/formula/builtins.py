"""
OpenScript 内置函数库
基于 numpy 实现所有 OpenScript 可用的技术指标和辅助函数。
"""

import numpy as np
from typing import Any


# ============================================================
# 技术指标函数
# ============================================================

def sma(source: np.ndarray, length: int) -> np.ndarray:
    """简单移动平均"""
    source = np.asarray(source, dtype=np.float64)
    out = np.full_like(source, np.nan)
    if length < 1 or length > len(source):
        return out
    cumsum = np.cumsum(source)
    out[length - 1:] = (cumsum[length - 1:] - np.concatenate([[0], cumsum[:-length]])) / length
    return out


def ema(source: np.ndarray, length: int) -> np.ndarray:
    """指数移动平均"""
    source = np.asarray(source, dtype=np.float64)
    out = np.full_like(source, np.nan)
    if length < 1 or length > len(source):
        return out
    alpha = 2.0 / (length + 1)
    # 用前 length 个值的 SMA 作为初始值
    out[length - 1] = np.mean(source[:length])
    for i in range(length, len(source)):
        out[i] = alpha * source[i] + (1 - alpha) * out[i - 1]
    return out


def wma(source: np.ndarray, length: int) -> np.ndarray:
    """加权移动平均"""
    source = np.asarray(source, dtype=np.float64)
    out = np.full_like(source, np.nan)
    if length < 1 or length > len(source):
        return out
    weights = np.arange(1, length + 1, dtype=np.float64)
    w_sum = weights.sum()
    for i in range(length - 1, len(source)):
        out[i] = np.dot(source[i - length + 1: i + 1], weights) / w_sum
    return out


def rsi(source: np.ndarray, length: int) -> np.ndarray:
    """RSI 相对强弱指数"""
    source = np.asarray(source, dtype=np.float64)
    out = np.full_like(source, np.nan)
    if length < 1 or len(source) < length + 1:
        return out

    delta = np.diff(source)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    # 使用 Wilder 平滑方法
    avg_gain = np.mean(gain[:length])
    avg_loss = np.mean(loss[:length])

    if avg_loss == 0:
        out[length] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[length] = 100.0 - 100.0 / (1.0 + rs)

    for i in range(length, len(delta)):
        avg_gain = (avg_gain * (length - 1) + gain[i]) / length
        avg_loss = (avg_loss * (length - 1) + loss[i]) / length
        if avg_loss == 0:
            out[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i + 1] = 100.0 - 100.0 / (1.0 + rs)

    return out


def macd(source: np.ndarray, fast: int = 12, slow: int = 26, sig: int = 9) -> list[np.ndarray]:
    """
    MACD 指标

    Returns:
        [dif, dea, hist] 三个数组
    """
    ema_fast = ema(source, fast)
    ema_slow = ema(source, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, sig)
    hist = (dif - dea) * 2
    return [dif, dea, hist]


def stdev(source: np.ndarray, length: int) -> np.ndarray:
    """滚动标准差"""
    source = np.asarray(source, dtype=np.float64)
    out = np.full_like(source, np.nan)
    if length < 1 or length > len(source):
        return out
    for i in range(length - 1, len(source)):
        out[i] = np.std(source[i - length + 1: i + 1], ddof=0)
    return out


def highest(source: np.ndarray, length: int) -> np.ndarray:
    """滚动最高值"""
    source = np.asarray(source, dtype=np.float64)
    out = np.full_like(source, np.nan)
    if length < 1 or length > len(source):
        return out
    for i in range(length - 1, len(source)):
        out[i] = np.max(source[i - length + 1: i + 1])
    return out


def lowest(source: np.ndarray, length: int) -> np.ndarray:
    """滚动最低值"""
    source = np.asarray(source, dtype=np.float64)
    out = np.full_like(source, np.nan)
    if length < 1 or length > len(source):
        return out
    for i in range(length - 1, len(source)):
        out[i] = np.min(source[i - length + 1: i + 1])
    return out


def sum_func(source: np.ndarray, length: int) -> np.ndarray:
    """滚动求和（避免与 Python 内置 sum 冲突）"""
    source = np.asarray(source, dtype=np.float64)
    out = np.full_like(source, np.nan)
    if length < 1 or length > len(source):
        return out
    cumsum = np.cumsum(source)
    out[length - 1:] = cumsum[length - 1:] - np.concatenate([[0], cumsum[:-length]])
    return out


# ============================================================
# 数学函数
# ============================================================

def abs_func(x):
    """绝对值"""
    if isinstance(x, np.ndarray):
        return np.abs(x)
    return abs(x)


def max_func(a, b):
    """逐元素取最大值"""
    return np.maximum(a, b)


def min_func(a, b):
    """逐元素取最小值"""
    return np.minimum(a, b)


def pow_func(base, exp):
    """幂运算"""
    return np.power(base, exp)


def sqrt_func(x):
    """平方根"""
    return np.sqrt(x)


def log_func(x):
    """自然对数"""
    return np.log(x)


# ============================================================
# 信号函数
# ============================================================

def crossover(a: np.ndarray, b) -> np.ndarray:
    """
    上穿信号：a 从下方穿越 b
    返回 bool 数组
    """
    a = np.asarray(a, dtype=np.float64)
    if np.isscalar(b):
        b = np.full_like(a, b)
    else:
        b = np.asarray(b, dtype=np.float64)

    out = np.zeros(len(a), dtype=bool)
    for i in range(1, len(a)):
        if not np.isnan(a[i]) and not np.isnan(b[i]) and not np.isnan(a[i - 1]) and not np.isnan(b[i - 1]):
            out[i] = (a[i] > b[i]) and (a[i - 1] <= b[i - 1])
    return out


def crossunder(a: np.ndarray, b) -> np.ndarray:
    """
    下穿信号：a 从上方穿越 b
    返回 bool 数组
    """
    a = np.asarray(a, dtype=np.float64)
    if np.isscalar(b):
        b = np.full_like(a, b)
    else:
        b = np.asarray(b, dtype=np.float64)

    out = np.zeros(len(a), dtype=bool)
    for i in range(1, len(a)):
        if not np.isnan(a[i]) and not np.isnan(b[i]) and not np.isnan(a[i - 1]) and not np.isnan(b[i - 1]):
            out[i] = (a[i] < b[i]) and (a[i - 1] >= b[i - 1])
    return out


def rising(source: np.ndarray, length: int) -> np.ndarray:
    """连续上升 length 根K线"""
    source = np.asarray(source, dtype=np.float64)
    out = np.zeros(len(source), dtype=bool)
    for i in range(length, len(source)):
        is_rising = True
        for j in range(1, length + 1):
            if np.isnan(source[i - j + 1]) or np.isnan(source[i - j]):
                is_rising = False
                break
            if source[i - j + 1] <= source[i - j]:
                is_rising = False
                break
        out[i] = is_rising
    return out


def falling(source: np.ndarray, length: int) -> np.ndarray:
    """连续下降 length 根K线"""
    source = np.asarray(source, dtype=np.float64)
    out = np.zeros(len(source), dtype=bool)
    for i in range(length, len(source)):
        is_falling = True
        for j in range(1, length + 1):
            if np.isnan(source[i - j + 1]) or np.isnan(source[i - j]):
                is_falling = False
                break
            if source[i - j + 1] >= source[i - j]:
                is_falling = False
                break
        out[i] = is_falling
    return out


def valuewhen(condition: np.ndarray, source: np.ndarray, occurrence: int = 0) -> np.ndarray:
    """
    返回条件成立时 source 的值。
    occurrence=0 表示最近一次，occurrence=1 表示前一次，依此类推。
    """
    condition = np.asarray(condition, dtype=bool)
    source = np.asarray(source, dtype=np.float64)
    out = np.full_like(source, np.nan)

    for i in range(len(source)):
        count = 0
        for j in range(i, -1, -1):
            if condition[j]:
                if count == occurrence:
                    out[i] = source[j]
                    break
                count += 1
    return out


def barssince(condition: np.ndarray) -> np.ndarray:
    """
    返回距离条件最后一次为真经过的K线数。
    如果条件从未为真，返回 NaN。
    """
    condition = np.asarray(condition, dtype=bool)
    out = np.full(len(condition), np.nan)

    last_true = -1
    for i in range(len(condition)):
        if condition[i]:
            last_true = i
        if last_true >= 0:
            out[i] = float(i - last_true)
    return out


# ============================================================
# 绘图函数（返回绘图指令 dict）
# ============================================================

_plot_registry: list[dict] = []
_shape_registry: list[dict] = []
_alert_registry: list[dict] = []


def reset_registries():
    """重置所有绘图注册表"""
    global _plot_registry, _shape_registry, _alert_registry
    _plot_registry = []
    _shape_registry = []
    _alert_registry = []


def get_registries() -> dict:
    """获取所有注册的绘图指令"""
    return {
        "plots": list(_plot_registry),
        "shapes": list(_shape_registry),
        "alerts": list(_alert_registry),
    }


def plot(series, title: str = "", color: str = "#2196F3", linewidth: int = 1,
         style: str = "line", **kwargs) -> dict:
    """绘制线条"""
    if isinstance(series, np.ndarray):
        data = series.tolist()
    else:
        data = series

    info = {
        "type": "plot",
        "title": title,
        "color": color,
        "linewidth": linewidth,
        "style": style,
        "data": data,
        **kwargs,
    }
    _plot_registry.append(info)
    return info


def plotshape(series, title: str = "", style: str = "triangleup",
              location: str = "belowbar", color: str = "#4CAF50",
              size: str = "small", text: str = "", **kwargs) -> dict:
    """绘制形状标记"""
    if isinstance(series, np.ndarray):
        data = series.tolist()
    else:
        data = series

    info = {
        "type": "plotshape",
        "title": title,
        "style": style,
        "location": location,
        "color": color,
        "size": size,
        "text": text,
        "data": data,
        **kwargs,
    }
    _shape_registry.append(info)
    return info


def hline(price: float, title: str = "", color: str = "#787878",
          linestyle: str = "dashed", linewidth: int = 1, **kwargs) -> dict:
    """绘制水平线"""
    info = {
        "type": "hline",
        "price": price,
        "title": title,
        "color": color,
        "linestyle": linestyle,
        "linewidth": linewidth,
        **kwargs,
    }
    _plot_registry.append(info)
    return info


def fill(plot1, plot2, color: str = "rgba(33,150,243,0.1)", **kwargs) -> dict:
    """填充两条线之间区域"""
    info = {
        "type": "fill",
        "plot1": plot1,
        "plot2": plot2,
        "color": color,
        **kwargs,
    }
    _plot_registry.append(info)
    return info


def bgcolor(condition, color: str = "rgba(76,175,80,0.1)", **kwargs) -> dict:
    """条件背景色"""
    if isinstance(condition, np.ndarray):
        data = condition.tolist()
    else:
        data = condition

    info = {
        "type": "bgcolor",
        "color": color,
        "data": data,
        **kwargs,
    }
    _plot_registry.append(info)
    return info


# ============================================================
# 警报函数
# ============================================================

def alertcondition(condition, title: str = "", message: str = "", **kwargs) -> dict:
    """定义警报条件"""
    if isinstance(condition, np.ndarray):
        data = condition.tolist()
    else:
        data = condition

    info = {
        "type": "alertcondition",
        "title": title,
        "message": message,
        "data": data,
        **kwargs,
    }
    _alert_registry.append(info)
    return info


# ============================================================
# 策略对象
# ============================================================

class _Strategy:
    """策略操作对象"""

    def __init__(self):
        self.orders: list[dict] = []
        self.initial_capital = 100000

    def reset(self, initial_capital: int = 100000):
        self.orders = []
        self.initial_capital = initial_capital

    def entry(self, id: str, direction: str, qty: float = 1.0,
              when: Any = True, comment: str = "", **kwargs):
        """
        策略入场

        Args:
            id: 订单标识
            direction: "long" 或 "short"
            qty: 数量
            when: 触发条件（bool 或 bool 数组）
            comment: 注释
        """
        if isinstance(when, np.ndarray):
            when_data = when.tolist()
        elif isinstance(when, bool):
            when_data = when
        else:
            when_data = bool(when)

        self.orders.append({
            "action": "entry",
            "id": id,
            "direction": direction,
            "qty": qty,
            "when": when_data,
            "comment": comment,
            **kwargs,
        })

    def close(self, id: str, when: Any = True, comment: str = "", **kwargs):
        """策略平仓"""
        if isinstance(when, np.ndarray):
            when_data = when.tolist()
        elif isinstance(when, bool):
            when_data = when
        else:
            when_data = bool(when)

        self.orders.append({
            "action": "close",
            "id": id,
            "when": when_data,
            "comment": comment,
            **kwargs,
        })

    def exit(self, id: str, from_entry: str = "", profit: float = 0,
             loss: float = 0, when: Any = True, **kwargs):
        """策略止盈止损退出"""
        if isinstance(when, np.ndarray):
            when_data = when.tolist()
        elif isinstance(when, bool):
            when_data = when
        else:
            when_data = bool(when)

        self.orders.append({
            "action": "exit",
            "id": id,
            "from_entry": from_entry,
            "profit": profit,
            "loss": loss,
            "when": when_data,
            **kwargs,
        })


# 全局策略实例
strategy = _Strategy()


# ============================================================
# 导出：构建安全的全局变量字典
# ============================================================

def build_globals(ohlcv: dict[str, np.ndarray], user_params: dict | None = None) -> dict:
    """
    构建执行 OpenScript 时使用的全局变量字典。

    Args:
        ohlcv: 包含 open, high, low, close, volume 的字典
        user_params: 用户自定义参数

    Returns:
        安全的全局变量字典
    """
    reset_registries()
    strategy.reset()

    g = {
        # 数据引用
        "open": ohlcv.get("open", np.array([])),
        "high": ohlcv.get("high", np.array([])),
        "low": ohlcv.get("low", np.array([])),
        "close": ohlcv.get("close", np.array([])),
        "volume": ohlcv.get("volume", np.array([])),

        # 技术指标
        "sma": sma,
        "ema": ema,
        "wma": wma,
        "rsi": rsi,
        "macd": macd,
        "stdev": stdev,
        "highest": highest,
        "lowest": lowest,
        "sum_func": sum_func,

        # 数学函数
        "abs": abs_func,
        "max": max_func,
        "min": min_func,
        "pow": pow_func,
        "sqrt": sqrt_func,
        "log": log_func,

        # 信号函数
        "crossover": crossover,
        "crossunder": crossunder,
        "rising": rising,
        "falling": falling,
        "valuewhen": valuewhen,
        "barssince": barssince,

        # 绘图函数
        "plot": plot,
        "plotshape": plotshape,
        "hline": hline,
        "fill": fill,
        "bgcolor": bgcolor,

        # 警报
        "alertcondition": alertcondition,

        # 策略（作为独立函数暴露）
        "strategy_entry": strategy.entry,
        "strategy_close": strategy.close,
        "strategy_exit": strategy.exit,

        # numpy 基础操作
        "np": np,
        "nan": np.nan,
        "True": True,
        "False": False,
        "None": None,

        # 禁止的内置函数置空
        "__builtins__": {},
    }

    # 注入用户参数
    if user_params:
        g.update(user_params)

    return g
