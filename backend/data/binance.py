"""
Binance 数据源实现 — REST + WebSocket。
公开数据接口无需 API Key。
"""
import asyncio
import json
import time
import logging
from typing import List, Dict, Optional, Callable, Any

import aiohttp

from backend.data.fetcher import DataFetcher
from backend.data.models import Symbol, Candle, Market, Interval

logger = logging.getLogger(__name__)

# ---------- Interval → Binance interval 参数映射 ----------
_INTERVAL_MAP: Dict[Interval, str] = {
    Interval.M1:  "1m",
    Interval.M5:  "5m",
    Interval.M15: "15m",
    Interval.M30: "30m",
    Interval.H1:  "1h",
    Interval.H4:  "4h",
    Interval.D1:  "1d",
    Interval.W1:  "1w",
    Interval.MN:  "1M",
}

# Interval → WebSocket stream 名称
_WS_STREAM_MAP: Dict[Interval, str] = {
    Interval.M1:  "kline_1m",
    Interval.M5:  "kline_5m",
    Interval.M15: "kline_15m",
    Interval.M30: "kline_30m",
    Interval.H1:  "kline_1h",
    Interval.H4:  "kline_4h",
    Interval.D1:  "kline_1d",
    Interval.W1:  "kline_1w",
    Interval.MN:  "kline_1M",
}

_BASE_URL = "https://api.binance.com"
_WS_URL = "wss://stream.binance.com:9443/ws"

# REST 限频：1200 次 / 分钟 ≈ 50ms 间隔
_RATE_LIMIT_INTERVAL = 60.0 / 1200


def _symbol_to_binance(symbol: str) -> str:
    """BTC-USDT → BTCUSDT"""
    return symbol.replace("-", "")


def _symbol_from_binance(symbol: str, base: str = "", quote: str = "") -> str:
    """BTCUSDT → BTC-USDT（需要 base/quote 信息）"""
    if base and quote:
        return f"{base}-{quote}"
    return symbol


class BinanceFetcher(DataFetcher):
    """Binance 数据源，继承 DataFetcher 抽象基类。"""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

        # WebSocket 相关
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._subscriptions: Dict[str, Dict[str, Any]] = {}  # key = "symbol:stream"
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
        now = time.monotonic()
        delta = _RATE_LIMIT_INTERVAL - (now - self._last_request_ts)
        if delta > 0:
            await asyncio.sleep(delta)
        self._last_request_ts = time.monotonic()

    async def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        """发起 GET 请求并返回 JSON。"""
        await self._throttle()
        session = await self._ensure_session()
        url = f"{_BASE_URL}{path}"
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            body = await resp.json()
            if isinstance(body, dict) and "code" in body and body["code"] != 0:
                raise RuntimeError(f"Binance API error: {body.get('msg', body)}")
            return body

    # ------------------------------------------------------------------ #
    #                     DataFetcher 抽象方法实现                          #
    # ------------------------------------------------------------------ #

    async def get_symbols(self, query: str = "") -> List[Symbol]:
        """
        获取 SPOT 交易品种列表。
        query 不为空时按 symbol 模糊过滤。
        """
        data = await self._get("/api/v3/exchangeInfo")
        symbols: List[Symbol] = []
        query_upper = query.upper()

        for item in data.get("symbols", []):
            if item.get("status") != "TRADING":
                continue
            raw_symbol: str = item["symbol"]
            base = item.get("baseAsset", "")
            quote = item.get("quoteAsset", "")
            display_symbol = f"{base}-{quote}"

            if query_upper and query_upper not in raw_symbol.upper() and query_upper not in display_symbol.upper():
                continue

            symbols.append(Symbol(
                symbol=display_symbol,
                name=display_symbol,
                market=Market.CRYPTO,
                exchange="binance",
                base=base,
                quote=quote,
            ))
        return symbols

    async def get_klines(
        self, symbol: str, interval: Interval, limit: int = 500, end_time_ms: Optional[int] = None
    ) -> List[Candle]:
        """
        获取历史 K 线。Binance 单次最多 1000 根，用 endTime 翻页。
        end_time_ms: 毫秒时间戳，只返回早于此时间的K线（向左懒加载）。
        返回按时间升序排列的 Candle 列表。
        """
        bn_interval = _INTERVAL_MAP.get(interval)
        if bn_interval is None:
            raise ValueError(f"不支持的 Interval: {interval}")

        bn_symbol = _symbol_to_binance(symbol)
        all_candles: List[Candle] = []
        # 如果指定了 end_time_ms，用它作为初始 endTime；否则从最新开始
        end_time: Optional[int] = (end_time_ms - 1) if end_time_ms is not None else None
        remaining = limit

        while remaining > 0:
            batch_size = min(remaining, 1000)
            params: Dict[str, Any] = {
                "symbol": bn_symbol,
                "interval": bn_interval,
                "limit": batch_size,
            }
            if end_time is not None:
                params["endTime"] = end_time - 1  # 不包含上次最早的那根

            data = await self._get("/api/v3/klines", params)
            if not data:
                break

            batch: List[Candle] = []
            for row in data:
                # Binance K线数组:
                # [openTime, open, high, low, close, volume,
                #  closeTime, quoteVolume, trades, takerBuyBaseVol, takerBuyQuoteVol, ignore]
                batch.append(Candle(
                    timestamp=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    turnover=float(row[7]),  # quoteAssetVolume
                ))

            all_candles.extend(batch)

            # 取本批最早的 timestamp 作为下一次的 endTime
            earliest_ts = min(c.timestamp for c in batch)
            end_time = earliest_ts
            remaining -= len(batch)

            if len(batch) < batch_size:
                break

        # 去重并按时间升序排列
        seen = set()
        unique: List[Candle] = []
        for c in all_candles:
            if c.timestamp not in seen:
                seen.add(c.timestamp)
                unique.append(c)
        unique.sort(key=lambda c: c.timestamp)
        return unique

    async def subscribe_realtime(
        self, symbol: str, interval: Interval, callback: Callable
    ) -> None:
        """
        通过 WebSocket 订阅实时 K 线推送。
        callback 签名: async def callback(candle: Candle, confirm: bool) -> None
        """
        stream = _WS_STREAM_MAP.get(interval)
        if stream is None:
            raise ValueError(f"不支持的 Interval: {interval}")

        bn_symbol = _symbol_to_binance(symbol).lower()
        stream_name = f"{bn_symbol}@{stream}"
        key = f"{symbol}:{stream}"

        self._subscriptions[key] = {
            "stream": stream_name,
            "symbol": symbol,
            "callback": callback,
        }

        if self._ws_task is None or self._ws_task.done():
            self._running = True
            self._ws_task = asyncio.create_task(self._ws_loop())
        else:
            # 已有连接，发送订阅
            await self._ws_send_subscribe([stream_name])

    async def unsubscribe(self, symbol: str) -> None:
        """取消该 symbol 的所有 WebSocket 订阅。"""
        keys_to_remove = [k for k in self._subscriptions if k.startswith(f"{symbol}:")]
        streams_to_unsub = []
        for key in keys_to_remove:
            sub = self._subscriptions.pop(key)
            streams_to_unsub.append(sub["stream"])

        if streams_to_unsub and self._ws and not self._ws.closed:
            msg = {
                "method": "UNSUBSCRIBE",
                "params": streams_to_unsub,
                "id": int(time.time() * 1000),
            }
            await self._ws.send_json(msg)
            logger.info("Binance WS 取消订阅: %s", streams_to_unsub)

        if not self._subscriptions:
            self._running = False
            if self._ws and not self._ws.closed:
                await self._ws.close()

    # ------------------------------------------------------------------ #
    #                      额外 REST 接口方法                              #
    # ------------------------------------------------------------------ #

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取最新价格。"""
        bn_symbol = _symbol_to_binance(symbol)
        data = await self._get("/api/v3/ticker/price", {"symbol": bn_symbol})
        return {
            "symbol": symbol,
            "price": float(data.get("price", 0)),
        }

    # ------------------------------------------------------------------ #
    #                       WebSocket 内部方法                             #
    # ------------------------------------------------------------------ #

    async def _ws_send_subscribe(self, streams: List[str]) -> None:
        """向 WS 发送订阅指令。"""
        if self._ws and not self._ws.closed:
            msg = {
                "method": "SUBSCRIBE",
                "params": streams,
                "id": int(time.time() * 1000),
            }
            await self._ws.send_json(msg)
            logger.info("Binance WS 已订阅: %s", streams)

    async def _ws_subscribe_all(self) -> None:
        """把当前所有订阅重新发送（用于重连后恢复）。"""
        streams = [sub["stream"] for sub in self._subscriptions.values()]
        if streams:
            await self._ws_send_subscribe(streams)

    async def _ws_loop(self) -> None:
        """
        WebSocket 主循环：连接 → 订阅 → 接收消息 → 断线重连。
        """
        reconnect_delay = 1.0
        max_reconnect_delay = 60.0

        while self._running:
            try:
                session = await self._ensure_session()
                logger.info("Binance WS 连接中: %s", _WS_URL)
                self._ws = await session.ws_connect(
                    _WS_URL, heartbeat=30, timeout=aiohttp.ClientTimeout(total=30)
                )
                logger.info("Binance WS 已连接")
                reconnect_delay = 1.0

                await self._ws_subscribe_all()

                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_ws_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error("Binance WS error: %s", self._ws.exception())
                        break
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        logger.warning("Binance WS 连接关闭")
                        break

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                logger.warning("Binance WS 连接异常: %s，%s 秒后重连", exc, reconnect_delay)

            if not self._running:
                break

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

        logger.info("Binance WS loop 已退出")

    async def _handle_ws_message(self, raw: str) -> None:
        """处理 WebSocket 收到的消息。"""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Binance WS 忽略非 JSON: %s", raw[:100])
            return

        # 订阅确认等响应消息
        if "result" in payload or "id" in payload:
            return

        # K线数据推送格式: {"e": "kline", "s": "BTCUSDT", "k": {...}}
        if payload.get("e") != "kline":
            return

        kline = payload.get("k", {})
        raw_symbol = payload.get("s", "")
        stream_interval = kline.get("i", "")
        stream_name = f"{raw_symbol.lower()}@kline_{stream_interval}"

        # 查找匹配的回调
        callback = None
        for sub in self._subscriptions.values():
            if sub["stream"] == stream_name:
                callback = sub["callback"]
                break

        if callback is None:
            return

        candle = Candle(
            timestamp=int(kline["t"]),
            open=float(kline["o"]),
            high=float(kline["h"]),
            low=float(kline["l"]),
            close=float(kline["c"]),
            volume=float(kline["v"]),
            turnover=float(kline.get("q", 0)),
        )
        confirm = kline.get("x", False)

        try:
            await callback(candle, confirm)
        except Exception:
            logger.exception("Binance WS 回调异常: %s", stream_name)

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
        logger.info("BinanceFetcher 已关闭")
