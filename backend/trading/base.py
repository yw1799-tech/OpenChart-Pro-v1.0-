"""
交易接口抽象基类
定义统一的交易操作接口，所有交易所实现类必须继承此基类。
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# 枚举类型
# ============================================================================


class OrderSide(Enum):
    """订单方向"""

    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    """订单类型"""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(Enum):
    """订单状态"""

    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PositionSide(Enum):
    """仓位方向"""

    LONG = "long"
    SHORT = "short"


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class Order:
    """订单"""

    order_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.MARKET
    price: float = 0.0
    size: float = 0.0
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    timestamp: int = 0
    client_order_id: str = ""
    stop_price: float = 0.0
    take_profit: float = 0.0
    stop_loss: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    """持仓"""

    symbol: str = ""
    side: PositionSide = PositionSide.LONG
    size: float = 0.0
    avg_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    margin: float = 0.0
    leverage: int = 1
    liquidation_price: float = 0.0
    timestamp: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Balance:
    """账户余额"""

    currency: str = ""
    total: float = 0.0
    available: float = 0.0
    frozen: float = 0.0
    equity: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class TradeRecord:
    """成交记录"""

    trade_id: str = ""
    order_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    price: float = 0.0
    size: float = 0.0
    fee: float = 0.0
    fee_currency: str = ""
    timestamp: int = 0


# ============================================================================
# 交易接口基类
# ============================================================================


class TradingBase(ABC):
    """
    交易接口抽象基类。
    所有交易所适配器必须实现以下方法。

    注意: 这是预留接口，实际下单功能需谨慎对接和测试。
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ──────────────── 连接管理 ────────────────

    @abstractmethod
    async def connect(self) -> bool:
        """建立与交易所的连接。返回连接是否成功。"""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接，清理资源。"""
        ...

    # ──────────────── 账户信息 ────────────────

    @abstractmethod
    async def get_balance(self, currency: str = "USDT") -> Optional[Balance]:
        """获取账户余额。"""
        ...

    @abstractmethod
    async def get_positions(self, symbol: str = "") -> List[Position]:
        """获取持仓列表。symbol为空则获取所有持仓。"""
        ...

    # ──────────────── 下单操作 ────────────────

    @abstractmethod
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
        """下单。返回 Order 对象，失败返回 None。"""
        ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """撤销订单。返回是否撤销成功。"""
        ...

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> Optional[Order]:
        """查询订单详情。"""
        ...

    @abstractmethod
    async def get_open_orders(self, symbol: str = "") -> List[Order]:
        """获取未完成订单列表。"""
        ...

    # ──────────────── 交易记录 ────────────────

    @abstractmethod
    async def get_trades(self, symbol: str, limit: int = 50) -> List[TradeRecord]:
        """获取最近成交记录。"""
        ...

    # ──────────────── 便捷方法 ────────────────

    async def market_buy(self, symbol: str, size: float, **kwargs) -> Optional[Order]:
        """市价买入"""
        return await self.place_order(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            size=size,
            **kwargs,
        )

    async def market_sell(self, symbol: str, size: float, **kwargs) -> Optional[Order]:
        """市价卖出"""
        return await self.place_order(
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            size=size,
            **kwargs,
        )

    async def limit_buy(self, symbol: str, size: float, price: float, **kwargs) -> Optional[Order]:
        """限价买入"""
        return await self.place_order(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            size=size,
            price=price,
            **kwargs,
        )

    async def limit_sell(self, symbol: str, size: float, price: float, **kwargs) -> Optional[Order]:
        """限价卖出"""
        return await self.place_order(
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            size=size,
            price=price,
            **kwargs,
        )

    async def cancel_all_orders(self, symbol: str) -> int:
        """撤销指定品种的所有未完成订单。返回成功撤销的订单数。"""
        orders = await self.get_open_orders(symbol)
        canceled = 0
        for order in orders:
            try:
                if await self.cancel_order(symbol, order.order_id):
                    canceled += 1
            except Exception as e:
                logger.warning("撤单失败 %s: %s", order.order_id, e)
        return canceled


# 向后兼容：保留旧名称
TraderBase = TradingBase
