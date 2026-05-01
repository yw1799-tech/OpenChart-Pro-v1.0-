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
                        logger.warning("恐惧贪婪指数API返回 %d，返回空数据", resp.status)
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
                                "date": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                                "value": int(entry.get("value", 0)),
                                "label": entry.get("value_classification", "Unknown"),
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
        history_url = f"{self.okx_base}/public/funding-rate-history?instId={symbol}&limit=48"

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
                                "annualized_pct": round(float(rate) * 100 * 3 * 365, 2),
                            }
                            next_ts = item.get("nextFundingTime", "")
                            if next_ts:
                                result["next_funding_time"] = (
                                    datetime.utcfromtimestamp(int(next_ts) / 1000).isoformat() + "Z"
                                )
                    else:
                        logger.warning("获取当前资金费率失败，状态码: %d", resp.status)

                # 获取历史费率
                async with session.get(history_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for item in data.get("data", []):
                            rate = float(item.get("fundingRate", "0"))
                            ts = int(item.get("fundingTime", "0"))
                            result["history"].append(
                                {
                                    "time": datetime.utcfromtimestamp(ts / 1000).isoformat() + "Z",
                                    "rate": rate,
                                    "rate_pct": round(rate * 100, 4),
                                }
                            )
                    else:
                        logger.warning("获取历史资金费率失败，状态码: %d", resp.status)

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
                        logger.warning("获取持仓量失败，状态码: %d", resp.status)
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
                        "timestamp": datetime.utcfromtimestamp(ts / 1000).isoformat() + "Z",
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
        url = f"{self.okx_base}/rubik/stat/contracts/long-short-account-ratio?ccy={coin}&period=1H"

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning("获取多空比失败，状态码: %d", resp.status)
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
                        ts = int(item[0]) if isinstance(item, list) else int(item.get("ts", 0))
                        ratio = float(item[1]) if isinstance(item, list) else float(item.get("ratio", 0))
                        history.append(
                            {
                                "time": datetime.utcfromtimestamp(ts / 1000).isoformat() + "Z",
                                "ratio": ratio,
                                "long_pct": round(ratio / (1 + ratio) * 100, 2),
                                "short_pct": round(1 / (1 + ratio) * 100, 2),
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

    async def get_top_trader_ratio(self, coin: str = "BTC") -> dict:
        """
        大户持仓多空比：TOP 5% 账户的多头/空头持仓比例
        数据源: OKX /rubik/stat/contracts/long-short-position-ratio-contract-top-trader
        * 用"持仓"维度而非"账户"，更能反映大户真实仓位方向
        """
        swap = f"{coin}-USDT-SWAP"
        url = f"{self.okx_base}/rubik/stat/contracts/long-short-position-ratio-contract-top-trader?instId={swap}&period=1H"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return {"coin": coin, "current": None, "history": [], "source": "error"}
                    data = await resp.json()
                    items = data.get("data", [])
                    history = []
                    for item in items[:24]:   # 24 根 1H
                        if isinstance(item, list) and len(item) >= 2:
                            ts = int(item[0])
                            ratio = float(item[1])
                        else:
                            continue
                        history.append({
                            "time": datetime.utcfromtimestamp(ts / 1000).isoformat() + "Z",
                            "ratio": ratio,
                            "long_pct": round(ratio / (1 + ratio) * 100, 2),
                            "short_pct": round(1 / (1 + ratio) * 100, 2),
                        })
                    current = history[0] if history else None
                    signal = "中性"
                    if current:
                        r = current["ratio"]
                        if r > 1.8: signal = "大户极度看多"
                        elif r > 1.3: signal = "大户偏多"
                        elif r < 0.55: signal = "大户极度看空"
                        elif r < 0.75: signal = "大户偏空"
                    return {"coin": coin, "current": current, "signal": signal, "history": history, "source": "okx"}
        except Exception as e:
            logger.error("获取顶级交易员多空比失败: %s", e)
            return {"coin": coin, "current": None, "history": [], "source": "error"}

    async def get_taker_volume(self, coin: str = "BTC") -> dict:
        """
        主动买卖成交量（吃单量）：反映资金流向的即时意愿
        数据源: OKX /rubik/stat/taker-volume-contract（合约维度）
        * 主动买 > 主动卖 = 多头占优
        """
        swap = f"{coin}-USDT-SWAP"
        url = f"{self.okx_base}/rubik/stat/taker-volume-contract?instId={swap}&period=1H"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return {"coin": coin, "current": None, "history": [], "source": "error"}
                    data = await resp.json()
                    items = data.get("data", [])
                    history = []
                    for item in items[:24]:
                        if isinstance(item, list) and len(item) >= 3:
                            ts = int(item[0])
                            sell_vol = float(item[1])
                            buy_vol = float(item[2])
                        else:
                            continue
                        total = buy_vol + sell_vol
                        if total <= 0:
                            continue
                        buy_pct = buy_vol / total * 100
                        history.append({
                            "time": datetime.utcfromtimestamp(ts / 1000).isoformat() + "Z",
                            "buy": buy_vol,
                            "sell": sell_vol,
                            "buy_pct": round(buy_pct, 2),
                            "sell_pct": round(100 - buy_pct, 2),
                            "net_flow": buy_vol - sell_vol,
                        })
                    current = history[0] if history else None
                    signal = "中性"
                    if current:
                        bp = current["buy_pct"]
                        if bp > 60: signal = "买盘强劲"
                        elif bp > 53: signal = "买盘占优"
                        elif bp < 40: signal = "卖盘强劲"
                        elif bp < 47: signal = "卖盘占优"
                    return {"coin": coin, "current": current, "signal": signal, "history": history, "source": "okx"}
        except Exception as e:
            logger.error("获取主动买卖量失败: %s", e)
            return {"coin": coin, "current": None, "history": [], "source": "error"}

    async def get_ticker_24h(self, symbol: str = "BTC-USDT-SWAP") -> dict:
        """
        24 小时行情统计：价格 / 涨跌幅 / 24h 成交量 / 最高 / 最低
        数据源: OKX /market/ticker
        """
        url = f"{self.okx_base}/market/ticker?instId={symbol}"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return {"symbol": symbol, "source": "error"}
                    data = await resp.json()
                    items = data.get("data", [])
                    if not items:
                        return {"symbol": symbol, "source": "okx"}
                    t = items[0]
                    last = float(t.get("last") or 0)
                    open24 = float(t.get("open24h") or 0)
                    high = float(t.get("high24h") or 0)
                    low = float(t.get("low24h") or 0)
                    vol_ccy = float(t.get("volCcy24h") or 0)
                    change_pct = ((last - open24) / open24 * 100) if open24 else 0
                    return {
                        "symbol": symbol,
                        "last": last, "open24h": open24,
                        "high24h": high, "low24h": low,
                        "change_pct_24h": round(change_pct, 3),
                        "vol_ccy_24h": vol_ccy,
                        "source": "okx",
                    }
        except Exception as e:
            logger.error("获取 24h Ticker 失败: %s", e)
            return {"symbol": symbol, "source": "error"}

    async def get_oi_history(self, symbol: str = "BTC-USDT-SWAP") -> dict:
        """
        持仓量历史（24 根 1H）—— 用于判断"OI 上升+价格上升=真多头"等关系
        数据源: OKX /rubik/stat/contracts/open-interest-volume
        """
        coin = symbol.split("-")[0]
        url = f"{self.okx_base}/rubik/stat/contracts/open-interest-volume?ccy={coin}&period=1H"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return {"coin": coin, "source": "error", "history": []}
                    data = await resp.json()
                    items = data.get("data", [])
                    history = []
                    for item in items[:24]:
                        if isinstance(item, list) and len(item) >= 3:
                            ts = int(item[0]); oi = float(item[1]); vol = float(item[2])
                        else:
                            continue
                        history.append({
                            "time": datetime.utcfromtimestamp(ts / 1000).isoformat() + "Z",
                            "oi": oi, "vol": vol,
                        })
                    # 计算 24h OI 变化
                    oi_change_pct = None
                    if len(history) >= 2:
                        try:
                            oldest = history[-1]["oi"]
                            latest = history[0]["oi"]
                            if oldest > 0:
                                oi_change_pct = round((latest - oldest) / oldest * 100, 2)
                        except Exception:
                            pass
                    return {"coin": coin, "oi_change_24h_pct": oi_change_pct, "history": history, "source": "okx"}
        except Exception as e:
            logger.error("获取 OI 历史失败: %s", e)
            return {"coin": coin, "source": "error", "history": []}

    async def get_insights(self, symbol: str = "BTC-USDT") -> dict:
        """
        一站式融合指标：统一返回某币种的所有加密市场情绪数据。
        symbol: BTC-USDT / ETH-USDT / ... （现货代码）
        内部会转成期货 swap 代码 (BTC-USDT-SWAP) 查询。
        """
        coin = symbol.split("-")[0]
        swap = f"{coin}-USDT-SWAP"
        # 并行拉取所有免费数据
        import asyncio
        results = await asyncio.gather(
            self.get_ticker_24h(swap),
            self.get_funding_rate(swap),
            self.get_open_interest(swap),
            self.get_oi_history(swap),
            self.get_long_short_ratio(coin),
            self.get_top_trader_ratio(coin),
            self.get_taker_volume(coin),
            self.get_fear_greed_index(),
            return_exceptions=True,
        )
        safe = lambda x: x if not isinstance(x, Exception) else {"source": "error"}
        ticker, funding, oi, oi_hist, ls_ratio, top_trader, taker_vol, fng = [safe(r) for r in results]
        return {
            "symbol": symbol,
            "coin": coin,
            "ticker": ticker,
            "funding_rate": funding,
            "open_interest": oi,
            "oi_history": oi_hist,
            "long_short_ratio": ls_ratio,
            "top_trader_ratio": top_trader,
            "taker_volume": taker_vol,
            "fear_greed": {
                "value": fng.get("value"),
                "label_cn": fng.get("label_cn"),
            } if isinstance(fng, dict) else {},
        }
