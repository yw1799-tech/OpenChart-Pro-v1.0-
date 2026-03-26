from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum


class Market(Enum):
    CRYPTO = "crypto"
    US = "us"
    HK = "hk"
    CN = "cn"


class Interval(Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1H"
    H4 = "4H"
    D1 = "1D"
    W1 = "1W"
    MN = "1M"


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0


@dataclass
class Symbol:
    symbol: str
    name: str
    market: Market
    exchange: str
    base: str = ""
    quote: str = ""


@dataclass
class KlineData:
    symbol: str
    market: Market
    interval: Interval
    candles: List[Candle]
    indicators: Dict[str, List[float]] = field(default_factory=dict)


@dataclass
class Alert:
    id: str
    symbol: str
    market: Market
    condition_type: str
    condition: Dict[str, Any]
    message: str
    notify_methods: List[str] = field(default_factory=lambda: ["browser", "sound"])
    label: str = ""
    repeat_mode: str = "once"
    cooldown: int = 300
    enabled: bool = True
    triggered_count: int = 0
    last_triggered: Optional[int] = None


@dataclass
class BacktestResult:
    id: str
    strategy_name: str
    symbol: str
    interval: str
    start_date: str
    end_date: str
    summary: Dict[str, Any]
    equity_curve: List[Dict]
    benchmark_curve: List[Dict]
    drawdown_curve: List[Dict]
    trades: List[Dict]
    monthly_returns: Dict
    optimization: Optional[Dict] = None
