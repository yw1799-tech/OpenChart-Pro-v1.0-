"""
警报管理器模块。
负责警报的 CRUD、条件检测调度、触发处理和通知分发。
"""

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from backend.data.models import Alert, Candle, Market
from backend.alerts.conditions import check_condition
from backend.alerts.notifiers import dispatch_notification

logger = logging.getLogger(__name__)


class AlertManager:
    """
    警报管理器 — 管理所有警报的生命周期。

    典型用法::

        manager = AlertManager(db)
        await manager.load_alerts()
        # 当新K线到达时
        triggered = await manager.check_alerts("BTC-USDT", candles, indicators)
    """

    def __init__(self, db, webhook_urls: Optional[List[str]] = None):
        """
        Args:
            db: DatabaseManager 实例。
            webhook_urls: Webhook 通知 URL 列表。
        """
        self.db = db
        self.webhook_urls = webhook_urls or []
        # 内存中的活跃警报缓存: alert_id -> alert dict
        self._alerts: Dict[str, Dict[str, Any]] = {}
        # 上次触发时间记录: alert_id -> timestamp
        self._last_triggered: Dict[str, float] = {}

    # ──────────────────────── 加载 ────────────────────────

    async def load_alerts(self):
        """从数据库加载所有启用的警报到内存缓存。"""
        alerts = await self.db.get_alerts(enabled_only=True)
        self._alerts.clear()
        for a in alerts:
            self._alerts[a["id"]] = a
        logger.info(f"已加载 {len(self._alerts)} 条活跃警报")

    # ──────────────────────── CRUD ────────────────────────

    async def add_alert(self, alert_data: Dict[str, Any]) -> str:
        """
        创建新警报并保存到数据库。

        Args:
            alert_data: 警报配置字典，需要包含 symbol, market, condition_type, condition 等。

        Returns:
            新警报的 ID。
        """
        if "id" not in alert_data:
            alert_data["id"] = str(uuid.uuid4())

        alert_id = await self.db.create_alert(alert_data)

        # 如果启用，加入内存缓存
        if alert_data.get("enabled", True):
            alert = await self.db.get_alert_by_id(alert_id)
            if alert:
                self._alerts[alert_id] = alert

        logger.info(f"创建警报: {alert_id} ({alert_data.get('symbol')} / {alert_data.get('condition_type')})")
        return alert_id

    async def update_alert(self, alert_id: str, data: Dict[str, Any]):
        """
        更新警报配置。

        Args:
            alert_id: 警报 ID。
            data: 要更新的字段字典。
        """
        await self.db.update_alert(alert_id, data)

        # 更新内存缓存
        updated = await self.db.get_alert_by_id(alert_id)
        if updated and updated.get("enabled"):
            self._alerts[alert_id] = updated
        else:
            self._alerts.pop(alert_id, None)

        logger.info(f"更新警报: {alert_id}")

    async def delete_alert(self, alert_id: str):
        """
        删除警报。

        Args:
            alert_id: 警报 ID。
        """
        await self.db.delete_alert(alert_id)
        self._alerts.pop(alert_id, None)
        self._last_triggered.pop(alert_id, None)
        logger.info(f"删除警报: {alert_id}")

    def get_active_alerts(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取内存中的活跃警报列表。

        Args:
            symbol: 可选，按品种过滤。

        Returns:
            活跃警报列表。
        """
        alerts = list(self._alerts.values())
        if symbol:
            alerts = [a for a in alerts if a.get("symbol") == symbol]
        return alerts

    # ──────────────────────── 检测 ────────────────────────

    async def check_alerts(
        self,
        symbol: str,
        candles: List[Candle],
        indicators: Optional[Dict[str, List[float]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        检查指定品种的所有活跃警报。

        Args:
            symbol: 交易品种，如 "BTC-USDT"。
            candles: 最近的K线数据列表（按时间升序）。
            indicators: 已计算的指标数据。

        Returns:
            本次触发的警报列表 [{alert, message, price}, ...]。
        """
        if indicators is None:
            indicators = {}

        if not candles:
            return []

        # 筛选该品种的活跃警报
        relevant_alerts = [a for a in self._alerts.values() if a.get("symbol") == symbol and a.get("enabled", True)]

        if not relevant_alerts:
            return []

        triggered_list = []
        current_price = candles[-1].close

        for alert in relevant_alerts:
            alert_id = alert["id"]

            # 冷却时间检查
            if not self._check_cooldown(alert):
                continue

            # 构建条件字典
            condition = alert.get("condition", {})
            if not condition.get("type"):
                condition["type"] = alert.get("condition_type", "")
            # 补充 symbol 信息
            if "symbol" not in condition:
                condition["symbol"] = symbol

            # 检测条件
            triggered, message = check_condition(condition, candles, indicators)

            if triggered:
                await self._on_triggered(alert, message, current_price)
                triggered_list.append(
                    {
                        "alert": alert,
                        "message": message,
                        "price": current_price,
                    }
                )

        return triggered_list

    def _check_cooldown(self, alert: Dict[str, Any]) -> bool:
        """
        检查警报是否在冷却期内。

        Args:
            alert: 警报字典。

        Returns:
            True 表示可以触发（不在冷却期），False 表示应跳过。
        """
        alert_id = alert["id"]
        cooldown = alert.get("cooldown", 300)

        last_time = self._last_triggered.get(alert_id)
        if last_time is None:
            return True

        elapsed = time.time() - last_time
        if elapsed < cooldown:
            return False
        return True

    # ──────────────────────── 触发处理 ────────────────────────

    async def _on_triggered(self, alert: Dict[str, Any], message: str, price: float):
        """
        警报触发后的处理流程：
        1. 记录触发历史到数据库
        2. 分发通知（WebSocket / Webhook / 提示音）
        3. 更新触发状态
        4. once 模式自动禁用

        Args:
            alert: 警报字典。
            message: 触发消息。
            price: 触发时价格。
        """
        alert_id = alert["id"]
        symbol = alert.get("symbol", "")
        market = alert.get("market", "")
        now = time.time()

        logger.info(f"警报触发: [{symbol}] {alert.get('label', alert_id)} - {message} @ {price}")

        # 1. 记录触发时间
        self._last_triggered[alert_id] = now

        # 2. 记录到数据库历史
        try:
            await self.db.add_alert_history(
                alert_id=alert_id,
                symbol=symbol,
                market=market,
                price=price,
                message=message,
            )
        except Exception as e:
            logger.error(f"记录警报历史失败: {e}")

        # 3. 分发通知
        notify_methods = alert.get("notify_methods", ["browser", "sound"])
        try:
            results = await dispatch_notification(
                alert=alert,
                message=message,
                notify_methods=notify_methods,
                webhook_urls=self.webhook_urls,
            )
            logger.debug(f"通知分发结果: {results}")
        except Exception as e:
            logger.error(f"通知分发异常: {e}")

        # 4. once 模式：触发后自动禁用
        repeat_mode = alert.get("repeat_mode", "once")
        if repeat_mode == "once":
            try:
                await self.db.update_alert(alert_id, {"enabled": False})
                self._alerts.pop(alert_id, None)
                logger.info(f"警报 {alert_id} 已自动禁用（once 模式）")
            except Exception as e:
                logger.error(f"禁用警报失败: {e}")
