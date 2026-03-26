"""
策略定义与解析模块
- parse_strategy: 解析OpenScript / Python策略代码
- generate_signals: 根据策略和OHLCV数据生成买卖信号
- 支持条件构建器模式（entry/exit条件组合）
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ======================================================================
# 内置技术指标计算（纯numpy实现）
# ======================================================================

def _sma(arr: np.ndarray, period: int) -> np.ndarray:
    """简单移动平均"""
    out = np.full_like(arr, np.nan, dtype=np.float64)
    if len(arr) < period:
        return out
    cumsum = np.cumsum(arr)
    cumsum[period:] = cumsum[period:] - cumsum[:-period]
    out[period - 1:] = cumsum[period - 1:] / period
    return out


def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    """指数移动平均"""
    out = np.full_like(arr, np.nan, dtype=np.float64)
    if len(arr) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = np.mean(arr[:period])
    for i in range(period, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def _rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI"""
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = _ema(gain, period)
    avg_loss = _ema(loss, period)
    rs = np.where(avg_loss != 0, avg_gain / avg_loss, 100.0)
    return 100.0 - 100.0 / (1.0 + rs)


def _macd(
    close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MACD: 返回 (dif, dea, histogram)"""
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    dif = ema_fast - ema_slow
    dea = _ema(np.nan_to_num(dif, nan=0.0), signal)
    hist = 2 * (dif - dea)
    return dif, dea, hist


def _bollinger(
    close: np.ndarray, period: int = 20, std_dev: float = 2.0
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """布林带: 返回 (upper, mid, lower)"""
    mid = _sma(close, period)
    std = np.full_like(close, np.nan, dtype=np.float64)
    for i in range(period - 1, len(close)):
        std[i] = np.std(close[i - period + 1: i + 1], ddof=0)
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower


# ======================================================================
# 策略数据结构
# ======================================================================

class Strategy:
    """解析后的策略对象"""

    def __init__(self):
        self.name: str = "unnamed"
        self.entry_conditions: List[Dict[str, Any]] = []
        self.exit_conditions: List[Dict[str, Any]] = []
        self.params: Dict[str, Any] = {}
        self.raw_code: str = ""
        self.strategy_type: str = "openscript"

    def __repr__(self):
        return (
            f"Strategy(name={self.name}, "
            f"entries={len(self.entry_conditions)}, "
            f"exits={len(self.exit_conditions)})"
        )


# ======================================================================
# 解析器
# ======================================================================

def parse_strategy(code: str, strategy_type: str = "openscript") -> Strategy:
    """
    解析策略代码，返回 Strategy 对象。

    支持两种模式：
    - openscript: 类Pine Script的声明式语法
    - python: 直接Python代码（eval方式）
    """
    strategy = Strategy()
    strategy.raw_code = code
    strategy.strategy_type = strategy_type

    if strategy_type == "python":
        return _parse_python_strategy(strategy, code)
    else:
        return _parse_openscript(strategy, code)


def _parse_openscript(strategy: Strategy, code: str) -> Strategy:
    """
    解析 OpenScript 声明式策略。

    语法示例：
        @name = "双均线策略"
        @param fast_period = 5
        @param slow_period = 20

        @entry
        cross_above(sma(close, fast_period), sma(close, slow_period))

        @exit
        cross_below(sma(close, fast_period), sma(close, slow_period))
    """
    lines = code.strip().split("\n")
    current_block = None  # "entry" | "exit" | None

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue

        # 策略名称
        m = re.match(r'@name\s*=\s*["\'](.+?)["\']', line)
        if m:
            strategy.name = m.group(1)
            continue

        # 参数定义
        m = re.match(r'@param\s+(\w+)\s*=\s*(.+)', line)
        if m:
            key = m.group(1)
            val_str = m.group(2).strip()
            strategy.params[key] = _parse_value(val_str)
            continue

        # 区块标记
        if line == "@entry":
            current_block = "entry"
            continue
        if line == "@exit":
            current_block = "exit"
            continue

        # 条件行
        if current_block and line:
            condition = _parse_condition_line(line)
            if condition:
                if current_block == "entry":
                    strategy.entry_conditions.append(condition)
                else:
                    strategy.exit_conditions.append(condition)

    return strategy


def _parse_python_strategy(strategy: Strategy, code: str) -> Strategy:
    """
    解析 Python 策略代码。

    约定用户代码定义两个函数:
        def entry_signal(df, params) -> np.ndarray[bool]
        def exit_signal(df, params) -> np.ndarray[bool]
    """
    strategy.name = "python_strategy"
    strategy.entry_conditions = [{"type": "python_func", "func_name": "entry_signal", "code": code}]
    strategy.exit_conditions = [{"type": "python_func", "func_name": "exit_signal", "code": code}]
    return strategy


def _parse_condition_line(line: str) -> Optional[Dict[str, Any]]:
    """将一行条件表达式解析为条件字典"""

    # cross_above(a, b)
    m = re.match(r'cross_above\((.+),\s*(.+)\)', line)
    if m:
        return {"type": "cross_above", "left": m.group(1).strip(), "right": m.group(2).strip()}

    # cross_below(a, b)
    m = re.match(r'cross_below\((.+),\s*(.+)\)', line)
    if m:
        return {"type": "cross_below", "left": m.group(1).strip(), "right": m.group(2).strip()}

    # a > b, a < b, a >= b, a <= b
    for op, op_name in [(">=", "gte"), ("<=", "lte"), (">", "gt"), ("<", "lt"), ("==", "eq")]:
        if op in line:
            parts = line.split(op, 1)
            if len(parts) == 2:
                return {"type": f"compare_{op_name}", "left": parts[0].strip(), "right": parts[1].strip()}

    # 简单函数调用作为布尔条件
    m = re.match(r'(\w+)\((.+)\)', line)
    if m:
        return {"type": "func_call", "func": m.group(1), "args": m.group(2).strip()}

    logger.warning(f"无法解析条件行: {line}")
    return None


def _parse_value(val_str: str) -> Any:
    """将字符串值转换为Python类型"""
    val_str = val_str.strip().strip("'\"")
    try:
        if "." in val_str:
            return float(val_str)
        return int(val_str)
    except ValueError:
        if val_str.lower() == "true":
            return True
        if val_str.lower() == "false":
            return False
        return val_str


# ======================================================================
# 信号生成
# ======================================================================

def generate_signals(
    strategy: Strategy,
    ohlcv: pd.DataFrame,
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    根据策略和OHLCV数据生成买卖信号。

    参数:
        strategy: 解析后的策略对象
        ohlcv: 包含 open/high/low/close/volume 的 DataFrame
        params: 覆盖策略默认参数

    返回:
        (entries, exits) - 两个布尔数组
    """
    n = len(ohlcv)
    merged_params = {**strategy.params, **(params or {})}

    # 预计算指标缓存
    ctx = _build_indicator_context(ohlcv, merged_params)

    if strategy.strategy_type == "python":
        return _generate_python_signals(strategy, ohlcv, merged_params)

    # 评估entry条件 (AND逻辑)
    entry_mask = np.ones(n, dtype=bool)
    for cond in strategy.entry_conditions:
        mask = _eval_condition(cond, ctx, n)
        entry_mask &= mask

    # 评估exit条件 (AND逻辑)
    exit_mask = np.ones(n, dtype=bool)
    for cond in strategy.exit_conditions:
        mask = _eval_condition(cond, ctx, n)
        exit_mask &= mask

    # 清理：不在同一根K线同时开平仓，entry优先
    conflict = entry_mask & exit_mask
    exit_mask[conflict] = False

    return entry_mask, exit_mask


def _generate_python_signals(
    strategy: Strategy,
    ohlcv: pd.DataFrame,
    params: Dict[str, Any],
) -> Tuple[np.ndarray, np.ndarray]:
    """执行Python策略代码生成信号"""
    n = len(ohlcv)
    namespace = {
        "np": np,
        "pd": pd,
        "sma": _sma,
        "ema": _ema,
        "rsi": _rsi,
        "macd": _macd,
        "bollinger": _bollinger,
    }
    code = strategy.entry_conditions[0]["code"]
    exec(code, namespace)

    entry_func = namespace.get("entry_signal")
    exit_func = namespace.get("exit_signal")

    entries = entry_func(ohlcv, params) if entry_func else np.zeros(n, dtype=bool)
    exits = exit_func(ohlcv, params) if exit_func else np.zeros(n, dtype=bool)
    return entries.astype(bool), exits.astype(bool)


def _build_indicator_context(
    ohlcv: pd.DataFrame, params: Dict[str, Any]
) -> Dict[str, np.ndarray]:
    """预计算常用指标，建立名称→数组的映射"""
    close = ohlcv["close"].values.astype(np.float64)
    high = ohlcv["high"].values.astype(np.float64)
    low = ohlcv["low"].values.astype(np.float64)
    volume = ohlcv["volume"].values.astype(np.float64)
    open_ = ohlcv["open"].values.astype(np.float64)

    ctx: Dict[str, np.ndarray] = {
        "close": close,
        "open": open_,
        "high": high,
        "low": low,
        "volume": volume,
    }

    # 预计算常见均线周期
    for period in [5, 10, 20, 30, 60, 120, 200]:
        ctx[f"sma(close, {period})"] = _sma(close, period)
        ctx[f"ema(close, {period})"] = _ema(close, period)

    # 预计算参数中引用的自定义周期
    for key, val in params.items():
        if isinstance(val, int) and val > 0:
            ctx[f"sma(close, {key})"] = _sma(close, val)
            ctx[f"ema(close, {key})"] = _ema(close, val)
            # 也按参数名映射
            ctx[f"sma(close, {val})"] = _sma(close, val)
            ctx[f"ema(close, {val})"] = _ema(close, val)

    # RSI
    ctx["rsi(close, 14)"] = _rsi(close, 14)
    ctx["rsi(close, 6)"] = _rsi(close, 6)

    # MACD
    dif, dea, hist = _macd(close)
    ctx["macd_dif"] = dif
    ctx["macd_dea"] = dea
    ctx["macd_hist"] = hist

    # 布林带
    upper, mid, lower = _bollinger(close)
    ctx["boll_upper"] = upper
    ctx["boll_mid"] = mid
    ctx["boll_lower"] = lower

    return ctx


def _resolve_series(expr: str, ctx: Dict[str, np.ndarray]) -> np.ndarray:
    """将表达式字符串解析为数值数组"""
    expr = expr.strip()

    # 直接匹配上下文
    if expr in ctx:
        return ctx[expr]

    # 纯数字
    try:
        val = float(expr)
        n = len(next(iter(ctx.values())))
        return np.full(n, val, dtype=np.float64)
    except (ValueError, StopIteration):
        pass

    # 带括号的函数表达式 sma(close, 10)
    m = re.match(r'(sma|ema)\((\w+),\s*(\w+)\)', expr)
    if m:
        func_name, series_name, period_str = m.group(1), m.group(2), m.group(3)
        series = ctx.get(series_name)
        if series is None:
            raise ValueError(f"未知序列: {series_name}")
        try:
            period = int(period_str)
        except ValueError:
            # 可能是参数名，在ctx中查找
            raise ValueError(f"无法解析周期: {period_str}")
        func = _sma if func_name == "sma" else _ema
        result = func(series, period)
        ctx[expr] = result  # 缓存
        return result

    m = re.match(r'rsi\((\w+),\s*(\d+)\)', expr)
    if m:
        series = ctx.get(m.group(1))
        if series is not None:
            result = _rsi(series, int(m.group(2)))
            ctx[expr] = result
            return result

    # 回退：尝试在上下文中模糊查找
    for key, val in ctx.items():
        if expr in key:
            return val

    raise ValueError(f"无法解析表达式: {expr}")


def _eval_condition(
    cond: Dict[str, Any], ctx: Dict[str, np.ndarray], n: int
) -> np.ndarray:
    """评估单个条件，返回布尔数组"""
    ctype = cond["type"]

    if ctype == "cross_above":
        left = _resolve_series(cond["left"], ctx)
        right = _resolve_series(cond["right"], ctx)
        prev_below = np.roll(left, 1) <= np.roll(right, 1)
        curr_above = left > right
        result = prev_below & curr_above
        result[0] = False
        return result

    if ctype == "cross_below":
        left = _resolve_series(cond["left"], ctx)
        right = _resolve_series(cond["right"], ctx)
        prev_above = np.roll(left, 1) >= np.roll(right, 1)
        curr_below = left < right
        result = prev_above & curr_below
        result[0] = False
        return result

    if ctype.startswith("compare_"):
        left = _resolve_series(cond["left"], ctx)
        right = _resolve_series(cond["right"], ctx)
        op = ctype.replace("compare_", "")
        ops = {"gt": np.greater, "lt": np.less, "gte": np.greater_equal, "lte": np.less_equal, "eq": np.equal}
        func = ops.get(op)
        if func:
            # 处理NaN
            valid = ~(np.isnan(left) | np.isnan(right))
            result = np.zeros(n, dtype=bool)
            result[valid] = func(left[valid], right[valid])
            return result

    logger.warning(f"未知条件类型: {ctype}，返回全False")
    return np.zeros(n, dtype=bool)
