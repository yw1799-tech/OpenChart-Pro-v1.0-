"""
OKX 交易接口实现（预留空壳）
对接 OKX V5 REST API，实现 TradingBase 抽象接口。

注意: 这是预留模块，实际启用前需要:
1. 配置 API Key / Secret / Passphrase
2. 在模拟盘完成充分测试
3. 确认风控参数
"""

import hashlib
import hmac
import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from backend.trading.base import (
    TradingBase,
    Order,
    OrderSide,
    OrderType,
    OrderStatus,
    Position,
    PositionSide,
    Balance,
    TradeRecord,
)

logger = logging.getLogger(__name__)


class OKXTrader(TradingBase):
    """
    OKX V5 API 交易实现。

    配置示例::

        config = {
            "api_key": "xxx",
            "secret_key": "xxx",
            "passphrase": "xxx",
            "simulated": True,       # True=模拟盘, False=实盘
            "base_url": "https://www.okx.com",
        }
        trader = OKXTrader(config)
        await trader.connect()
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.api_key = self.config.get("api_key", "")
        self.secret_key = self.config.get("secret_key", "")
        self.passphrase = self.config.get("passphrase", "")
        self.simulated = self.config.get("simulated", True)
        self.base_url = self.config.get("base_url", "https://www.okx.com")
        self._session: Optional[aiohttp.ClientSession] = None

    # ──────────────── 签名与请求 ────────────────

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """生成 OKX API 签名"""
        message = timestamp + method.upper() + path + body
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """构造认证请求头"""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        sign = self._sign(timestamp, method, path, body)

        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.simulated:
            headers["x-simulated-trading"] = "1"

        return headers

    async def _request(
        self, method: str, path: str, params: Optional[Dict] = None, body: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """发起API请求"""
        if self._session is None or self._session.closed:
            raise RuntimeError("OKX 未连接，请先调用 connect()")

        url = self.base_url + path
        body_str = json.dumps(body) if body else ""

        # 构建查询字符串
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items() if v)
            request_path = f"{path}?{query}" if query else path
            url = f"{url}?{query}" if query else url
        else:
            request_path = path

        headers = self._headers(method, request_path, body_str)

        try:
            if method.upper() == "GET":
                async with self._session.get(url, headers=headers) as resp:
                    return await resp.json()
            else:
                async with self._session.post(url, headers=headers, data=body_str) as resp:
                    return await resp.json()
        except Exception as e:
            logger.error("OKX API 请求失败: %s %s -> %s", method, path, e)
            return {"code": "-1", "msg": str(e), "data": []}

    # ──────────────── 连接管理 ────────────────

    async def connect(self) -> bool:
        """建立连接"""
        if not self.api_key or not self.secret_key:
            logger.warning("OKX API Key 未配置，交易功能不可用")
            return False

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )

        # 验证连接
        try:
            result = await self._request("GET", "/api/v5/account/balance")
            if result.get("code") == "0":
                self._connected = True
                mode = "模拟盘" if self.simulated else "实盘"
                logger.info("OKX 连接成功 (%s)", mode)
                return True
            else:
                logger.error("OKX 连接验证失败: %s", result.get("msg"))
                return False
        except Exception as e:
            logger.error("OKX 连接异常: %s", e)
            return False

    async def disconnect(self) -> None:
        """断开连接"""
        self._connected = False
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("OKX 连接已断开")

    # ──────────────── 账户信息 ────────────────

    async def get_balance(self, currency: str = "USDT") -> Optional[Balance]:
        """获取账户余额"""
        result = await self._request("GET", "/api/v5/account/balance", {"ccy": currency})
        if result.get("code") != "0":
            logger.warning("获取余额失败: %s", result.get("msg"))
            return None

        data_list = result.get("data", [])
        if not data_list:
            return None

        details = data_list[0].get("details", [])
        for d in details:
            if d.get("ccy") == currency:
                return Balance(
                    currency=currency,
                    total=float(d.get("cashBal", 0)),
                    available=float(d.get("availBal", 0)),
                    frozen=float(d.get("frozenBal", 0)),
                    equity=float(d.get("eq", 0)),
                    unrealized_pnl=float(d.get("upl", 0)),
                )

        return None

    async def get_positions(self, symbol: str = "") -> List[Position]:
        """获取持仓"""
        params = {}
        if symbol:
            params["instId"] = symbol

        result = await self._request("GET", "/api/v5/account/positions", params)
        if result.get("code") != "0":
            logger.warning("获取持仓失败: %s", result.get("msg"))
            return []

        positions = []
        for d in result.get("data", []):
            pos_size = float(d.get("pos", 0))
            if pos_size == 0:
                continue

            positions.append(Position(
                symbol=d.get("instId", ""),
                side=PositionSide.LONG if d.get("posSide") == "long" else PositionSide.SHORT,
                size=abs(pos_size),
                avg_price=float(d.get("avgPx", 0)),
                unrealized_pnl=float(d.get("upl", 0)),
                realized_pnl=float(d.get("realizedPnl", 0)),
                margin=float(d.get("margin", 0)),
                leverage=int(d.get("lever", 1)),
                liquidation_price=float(d.get("liqPx", 0)) if d.get("liqPx") else 0,
                timestamp=int(d.get("uTime", 0)),
            ))

        return positions

    # ──────────────── 下单操作 ────────────────

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        size: float,
        price: float = 0.0,
        stop_price: float = 0.0,
        client_order_id: str = "",
        **kwargs,
    ) -> Optional[Order]:
        """下单"""
        # 映射订单类型
        okx_ord_type_map = {
            OrderType.MARKET: "market",
            OrderType.LIMIT: "limit",
            OrderType.STOP: "trigger",
            OrderType.STOP_LIMIT: "trigger",
        }
        okx_ord_type = okx_ord_type_map.get(order_type, "market")

        body = {
            "instId": symbol,
            "tdMode": kwargs.get("td_mode", "cross"),
            "side": side.value,
            "ordType": okx_ord_type,
            "sz": str(size),
        }

        if price > 0:
            body["px"] = str(price)
        if client_order_id:
            body["clOrdId"] = client_order_id
        if stop_price > 0:
            body["triggerPx"] = str(stop_price)

        result = await self._request("POST", "/api/v5/trade/order", body=body)
        if result.get("code") != "0":
            logger.error("下单失败: %s", result.get("msg"))
            return None

        data_list = result.get("data", [])
        if not data_list:
            return None

        d = data_list[0]
        return Order(
            order_id=d.get("ordId", ""),
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
            size=size,
            status=OrderStatus.PENDING,
            client_order_id=d.get("clOrdId", ""),
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """撤单"""
        body = {"instId": symbol, "ordId": order_id}
        result = await self._request("POST", "/api/v5/trade/cancel-order", body=body)
        return result.get("code") == "0"

    async def get_order(self, symbol: str, order_id: str) -> Optional[Order]:
        """查询订单"""
        result = await self._request(
            "GET", "/api/v5/trade/order",
            {"instId": symbol, "ordId": order_id},
        )
        if result.get("code") != "0":
            return None

        data_list = result.get("data", [])
        if not data_list:
            return None

        d = data_list[0]
        status_map = {
            "live": OrderStatus.OPEN,
            "partially_filled": OrderStatus.PARTIALLY_FILLED,
            "filled": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELED,
        }

        return Order(
            order_id=d.get("ordId", ""),
            symbol=d.get("instId", ""),
            side=OrderSide.BUY if d.get("side") == "buy" else OrderSide.SELL,
            price=float(d.get("px", 0)),
            size=float(d.get("sz", 0)),
            filled_size=float(d.get("fillSz", 0)),
            avg_fill_price=float(d.get("avgPx", 0)) if d.get("avgPx") else 0,
            status=status_map.get(d.get("state", ""), OrderStatus.PENDING),
            timestamp=int(d.get("uTime", 0)),
        )

    async def get_open_orders(self, symbol: str = "") -> List[Order]:
        """获取未完成订单"""
        params = {}
        if symbol:
            params["instId"] = symbol

        result = await self._request("GET", "/api/v5/trade/orders-pending", params)
        if result.get("code") != "0":
            return []

        orders = []
        for d in result.get("data", []):
            orders.append(Order(
                order_id=d.get("ordId", ""),
                symbol=d.get("instId", ""),
                side=OrderSide.BUY if d.get("side") == "buy" else OrderSide.SELL,
                price=float(d.get("px", 0)),
                size=float(d.get("sz", 0)),
                filled_size=float(d.get("fillSz", 0)),
                status=OrderStatus.OPEN,
                timestamp=int(d.get("uTime", 0)),
            ))

        return orders

    async def get_trades(self, symbol: str, limit: int = 50) -> List[TradeRecord]:
        """获取成交记录"""
        result = await self._request(
            "GET", "/api/v5/trade/fills",
            {"instId": symbol, "limit": str(limit)},
        )
        if result.get("code") != "0":
            return []

        trades = []
        for d in result.get("data", []):
            trades.append(TradeRecord(
                trade_id=d.get("tradeId", ""),
                order_id=d.get("ordId", ""),
                symbol=d.get("instId", ""),
                side=OrderSide.BUY if d.get("side") == "buy" else OrderSide.SELL,
                price=float(d.get("fillPx", 0)),
                size=float(d.get("fillSz", 0)),
                fee=float(d.get("fee", 0)),
                fee_currency=d.get("feeCcy", ""),
                timestamp=int(d.get("ts", 0)),
            ))

        return trades
