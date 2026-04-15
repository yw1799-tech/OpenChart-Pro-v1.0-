"""
WebSocket 推送中心
管理所有客户端连接，按订阅关系分发数据。
"""

import json
import asyncio
import uuid
import logging
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class WebSocketHub:
    def __init__(self):
        self.connections = {}  # client_id -> websocket
        self.subscriptions = {}  # client_id -> {"symbol": str, "interval": str}

    async def handle_client(self, websocket: WebSocket):
        """处理新的WebSocket连接"""
        await websocket.accept()
        client_id = str(uuid.uuid4())
        self.connections[client_id] = websocket
        logger.info(f"Client connected: {client_id}")

        try:
            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)
                await self._handle_message(client_id, msg)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"WebSocket error for {client_id}: {e}")
        finally:
            self.connections.pop(client_id, None)
            self.subscriptions.pop(client_id, None)
            logger.info(f"Client disconnected: {client_id}")

    async def _handle_message(self, client_id, msg):
        action = msg.get("action") or msg.get("type")
        symbol = msg.get("symbol")
        interval = msg.get("interval")

        if action == "subscribe":
            self.subscriptions[client_id] = {"symbol": symbol, "interval": interval}
            await self._send(
                client_id,
                {"type": "subscription_result", "action": "subscribe", "symbol": symbol, "status": "ok", "message": ""},
            )
        elif action == "unsubscribe":
            self.subscriptions.pop(client_id, None)
            await self._send(
                client_id,
                {
                    "type": "subscription_result",
                    "action": "unsubscribe",
                    "symbol": symbol,
                    "status": "ok",
                    "message": "",
                },
            )
        elif action == "switch":
            self.subscriptions[client_id] = {"symbol": symbol, "interval": interval}
            await self._send(
                client_id,
                {"type": "subscription_result", "action": "switch", "symbol": symbol, "status": "ok", "message": ""},
            )

    async def broadcast_kline(self, symbol, market, interval, candle_data, indicators=None):
        """向所有订阅了该品种的客户端推送K线数据"""
        msg = {
            "type": "kline",
            "symbol": symbol,
            "market": market,
            "interval": interval,
            "data": candle_data,
            "indicators": indicators or {},
        }
        for client_id, sub in list(self.subscriptions.items()):
            if sub.get("symbol") == symbol and sub.get("interval") == interval:
                await self._send(client_id, msg)

    async def broadcast_alert(self, alert_data):
        """广播警报触发通知给所有连接的客户端"""
        for client_id in list(self.connections.keys()):
            await self._send(client_id, alert_data)

    async def broadcast_backtest_progress(self, backtest_id, progress, status="running"):
        msg = {"type": "backtest_progress", "id": backtest_id, "progress": progress, "status": status}
        for client_id in list(self.connections.keys()):
            await self._send(client_id, msg)

    async def broadcast_dashboard_update(self, data):
        msg = {"type": "dashboard_update", **data}
        for client_id in list(self.connections.keys()):
            await self._send(client_id, msg)

    async def broadcast_news(self, news_msg):
        """新闻快讯推送（Phase 3A）。
        news_msg 形如 {"type": "flash_news", "data": {...}}
        """
        for client_id in list(self.connections.keys()):
            await self._send(client_id, news_msg)

    async def broadcast_pool_update(self, action, data):
        """候选池变动推送（Phase 3A）。
        action: 'added' | 'removed' | 'scored'
        """
        msg = {"type": "pool_update", "action": action, "data": data}
        for client_id in list(self.connections.keys()):
            await self._send(client_id, msg)

    async def broadcast_signal(self, signal_data):
        """策略信号推送（Phase 4）。"""
        msg = {"type": "signal", "data": signal_data}
        for client_id in list(self.connections.keys()):
            await self._send(client_id, msg)

    async def broadcast_position_advice(self, advice_data):
        """持仓建议推送（Phase 5）。"""
        msg = {"type": "position_advice", "data": advice_data}
        for client_id in list(self.connections.keys()):
            await self._send(client_id, msg)

    async def _send(self, client_id, data):
        ws = self.connections.get(client_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                self.connections.pop(client_id, None)
                self.subscriptions.pop(client_id, None)


# 全局实例
hub = WebSocketHub()
