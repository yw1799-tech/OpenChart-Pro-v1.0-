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

        # 输入校验：防止 None/非法值进入订阅表或日志
        if action in ("subscribe", "switch"):
            if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 50:
                await self._send(client_id, {"type": "error", "message": "无效 symbol"})
                return
            if not isinstance(interval, str) or not interval.strip():
                await self._send(client_id, {"type": "error", "message": "无效 interval"})
                return
            symbol = symbol.strip().upper()
            interval = interval.strip()

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
        elif action == "ping":
            # 心跳响应：回 pong，让前端心跳计数清零
            await self._send(client_id, {"type": "pong", "ts": msg.get("ts")})

    async def broadcast_kline(self, symbol, market, interval, candle_data, indicators=None):
        """向所有订阅了该品种的客户端推送K线数据。
        v12.11: 改并发广播，1 个慢客户端不再拖死全部 K 线推送（最高频）。
        """
        msg = {
            "type": "kline",
            "symbol": symbol,
            "market": market,
            "interval": interval,
            "data": candle_data,
            "indicators": indicators or {},
        }
        targets = [
            client_id for client_id, sub in list(self.subscriptions.items())
            if sub.get("symbol") == symbol and sub.get("interval") == interval
        ]
        await self._broadcast_concurrent(targets, msg)

    async def broadcast_alert(self, alert_data):
        await self._broadcast_concurrent(list(self.connections.keys()), alert_data)

    async def broadcast_backtest_progress(self, backtest_id, progress, status="running"):
        msg = {"type": "backtest_progress", "id": backtest_id, "progress": progress, "status": status}
        await self._broadcast_concurrent(list(self.connections.keys()), msg)

    async def broadcast_dashboard_update(self, data):
        msg = {"type": "dashboard_update", **data}
        await self._broadcast_concurrent(list(self.connections.keys()), msg)

    async def broadcast_news(self, news_msg):
        """新闻快讯推送（Phase 3A）。
        news_msg 形如 {"type": "flash_news", "data": {...}}
        """
        await self._broadcast_concurrent(list(self.connections.keys()), news_msg)

    async def broadcast_flash_news(self, msg):
        """新闻/事件快讯通用通道（兼容 scheduler/macro_impact 调用点）。
        注意：auto_trade 事件请用 broadcast_auto_trade。
        """
        await self._broadcast_concurrent(list(self.connections.keys()), msg)

    async def broadcast_auto_trade(self, event_data):
        """自动交易执行事件独立通道（与新闻分离，便于前端订阅过滤）。"""
        msg = {"type": "auto_trade", "data": event_data}
        await self._broadcast_concurrent(list(self.connections.keys()), msg)

    async def broadcast_pool_update(self, action, data):
        msg = {"type": "pool_update", "action": action, "data": data}
        await self._broadcast_concurrent(list(self.connections.keys()), msg)

    async def broadcast_signal(self, signal_data):
        msg = {"type": "signal", "data": signal_data}
        await self._broadcast_concurrent(list(self.connections.keys()), msg)

    async def broadcast_position_advice(self, advice_data):
        msg = {"type": "position_advice", "data": advice_data}
        await self._broadcast_concurrent(list(self.connections.keys()), msg)

    async def _send(self, client_id, data):
        ws = self.connections.get(client_id)
        if ws:
            try:
                # 加 5 秒超时：单个慢/僵尸客户端不能阻塞整个广播
                await asyncio.wait_for(ws.send_json(data), timeout=5.0)
            except Exception:
                self.connections.pop(client_id, None)
                self.subscriptions.pop(client_id, None)

    async def _broadcast_concurrent(self, client_ids, data):
        """并发广播给一批客户端。单个失败不影响其他，超时自动清理。"""
        if not client_ids:
            return
        await asyncio.gather(
            *[self._send(cid, data) for cid in client_ids],
            return_exceptions=True,
        )


# 全局实例
hub = WebSocketHub()
