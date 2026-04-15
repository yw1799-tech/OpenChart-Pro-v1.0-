"""
模拟交易器（Phase 7 预留 / TDD §6.7）。

当 Phase 7 自动交易未启用时使用：
- 信号触发时记录"假如下单"的虚拟成交
- 用于评估策略实盘效果（不真实下单）
- API 兼容 BrokerAdapter，未来可无缝替换为真实经纪商
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SimulatorBroker:
    """
    虚拟下单器。
    所有"订单"只记录到内存/日志，不发送到真实经纪商。
    """

    def __init__(self):
        self._orders: List[Dict[str, Any]] = []
        self._positions: Dict[str, Dict[str, Any]] = {}  # symbol -> {qty, avg_cost}

    async def connect(self):
        logger.info("SimulatorBroker connected (dry-run mode)")

    async def close(self):
        pass

    async def place_order(
        self,
        symbol: str,
        side: str,  # 'buy' | 'sell'
        quantity: float,
        price: Optional[float] = None,
        order_type: str = "market",
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        模拟下单：立即按"市价"成交，记录到内部账本。
        返回模拟订单对象。
        """
        order_id = client_order_id or str(uuid.uuid4())
        fill_price = price or 0.0  # 简化：调用方传入参考价
        order = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": fill_price,
            "order_type": order_type,
            "status": "filled",
            "filled_at": int(time.time() * 1000),
        }
        self._orders.append(order)
        # 更新虚拟持仓
        pos = self._positions.setdefault(symbol, {"qty": 0.0, "avg_cost": 0.0})
        if side == "buy":
            new_qty = pos["qty"] + quantity
            if new_qty > 0:
                pos["avg_cost"] = (pos["qty"] * pos["avg_cost"] + quantity * fill_price) / new_qty
            pos["qty"] = new_qty
        else:
            pos["qty"] -= quantity
            if pos["qty"] <= 0:
                pos["qty"] = 0.0
                pos["avg_cost"] = 0.0
        logger.info(
            f"[Simulator] {side.upper()} {symbol} {quantity}@{fill_price} → "
            f"虚拟持仓: qty={pos['qty']}, avg_cost={pos['avg_cost']:.4f}"
        )
        return order

    async def cancel_order(self, order_id: str) -> bool:
        for o in self._orders:
            if o["order_id"] == order_id and o["status"] != "filled":
                o["status"] = "canceled"
                return True
        return False

    async def get_order(self, order_id: str) -> Optional[Dict]:
        for o in self._orders:
            if o["order_id"] == order_id:
                return o
        return None

    async def list_orders(self, limit: int = 100) -> List[Dict]:
        return self._orders[-limit:]

    async def get_positions(self) -> List[Dict]:
        return [
            {"symbol": sym, "quantity": p["qty"], "avg_cost": p["avg_cost"]}
            for sym, p in self._positions.items()
            if p["qty"] > 0
        ]

    async def get_balance(self) -> Dict:
        return {"mode": "simulator", "cash_usd": 0.0, "note": "虚拟账户，不追踪现金余额"}


# 全局实例（Phase 7 启用真实经纪商时替换为对应 Adapter）
simulator = SimulatorBroker()
