"""
OKX 数据源实现 — REST + WebSocket。
公开数据接口无需 API Key。
"""

import asyncio
import json
import time
import logging
from typing import List, Dict, Optional, Callable, Any

import aiohttp

import backend.config as config
from backend.data.fetcher import DataFetcher
from backend.data.models import Symbol, Candle, Interval, Market

logger = logging.getLogger(__name__)

# ---------- Interval → OKX bar 参数映射 ----------
_INTERVAL_MAP: Dict[Interval, str] = {
    Interval.M1: "1m",
    Interval.M5: "5m",
    Interval.M15: "15m",
    Interval.M30: "30m",
    Interval.H1: "1H",
    Interval.H4: "4H",
    Interval.D1: "1D",
    Interval.W1: "1W",
    Interval.MN: "1M",
}

# Interval → WebSocket channel 名称
_WS_CHANNEL_MAP: Dict[Interval, str] = {
    Interval.M1: "candle1m",
    Interval.M5: "candle5m",
    Interval.M15: "candle15m",
    Interval.M30: "candle30m",
    Interval.H1: "candle1H",
    Interval.H4: "candle4H",
    Interval.D1: "candle1D",
    Interval.W1: "candle1W",
    Interval.MN: "candle1M",
}

# REST 限频：20 次 / 2 秒
_RATE_LIMIT_INTERVAL = 2.0 / 20  # 0.1 秒


class OKXFetcher(DataFetcher):
    """OKX 数据源，继承 DataFetcher 抽象基类。"""

    def __init__(self) -> None:
        self._base_url: str = config.OKX_BASE_URL
        self._ws_url: str = config.OKX_WS_PUBLIC
        self._session: Optional[aiohttp.ClientSession] = None

        # WebSocket 相关
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._subscriptions: Dict[str, Dict[str, Any]] = {}  # key = "instId:channel"
        self._running = False

        # 限频控制
        self._last_request_ts: float = 0.0

    # ------------------------------------------------------------------ #
    #                          HTTP 基础方法                               #
    # ------------------------------------------------------------------ #

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _throttle(self) -> None:
        """简易限频：确保两次请求间隔 >= _RATE_LIMIT_INTERVAL。"""
        now = time.monotonic()
        delta = _RATE_LIMIT_INTERVAL - (now - self._last_request_ts)
        if delta > 0:
            await asyncio.sleep(delta)
        self._last_request_ts = time.monotonic()

    async def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        """发起 GET 请求并返回 JSON data 字段。"""
        await self._throttle()
        session = await self._ensure_session()
        url = f"{self._base_url}{path}"
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            body = await resp.json()
            if body.get("code") != "0":
                raise RuntimeError(f"OKX API error: {body.get('msg', body)}")
            return body.get("data", [])

    # ------------------------------------------------------------------ #
    #                     DataFetcher 抽象方法实现                          #
    # ------------------------------------------------------------------ #

    async def get_symbols(self, query: str = "") -> List[Symbol]:
        """
        获取 SPOT 交易品种列表。
        query 不为空时按 instId 模糊过滤。
        """
        data = await self._get("/api/v5/public/instruments", {"instType": "SPOT"})
        symbols: List[Symbol] = []
        query_upper = query.upper()
        for item in data:
            inst_id: str = item["instId"]
            if query_upper and query_upper not in inst_id.upper():
                continue
            symbols.append(
                Symbol(
                    symbol=inst_id,
                    name=inst_id,
                    market=Market.CRYPTO,
                    exchange="okx",
                    base=item.get("baseCcy", ""),
                    quote=item.get("quoteCcy", ""),
                )
            )
        return symbols

    async def get_klines(
        self, symbol: str, interval: Interval, limit: int = 500, end_time_ms: Optional[int] = None
    ) -> List[Candle]:
        """
        获取历史 K 线，自动分页拉取直到 limit 满足。
        OKX 单次最多 100 根，用 after 参数翻页。
        end_time_ms: 毫秒时间戳，只返回早于此时间的K线（向左懒加载）。
        返回按时间升序排列的 Candle 列表。
        """
        bar = _INTERVAL_MAP.get(interval)
        if bar is None:
            raise ValueError(f"不支持的 Interval: {interval}")

        all_candles: List[Candle] = []
        # end_time_ms 存在时直接走 history-candles，after 设为该时间戳
        after: Optional[str] = str(end_time_ms) if end_time_ms is not None else None
        remaining = limit

        while remaining > 0:
            batch_size = min(remaining, 100)
            params: Dict[str, Any] = {
                "instId": symbol,
                "bar": bar,
                "limit": str(batch_size),
            }

            # end_time_ms 存在或已翻页，走 history-candles；否则走 candles（含最新未收盘K线）
            if after is not None:
                path = "/api/v5/market/history-candles"
                params["after"] = after
            else:
                path = "/api/v5/market/candles"

            data = await self._get(path, params)
            if not data:
                break

            for row in data:
                # OKX 数组: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
                all_candles.append(
                    Candle(
                        timestamp=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        turnover=float(row[7]) if len(row) > 7 else 0.0,
                    )
                )

            # OKX 返回按时间降序，最后一条的 ts 最早 → 用作 after
            after = data[-1][0]
            remaining -= len(data)

            # 如果本批不足 batch_size，说明没有更多数据
            if len(data) < batch_size:
                break

        # 按时间升序排列
        all_candles.sort(key=lambda c: c.timestamp)
        return all_candles

    async def subscribe_realtime(self, symbol: str, interval: Interval, callback: Callable) -> None:
        """
        通过 WebSocket 订阅实时 K 线推送。
        callback 签名: async def callback(candle: Candle, confirm: bool) -> None
        """
        channel = _WS_CHANNEL_MAP.get(interval)
        if channel is None:
            raise ValueError(f"不支持的 Interval: {interval}")

        key = f"{symbol}:{channel}"
        self._subscriptions[key] = {
            "channel": channel,
            "instId": symbol,
            "callback": callback,
        }

        # 如果 WS loop 还没启动，启动它
        if self._ws_task is None or self._ws_task.done():
            self._running = True
            self._ws_task = asyncio.create_task(self._ws_loop())
        else:
            # 已有连接，直接发送订阅
            await self._ws_send_subscribe(channel, symbol)

    async def unsubscribe(self, symbol: str) -> None:
        """取消该 symbol 的所有 WebSocket 订阅。"""
        keys_to_remove = [k for k in self._subscriptions if k.startswith(f"{symbol}:")]
        for key in keys_to_remove:
            sub = self._subscriptions.pop(key)
            if self._ws and not self._ws.closed:
                msg = {
                    "op": "unsubscribe",
                    "args": [{"channel": sub["channel"], "instId": symbol}],
                }
                await self._ws.send_json(msg)
                logger.info("OKX WS 取消订阅: %s %s", sub["channel"], symbol)

        # 无订阅时关闭 WS
        if not self._subscriptions:
            self._running = False
            if self._ws and not self._ws.closed:
                await self._ws.close()

    # ------------------------------------------------------------------ #
    #                      额外 REST 接口方法                              #
    # ------------------------------------------------------------------ #

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取最新行情 ticker。"""
        data = await self._get("/api/v5/market/ticker", {"instId": symbol})
        if not data:
            return {}
        t = data[0]
        return {
            "instId": t["instId"],
            "last": float(t["last"]),
            "askPx": float(t.get("askPx", 0)),
            "bidPx": float(t.get("bidPx", 0)),
            "high24h": float(t.get("high24h", 0)),
            "low24h": float(t.get("low24h", 0)),
            "vol24h": float(t.get("vol24h", 0)),
            "volCcy24h": float(t.get("volCcy24h", 0)),
            "ts": int(t["ts"]),
        }

    async def get_funding_rate(self, symbol: str) -> Dict[str, Any]:
        """
        获取资金费率（仅永续合约，如 BTC-USDT-SWAP）。
        """
        data = await self._get("/api/v5/public/funding-rate", {"instId": symbol})
        if not data:
            return {}
        r = data[0]
        return {
            "instId": r["instId"],
            "fundingRate": float(r.get("fundingRate", 0)),
            "nextFundingRate": float(r.get("nextFundingRate", 0)),
            "fundingTime": int(r.get("fundingTime", 0)),
        }

    async def get_open_interest(self, symbol: str) -> Dict[str, Any]:
        """
        获取持仓量（仅永续/交割合约，如 BTC-USDT-SWAP）。
        """
        data = await self._get("/api/v5/public/open-interest", {"instId": symbol})
        if not data:
            return {}
        r = data[0]
        return {
            "instId": r["instId"],
            "oi": float(r.get("oi", 0)),
            "oiCcy": float(r.get("oiCcy", 0)),
            "ts": int(r.get("ts", 0)),
        }

    async def get_long_short_ratio(self, currency: str = "BTC", period: str = "1H") -> List[Dict[str, Any]]:
        """
        获取多空比（账户持仓人数比）。
        currency: 币种，如 BTC / ETH
        period: 1H / 1D 等
        """
        data = await self._get(
            "/api/v5/rubik/stat/contracts/long-short-account-ratio",
            {"ccy": currency, "period": period},
        )
        results = []
        for row in data:
            results.append(
                {
                    "ts": int(row.get("ts", 0)),
                    "longShortRatio": float(row.get("longShortRatio", 0)),
                }
            )
        return results

    # ------------------------------------------------------------------ #
    #                       WebSocket 内部方法                             #
    # ------------------------------------------------------------------ #

    async def _ws_send_subscribe(self, channel: str, inst_id: str) -> None:
        """向 WS 发送单个订阅指令。"""
        if self._ws and not self._ws.closed:
            msg = {
                "op": "subscribe",
                "args": [{"channel": channel, "instId": inst_id}],
            }
            await self._ws.send_json(msg)
            logger.info("OKX WS 已订阅: %s %s", channel, inst_id)

    async def _ws_subscribe_all(self) -> None:
        """把当前所有订阅重新发送（用于重连后恢复）。"""
        for sub in self._subscriptions.values():
            await self._ws_send_subscribe(sub["channel"], sub["instId"])

    async def _ws_loop(self) -> None:
        """
        WebSocket 主循环：连接 → 订阅 → 接收消息 → 心跳 → 断线重连。
        每 25 秒发送 "ping" 保活。
        """
        reconnect_delay = 1.0  # 初始重连间隔
        max_reconnect_delay = 60.0

        while self._running:
            try:
                session = await self._ensure_session()
                logger.info("OKX WS 连接中: %s", self._ws_url)
                self._ws = await session.ws_connect(self._ws_url, heartbeat=25, timeout=aiohttp.ClientTimeout(total=30))
                logger.info("OKX WS 已连接")
                reconnect_delay = 1.0  # 连接成功重置

                # 订阅所有 channel
                await self._ws_subscribe_all()

                # 心跳任务
                heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                try:
                    async for msg in self._ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_ws_message(msg.data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error("OKX WS error: %s", self._ws.exception())
                            break
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            logger.warning("OKX WS 连接关闭")
                            break
                finally:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                logger.warning("OKX WS 连接异常: %s，%s 秒后重连", exc, reconnect_delay)

            if not self._running:
                break

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

        logger.info("OKX WS loop 已退出")

    async def _heartbeat_loop(self) -> None:
        """每 25 秒发送 ping 保活。"""
        try:
            while self._running and self._ws and not self._ws.closed:
                await self._ws.send_str("ping")
                await asyncio.sleep(25)
        except (asyncio.CancelledError, Exception):
            pass

    async def _handle_ws_message(self, raw: str) -> None:
        """处理 WebSocket 收到的消息。"""
        if raw == "pong":
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("OKX WS 忽略非 JSON: %s", raw[:100])
            return

        # 事件消息（subscribe 确认等）
        if "event" in payload:
            if payload["event"] == "error":
                logger.error("OKX WS 事件错误: %s", payload.get("msg"))
            return

        # 数据推送
        arg = payload.get("arg", {})
        channel = arg.get("channel", "")
        inst_id = arg.get("instId", "")
        data_list = payload.get("data", [])

        key = f"{inst_id}:{channel}"
        sub = self._subscriptions.get(key)
        if sub is None:
            return

        callback = sub["callback"]
        for row in data_list:
            # row: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
            candle = Candle(
                timestamp=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            confirm = row[8] == "1" if len(row) > 8 else False
            try:
                await callback(candle, confirm)
            except Exception:
                logger.exception("OKX WS 回调异常: %s %s", inst_id, channel)

    # ------------------------------------------------------------------ #
    #                           清理                                      #
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """关闭所有连接并清理资源。"""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        self._subscriptions.clear()
        logger.info("OKXFetcher 已关闭")
