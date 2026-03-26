"""
链上数据模块
数据源: CryptoQuant, Blockchain.com, Glassnode
"""
import os
import aiohttp
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class OnChainData:
    """链上数据聚合器，提供交易所流入流出、巨鲸追踪、活跃地址等指标"""

    def __init__(self):
        self.cryptoquant_api_key = os.getenv("CRYPTOQUANT_API_KEY", "")
        self.glassnode_api_key = os.getenv("GLASSNODE_API_KEY", "")
        self.blockchain_base = "https://api.blockchain.info"
        self.timeout = aiohttp.ClientTimeout(total=15)

    async def get_exchange_flow(self, coin: str = "BTC") -> dict:
        """
        交易所净流入/流出
        数据源: CryptoQuant API (免费档)
        URL: https://api.cryptoquant.com/v1/btc/exchange-flows/netflow
        返回: {"netflow": [...], "inflow": [...], "outflow": [...]}
        如果API Key未配置或请求失败，返回模拟数据
        """
        if not self.cryptoquant_api_key:
            logger.warning("CRYPTOQUANT_API_KEY 未配置，返回模拟数据")
            return self._mock_exchange_flow()

        url = f"https://api.cryptoquant.com/v1/{coin.lower()}/exchange-flows/netflow"
        headers = {"Authorization": f"Bearer {self.cryptoquant_api_key}"}

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", {})
                        return {
                            "netflow": result.get("netflow", []),
                            "inflow": result.get("inflow", []),
                            "outflow": result.get("outflow", []),
                            "source": "cryptoquant",
                        }
                    else:
                        logger.warning(
                            "CryptoQuant API 返回 %d，降级为模拟数据", resp.status
                        )
                        return self._mock_exchange_flow()
        except Exception as e:
            logger.error("获取交易所流入流出数据失败: %s", e)
            return self._mock_exchange_flow()

    def _mock_exchange_flow(self) -> dict:
        """生成模拟交易所流入流出数据"""
        import random

        now = datetime.utcnow()
        days = 30
        netflow, inflow, outflow = [], [], []
        for i in range(days):
            ts = now.timestamp() - (days - i) * 86400
            dt_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            inf = round(random.uniform(500, 5000), 2)
            outf = round(random.uniform(500, 5000), 2)
            inflow.append({"date": dt_str, "value": inf})
            outflow.append({"date": dt_str, "value": outf})
            netflow.append({"date": dt_str, "value": round(inf - outf, 2)})
        return {
            "netflow": netflow,
            "inflow": inflow,
            "outflow": outflow,
            "source": "mock",
        }

    async def get_whale_transactions(
        self, coin: str = "BTC", min_value: int = 1_000_000
    ) -> list:
        """
        巨鲸交易追踪
        数据源: Blockchain.com API (免费)
        URL: https://blockchain.info/unconfirmed-transactions?format=json
        过滤大额交易(>min_value USD)
        返回: 近期大额转账列表
        """
        url = "https://blockchain.info/unconfirmed-transactions?format=json"

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Blockchain.com API 返回 %d，返回空列表", resp.status
                        )
                        return []

                    data = await resp.json()
                    txs = data.get("txs", [])
                    whale_txs = []

                    for tx in txs:
                        # 计算交易总输出（单位为聪，转换为BTC）
                        total_out_satoshi = sum(
                            out.get("value", 0) for out in tx.get("out", [])
                        )
                        total_out_btc = total_out_satoshi / 1e8

                        # 粗略估算USD价值（使用简单乘数，实际应查询实时价格）
                        # 这里用一个保守的BTC价格估算
                        estimated_usd = total_out_btc * 60000

                        if estimated_usd >= min_value:
                            whale_txs.append(
                                {
                                    "hash": tx.get("hash", ""),
                                    "time": datetime.utcfromtimestamp(
                                        tx.get("time", 0)
                                    ).isoformat()
                                    + "Z",
                                    "total_btc": round(total_out_btc, 4),
                                    "estimated_usd": round(estimated_usd, 2),
                                    "inputs_count": len(tx.get("inputs", [])),
                                    "outputs_count": len(tx.get("out", [])),
                                }
                            )

                    # 按金额降序排列，取前50条
                    whale_txs.sort(key=lambda x: x["estimated_usd"], reverse=True)
                    return whale_txs[:50]

        except Exception as e:
            logger.error("获取巨鲸交易数据失败: %s", e)
            return []

    async def get_active_addresses(self, coin: str = "BTC") -> dict:
        """
        活跃地址数趋势
        数据源: Blockchain.com
        URL: https://api.blockchain.info/charts/n-unique-addresses?timespan=30days&format=json
        返回: 每日活跃地址数时序数据
        """
        url = f"{self.blockchain_base}/charts/n-unique-addresses?timespan=30days&format=json"

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "获取活跃地址数失败，状态码: %d", resp.status
                        )
                        return {"values": [], "source": "error"}

                    data = await resp.json()
                    values = []
                    for point in data.get("values", []):
                        values.append(
                            {
                                "date": datetime.utcfromtimestamp(
                                    point["x"]
                                ).strftime("%Y-%m-%d"),
                                "count": int(point["y"]),
                            }
                        )

                    return {
                        "name": data.get("name", "n-unique-addresses"),
                        "unit": data.get("unit", "Addresses"),
                        "period": data.get("period", "day"),
                        "values": values,
                        "source": "blockchain.com",
                    }

        except Exception as e:
            logger.error("获取活跃地址数据失败: %s", e)
            return {"values": [], "source": "error"}

    async def get_nupl(self) -> dict:
        """
        Net Unrealized Profit/Loss (NUPL)
        数据源: Glassnode免费档
        NUPL > 0.75 极度贪婪, < 0 极度恐惧
        """
        if not self.glassnode_api_key:
            logger.warning("GLASSNODE_API_KEY 未配置，返回模拟数据")
            return self._mock_nupl()

        url = "https://api.glassnode.com/v1/metrics/indicators/net_unrealized_profit_loss"
        params = {
            "a": "BTC",
            "api_key": self.glassnode_api_key,
            "s": str(int((datetime.utcnow().timestamp()) - 30 * 86400)),
            "i": "24h",
        }

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Glassnode API 返回 %d，降级为模拟数据", resp.status
                        )
                        return self._mock_nupl()

                    data = await resp.json()
                    values = []
                    for point in data:
                        values.append(
                            {
                                "date": datetime.utcfromtimestamp(
                                    point["t"]
                                ).strftime("%Y-%m-%d"),
                                "nupl": round(point["v"], 4),
                            }
                        )

                    latest = values[-1]["nupl"] if values else 0
                    phase = self._nupl_phase(latest)

                    return {
                        "current": latest,
                        "phase": phase,
                        "history": values,
                        "source": "glassnode",
                    }

        except Exception as e:
            logger.error("获取NUPL数据失败: %s", e)
            return self._mock_nupl()

    def _nupl_phase(self, nupl: float) -> str:
        """根据NUPL值判断市场阶段"""
        if nupl > 0.75:
            return "极度贪婪(Euphoria)"
        elif nupl > 0.5:
            return "贪婪(Greed)"
        elif nupl > 0.25:
            return "乐观(Optimism)"
        elif nupl > 0:
            return "希望(Hope)"
        elif nupl > -0.25:
            return "恐惧(Fear)"
        else:
            return "投降(Capitulation)"

    def _mock_nupl(self) -> dict:
        """生成模拟NUPL数据"""
        import random

        now = datetime.utcnow()
        values = []
        base = 0.4
        for i in range(30):
            ts = now.timestamp() - (30 - i) * 86400
            dt_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            nupl = round(base + random.uniform(-0.05, 0.05), 4)
            base = nupl
            values.append({"date": dt_str, "nupl": nupl})

        latest = values[-1]["nupl"]
        return {
            "current": latest,
            "phase": self._nupl_phase(latest),
            "history": values,
            "source": "mock",
        }

    async def get_miner_data(self) -> dict:
        """
        矿工数据：哈希率、矿工收入
        数据源: Blockchain.com API
        URLs:
        - https://api.blockchain.info/charts/hash-rate?timespan=30days&format=json
        - https://api.blockchain.info/charts/miners-revenue?timespan=30days&format=json
        """
        hash_rate_url = (
            f"{self.blockchain_base}/charts/hash-rate?timespan=30days&format=json"
        )
        revenue_url = (
            f"{self.blockchain_base}/charts/miners-revenue?timespan=30days&format=json"
        )

        result = {
            "hash_rate": {"values": [], "unit": ""},
            "miners_revenue": {"values": [], "unit": ""},
            "source": "blockchain.com",
        }

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                # 并发请求两个API
                hash_task = session.get(hash_rate_url)
                revenue_task = session.get(revenue_url)

                async with hash_task as hash_resp, revenue_task as rev_resp:
                    # 处理哈希率
                    if hash_resp.status == 200:
                        hash_data = await hash_resp.json()
                        result["hash_rate"]["unit"] = hash_data.get(
                            "unit", "TH/s"
                        )
                        for point in hash_data.get("values", []):
                            result["hash_rate"]["values"].append(
                                {
                                    "date": datetime.utcfromtimestamp(
                                        point["x"]
                                    ).strftime("%Y-%m-%d"),
                                    "value": round(point["y"], 2),
                                }
                            )
                    else:
                        logger.warning(
                            "获取哈希率失败，状态码: %d", hash_resp.status
                        )

                    # 处理矿工收入
                    if rev_resp.status == 200:
                        rev_data = await rev_resp.json()
                        result["miners_revenue"]["unit"] = rev_data.get(
                            "unit", "USD"
                        )
                        for point in rev_data.get("values", []):
                            result["miners_revenue"]["values"].append(
                                {
                                    "date": datetime.utcfromtimestamp(
                                        point["x"]
                                    ).strftime("%Y-%m-%d"),
                                    "value": round(point["y"], 2),
                                }
                            )
                    else:
                        logger.warning(
                            "获取矿工收入失败，状态码: %d", rev_resp.status
                        )

        except Exception as e:
            logger.error("获取矿工数据失败: %s", e)
            result["source"] = "error"

        return result
