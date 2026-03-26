"""
指标注册表 - 管理所有内置指标的元数据与调用
"""

from backend.indicators.builtin import (
    calc_ma, calc_ema, calc_boll, calc_sar, calc_ichimoku, calc_vwap,
    calc_donchian, calc_envelope,
    calc_macd, calc_rsi, calc_kdj, calc_cci, calc_williams, calc_roc,
    calc_stoch_rsi, calc_mfi, calc_stoch,
    calc_obv, calc_cmf, calc_volume_ma,
    calc_atr, calc_stddev,
    calc_dmi, calc_trix, calc_adl,
)


def _p(name, type="int", default=None, min_val=None, max_val=None):
    """快捷构造参数描述"""
    param = {"name": name, "type": type, "default": default}
    if min_val is not None:
        param["min"] = min_val
    if max_val is not None:
        param["max"] = max_val
    return param


# ============================================================================
# 指标注册表
# ============================================================================

INDICATOR_REGISTRY = {
    # ------------------------------------------------------------------
    # 趋势类（主图叠加）
    # ------------------------------------------------------------------
    "MA": {
        "name": "简单移动平均线",
        "category": "trend",
        "overlay": True,
        "params": [
            _p("period", "int", 20, 1, 500),
        ],
        "outputs": ["ma"],
        "calc_func": "calc_ma",
    },
    "EMA": {
        "name": "指数移动平均线",
        "category": "trend",
        "overlay": True,
        "params": [
            _p("period", "int", 12, 1, 500),
        ],
        "outputs": ["ema"],
        "calc_func": "calc_ema",
    },
    "BOLL": {
        "name": "布林带",
        "category": "trend",
        "overlay": True,
        "params": [
            _p("period", "int", 20, 1, 500),
            _p("multiplier", "float", 2.0, 0.1, 10.0),
        ],
        "outputs": ["upper", "middle", "lower"],
        "calc_func": "calc_boll",
    },
    "SAR": {
        "name": "抛物线转向",
        "category": "trend",
        "overlay": True,
        "params": [
            _p("af_step", "float", 0.02, 0.001, 0.1),
            _p("af_max", "float", 0.2, 0.05, 1.0),
        ],
        "outputs": ["sar"],
        "calc_func": "calc_sar",
    },
    "ICHIMOKU": {
        "name": "一目均衡表",
        "category": "trend",
        "overlay": True,
        "params": [
            _p("tenkan", "int", 9, 1, 100),
            _p("kijun", "int", 26, 1, 100),
            _p("senkou", "int", 52, 1, 200),
        ],
        "outputs": ["tenkan_sen", "kijun_sen", "senkou_a", "senkou_b", "chikou_span"],
        "calc_func": "calc_ichimoku",
    },
    "VWAP": {
        "name": "成交量加权均价",
        "category": "trend",
        "overlay": True,
        "params": [],
        "outputs": ["vwap"],
        "calc_func": "calc_vwap",
    },
    "DONCHIAN": {
        "name": "唐奇安通道",
        "category": "trend",
        "overlay": True,
        "params": [
            _p("period", "int", 20, 1, 500),
        ],
        "outputs": ["upper", "middle", "lower"],
        "calc_func": "calc_donchian",
    },
    "ENVELOPE": {
        "name": "包络线",
        "category": "trend",
        "overlay": True,
        "params": [
            _p("period", "int", 20, 1, 500),
            _p("pct", "float", 0.05, 0.001, 0.5),
        ],
        "outputs": ["upper", "middle", "lower"],
        "calc_func": "calc_envelope",
    },

    # ------------------------------------------------------------------
    # 动量类（副图）
    # ------------------------------------------------------------------
    "MACD": {
        "name": "MACD",
        "category": "momentum",
        "overlay": False,
        "params": [
            _p("fast", "int", 12, 1, 100),
            _p("slow", "int", 26, 1, 200),
            _p("signal", "int", 9, 1, 100),
        ],
        "outputs": ["dif", "dea", "histogram"],
        "calc_func": "calc_macd",
    },
    "RSI": {
        "name": "相对强弱指标",
        "category": "momentum",
        "overlay": False,
        "params": [
            _p("period", "int", 14, 1, 100),
        ],
        "outputs": ["rsi"],
        "calc_func": "calc_rsi",
    },
    "KDJ": {
        "name": "KDJ随机指标",
        "category": "momentum",
        "overlay": False,
        "params": [
            _p("period", "int", 9, 1, 100),
            _p("k_smooth", "int", 3, 1, 20),
            _p("d_smooth", "int", 3, 1, 20),
        ],
        "outputs": ["k", "d", "j"],
        "calc_func": "calc_kdj",
    },
    "CCI": {
        "name": "商品通道指数",
        "category": "momentum",
        "overlay": False,
        "params": [
            _p("period", "int", 14, 1, 100),
        ],
        "outputs": ["cci"],
        "calc_func": "calc_cci",
    },
    "WILLIAMS": {
        "name": "威廉指标",
        "category": "momentum",
        "overlay": False,
        "params": [
            _p("period", "int", 14, 1, 100),
        ],
        "outputs": ["williams"],
        "calc_func": "calc_williams",
    },
    "ROC": {
        "name": "变化率",
        "category": "momentum",
        "overlay": False,
        "params": [
            _p("period", "int", 12, 1, 100),
        ],
        "outputs": ["roc"],
        "calc_func": "calc_roc",
    },
    "STOCH_RSI": {
        "name": "随机RSI",
        "category": "momentum",
        "overlay": False,
        "params": [
            _p("period", "int", 14, 1, 100),
            _p("k", "int", 3, 1, 20),
            _p("d", "int", 3, 1, 20),
        ],
        "outputs": ["k", "d"],
        "calc_func": "calc_stoch_rsi",
    },
    "MFI": {
        "name": "资金流量指标",
        "category": "momentum",
        "overlay": False,
        "params": [
            _p("period", "int", 14, 1, 100),
        ],
        "outputs": ["mfi"],
        "calc_func": "calc_mfi",
    },
    "STOCH": {
        "name": "随机指标",
        "category": "momentum",
        "overlay": False,
        "params": [
            _p("k_period", "int", 14, 1, 100),
            _p("d_period", "int", 3, 1, 20),
        ],
        "outputs": ["k", "d"],
        "calc_func": "calc_stoch",
    },

    # ------------------------------------------------------------------
    # 成交量类
    # ------------------------------------------------------------------
    "OBV": {
        "name": "能量潮",
        "category": "volume",
        "overlay": False,
        "params": [],
        "outputs": ["obv"],
        "calc_func": "calc_obv",
    },
    "CMF": {
        "name": "蔡金资金流量",
        "category": "volume",
        "overlay": False,
        "params": [
            _p("period", "int", 20, 1, 100),
        ],
        "outputs": ["cmf"],
        "calc_func": "calc_cmf",
    },
    "VOLUME_MA": {
        "name": "成交量均线",
        "category": "volume",
        "overlay": False,
        "params": [
            _p("period", "int", 20, 1, 500),
        ],
        "outputs": ["volume_ma"],
        "calc_func": "calc_volume_ma",
    },

    # ------------------------------------------------------------------
    # 波动类
    # ------------------------------------------------------------------
    "ATR": {
        "name": "平均真实波幅",
        "category": "volatility",
        "overlay": False,
        "params": [
            _p("period", "int", 14, 1, 100),
        ],
        "outputs": ["atr"],
        "calc_func": "calc_atr",
    },
    "STDDEV": {
        "name": "标准差",
        "category": "volatility",
        "overlay": False,
        "params": [
            _p("period", "int", 20, 1, 500),
        ],
        "outputs": ["stddev"],
        "calc_func": "calc_stddev",
    },

    # ------------------------------------------------------------------
    # 趋势强度类
    # ------------------------------------------------------------------
    "DMI": {
        "name": "趋势方向指标",
        "category": "strength",
        "overlay": False,
        "params": [
            _p("period", "int", 14, 1, 100),
        ],
        "outputs": ["plus_di", "minus_di", "adx"],
        "calc_func": "calc_dmi",
    },
    "TRIX": {
        "name": "三重指数平滑",
        "category": "strength",
        "overlay": False,
        "params": [
            _p("period", "int", 12, 1, 100),
        ],
        "outputs": ["trix", "signal"],
        "calc_func": "calc_trix",
    },
    "ADL": {
        "name": "累积派发线",
        "category": "strength",
        "overlay": False,
        "params": [],
        "outputs": ["adl"],
        "calc_func": "calc_adl",
    },
}

# ============================================================================
# 别名映射
# ============================================================================

_ALIASES = {
    "SMA": "MA",
    "BOLLINGER": "BOLL",
    "BB": "BOLL",
    "PARABOLIC_SAR": "SAR",
    "PSAR": "SAR",
    "WR": "WILLIAMS",
    "WILLR": "WILLIAMS",
    "ADX": "DMI",
    "STOCHASTIC": "STOCH",
    "VOL_MA": "VOLUME_MA",
}

# 将别名也注册到注册表中（引用同一份配置）
for alias, canonical in _ALIASES.items():
    INDICATOR_REGISTRY[alias] = INDICATOR_REGISTRY[canonical]


# ============================================================================
# 计算函数映射（calc_func字符串 -> 实际函数）
# ============================================================================

_CALC_FUNCTIONS = {
    "calc_ma": calc_ma,
    "calc_ema": calc_ema,
    "calc_boll": calc_boll,
    "calc_sar": calc_sar,
    "calc_ichimoku": calc_ichimoku,
    "calc_vwap": calc_vwap,
    "calc_donchian": calc_donchian,
    "calc_envelope": calc_envelope,
    "calc_macd": calc_macd,
    "calc_rsi": calc_rsi,
    "calc_kdj": calc_kdj,
    "calc_cci": calc_cci,
    "calc_williams": calc_williams,
    "calc_roc": calc_roc,
    "calc_stoch_rsi": calc_stoch_rsi,
    "calc_mfi": calc_mfi,
    "calc_stoch": calc_stoch,
    "calc_obv": calc_obv,
    "calc_cmf": calc_cmf,
    "calc_volume_ma": calc_volume_ma,
    "calc_atr": calc_atr,
    "calc_stddev": calc_stddev,
    "calc_dmi": calc_dmi,
    "calc_trix": calc_trix,
    "calc_adl": calc_adl,
}

# 各函数需要的OHLCV字段映射
_INPUT_FIELDS = {
    "calc_ma":        ["close"],
    "calc_ema":       ["close"],
    "calc_boll":      ["close"],
    "calc_sar":       ["high", "low"],
    "calc_ichimoku":  ["high", "low", "close"],
    "calc_vwap":      ["high", "low", "close", "volume"],
    "calc_donchian":  ["high", "low"],
    "calc_envelope":  ["close"],
    "calc_macd":      ["close"],
    "calc_rsi":       ["close"],
    "calc_kdj":       ["high", "low", "close"],
    "calc_cci":       ["high", "low", "close"],
    "calc_williams":  ["high", "low", "close"],
    "calc_roc":       ["close"],
    "calc_stoch_rsi": ["close"],
    "calc_mfi":       ["high", "low", "close", "volume"],
    "calc_stoch":     ["high", "low", "close"],
    "calc_obv":       ["close", "volume"],
    "calc_cmf":       ["high", "low", "close", "volume"],
    "calc_volume_ma": ["volume"],
    "calc_atr":       ["high", "low", "close"],
    "calc_stddev":    ["close"],
    "calc_dmi":       ["high", "low", "close"],
    "calc_trix":      ["close"],
    "calc_adl":       ["high", "low", "close", "volume"],
}


# ============================================================================
# 统一计算入口
# ============================================================================

def calculate_indicator(name, ohlcv_data, params=None):
    """根据注册表查找并调用对应的计算函数

    参数:
        name: 指标名称（大写，如 "MA", "MACD", "RSI"），支持别名
        ohlcv_data: dict，包含 open/high/low/close/volume 的numpy数组
        params: dict，覆盖默认参数值（可选）

    返回:
        numpy数组（单输出指标）或 dict（多输出指标）

    异常:
        KeyError: 指标未注册
        ValueError: 缺少所需的OHLCV字段
    """
    name_upper = name.upper()

    if name_upper not in INDICATOR_REGISTRY:
        raise KeyError(f"未注册的指标: {name}")

    meta = INDICATOR_REGISTRY[name_upper]
    func_name = meta["calc_func"]
    func = _CALC_FUNCTIONS[func_name]
    required_fields = _INPUT_FIELDS[func_name]

    # 检查必需字段
    for field in required_fields:
        if field not in ohlcv_data:
            raise ValueError(f"指标 {name_upper} 需要 '{field}' 字段，但未在ohlcv_data中提供")

    # 构造位置参数（OHLCV数据）
    positional_args = [ohlcv_data[field] for field in required_fields]

    # 构造关键字参数（用户参数覆盖默认值）
    kwargs = {}
    default_params = {p["name"]: p["default"] for p in meta["params"]}
    if params:
        default_params.update(params)
    kwargs = default_params

    return func(*positional_args, **kwargs)


def get_indicator_info(name):
    """获取指标的元信息"""
    name_upper = name.upper()
    if name_upper not in INDICATOR_REGISTRY:
        return None
    return INDICATOR_REGISTRY[name_upper]


def list_indicators(category=None):
    """列出所有指标（可按类别过滤）

    参数:
        category: 可选，"trend"/"momentum"/"volume"/"volatility"/"strength"

    返回:
        list[dict]，每项包含 id, name, category, overlay
    """
    seen = set()
    result = []
    for ind_id, meta in INDICATOR_REGISTRY.items():
        # 跳过别名的重复
        if id(meta) in seen:
            continue
        seen.add(id(meta))
        if category and meta["category"] != category:
            continue
        result.append({
            "id": ind_id,
            "name": meta["name"],
            "category": meta["category"],
            "overlay": meta["overlay"],
        })
    return result
