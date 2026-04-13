"""
ScreenerEngine - 规则筛选引擎
支持价格/涨跌幅/成交量/技术指标等多维度筛选
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ======================================================================
# 指标计算（纯numpy）
# ======================================================================


def _sma(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(arr, np.nan, dtype=np.float64)
    if len(arr) < period:
        return out
    cumsum = np.cumsum(arr)
    cumsum[period:] = cumsum[period:] - cumsum[:-period]
    out[period - 1 :] = cumsum[period - 1 :] / period
    return out


def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(arr, np.nan, dtype=np.float64)
    if len(arr) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = np.mean(arr[:period])
    for i in range(period, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def _rsi(close: np.ndarray, period: int = 14) -> float:
    """计算最新RSI值"""
    if len(close) < period + 1:
        return np.nan
    delta = np.diff(close[-(period + 1) :])
    gain = np.mean(delta[delta > 0]) if np.any(delta > 0) else 0.0
    loss = np.mean(-delta[delta < 0]) if np.any(delta < 0) else 1e-10
    rs = gain / loss if loss > 0 else 100.0
    return 100.0 - 100.0 / (1.0 + rs)


def _macd(close: np.ndarray, fast=12, slow=26, signal=9):
    """返回最近两根K线的MACD DIF/DEA值，用于判断金叉/死叉"""
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    dif = ema_fast - ema_slow
    dea = _ema(np.nan_to_num(dif, nan=0.0), signal)
    return dif, dea


def _bollinger(close: np.ndarray, period=20, std_dev=2.0):
    """返回最新的布林上轨/下轨"""
    if len(close) < period:
        return np.nan, np.nan
    mid = np.mean(close[-period:])
    std = np.std(close[-period:], ddof=0)
    return mid + std_dev * std, mid - std_dev * std


# ======================================================================
# 筛选引擎
# ======================================================================


class ScreenerEngine:
    """
    规则筛选引擎。

    使用方式:
        engine = ScreenerEngine()
        results = await engine.screen(
            markets=["A股", "US"],
            filters=[
                {"type": "price_above_ma", "params": {"period": 20}},
                {"type": "rsi_below", "params": {"value": 30}},
            ],
            sort_by="change_pct",
            sort_order="desc",
            limit=50,
        )
    """

    FILTER_TYPES = {
        "price_above",  # 价格高于某值
        "price_below",  # 价格低于某值
        "change_pct_above",  # 涨幅大于
        "change_pct_below",  # 跌幅大于（传负值或正值表示绝对值）
        "volume_above_ma",  # 成交量大于MA均量
        "turnover_above",  # 成交额大于
        "rsi_above",  # RSI高于
        "rsi_below",  # RSI低于
        "macd_golden_cross",  # MACD金叉
        "macd_death_cross",  # MACD死叉
        "price_above_ma",  # 价格在均线上方
        "price_below_ma",  # 价格在均线下方
        "boll_upper_break",  # 突破布林上轨
        "boll_lower_break",  # 跌破布林下轨
        "new_high",  # N日新高
        "new_low",  # N日新低
    }

    async def screen(
        self,
        markets: List[str],
        filters: List[Dict[str, Any]],
        sort_by: str = "change_pct",
        sort_order: str = "desc",
        limit: int = 50,
    ) -> Dict[str, Any]:
        """
        执行筛选。

        1. 从数据库加载各市场品种的最近K线
        2. 批量计算指标
        3. 逐品种检查所有条件 (AND)
        4. 排序并截取结果

        返回:
            {"results": [...], "total_matched": N, "filters_applied": [...]}
        """
        # 验证筛选条件
        for f in filters:
            if f["type"] not in self.FILTER_TYPES:
                raise ValueError(f"不支持的筛选条件: {f['type']}，支持: {self.FILTER_TYPES}")

        # 加载品种列表与K线
        symbols = await self._load_symbols(markets)
        matched: List[Dict] = []

        for sym_info in symbols:
            symbol = sym_info["symbol"]
            try:
                ohlcv = await self._load_kline(symbol, limit_bars=200)
                if ohlcv is None or len(ohlcv) < 5:
                    continue

                # 检查所有条件
                if self._check_all_filters(ohlcv, sym_info, filters):
                    row = self._build_result_row(ohlcv, sym_info)
                    matched.append(row)

            except Exception as e:
                logger.debug(f"筛选 {symbol} 异常: {e}")
                continue

        # 排序
        reverse = sort_order == "desc"
        matched.sort(key=lambda x: x.get(sort_by, 0) or 0, reverse=reverse)

        return {
            "results": matched[:limit],
            "total_matched": len(matched),
            "filters_applied": [f["type"] for f in filters],
        }

    # ------------------------------------------------------------------
    # 条件检查
    # ------------------------------------------------------------------

    def _check_all_filters(
        self,
        ohlcv: pd.DataFrame,
        sym_info: Dict,
        filters: List[Dict],
    ) -> bool:
        """检查品种是否满足所有筛选条件"""
        close = ohlcv["close"].values.astype(np.float64)
        volume = ohlcv["volume"].values.astype(np.float64)
        high = ohlcv["high"].values.astype(np.float64)
        low = ohlcv["low"].values.astype(np.float64)

        latest_close = close[-1]
        prev_close = close[-2] if len(close) > 1 else latest_close
        change_pct = (latest_close / prev_close - 1) * 100 if prev_close > 0 else 0

        for f in filters:
            ftype = f["type"]
            params = f.get("params", {})

            if ftype == "price_above":
                if latest_close <= params.get("value", 0):
                    return False

            elif ftype == "price_below":
                if latest_close >= params.get("value", float("inf")):
                    return False

            elif ftype == "change_pct_above":
                if change_pct <= params.get("value", 0):
                    return False

            elif ftype == "change_pct_below":
                if change_pct >= params.get("value", 0):
                    return False

            elif ftype == "volume_above_ma":
                period = params.get("period", 20)
                vol_ma = _sma(volume, period)
                if np.isnan(vol_ma[-1]) or volume[-1] <= vol_ma[-1]:
                    return False

            elif ftype == "turnover_above":
                # turnover = close * volume (近似)
                turnover = latest_close * volume[-1]
                if turnover <= params.get("value", 0):
                    return False

            elif ftype == "rsi_above":
                period = params.get("period", 14)
                value = params.get("value", 70)
                rsi = _rsi(close, period)
                if np.isnan(rsi) or rsi <= value:
                    return False

            elif ftype == "rsi_below":
                period = params.get("period", 14)
                value = params.get("value", 30)
                rsi = _rsi(close, period)
                if np.isnan(rsi) or rsi >= value:
                    return False

            elif ftype == "macd_golden_cross":
                dif, dea = _macd(close)
                if np.isnan(dif[-1]) or np.isnan(dif[-2]):
                    return False
                # 金叉: 前一根DIF<=DEA，当前DIF>DEA
                if not (dif[-2] <= dea[-2] and dif[-1] > dea[-1]):
                    return False

            elif ftype == "macd_death_cross":
                dif, dea = _macd(close)
                if np.isnan(dif[-1]) or np.isnan(dif[-2]):
                    return False
                if not (dif[-2] >= dea[-2] and dif[-1] < dea[-1]):
                    return False

            elif ftype == "price_above_ma":
                period = params.get("period", 20)
                ma = _sma(close, period)
                if np.isnan(ma[-1]) or latest_close <= ma[-1]:
                    return False

            elif ftype == "price_below_ma":
                period = params.get("period", 20)
                ma = _sma(close, period)
                if np.isnan(ma[-1]) or latest_close >= ma[-1]:
                    return False

            elif ftype == "boll_upper_break":
                upper, _ = _bollinger(close)
                if np.isnan(upper) or latest_close <= upper:
                    return False

            elif ftype == "boll_lower_break":
                _, lower = _bollinger(close)
                if np.isnan(lower) or latest_close >= lower:
                    return False

            elif ftype == "new_high":
                period = params.get("period", 20)
                if len(high) < period:
                    return False
                if high[-1] < np.max(high[-period:]):
                    return False

            elif ftype == "new_low":
                period = params.get("period", 20)
                if len(low) < period:
                    return False
                if low[-1] > np.min(low[-period:]):
                    return False

        return True

    # ------------------------------------------------------------------
    # 结果构建
    # ------------------------------------------------------------------

    def _build_result_row(self, ohlcv: pd.DataFrame, sym_info: Dict) -> Dict:
        """构建单个筛选结果行"""
        close = ohlcv["close"].values
        volume = ohlcv["volume"].values
        latest = close[-1]
        prev = close[-2] if len(close) > 1 else latest
        change_pct = (latest / prev - 1) * 100 if prev > 0 else 0

        # RSI
        rsi_val = _rsi(close, 14)

        # 成交量比率
        vol_ma = _sma(volume.astype(np.float64), 20)
        vol_ratio = volume[-1] / vol_ma[-1] if not np.isnan(vol_ma[-1]) and vol_ma[-1] > 0 else 1.0

        return {
            "symbol": sym_info.get("symbol", ""),
            "name": sym_info.get("name", ""),
            "market": sym_info.get("market", ""),
            "price": round(float(latest), 4),
            "change_pct": round(float(change_pct), 2),
            "volume": int(volume[-1]),
            "turnover": round(float(latest * volume[-1]), 2),
            "rsi": round(float(rsi_val), 2) if not np.isnan(rsi_val) else None,
            "volume_ratio": round(float(vol_ratio), 2),
            "high_52w": round(float(np.max(close[-252:])), 4) if len(close) >= 252 else round(float(np.max(close)), 4),
            "low_52w": round(float(np.min(close[-252:])), 4) if len(close) >= 252 else round(float(np.min(close)), 4),
        }

    # ------------------------------------------------------------------
    # 数据加载 (可覆盖)
    # ------------------------------------------------------------------

    async def _load_symbols(self, markets: List[str]) -> List[Dict]:
        """从数据库加载品种列表"""
        try:
            from backend.db.database import get_database

            db = await get_database()
            query = {}
            if markets:
                query["market"] = {"$in": markets}
            cursor = db["symbols"].find(query, {"_id": 0})
            return await cursor.to_list(length=None)
        except Exception as e:
            logger.error(f"加载品种列表失败: {e}")
            return []

    async def _load_kline(self, symbol: str, limit_bars: int = 200) -> Optional[pd.DataFrame]:
        """从数据库加载最近N根K线"""
        try:
            from backend.db.database import get_database

            db = await get_database()
            collection_name = f"kline_{symbol.lower().replace('/', '_')}_1d"
            cursor = db[collection_name].find({}, {"_id": 0}).sort("timestamp", -1).limit(limit_bars)

            records = await cursor.to_list(length=None)
            if not records:
                return None

            df = pd.DataFrame(records)
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df.set_index("timestamp", inplace=True)
            df.sort_index(inplace=True)
            return df
        except Exception as e:
            logger.debug(f"加载K线失败 {symbol}: {e}")
            return None
