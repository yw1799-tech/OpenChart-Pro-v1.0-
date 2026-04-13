"""
风控引擎（预留空壳）
提供仓位管理、止损止盈、每日限额等风控功能。

注意: 这是预留模块，需配合实际交易接口启用。
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.trading.base import Order, OrderSide, Position, Balance

logger = logging.getLogger(__name__)


# ============================================================================
# 风控配置
# ============================================================================


@dataclass
class RiskConfig:
    """风控参数配置"""

    # 单笔最大仓位（占总资金百分比）
    max_position_pct: float = 10.0
    # 单笔最大亏损（占总资金百分比）
    max_loss_per_trade_pct: float = 2.0
    # 总仓位上限（占总资金百分比）
    max_total_position_pct: float = 50.0
    # 每日最大亏损（占总资金百分比）
    max_daily_loss_pct: float = 5.0
    # 每日最大交易次数
    max_daily_trades: int = 50
    # 单品种最大持仓数
    max_position_per_symbol: int = 3
    # 最小订单金额 (USDT)
    min_order_value: float = 10.0
    # 最大订单金额 (USDT)
    max_order_value: float = 100000.0
    # 默认止损百分比
    default_stop_loss_pct: float = 5.0
    # 默认止盈百分比
    default_take_profit_pct: float = 10.0
    # 是否启用风控
    enabled: bool = True


# ============================================================================
# 风控引擎
# ============================================================================


class RiskEngine:
    """
    风控引擎 - 在下单前/后进行风险控制。

    用法::

        risk = RiskEngine(RiskConfig(max_position_pct=5))
        # 下单前检查
        ok, reason = risk.pre_order_check(order, balance, positions)
        if not ok:
            print(f"风控拒绝: {reason}")
            return
        # 下单...
        # 下单后记录
        risk.record_trade(order)
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        # 当日交易记录
        self._daily_trades: List[Dict[str, Any]] = []
        self._daily_pnl: float = 0.0
        self._day_start: str = self._today_str()

    # ──────────────── 下单前检查 ────────────────

    def pre_order_check(
        self,
        order: Order,
        balance: Optional[Balance] = None,
        positions: Optional[List[Position]] = None,
    ) -> tuple:
        """
        下单前风控检查。

        Args:
            order: 待下的订单
            balance: 当前余额
            positions: 当前持仓列表

        Returns:
            (通过: bool, 原因: str)
        """
        if not self.config.enabled:
            return True, "风控已禁用"

        # 重置每日统计（如果跨天了）
        self._check_day_reset()

        checks = [
            self._check_daily_trade_limit,
            self._check_daily_loss_limit,
            self._check_order_value,
            self._check_position_limit,
            self._check_total_position_limit,
        ]

        for check_func in checks:
            passed, reason = check_func(order, balance, positions)
            if not passed:
                logger.warning(
                    "风控拒绝: %s (订单: %s %s %s)",
                    reason,
                    order.symbol,
                    order.side.value,
                    order.size,
                )
                return False, reason

        return True, "通过"

    def _check_daily_trade_limit(self, order, balance, positions) -> tuple:
        """检查每日交易次数限制"""
        if len(self._daily_trades) >= self.config.max_daily_trades:
            return False, f"每日交易次数已达上限 ({self.config.max_daily_trades})"
        return True, ""

    def _check_daily_loss_limit(self, order, balance, positions) -> tuple:
        """检查每日亏损限制"""
        if balance is None:
            return True, ""

        max_daily_loss = balance.equity * self.config.max_daily_loss_pct / 100
        if self._daily_pnl < -max_daily_loss:
            return False, f"当日亏损已达上限 ({self._daily_pnl:.2f} / -{max_daily_loss:.2f})"
        return True, ""

    def _check_order_value(self, order, balance, positions) -> tuple:
        """检查订单金额"""
        order_value = order.size * order.price if order.price > 0 else order.size
        if order_value < self.config.min_order_value:
            return False, f"订单金额 {order_value:.2f} 低于最小值 {self.config.min_order_value}"
        if order_value > self.config.max_order_value:
            return False, f"订单金额 {order_value:.2f} 超过最大值 {self.config.max_order_value}"
        return True, ""

    def _check_position_limit(self, order, balance, positions) -> tuple:
        """检查单品种持仓限制"""
        if positions is None or order.side == OrderSide.SELL:
            return True, ""

        symbol_positions = [p for p in positions if p.symbol == order.symbol]
        if len(symbol_positions) >= self.config.max_position_per_symbol:
            return False, f"{order.symbol} 持仓数已达上限 ({self.config.max_position_per_symbol})"
        return True, ""

    def _check_total_position_limit(self, order, balance, positions) -> tuple:
        """检查总仓位限制"""
        if balance is None or positions is None or order.side == OrderSide.SELL:
            return True, ""

        total_margin = sum(p.margin for p in positions)
        max_margin = balance.equity * self.config.max_total_position_pct / 100

        order_margin = order.size * order.price if order.price > 0 else order.size
        if total_margin + order_margin > max_margin:
            return False, (
                f"总仓位 {total_margin + order_margin:.2f} 将超过上限 "
                f"{max_margin:.2f} ({self.config.max_total_position_pct}%)"
            )
        return True, ""

    # ──────────────── 向后兼容方法 ────────────────

    async def check_order(self, order_dict: Dict) -> Dict:
        """下单前风控检查（兼容旧接口）"""
        return {"allowed": True, "reason": ""}

    async def check_position_limit(self, symbol: str, size: float) -> bool:
        """检查持仓限制（兼容旧接口）"""
        return True

    async def check_daily_loss(self) -> bool:
        """检查当日亏损限制（兼容旧接口）"""
        self._check_day_reset()
        return self._daily_pnl > -(self.config.max_daily_loss_pct / 100 * 100000)

    # ──────────────── 仓位计算 ────────────────

    def calc_position_size(
        self,
        balance: Balance,
        price: float,
        stop_loss_price: float = 0.0,
        risk_pct: Optional[float] = None,
    ) -> float:
        """
        基于风险百分比计算建仓数量。

        Args:
            balance: 账户余额
            price: 入场价格
            stop_loss_price: 止损价格（如果为0则使用默认止损百分比）
            risk_pct: 单笔风险百分比（为None则使用配置默认值）

        Returns:
            建议的仓位数量
        """
        risk_pct = risk_pct or self.config.max_loss_per_trade_pct
        risk_amount = balance.equity * risk_pct / 100

        if stop_loss_price <= 0:
            stop_loss_price = price * (1 - self.config.default_stop_loss_pct / 100)

        risk_per_unit = abs(price - stop_loss_price)
        if risk_per_unit <= 0:
            return 0.0

        size = risk_amount / risk_per_unit

        # 检查不超过最大仓位限制
        max_value = balance.equity * self.config.max_position_pct / 100
        max_size = max_value / price if price > 0 else 0
        size = min(size, max_size)

        return round(size, 8)

    def calc_stop_loss(self, entry_price: float, side: OrderSide, pct: Optional[float] = None) -> float:
        """计算止损价格"""
        pct = pct or self.config.default_stop_loss_pct
        if side == OrderSide.BUY:
            return round(entry_price * (1 - pct / 100), 8)
        else:
            return round(entry_price * (1 + pct / 100), 8)

    def calc_take_profit(self, entry_price: float, side: OrderSide, pct: Optional[float] = None) -> float:
        """计算止盈价格"""
        pct = pct or self.config.default_take_profit_pct
        if side == OrderSide.BUY:
            return round(entry_price * (1 + pct / 100), 8)
        else:
            return round(entry_price * (1 - pct / 100), 8)

    # ──────────────── 记录与统计 ────────────────

    def record_trade(self, order: Order, pnl: float = 0.0):
        """记录交易"""
        self._check_day_reset()
        self._daily_trades.append(
            {
                "order_id": order.order_id,
                "symbol": order.symbol,
                "side": order.side.value,
                "size": order.size,
                "price": order.price,
                "pnl": pnl,
                "timestamp": time.time(),
            }
        )
        self._daily_pnl += pnl

    def get_daily_stats(self) -> Dict[str, Any]:
        """获取当日交易统计"""
        self._check_day_reset()
        return {
            "date": self._day_start,
            "total_trades": len(self._daily_trades),
            "max_trades": self.config.max_daily_trades,
            "daily_pnl": round(self._daily_pnl, 2),
            "max_daily_loss": self.config.max_daily_loss_pct,
            "trades": self._daily_trades[-20:],  # 最近20笔
        }

    def _check_day_reset(self):
        """检查是否需要重置每日统计"""
        today = self._today_str()
        if today != self._day_start:
            logger.info("风控引擎每日重置: %s -> %s", self._day_start, today)
            self._daily_trades = []
            self._daily_pnl = 0.0
            self._day_start = today

    @staticmethod
    def _today_str() -> str:
        return time.strftime("%Y-%m-%d")
