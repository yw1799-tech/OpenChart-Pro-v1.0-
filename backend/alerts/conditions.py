"""
警报条件定义与检测模块。
支持6种条件类型：价格、指标、均线交叉、成交量异常、涨跌幅、自定义公式。
"""

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from backend.data.models import Candle

logger = logging.getLogger(__name__)


# ──────────────────────── 辅助函数 ────────────────────────


def _safe_float(value: Any, default: float = 0.0) -> float:
    """安全转换为 float。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _crossover(series_a: List[float], series_b: List[float]) -> bool:
    """判断 series_a 是否向上穿越 series_b（前一根 a<=b，当前根 a>b）。"""
    if len(series_a) < 2 or len(series_b) < 2:
        return False
    return series_a[-2] <= series_b[-2] and series_a[-1] > series_b[-1]


def _crossunder(series_a: List[float], series_b: List[float]) -> bool:
    """判断 series_a 是否向下穿越 series_b（前一根 a>=b，当前根 a<b）。"""
    if len(series_a) < 2 or len(series_b) < 2:
        return False
    return series_a[-2] >= series_b[-2] and series_a[-1] < series_b[-1]


def _ema(data: List[float], period: int) -> List[float]:
    """计算 EMA 序列。"""
    if not data or period <= 0:
        return []
    result = []
    multiplier = 2.0 / (period + 1)
    ema_val = data[0]
    result.append(ema_val)
    for i in range(1, len(data)):
        ema_val = (data[i] - ema_val) * multiplier + ema_val
        result.append(ema_val)
    return result


def _sma(data: List[float], period: int) -> List[float]:
    """计算 SMA 序列。"""
    if not data or period <= 0:
        return []
    result = []
    for i in range(len(data)):
        if i < period - 1:
            result.append(sum(data[: i + 1]) / (i + 1))
        else:
            result.append(sum(data[i - period + 1 : i + 1]) / period)
    return result


def _rsi(closes: List[float], period: int = 14) -> List[float]:
    """计算 RSI 序列。"""
    if len(closes) < 2:
        return [50.0] * len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]

    result = [50.0]  # 第一根没有delta，用中性值填充

    avg_gain = sum(gains[:period]) / period if len(gains) >= period else sum(gains) / max(len(gains), 1)
    avg_loss = sum(losses[:period]) / period if len(losses) >= period else sum(losses) / max(len(losses), 1)

    for i in range(len(deltas)):
        if i < period:
            # 用简单平均
            g = sum(gains[: i + 1]) / (i + 1)
            l_ = sum(losses[: i + 1]) / (i + 1)
        elif i == period:
            g = avg_gain
            l_ = avg_loss
        else:
            g = (avg_gain * (period - 1) + gains[i]) / period
            l_ = (avg_loss * (period - 1) + losses[i]) / period
            avg_gain = g
            avg_loss = l_

        if l_ == 0:
            result.append(100.0)
        else:
            rs = g / l_
            result.append(100.0 - 100.0 / (1.0 + rs))

    return result


def _macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, List[float]]:
    """计算 MACD（DIF, DEA, MACD柱）。"""
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)
    macd_hist = [2.0 * (d - e) for d, e in zip(dif, dea)]
    return {"DIF": dif, "DEA": dea, "MACD": macd_hist}


def _compute_indicator(name: str, closes: List[float], params: Dict[str, Any]) -> List[float]:
    """根据指标名称和参数计算指标序列。"""
    name_upper = name.upper()
    if name_upper == "EMA":
        return _ema(closes, params.get("period", 20))
    elif name_upper == "SMA" or name_upper == "MA":
        return _sma(closes, params.get("period", 20))
    elif name_upper == "RSI":
        return _rsi(closes, params.get("period", 14))
    elif name_upper == "MACD":
        result = _macd(
            closes,
            fast=params.get("fast", 12),
            slow=params.get("slow", 26),
            signal=params.get("signal", 9),
        )
        component = params.get("component", "DIF")
        return result.get(component, result["DIF"])
    else:
        logger.warning(f"未知指标: {name}，返回空序列")
        return []


def _get_indicator_values(
    indicator_name: str,
    params: Dict[str, Any],
    candles: List[Candle],
    indicators: Dict[str, List[float]],
) -> List[float]:
    """优先从已有 indicators dict 取值，取不到则自行计算。"""
    # 尝试从预计算的指标中获取
    key_variants = [
        f"{indicator_name}_{params.get('period', '')}",
        indicator_name.upper(),
        indicator_name.lower(),
        f"{indicator_name.upper()}({params.get('period', '')})",
    ]
    for key in key_variants:
        if key in indicators and indicators[key]:
            return indicators[key]

    # 自行计算
    closes = [c.close for c in candles]
    return _compute_indicator(indicator_name, closes, params)


# ──────────────────────── 条件检测器 ────────────────────────


def _check_price(condition: Dict, candles: List[Candle], **_kw) -> Tuple[bool, str]:
    """价格条件检测。"""
    if len(candles) < 2:
        return False, ""

    operator = condition.get("operator", "above")
    target = _safe_float(condition.get("value", 0))
    symbol = condition.get("symbol", "")
    current = candles[-1].close
    prev = candles[-2].close

    triggered = False
    if operator == "above":
        triggered = current > target
    elif operator == "below":
        triggered = current < target
    elif operator == "cross_above":
        triggered = prev <= target and current > target
    elif operator == "cross_below":
        triggered = prev >= target and current < target

    if triggered:
        direction_map = {
            "above": "高于",
            "below": "低于",
            "cross_above": "向上突破",
            "cross_below": "向下跌破",
        }
        desc = direction_map.get(operator, operator)
        msg = f"{symbol} 当前价格 {current:.4f} {desc} {target:.4f}"
        return True, msg
    return False, ""


def _check_indicator(condition: Dict, candles: List[Candle], indicators: Dict, **_kw) -> Tuple[bool, str]:
    """指标条件检测。"""
    if len(candles) < 2:
        return False, ""

    ind_name = condition.get("indicator", "RSI")
    params = condition.get("params", {})
    operator = condition.get("operator", "above")
    target = _safe_float(condition.get("value", 0))

    values = _get_indicator_values(ind_name, params, candles, indicators)
    if len(values) < 2:
        return False, ""

    current = values[-1]
    prev = values[-2]

    triggered = False
    if operator == "above":
        triggered = current > target
    elif operator == "below":
        triggered = current < target
    elif operator == "cross_above":
        triggered = prev <= target and current > target
    elif operator == "cross_below":
        triggered = prev >= target and current < target

    if triggered:
        direction_map = {
            "above": "高于",
            "below": "低于",
            "cross_above": "向上突破",
            "cross_below": "向下跌破",
        }
        desc = direction_map.get(operator, operator)
        param_str = ", ".join(f"{k}={v}" for k, v in params.items())
        msg = f"{ind_name}({param_str}) = {current:.2f} {desc} {target:.2f}"
        return True, msg
    return False, ""


def _check_crossover(condition: Dict, candles: List[Candle], indicators: Dict, **_kw) -> Tuple[bool, str]:
    """均线交叉条件检测。"""
    if len(candles) < 2:
        return False, ""

    fast_cfg = condition.get("fast", {"indicator": "EMA", "params": {"period": 12}})
    slow_cfg = condition.get("slow", {"indicator": "EMA", "params": {"period": 26}})
    direction = condition.get("direction", "both")

    fast_values = _get_indicator_values(
        fast_cfg.get("indicator", "EMA"),
        fast_cfg.get("params", {}),
        candles,
        indicators,
    )
    slow_values = _get_indicator_values(
        slow_cfg.get("indicator", "EMA"),
        slow_cfg.get("params", {}),
        candles,
        indicators,
    )

    if len(fast_values) < 2 or len(slow_values) < 2:
        return False, ""

    fast_name = f"{fast_cfg.get('indicator', 'EMA')}({fast_cfg.get('params', {}).get('period', '')})"
    slow_name = f"{slow_cfg.get('indicator', 'EMA')}({slow_cfg.get('params', {}).get('period', '')})"

    if direction in ("golden", "both"):
        if _crossover(fast_values, slow_values):
            msg = f"金叉: {fast_name} 上穿 {slow_name}（{fast_values[-1]:.2f} > {slow_values[-1]:.2f}）"
            return True, msg

    if direction in ("death", "both"):
        if _crossunder(fast_values, slow_values):
            msg = f"死叉: {fast_name} 下穿 {slow_name}（{fast_values[-1]:.2f} < {slow_values[-1]:.2f}）"
            return True, msg

    return False, ""


def _check_volume(condition: Dict, candles: List[Candle], **_kw) -> Tuple[bool, str]:
    """成交量异常检测。"""
    multiplier = _safe_float(condition.get("multiplier", 3), 3.0)
    ma_period = int(condition.get("ma_period", 20))

    if len(candles) < ma_period + 1:
        return False, ""

    volumes = [c.volume for c in candles]
    vol_ma = sum(volumes[-(ma_period + 1) : -1]) / ma_period
    current_vol = volumes[-1]

    if vol_ma <= 0:
        return False, ""

    ratio = current_vol / vol_ma

    if ratio >= multiplier:
        msg = f"成交量异常放大: 当前成交量 {current_vol:.0f} 是 {ma_period} 周期均量的 {ratio:.1f} 倍（阈值 {multiplier:.1f}x）"
        return True, msg
    return False, ""


def _check_change(condition: Dict, candles: List[Candle], **_kw) -> Tuple[bool, str]:
    """涨跌幅检测。"""
    period = condition.get("period", "5m")
    threshold = _safe_float(condition.get("threshold", 3), 3.0)
    direction = condition.get("direction", "both")

    # 根据 period 确定回看K线数
    period_map = {
        "1m": 1,
        "5m": 1,
        "15m": 1,
        "30m": 1,
        "1h": 1,
        "4h": 1,
        "1d": 1,
        "3": 3,
        "5": 5,
        "10": 10,
        "20": 20,
    }
    # 尝试从 period 中提取数字（如 "5" 表示5根K线）
    lookback = period_map.get(period.lower(), 1)
    try:
        lookback = int(period)
    except (TypeError, ValueError):
        pass

    if len(candles) < lookback + 1:
        return False, ""

    ref_price = candles[-(lookback + 1)].close
    current_price = candles[-1].close

    if ref_price == 0:
        return False, ""

    pct_change = (current_price - ref_price) / ref_price * 100.0

    triggered = False
    if direction == "up" and pct_change >= threshold:
        triggered = True
    elif direction == "down" and pct_change <= -threshold:
        triggered = True
    elif direction == "both" and abs(pct_change) >= threshold:
        triggered = True

    if triggered:
        change_desc = "上涨" if pct_change > 0 else "下跌"
        msg = f"价格{change_desc} {abs(pct_change):.2f}%（{lookback} 周期内，阈值 {threshold:.1f}%）"
        return True, msg
    return False, ""


def _check_formula(condition: Dict, candles: List[Candle], indicators: Dict, **_kw) -> Tuple[bool, str]:
    """自定义公式条件检测。

    支持简单内置函数：crossover, crossunder, ema, sma, rsi, close, open, high, low, volume
    复杂公式通过 eval 在受限环境执行。
    """
    code = condition.get("code", "").strip()
    if not code:
        return False, ""

    closes = [c.close for c in candles]
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    volumes = [c.volume for c in candles]

    # 构建安全的执行环境
    safe_env = {
        "__builtins__": {},
        # 数据序列
        "close": closes,
        "open": opens,
        "high": highs,
        "low": lows,
        "volume": volumes,
        # 函数
        "ema": _ema,
        "sma": _sma,
        "rsi": lambda data, period=14: _rsi(data, period),
        "macd": lambda data, fast=12, slow=26, signal=9: _macd(data, fast, slow, signal),
        "crossover": _crossover,
        "crossunder": _crossunder,
        "abs": abs,
        "max": max,
        "min": min,
        "sum": sum,
        "len": len,
        "math": math,
    }

    try:
        result = eval(code, safe_env)  # noqa: S307
        if result:
            msg = f"自定义公式触发: {code}"
            return True, msg
    except Exception as e:
        logger.error(f"公式执行错误: {code!r} -> {e}")

    return False, ""


# ──────────────────────── 主入口 ────────────────────────

# 条件类型 -> 检测函数映射
_CONDITION_CHECKERS = {
    "price": _check_price,
    "indicator": _check_indicator,
    "crossover": _check_crossover,
    "volume": _check_volume,
    "change": _check_change,
    "formula": _check_formula,
}


def check_condition(
    condition: Dict[str, Any],
    candles: List[Candle],
    indicators: Optional[Dict[str, List[float]]] = None,
) -> Tuple[bool, str]:
    """
    检查单个警报条件是否触发。

    Args:
        condition: 条件字典，必须包含 "type" 字段。
        candles: 最近的K线数据列表（按时间升序）。
        indicators: 已计算的指标数据 {名称: [值, ...]}。

    Returns:
        (triggered: bool, message: str) — 是否触发及触发描述。
    """
    if indicators is None:
        indicators = {}

    cond_type = condition.get("type", "")
    checker = _CONDITION_CHECKERS.get(cond_type)

    if checker is None:
        logger.warning(f"不支持的条件类型: {cond_type}")
        return False, f"不支持的条件类型: {cond_type}"

    try:
        return checker(condition, candles, indicators=indicators)
    except Exception as e:
        logger.error(f"条件检测异常 [{cond_type}]: {e}", exc_info=True)
        return False, ""
