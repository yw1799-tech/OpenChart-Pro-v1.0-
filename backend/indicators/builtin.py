"""
内置技术指标库 - 纯NumPy实现
所有函数输入为numpy数组，输出为numpy数组或dict
前期数据不足部分用NaN填充
"""

import numpy as np


# ============================================================================
# 工具函数
# ============================================================================


def _ensure_float(arr):
    """确保输入为float64 numpy数组"""
    return np.asarray(arr, dtype=np.float64)


def _rolling_max(arr, period):
    """滚动最大值"""
    n = len(arr)
    result = np.full(n, np.nan)
    for i in range(period - 1, n):
        result[i] = np.nanmax(arr[i - period + 1 : i + 1])
    return result


def _rolling_min(arr, period):
    """滚动最小值"""
    n = len(arr)
    result = np.full(n, np.nan)
    for i in range(period - 1, n):
        result[i] = np.nanmin(arr[i - period + 1 : i + 1])
    return result


def _rolling_sum(arr, period):
    """滚动求和"""
    n = len(arr)
    result = np.full(n, np.nan)
    cumsum = np.nancumsum(arr)
    result[period - 1] = cumsum[period - 1]
    for i in range(period, n):
        result[i] = cumsum[i] - cumsum[i - period]
    return result


def _rolling_mean(arr, period):
    """滚动均值"""
    return _rolling_sum(arr, period) / period


def _ema_core(arr, period):
    """指数移动平均核心实现"""
    n = len(arr)
    result = np.full(n, np.nan)
    alpha = 2.0 / (period + 1)
    # 找到第一个非NaN值作为起点
    start = 0
    while start < n and np.isnan(arr[start]):
        start += 1
    if start >= n:
        return result
    result[start] = arr[start]
    for i in range(start + 1, n):
        if np.isnan(arr[i]):
            result[i] = result[i - 1]
        else:
            result[i] = alpha * arr[i] + (1 - alpha) * result[i - 1]
    return result


def _wilder_smooth(arr, period):
    """Wilder平滑（用于RSI、ATR、DMI等）"""
    n = len(arr)
    result = np.full(n, np.nan)
    start = 0
    while start < n and np.isnan(arr[start]):
        start += 1
    if start + period > n:
        return result
    result[start + period - 1] = np.nanmean(arr[start : start + period])
    for i in range(start + period, n):
        result[i] = (result[i - 1] * (period - 1) + arr[i]) / period
    return result


def _true_range(high, low, close):
    """真实波幅"""
    high = _ensure_float(high)
    low = _ensure_float(low)
    close = _ensure_float(close)
    n = len(high)
    tr = np.full(n, np.nan)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    return tr


# ============================================================================
# 趋势类指标（主图叠加）
# ============================================================================


def calc_ma(close, period=20):
    """简单移动平均线 (SMA/MA)"""
    close = _ensure_float(close)
    return _rolling_mean(close, period)


def calc_ema(close, period=12):
    """指数移动平均线 (EMA)"""
    close = _ensure_float(close)
    return _ema_core(close, period)


def calc_boll(close, period=20, multiplier=2):
    """布林带 (Bollinger Bands)
    返回: dict(upper, middle, lower)
    """
    close = _ensure_float(close)
    middle = _rolling_mean(close, period)
    n = len(close)
    std = np.full(n, np.nan)
    for i in range(period - 1, n):
        std[i] = np.nanstd(close[i - period + 1 : i + 1], ddof=0)
    upper = middle + multiplier * std
    lower = middle - multiplier * std
    return {"upper": upper, "middle": middle, "lower": lower}


def calc_sar(high, low, af_step=0.02, af_max=0.2):
    """抛物线转向指标 (Parabolic SAR)"""
    high = _ensure_float(high)
    low = _ensure_float(low)
    n = len(high)
    sar = np.full(n, np.nan)
    if n < 2:
        return sar

    # 初始化：假设第一根为上升趋势
    bull = True
    af = af_step
    ep = high[0]
    sar[0] = low[0]

    for i in range(1, n):
        prev_sar = sar[i - 1]

        if bull:
            sar_val = prev_sar + af * (ep - prev_sar)
            # SAR不能高于前两根K线的最低价
            if i >= 2:
                sar_val = min(sar_val, low[i - 1], low[i - 2])
            else:
                sar_val = min(sar_val, low[i - 1])

            if low[i] < sar_val:
                # 转为下降趋势
                bull = False
                sar_val = ep
                ep = low[i]
                af = af_step
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
        else:
            sar_val = prev_sar + af * (ep - prev_sar)
            # SAR不能低于前两根K线的最高价
            if i >= 2:
                sar_val = max(sar_val, high[i - 1], high[i - 2])
            else:
                sar_val = max(sar_val, high[i - 1])

            if high[i] > sar_val:
                # 转为上升趋势
                bull = True
                sar_val = ep
                ep = high[i]
                af = af_step
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)

        sar[i] = sar_val

    return sar


def calc_ichimoku(high, low, close, tenkan=9, kijun=26, senkou=52):
    """一目均衡表 (Ichimoku Cloud)
    返回: dict(tenkan_sen, kijun_sen, senkou_a, senkou_b, chikou_span)
    """
    high = _ensure_float(high)
    low = _ensure_float(low)
    close = _ensure_float(close)
    n = len(high)

    # 转换线 = (tenkan周期最高 + tenkan周期最低) / 2
    tenkan_high = _rolling_max(high, tenkan)
    tenkan_low = _rolling_min(low, tenkan)
    tenkan_sen = (tenkan_high + tenkan_low) / 2

    # 基准线 = (kijun周期最高 + kijun周期最低) / 2
    kijun_high = _rolling_max(high, kijun)
    kijun_low = _rolling_min(low, kijun)
    kijun_sen = (kijun_high + kijun_low) / 2

    # 先行带A = (转换线 + 基准线) / 2，前移kijun周期
    senkou_a_raw = (tenkan_sen + kijun_sen) / 2
    senkou_a = np.full(n, np.nan)
    if n > kijun:
        senkou_a[kijun:] = senkou_a_raw[: n - kijun]

    # 先行带B = (senkou周期最高 + senkou周期最低) / 2，前移kijun周期
    senkou_high = _rolling_max(high, senkou)
    senkou_low = _rolling_min(low, senkou)
    senkou_b_raw = (senkou_high + senkou_low) / 2
    senkou_b = np.full(n, np.nan)
    if n > kijun:
        senkou_b[kijun:] = senkou_b_raw[: n - kijun]

    # 迟行线 = 收盘价后移kijun周期
    chikou_span = np.full(n, np.nan)
    if n > kijun:
        chikou_span[: n - kijun] = close[kijun:]

    return {
        "tenkan_sen": tenkan_sen,
        "kijun_sen": kijun_sen,
        "senkou_a": senkou_a,
        "senkou_b": senkou_b,
        "chikou_span": chikou_span,
    }


def calc_vwap(high, low, close, volume):
    """成交量加权均价 (VWAP)"""
    high = _ensure_float(high)
    low = _ensure_float(low)
    close = _ensure_float(close)
    volume = _ensure_float(volume)

    typical_price = (high + low + close) / 3
    cum_tp_vol = np.nancumsum(typical_price * volume)
    cum_vol = np.nancumsum(volume)
    vwap = np.where(cum_vol != 0, cum_tp_vol / cum_vol, np.nan)
    return vwap


def calc_donchian(high, low, period=20):
    """唐奇安通道 (Donchian Channel)
    返回: dict(upper, middle, lower)
    """
    high = _ensure_float(high)
    low = _ensure_float(low)
    upper = _rolling_max(high, period)
    lower = _rolling_min(low, period)
    middle = (upper + lower) / 2
    return {"upper": upper, "middle": middle, "lower": lower}


def calc_envelope(close, period=20, pct=0.05):
    """包络线 (Envelope / Moving Average Envelope)
    返回: dict(upper, middle, lower)
    """
    close = _ensure_float(close)
    middle = _rolling_mean(close, period)
    upper = middle * (1 + pct)
    lower = middle * (1 - pct)
    return {"upper": upper, "middle": middle, "lower": lower}


# ============================================================================
# 动量类指标（副图）
# ============================================================================


def calc_macd(close, fast=12, slow=26, signal=9):
    """MACD指标
    返回: dict(dif, dea, histogram)
    """
    close = _ensure_float(close)
    ema_fast = _ema_core(close, fast)
    ema_slow = _ema_core(close, slow)
    dif = ema_fast - ema_slow
    dea = _ema_core(dif, signal)
    histogram = 2 * (dif - dea)
    return {"dif": dif, "dea": dea, "histogram": histogram}


def calc_rsi(close, period=14):
    """相对强弱指标 (RSI)"""
    close = _ensure_float(close)
    n = len(close)
    rsi = np.full(n, np.nan)

    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    # 使用Wilder平滑
    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)

    if n <= period:
        return rsi

    avg_gain[period] = np.mean(gain[:period])
    avg_loss[period] = np.mean(loss[:period])

    for i in range(period + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i - 1]) / period

    for i in range(period, n):
        if avg_loss[i] == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            rsi[i] = 100.0 - 100.0 / (1.0 + rs)

    return rsi


def calc_kdj(high, low, close, period=9, k_smooth=3, d_smooth=3):
    """KDJ随机指标
    返回: dict(k, d, j)
    """
    high = _ensure_float(high)
    low = _ensure_float(low)
    close = _ensure_float(close)
    n = len(close)

    highest = _rolling_max(high, period)
    lowest = _rolling_min(low, period)

    rsv = np.full(n, np.nan)
    denom = highest - lowest
    valid = denom != 0
    rsv[valid] = (close[valid] - lowest[valid]) / denom[valid] * 100
    # 当最高=最低时RSV设为50
    rsv[~valid & ~np.isnan(highest)] = 50.0

    # K = SMA(RSV, k_smooth)  这里用Wilder风格平滑
    k = np.full(n, np.nan)
    d = np.full(n, np.nan)

    # 找到rsv第一个有效值
    start = 0
    while start < n and np.isnan(rsv[start]):
        start += 1
    if start >= n:
        return {"k": k, "d": d, "j": np.full(n, np.nan)}

    k[start] = 50.0
    d[start] = 50.0
    for i in range(start + 1, n):
        if np.isnan(rsv[i]):
            k[i] = k[i - 1]
        else:
            k[i] = (k[i - 1] * (k_smooth - 1) + rsv[i]) / k_smooth
        d[i] = (d[i - 1] * (d_smooth - 1) + k[i]) / d_smooth

    j = 3 * k - 2 * d
    return {"k": k, "d": d, "j": j}


def calc_cci(high, low, close, period=14):
    """商品通道指数 (CCI)"""
    high = _ensure_float(high)
    low = _ensure_float(low)
    close = _ensure_float(close)
    n = len(close)

    tp = (high + low + close) / 3
    tp_ma = _rolling_mean(tp, period)

    # 平均偏差
    md = np.full(n, np.nan)
    for i in range(period - 1, n):
        md[i] = np.mean(np.abs(tp[i - period + 1 : i + 1] - tp_ma[i]))

    cci = np.full(n, np.nan)
    valid = md != 0
    cci[valid] = (tp[valid] - tp_ma[valid]) / (0.015 * md[valid])
    return cci


def calc_williams(high, low, close, period=14):
    """威廉指标 (Williams %R)"""
    high = _ensure_float(high)
    low = _ensure_float(low)
    close = _ensure_float(close)

    highest = _rolling_max(high, period)
    lowest = _rolling_min(low, period)

    denom = highest - lowest
    wr = np.full(len(close), np.nan)
    valid = denom != 0
    wr[valid] = (highest[valid] - close[valid]) / denom[valid] * (-100)
    return wr


def calc_roc(close, period=12):
    """变化率 (Rate of Change)"""
    close = _ensure_float(close)
    n = len(close)
    roc = np.full(n, np.nan)
    for i in range(period, n):
        if close[i - period] != 0:
            roc[i] = (close[i] - close[i - period]) / close[i - period] * 100
    return roc


def calc_stoch_rsi(close, period=14, k=3, d=3):
    """随机RSI (Stochastic RSI)
    返回: dict(k, d)
    """
    close = _ensure_float(close)
    rsi = calc_rsi(close, period)
    n = len(close)

    rsi_highest = _rolling_max(rsi, period)
    rsi_lowest = _rolling_min(rsi, period)

    denom = rsi_highest - rsi_lowest
    stoch_rsi = np.full(n, np.nan)
    valid = (denom != 0) & ~np.isnan(denom)
    stoch_rsi[valid] = (rsi[valid] - rsi_lowest[valid]) / denom[valid] * 100

    k_line = _rolling_mean(stoch_rsi, k)
    d_line = _rolling_mean(k_line, d)
    return {"k": k_line, "d": d_line}


def calc_mfi(high, low, close, volume, period=14):
    """资金流量指标 (Money Flow Index)"""
    high = _ensure_float(high)
    low = _ensure_float(low)
    close = _ensure_float(close)
    volume = _ensure_float(volume)
    n = len(close)

    tp = (high + low + close) / 3
    raw_mf = tp * volume

    pos_mf = np.zeros(n)
    neg_mf = np.zeros(n)
    for i in range(1, n):
        if tp[i] > tp[i - 1]:
            pos_mf[i] = raw_mf[i]
        elif tp[i] < tp[i - 1]:
            neg_mf[i] = raw_mf[i]

    pos_sum = _rolling_sum(pos_mf, period)
    neg_sum = _rolling_sum(neg_mf, period)

    mfi = np.full(n, np.nan)
    valid = neg_sum != 0
    mfi[valid] = 100.0 - 100.0 / (1.0 + pos_sum[valid] / neg_sum[valid])
    # 当负资金流为0时MFI=100
    zero_neg = (neg_sum == 0) & ~np.isnan(neg_sum)
    mfi[zero_neg] = 100.0
    return mfi


def calc_stoch(high, low, close, k_period=14, d_period=3):
    """随机指标 (Stochastic Oscillator)
    返回: dict(k, d)
    """
    high = _ensure_float(high)
    low = _ensure_float(low)
    close = _ensure_float(close)

    highest = _rolling_max(high, k_period)
    lowest = _rolling_min(low, k_period)

    denom = highest - lowest
    n = len(close)
    k = np.full(n, np.nan)
    valid = denom != 0
    k[valid] = (close[valid] - lowest[valid]) / denom[valid] * 100

    d = _rolling_mean(k, d_period)
    return {"k": k, "d": d}


# ============================================================================
# 成交量类指标
# ============================================================================


def calc_obv(close, volume):
    """能量潮 (On Balance Volume)"""
    close = _ensure_float(close)
    volume = _ensure_float(volume)
    n = len(close)

    obv = np.full(n, np.nan)
    obv[0] = volume[0]
    for i in range(1, n):
        if close[i] > close[i - 1]:
            obv[i] = obv[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            obv[i] = obv[i - 1] - volume[i]
        else:
            obv[i] = obv[i - 1]
    return obv


def calc_cmf(high, low, close, volume, period=20):
    """蔡金资金流量 (Chaikin Money Flow)"""
    high = _ensure_float(high)
    low = _ensure_float(low)
    close = _ensure_float(close)
    volume = _ensure_float(volume)

    denom = high - low
    mfm = np.where(denom != 0, ((close - low) - (high - close)) / denom, 0.0)
    mfv = mfm * volume

    mfv_sum = _rolling_sum(mfv, period)
    vol_sum = _rolling_sum(volume, period)

    cmf = np.full(len(close), np.nan)
    valid = vol_sum != 0
    cmf[valid] = mfv_sum[valid] / vol_sum[valid]
    return cmf


def calc_volume_ma(volume, period=20):
    """成交量移动平均线"""
    volume = _ensure_float(volume)
    return _rolling_mean(volume, period)


# ============================================================================
# 波动类指标
# ============================================================================


def calc_atr(high, low, close, period=14):
    """平均真实波幅 (Average True Range)"""
    tr = _true_range(high, low, close)
    return _wilder_smooth(tr, period)


def calc_stddev(close, period=20):
    """标准差 (Standard Deviation)"""
    close = _ensure_float(close)
    n = len(close)
    result = np.full(n, np.nan)
    for i in range(period - 1, n):
        result[i] = np.std(close[i - period + 1 : i + 1], ddof=0)
    return result


# ============================================================================
# 趋势强度类指标
# ============================================================================


def calc_dmi(high, low, close, period=14):
    """趋势方向指标 (DMI / ADX)
    返回: dict(plus_di, minus_di, adx)
    """
    high = _ensure_float(high)
    low = _ensure_float(low)
    close = _ensure_float(close)
    n = len(high)

    # +DM 和 -DM
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        if up > down and up > 0:
            plus_dm[i] = up
        if down > up and down > 0:
            minus_dm[i] = down

    tr = _true_range(high, low, close)

    # Wilder平滑
    atr = _wilder_smooth(tr, period)
    smooth_plus_dm = _wilder_smooth(plus_dm, period)
    smooth_minus_dm = _wilder_smooth(minus_dm, period)

    # +DI 和 -DI
    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    valid = atr != 0
    plus_di[valid] = (smooth_plus_dm[valid] / atr[valid]) * 100
    minus_di[valid] = (smooth_minus_dm[valid] / atr[valid]) * 100

    # DX 和 ADX
    dx = np.full(n, np.nan)
    di_sum = plus_di + minus_di
    valid_di = di_sum != 0
    dx[valid_di] = np.abs(plus_di[valid_di] - minus_di[valid_di]) / di_sum[valid_di] * 100

    adx = _wilder_smooth(dx, period)

    return {"plus_di": plus_di, "minus_di": minus_di, "adx": adx}


def calc_trix(close, period=12):
    """三重指数平滑平均 (TRIX)
    返回: dict(trix, signal)
    """
    close = _ensure_float(close)

    ema1 = _ema_core(close, period)
    ema2 = _ema_core(ema1, period)
    ema3 = _ema_core(ema2, period)

    n = len(close)
    trix = np.full(n, np.nan)
    for i in range(1, n):
        if ema3[i - 1] != 0 and not np.isnan(ema3[i - 1]):
            trix[i] = (ema3[i] - ema3[i - 1]) / ema3[i - 1] * 100

    signal = _ema_core(trix, 9)
    return {"trix": trix, "signal": signal}


def calc_adl(high, low, close, volume):
    """累积/派发线 (Accumulation/Distribution Line)"""
    high = _ensure_float(high)
    low = _ensure_float(low)
    close = _ensure_float(close)
    volume = _ensure_float(volume)

    denom = high - low
    mfm = np.where(denom != 0, ((close - low) - (high - close)) / denom, 0.0)
    mfv = mfm * volume
    adl = np.nancumsum(mfv)
    return adl
