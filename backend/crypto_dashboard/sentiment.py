"""
情绪指标模块
数据源: Alternative.me, OKX
"""
import aiohttp
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class SentimentData:
    """市场情绪数据聚合器，提供恐惧贪婪指数、资金费率、持仓量、多空比"""

    def __init__(self):
        self.okx_base = "https://www.okx.com/api/v5"
        self.timeout = aiohttp.ClientTimeout(total=15)

    async def get_fear_greed_index(self) -> dict:
        """
        恐惧贪婪指数
        数据源: Alternative.me (完全免费，无需Key)
        URL: https://api.alternative.me/fng/?limit=30
        返回: {"value": 72, "label": "Greed", "history": [...]}
        """
        url = "https://api.alternative.me/fng/?limit=30"

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "恐惧贪婪指数API返回 %d，返回空数据", resp.status
                        )
                        return self._empty_fear_greed()

                    data = await resp.json()
                    entries = data.get("data", [])

                    if not entries:
                        return self._empty_fear_greed()

                    latest = entries[0]
                    current_value = int(latest.get("value", 0))
                    current_label = latest.get("value_classification", "Unknown")

                    history = []
                    for entry in entries:
                        ts = int(entry.get("timestamp", 0))
                        history.append(
                            {
                                "date": datetime.utcfromtimestamp(ts).strftime(
                                    "%Y-%m-%d"
                                ),
                                "value": int(entry.get("value", 0)),
                                "label": entry.get(
                                    "value_classification", "Unknown"
                                ),
                            }
                        )

                    return {
                        "value": current_value,
                        "label": current_label,
                        "label_cn": self._fng_label_cn(current_value),
                        "history": history,
                        "source": "alternative.me",
                    }

        except Exception as e:
            logger.error("获取恐惧贪婪指数失败: %s", e)
            return self._empty_fear_greed()

    def _fng_label_cn(self, value: int) -> str:
        """恐惧贪婪指数中文标签"""
        if value <= 10:
            return "极度恐惧"
        elif value <= 25:
            return "恐惧"
        elif value <= 45:
            return "偏恐惧"
        elif value <= 55:
            return "中性"
        elif value <= 75:
            return "贪婪"
        elif value <= 90:
            return "偏贪婪"
        else:
            return "极度贪婪"

    def _empty_fear_greed(self) -> dict:
        return {
            "value": None,
            "label": "Unknown",
            "label_cn": "未知",
            "history": [],
            "source": "error",
        }

    async def get_funding_rate(self, symbol: str = "BTC-USDT-SWAP") -> dict:
        """
        资金费率
        数据源: OKX API (免费)
        URL: GET https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP
        返回: 当前费率 + 历史费率
        """
        current_url = f"{self.okx_base}/public/funding-rate?instId={symbol}"
        history_url = (
            f"{self.okx_base}/public/funding-rate-history?instId={symbol}&limit=48"
        )

        result = {
            "symbol": symbol,
            "current": None,
            "next_funding_time": None,
            "history": [],
            "source": "okx",
        }

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                # 获取当前费率
                async with session.get(current_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("data", [])
                        if items:
                            item = items[0]
                            rate = item.get("fundingRate", "0")
                            result["current"] = {
                                "rate": float(rate),
                                "rate_pct": round(float(rate) * 100, 4),
                                "annualized_pct": round(
                                    float(rate) * 100 * 3 * 365, 2
                                ),
                            }
                            next_ts = item.get("nextFundingTime", "")
                            if next_ts:
                                result["next_funding_time"] = (
                                    datetime.utcfromtimestamp(
                                        int(next_ts) / 1000
                                    ).isoformat()
                                    + "Z"
                                )
                    else:
                        logger.warning(
                            "获取当前资金费率失败，状态码: %d", resp.status
                        )

                # 获取历史费率
                async with session.get(history_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("data", []):
                            rate = float(item.get("fundingRate", "0"))
                            ts = int(item.get("fundingTime", "0"))
                            result["history"].append(
                                {
                                    "time": datetime.utcfromtimestamp(
                                        ts / 1000
                                    ).isoformat()
                                    + "Z",
                                    "rate": rate,
                                    "rate_pct": round(rate * 100, 4),
                                }
                            )
                    else:
                        logger.warning(
                            "获取历史资金费率失败，状态码: %d", resp.status
                        )

        except Exception as e:
            logger.error("获取资金费率失败: %s", e)
            result["source"] = "error"

        return result

    async def get_open_interest(self, symbol: str = "BTC-USDT-SWAP") -> dict:
        """
        持仓量
        数据源: OKX API
        URL: GET https://www.okx.com/api/v5/public/open-interest?instId=BTC-USDT-SWAP
        """
        url = f"{self.okx_base}/public/open-interest?instId={symbol}"

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "获取持仓量失败，状态码: %d", resp.status
                        )
                        return {
                            "symbol": symbol,
                            "oi": None,
                            "source": "error",
                        }

                    data = await resp.json()
                    items = data.get("data", [])

                    if not items:
                        return {
                            "symbol": symbol,
                            "oi": None,
                            "source": "okx",
                        }

                    item = items[0]
                    oi = item.get("oi", "0")
                    oi_ccy = item.get("oiCcy", "0")
                    ts = int(item.get("ts", "0"))

                    return {
                        "symbol": symbol,
                        "oi": float(oi),
                        "oi_ccy": float(oi_ccy),
                        "oi_unit": "contracts",
                        "oi_ccy_unit": symbol.split("-")[0],
                        "timestamp": datetime.utcfromtimestamp(
                            ts / 1000
                        ).isoformat()
                        + "Z",
                        "source": "okx",
                    }

        except Exception as e:
            logger.error("获取持仓量失败: %s", e)
            return {"symbol": symbol, "oi": None, "source": "error"}

    async def get_long_short_ratio(self, coin: str = "BTC") -> dict:
        """
        多空比
        数据源: OKX API
        URL: GET https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=1H
        返回: 多空账户比例时序数据
        """
        url = (
            f"{self.okx_base}/rubik/stat/contracts/long-short-account-ratio"
            f"?ccy={coin}&period=1H"
        )

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "获取多空比失败，状态码: %d", resp.status
                        )
                        return {
                            "coin": coin,
                            "current": None,
                            "history": [],
                            "source": "error",
                        }

                    data = await resp.json()
                    items = data.get("data", [])

                    history = []
                    for item in items:
                        ts = int(item[0]) if isinstance(item, list) else int(
                            item.get("ts", 0)
                        )
                        ratio = (
                            float(item[1])
                            if isinstance(item, list)
                            else float(item.get("ratio", 0))
                        )
                        history.append(
                            {
                                "time": datetime.utcfromtimestamp(
                                    ts / 1000
                                ).isoformat()
                                + "Z",
                                "ratio": ratio,
                                "long_pct": round(
                                    ratio / (1 + ratio) * 100, 2
                                ),
                                "short_pct": round(
                                    1 / (1 + ratio) * 100, 2
                                ),
                            }
                        )

                    current = None
                    if history:
                        current = history[0]

                    signal = "中性"
                    if current and current["ratio"] > 2.0:
                        signal = "极度看多（注意反转风险）"
                    elif current and current["ratio"] > 1.5:
                        signal = "偏多"
                    elif current and current["ratio"] < 0.5:
                        signal = "极度看空（注意反转风险）"
                    elif current and current["ratio"] < 0.7:
                        signal = "偏空"

                    return {
                        "coin": coin,
                        "current": current,
                        "signal": signal,
                        "history": history,
                        "source": "okx",
                    }

        except Exception as e:
            logger.error("获取多空比失败: %s", e)
            return {
                "coin": coin,
                "current": None,
                "history": [],
                "source": "error",
            }
