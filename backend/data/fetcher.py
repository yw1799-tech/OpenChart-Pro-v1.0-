"""
数据源工厂，根据 market 类型自动选择对应的数据源。
"""
from abc import ABC, abstractmethod
from typing import List
from backend.data.models import Symbol, Candle, Market, Interval

class DataFetcher(ABC):
    # 子类可设置，用于区分同一fetcher服务不同市场
    _market: Market = None

    @abstractmethod
    async def get_symbols(self, query: str = "") -> List[Symbol]:
        pass

    @abstractmethod
    async def get_klines(self, symbol: str, interval: Interval, limit: int = 500) -> List[Candle]:
        pass

    @abstractmethod
    async def subscribe_realtime(self, symbol: str, interval: Interval, callback) -> None:
        pass

    @abstractmethod
    async def unsubscribe(self, symbol: str) -> None:
        pass

_fetcher_cache: dict = {}

def get_fetcher(market: Market) -> DataFetcher:
    """获取数据源实例（单例缓存，避免重复创建 session）。"""
    import backend.config as config

    # 构造缓存 key（US和HK分别缓存）
    if market == Market.CRYPTO:
        cache_key = f"crypto:{config.CRYPTO_EXCHANGE}"
    else:
        cache_key = market.value

    if cache_key in _fetcher_cache:
        return _fetcher_cache[cache_key]

    from backend.data.okx import OKXFetcher
    from backend.data.binance import BinanceFetcher
    from backend.data.yahoo import YahooFetcher
    from backend.data.eastmoney import EastMoneyFetcher

    if market == Market.CRYPTO:
        if config.CRYPTO_EXCHANGE == "okx":
            fetcher = OKXFetcher()
        else:
            fetcher = BinanceFetcher()
    elif market in (Market.US, Market.HK):
        fetcher = YahooFetcher()
        fetcher._market = market
    elif market == Market.CN:
        fetcher = EastMoneyFetcher()
    else:
        raise ValueError(f"Unsupported market: {market}")

    _fetcher_cache[cache_key] = fetcher
    return fetcher
