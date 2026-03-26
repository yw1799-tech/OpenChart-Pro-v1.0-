"""
Yahoo Finance 数据源实现 — 美股 / 港股。
使用 yfinance 库获取数据，轮询方式实现实时推送。
"""
import asyncio
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Callable, Any

import yfinance as yf

from backend.data.fetcher import DataFetcher
from backend.data.models import Symbol, Candle, Market, Interval

logger = logging.getLogger(__name__)

# ---------- Interval → yfinance 参数映射 ----------
# yfinance interval: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo
_INTERVAL_MAP: Dict[Interval, str] = {
    Interval.M1:  "1m",
    Interval.M5:  "5m",
    Interval.M15: "15m",
    Interval.M30: "30m",
    Interval.H1:  "1h",
    Interval.H4:  "1h",   # yfinance 不支持 4h，用 1h 合并
    Interval.D1:  "1d",
    Interval.W1:  "1wk",
    Interval.MN:  "1mo",
}

# 各 interval 对应的最大回溯 period
# 分钟级数据最多保留 60 天（免费版有 15 分钟延迟）
_PERIOD_MAP: Dict[Interval, str] = {
    Interval.M1:  "7d",
    Interval.M5:  "60d",
    Interval.M15: "60d",
    Interval.M30: "60d",
    Interval.H1:  "730d",
    Interval.H4:  "730d",
    Interval.D1:  "max",
    Interval.W1:  "max",
    Interval.MN:  "max",
}

# 北京时间偏移
_BJ_TZ = timezone(timedelta(hours=8))

# 轮询间隔（秒）
_POLL_INTERVAL = 10.0


def _is_us_trading_hours() -> bool:
    """
    判断当前是否在美股交易时段。
    夏令时：北京时间 21:30 - 次日 04:00
    冬令时：北京时间 22:30 - 次日 05:00
    简化处理：使用 UTC 时间判断（美东 9:30-16:00）。
    """
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()  # 0=Monday
    if weekday >= 5:  # 周末
        return False

    # 美东时间 = UTC - 5（冬令时）或 UTC - 4（夏令时）
    # 简化：3月第二个周日到11月第一个周日为夏令时
    year = now_utc.year
    # 粗略判断夏令时
    month = now_utc.month
    if 3 < month < 11:
        is_dst = True
    elif month == 3:
        # 第二个周日之后
        is_dst = now_utc.day >= 8 + (6 - datetime(year, 3, 1).weekday()) % 7
    elif month == 11:
        # 第一个周日之前
        is_dst = now_utc.day < 1 + (6 - datetime(year, 11, 1).weekday()) % 7
    else:
        is_dst = False

    offset = timedelta(hours=-4 if is_dst else -5)
    et_now = now_utc + offset
    market_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = et_now.replace(hour=16, minute=0, second=0, microsecond=0)

    return market_open <= et_now <= market_close


def _is_hk_trading_hours() -> bool:
    """
    判断当前是否在港股交易时段。
    北京时间 9:30 - 12:00, 13:00 - 16:00（工作日）。
    """
    now_bj = datetime.now(_BJ_TZ)
    weekday = now_bj.weekday()
    if weekday >= 5:
        return False

    t = now_bj.hour * 60 + now_bj.minute
    # 9:30-12:00 → 570-720, 13:00-16:00 → 780-960
    return (570 <= t <= 720) or (780 <= t <= 960)


def _merge_4h_candles(candles_1h: List[Candle]) -> List[Candle]:
    """将1小时K线合并为4小时K线。"""
    if not candles_1h:
        return []

    result: List[Candle] = []
    group: List[Candle] = []

    for c in candles_1h:
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

    # 处理剩余不足4根的
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


class YahooFetcher(DataFetcher):
    """Yahoo Finance 数据源，继承 DataFetcher 抽象基类。"""

    def __init__(self) -> None:
        self._poll_tasks: Dict[str, asyncio.Task] = {}
        self._running_polls: Dict[str, bool] = {}

    # ------------------------------------------------------------------ #
    #                     DataFetcher 抽象方法实现                          #
    # ------------------------------------------------------------------ #

    async def get_symbols(self, query: str = "") -> List[Symbol]:
        """
        搜索品种。
        美股直接用代码如 AAPL，港股加 .HK 如 0700.HK。
        yfinance 没有原生搜索接口，通过 Ticker 验证是否有效。
        """
        if not query:
            # 根据当前市场返回对应默认品种
            mkt = getattr(self, '_market', None)
            if mkt == Market.HK:
                hk_defaults = [
                    ("0700.HK", "腾讯控股"), ("9988.HK", "阿里巴巴"),
                    ("0005.HK", "汇丰控股"), ("1810.HK", "小米集团"),
                    ("2318.HK", "中国平安"), ("3690.HK", "美团"),
                    ("9618.HK", "京东集团"), ("1024.HK", "快手"),
                ]
                return [Symbol(symbol=s, name=n, market=Market.HK, exchange="HKG") for s, n in hk_defaults]
            else:
                us_defaults = [
                    ("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corp."),
                    ("GOOGL", "Alphabet Inc."), ("AMZN", "Amazon.com"),
                    ("NVDA", "NVIDIA Corp."), ("TSLA", "Tesla Inc."),
                    ("META", "Meta Platforms"), ("NFLX", "Netflix Inc."),
                    ("JPM", "JPMorgan Chase"), ("V", "Visa Inc."),
                ]
                return [Symbol(symbol=s, name=n, market=Market.US, exchange="NMS") for s, n in us_defaults]

        loop = asyncio.get_event_loop()
        results: List[Symbol] = []

        # 尝试多种格式
        candidates = [query.upper()]
        # 如果是纯数字，尝试港股格式
        if query.isdigit():
            candidates.append(f"{query}.HK")
            candidates.append(f"{int(query):04d}.HK")

        for candidate in candidates:
            try:
                ticker = await loop.run_in_executor(None, lambda c=candidate: yf.Ticker(c))
                info = await loop.run_in_executor(None, lambda t=ticker: t.info)

                if not info or info.get("regularMarketPrice") is None:
                    continue

                exchange = info.get("exchange", "")
                long_name = info.get("longName", "") or info.get("shortName", candidate)

                # 判断市场
                if candidate.endswith(".HK"):
                    market = Market.HK
                else:
                    market = Market.US

                results.append(Symbol(
                    symbol=candidate,
                    name=long_name,
                    market=market,
                    exchange=exchange,
                ))
            except Exception as exc:
                logger.debug("Yahoo 搜索 %s 失败: %s", candidate, exc)

        return results

    async def get_klines(
        self, symbol: str, interval: Interval, limit: int = 500
    ) -> List[Candle]:
        """
        获取历史 K 线。
        注意：免费版有 15 分钟延迟。
        """
        yf_interval = _INTERVAL_MAP.get(interval)
        yf_period = _PERIOD_MAP.get(interval)
        if yf_interval is None or yf_period is None:
            raise ValueError(f"不支持的 Interval: {interval}")

        need_4h_merge = (interval == Interval.H4)
        fetch_limit = limit * 4 if need_4h_merge else limit

        loop = asyncio.get_event_loop()

        def _fetch():
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=yf_period, interval=yf_interval)
            return df

        df = await loop.run_in_executor(None, _fetch)

        if df is None or df.empty:
            return []

        # 只取最新 fetch_limit 根
        if len(df) > fetch_limit:
            df = df.tail(fetch_limit)

        candles: List[Candle] = []
        for idx, row in df.iterrows():
            ts = int(idx.timestamp() * 1000) if hasattr(idx, 'timestamp') else 0
            candles.append(Candle(
                timestamp=ts,
                open=float(row.get("Open", 0)),
                high=float(row.get("High", 0)),
                low=float(row.get("Low", 0)),
                close=float(row.get("Close", 0)),
                volume=float(row.get("Volume", 0)),
            ))

        if need_4h_merge:
            candles = _merge_4h_candles(candles)
            candles = candles[-limit:]

        return candles

    async def subscribe_realtime(
        self, symbol: str, interval: Interval, callback: Callable
    ) -> None:
        """
        使用轮询方式模拟实时推送。
        每 10 秒获取最新价格，非交易时段自动暂停。
        callback 签名: async def callback(candle: Candle, confirm: bool) -> None
        """
        key = f"{symbol}:{interval.value}"
        self._running_polls[key] = True

        async def _poll_loop():
            while self._running_polls.get(key, False):
                try:
                    # 判断交易时段
                    is_hk = symbol.endswith(".HK")
                    if is_hk:
                        in_session = _is_hk_trading_hours()
                    else:
                        in_session = _is_us_trading_hours()

                    if not in_session:
                        logger.debug("Yahoo %s 非交易时段，暂停轮询", symbol)
                        await asyncio.sleep(60)
                        continue

                    # 获取最新价格
                    loop = asyncio.get_event_loop()

                    def _get_latest():
                        ticker = yf.Ticker(symbol)
                        info = ticker.info
                        return info

                    info = await loop.run_in_executor(None, _get_latest)

                    if info and info.get("regularMarketPrice") is not None:
                        now_ms = int(time.time() * 1000)
                        candle = Candle(
                            timestamp=now_ms,
                            open=float(info.get("regularMarketOpen", 0)),
                            high=float(info.get("regularMarketDayHigh", 0)),
                            low=float(info.get("regularMarketDayLow", 0)),
                            close=float(info.get("regularMarketPrice", 0)),
                            volume=float(info.get("regularMarketVolume", 0)),
                        )
                        try:
                            await callback(candle, False)
                        except Exception:
                            logger.exception("Yahoo 轮询回调异常: %s", symbol)

                except Exception as exc:
                    logger.warning("Yahoo 轮询 %s 异常: %s", symbol, exc)

                await asyncio.sleep(_POLL_INTERVAL)

        task = asyncio.create_task(_poll_loop())
        self._poll_tasks[key] = task
        logger.info("Yahoo 开始轮询: %s %s", symbol, interval.value)

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
            logger.info("Yahoo 停止轮询: %s", key)

    # ------------------------------------------------------------------ #
    #                           清理                                      #
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """关闭所有轮询任务。"""
        for key in list(self._poll_tasks.keys()):
            self._running_polls[key] = False
            task = self._poll_tasks.pop(key)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._running_polls.clear()
        logger.info("YahooFetcher 已关闭")
