"""
东方财富数据源实现 — A股 REST 接口 + 轮询实时行情。
无需 API Key，免费公开接口。
"""

import asyncio
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Callable, Any

import aiohttp

from backend.data.fetcher import DataFetcher
from backend.data.models import Symbol, Candle, Market, Interval

logger = logging.getLogger(__name__)

# 北京时间偏移
_BJ_TZ = timezone(timedelta(hours=8))

# ---------- Interval → 东方财富 klt 参数映射 ----------
# klt: 1=1分钟, 5=5分钟, 15=15分钟, 30=30分钟, 60=60分钟, 101=日, 102=周, 103=月
_KLT_MAP: Dict[Interval, int] = {
    Interval.M1: 1,
    Interval.M5: 5,
    Interval.M15: 15,
    Interval.M30: 30,
    Interval.H1: 60,
    Interval.H4: 60,  # 不支持4H，用60分钟合并4根
    Interval.D1: 101,
    Interval.W1: 102,
    Interval.MN: 103,
}

# 限频控制
_RATE_LIMIT_INTERVAL = 0.2  # 200ms

# 轮询间隔
_POLL_INTERVAL = 3.0

# 通用请求头
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}


def _get_secid(code: str) -> str:
    """
    根据股票代码生成东方财富的 secid 前缀。
    上海: 1.600xxx/1.601xxx/1.603xxx, 科创板: 1.688xxx
    深圳: 0.000xxx/0.001xxx, 中小板: 0.002xxx, 创业板: 0.300xxx/0.301xxx
    北交所: 0.8xxxxx/0.43xxxx
    """
    # 去掉可能的前缀
    code = code.strip().upper()
    for prefix in ("SH", "SZ", "BJ", "1.", "0."):
        if code.startswith(prefix):
            code = code[len(prefix) :]
            break

    if code.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return f"1.{code}"
    elif code.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return f"0.{code}"
    elif code.startswith(("8", "43")):
        return f"0.{code}"
    # 指数
    elif code.startswith("399"):
        return f"0.{code}"
    elif code.startswith(("000", "880")):
        return f"1.{code}"
    else:
        # 默认尝试深圳
        return f"0.{code}"


def _is_cn_trading_hours() -> bool:
    """
    判断当前是否在 A 股交易时段。
    工作日 9:15-11:30, 13:00-15:00（北京时间）。
    """
    now_bj = datetime.now(_BJ_TZ)
    weekday = now_bj.weekday()
    if weekday >= 5:  # 周末
        return False

    t = now_bj.hour * 60 + now_bj.minute
    # 9:15-11:30 → 555-690, 13:00-15:00 → 780-900
    return (555 <= t <= 690) or (780 <= t <= 900)


def _merge_4h_candles(candles_60m: List[Candle]) -> List[Candle]:
    """将60分钟K线合并为4小时K线。"""
    if not candles_60m:
        return []

    result: List[Candle] = []
    group: List[Candle] = []

    for c in candles_60m:
        group.append(c)
        if len(group) == 4:
            merged = Candle(
                timestamp=group[0].timestamp,
                open=group[0].open,
                high=max(g.high for g in group),
                low=min(g.low for g in group),
                close=group[-1].close,
                volume=sum(g.volume for g in group),
                turnover=sum(g.turnover for g in group),
            )
            result.append(merged)
            group = []

    if group:
        merged = Candle(
            timestamp=group[0].timestamp,
            open=group[0].open,
            high=max(g.high for g in group),
            low=min(g.low for g in group),
            close=group[-1].close,
            volume=sum(g.volume for g in group),
            turnover=sum(g.turnover for g in group),
        )
        result.append(merged)

    return result


class EastMoneyFetcher(DataFetcher):
    """东方财富数据源，继承 DataFetcher 抽象基类。"""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

        # 轮询相关
        self._poll_tasks: Dict[str, asyncio.Task] = {}
        self._running_polls: Dict[str, bool] = {}

        # 限频控制
        self._last_request_ts: float = 0.0

    # ------------------------------------------------------------------ #
    #                          HTTP 基础方法                               #
    # ------------------------------------------------------------------ #

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=_HEADERS)
        return self._session

    async def _throttle(self) -> None:
        now = time.monotonic()
        delta = _RATE_LIMIT_INTERVAL - (now - self._last_request_ts)
        if delta > 0:
            await asyncio.sleep(delta)
        self._last_request_ts = time.monotonic()

    async def _get(self, url: str, params: Optional[Dict] = None) -> Any:
        """发起 GET 请求并返回 JSON。"""
        await self._throttle()
        session = await self._ensure_session()
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            body = await resp.json(content_type=None)
            return body

    # ------------------------------------------------------------------ #
    #                     DataFetcher 抽象方法实现                          #
    # ------------------------------------------------------------------ #

    async def get_symbols(self, query: str = "") -> List[Symbol]:
        """
        搜索 A 股品种。
        使用东方财富搜索接口，支持股票代码和中文名称。
        """
        if not query:
            # 返回常用A股品种列表
            defaults = [
                ("600519", "贵州茅台", "SSE"),
                ("601318", "中国平安", "SSE"),
                ("000858", "五粮液", "SZSE"),
                ("000333", "美的集团", "SZSE"),
                ("600036", "招商银行", "SSE"),
                ("601166", "兴业银行", "SSE"),
                ("000001", "平安银行", "SZSE"),
                ("600276", "恒瑞医药", "SSE"),
                ("300750", "宁德时代", "SZSE"),
                ("002594", "比亚迪", "SZSE"),
                ("601888", "中国中免", "SSE"),
                ("600900", "长江电力", "SSE"),
                ("000568", "泸州老窖", "SZSE"),
                ("002475", "立讯精密", "SZSE"),
                ("603259", "药明康德", "SSE"),
                ("300059", "东方财富", "SZSE"),
            ]
            return [Symbol(symbol=s, name=n, market=Market.CN, exchange=e) for s, n, e in defaults]

        params = {
            "input": query,
            "type": "14",
            "token": "D43BF722C8E33BDC906FB84D85E326E8",
            "count": "10",
        }

        try:
            data = await self._get(
                "https://searchapi.eastmoney.com/api/suggest/get",
                params,
            )
        except Exception as exc:
            logger.warning("东方财富搜索异常: %s", exc)
            return []

        symbols: List[Symbol] = []
        quote_list = data.get("QuotationCodeTable", {}).get("Data", [])
        if not quote_list:
            return symbols

        for item in quote_list:
            code = item.get("Code", "")
            name = item.get("Name", "")
            market_type = item.get("MktNum", "")

            # 过滤非股票类型
            security_type = item.get("SecurityTypeName", "")
            if security_type and "股" not in security_type and "指数" not in security_type:
                continue

            # 判断交易所
            if market_type == "1":
                exchange = "SSE"  # 上交所
            elif market_type == "0":
                exchange = "SZSE"  # 深交所
            elif market_type == "2":
                exchange = "BSE"  # 北交所
            else:
                exchange = ""

            symbols.append(
                Symbol(
                    symbol=code,
                    name=name,
                    market=Market.CN,
                    exchange=exchange,
                )
            )

        return symbols

    async def get_klines(self, symbol: str, interval: Interval, limit: int = 500) -> List[Candle]:
        """
        获取历史 K 线数据。
        注意：4H 周期不直接支持，使用 60 分钟合并 4 根实现。
        """
        klt = _KLT_MAP.get(interval)
        if klt is None:
            raise ValueError(f"不支持的 Interval: {interval}")

        secid = _get_secid(symbol)
        need_4h_merge = interval == Interval.H4
        fetch_limit = limit * 4 if need_4h_merge else limit

        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": str(klt),
            "fqt": "1",  # 前复权
            "end": "20500101",
            "lmt": str(fetch_limit),
        }

        try:
            data = await self._get(
                "http://push2his.eastmoney.com/api/qt/stock/kline/get",
                params,
            )
        except Exception as exc:
            logger.warning("东方财富K线请求异常: %s", exc)
            return []

        klines_data = data.get("data", {})
        if not klines_data:
            return []

        klines_list = klines_data.get("klines", [])
        if not klines_list:
            return []

        candles: List[Candle] = []
        for line in klines_list:
            # 格式: "2024-01-02 09:30,10.50,10.80,10.40,10.70,12345,678901.00,..."
            parts = line.split(",")
            if len(parts) < 7:
                continue

            try:
                # 解析时间字符串为时间戳
                time_str = parts[0]
                if " " in time_str:
                    dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
                else:
                    dt = datetime.strptime(time_str, "%Y-%m-%d")
                dt = dt.replace(tzinfo=_BJ_TZ)
                ts = int(dt.timestamp() * 1000)

                candles.append(
                    Candle(
                        timestamp=ts,
                        open=float(parts[1]),
                        close=float(parts[2]),
                        high=float(parts[3]),
                        low=float(parts[4]),
                        volume=float(parts[5]),
                        turnover=float(parts[6]) if len(parts) > 6 else 0.0,
                    )
                )
            except (ValueError, IndexError) as exc:
                logger.debug("东方财富K线解析异常: %s, line=%s", exc, line)
                continue

        if need_4h_merge:
            candles = _merge_4h_candles(candles)
            candles = candles[-limit:]

        return candles

    async def subscribe_realtime(self, symbol: str, interval: Interval, callback: Callable) -> None:
        """
        使用轮询方式获取实时行情。
        每 3 秒获取最新数据，非交易时段自动暂停。
        callback 签名: async def callback(candle: Candle, confirm: bool) -> None
        """
        key = f"{symbol}:{interval.value}"
        self._running_polls[key] = True
        secid = _get_secid(symbol)

        async def _poll_loop():
            while self._running_polls.get(key, False):
                try:
                    if not _is_cn_trading_hours():
                        logger.debug("东方财富 %s 非交易时段，暂停轮询", symbol)
                        await asyncio.sleep(60)
                        continue

                    # 获取实时行情
                    params = {
                        "secid": secid,
                        "fields": "f43,f44,f45,f46,f47,f48,f50,f57,f58,f169,f170",
                    }
                    data = await self._get(
                        "http://push2.eastmoney.com/api/qt/stock/get",
                        params,
                    )

                    quote = data.get("data", {})
                    if quote:
                        now_ms = int(time.time() * 1000)

                        # f43=最新价, f44=最高, f45=最低, f46=开盘
                        # f47=成交量, f48=成交额, f170=涨跌幅
                        # 东方财富价格需除以 1000（有些品种除以 100）
                        divisor = 1000.0
                        price = quote.get("f43", 0)
                        if isinstance(price, str):
                            price = float(price) if price != "-" else 0
                        else:
                            price = float(price) / divisor if price else 0

                        high = quote.get("f44", 0)
                        high = float(high) / divisor if high and high != "-" else price

                        low = quote.get("f45", 0)
                        low = float(low) / divisor if low and low != "-" else price

                        open_price = quote.get("f46", 0)
                        open_price = float(open_price) / divisor if open_price and open_price != "-" else price

                        volume = float(quote.get("f47", 0)) if quote.get("f47") else 0
                        turnover = float(quote.get("f48", 0)) if quote.get("f48") else 0

                        candle = Candle(
                            timestamp=now_ms,
                            open=open_price,
                            high=high,
                            low=low,
                            close=price,
                            volume=volume,
                            turnover=turnover,
                        )

                        try:
                            await callback(candle, False)
                        except Exception:
                            logger.exception("东方财富轮询回调异常: %s", symbol)

                except Exception as exc:
                    logger.warning("东方财富轮询 %s 异常: %s", symbol, exc)

                await asyncio.sleep(_POLL_INTERVAL)

        task = asyncio.create_task(_poll_loop())
        self._poll_tasks[key] = task
        logger.info("东方财富开始轮询: %s %s", symbol, interval.value)

    async def unsubscribe(self, symbol: str) -> None:
        """取消该 symbol 的所有轮询订阅。"""
        keys_to_remove = [k for k in self._poll_tasks if k.startswith(f"{symbol}:")]
        for key in keys_to_remove:
            self._running_polls[key] = False
            task = self._poll_tasks.pop(key)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info("东方财富停止轮询: %s", key)

    # ------------------------------------------------------------------ #
    #                      额外 REST 接口方法                              #
    # ------------------------------------------------------------------ #

    async def get_realtime_quote(self, symbol: str) -> Dict[str, Any]:
        """获取单只股票的实时行情快照。"""
        secid = _get_secid(symbol)
        params = {
            "secid": secid,
            "fields": "f43,f44,f45,f46,f47,f48,f50,f57,f58,f169,f170",
        }
        data = await self._get(
            "http://push2.eastmoney.com/api/qt/stock/get",
            params,
        )
        quote = data.get("data", {})
        if not quote:
            return {}

        return {
            "code": quote.get("f57", symbol),
            "name": quote.get("f58", ""),
            "price": quote.get("f43", 0),
            "high": quote.get("f44", 0),
            "low": quote.get("f45", 0),
            "open": quote.get("f46", 0),
            "volume": quote.get("f47", 0),
            "turnover": quote.get("f48", 0),
            "change_pct": quote.get("f170", 0),
            "change_amount": quote.get("f169", 0),
        }

    # ------------------------------------------------------------------ #
    #                           清理                                      #
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """关闭所有连接并清理资源。"""
        for key in list(self._poll_tasks.keys()):
            self._running_polls[key] = False
            task = self._poll_tasks.pop(key)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._running_polls.clear()

        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("EastMoneyFetcher 已关闭")
