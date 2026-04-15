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


# ═══════════════════════════════════════════════════════════════════
# Phase 3A/3B — 新闻管线 + 宏观数据影响
# ═══════════════════════════════════════════════════════════════════


@dataclass
class FlashNews:
    """
    新闻快讯统一数据结构。
    由规则引擎产出基础字段（Phase 3A），LLM 异步回填 ai_analysis（Phase 3B）。
    宏观数据公布时，is_macro_data=True 且 macro_* 字段有值。
    """

    id: str
    title: str
    content: str
    source: str
    url: str
    published_at: int  # 源站发布时间（毫秒时间戳）
    collected_at: int  # 采集入库时间
    importance: int = 1  # 1~5 星
    sentiment: str = "neutral"  # bullish | bearish | neutral
    categories: List[str] = field(default_factory=list)  # 关联品种代码
    impact_tags: List[str] = field(default_factory=list)  # 事件类型标签
    keywords: List[str] = field(default_factory=list)  # 命中关键词
    is_holding_related: bool = False
    l2_score: float = 0.0  # 规则引擎原始分

    # 加密 6 币种影响（规则引擎产出）
    # 格式: [{"symbol": "BTC-USDT", "direction": "bullish", "strength": 4, "reason": "..."}]
    impact_on_crypto: Optional[List[Dict[str, Any]]] = None

    # 宏观数据字段（仅 is_macro_data=True 时填充）
    is_macro_data: bool = False
    macro_type: str = ""  # "CPI" | "FOMC" | "NFP"
    macro_actual: Optional[str] = None
    macro_forecast: Optional[str] = None
    macro_previous: Optional[str] = None
    macro_deviation_pct: Optional[float] = None  # (actual - forecast) / forecast
    macro_impact_strength: str = ""  # "neutral" | "light" | "strong"

    # LLM 深度解读（Phase 3B 异步回填）
    ai_analysis: Optional[Dict[str, Any]] = None


# ═══════════════════════════════════════════════════════════════════
# Phase 3A/3B — 候选池（仅股票市场）
# ═══════════════════════════════════════════════════════════════════


@dataclass
class WatchPoolItem:
    """候选池品种。market 仅允许 us/hk/cn，加密品种拒绝入池。"""

    id: str
    symbol: str
    market: Market  # CRYPTO 在 API 层和 DB CHECK 约束中双重拦截
    score: float = 0.0  # 综合评分 0-100
    status: str = "candidate"  # "candidate" | "monitoring" | "archived"
    source: str = "manual"  # "news" | "anomaly" | "macro_theme" | "manual"
    reason: str = ""
    added_at: int = 0
    last_scored_at: Optional[int] = None
    last_news_mention_at: Optional[int] = None  # 最近一次新闻提及
    low_score_since: Optional[int] = None  # 开始连续低分的时间


# ═══════════════════════════════════════════════════════════════════
# Phase 4 — 策略绑定 + 信号
# ═══════════════════════════════════════════════════════════════════


@dataclass
class StrategyBinding:
    """策略与品种的多对多绑定关系。一只品种可绑多个策略，一个策略可绑多只品种。"""

    id: str
    symbol: str
    market: Market
    strategy_name: str
    params: Dict[str, Any] = field(default_factory=dict)  # 可覆盖策略默认参数
    enabled: bool = True
    created_at: int = 0


@dataclass
class Signal:
    """
    策略监控产生的交易信号。
    confidence 计算见 PRD F7.5 / TDD §6.5.1 (基础分 + 通用加减分项)。
    """

    id: str
    symbol: str
    market: Market
    action: str  # "buy" | "sell"
    strategy_name: str
    confidence: int  # 0-100
    price: float
    suggested_qty: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    triggered_by: Dict[str, Any] = field(default_factory=dict)  # {flash_id, indicator, ...}
    generated_at: int = 0


# ═══════════════════════════════════════════════════════════════════
# Phase 5 — 持仓管理
# ═══════════════════════════════════════════════════════════════════


@dataclass
class Position:
    """用户录入的持仓。未来 Phase 7 可对接券商 API 自动同步。"""

    id: str
    symbol: str
    market: Market
    quantity: float
    avg_cost: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    latest_advice: str = ""  # "hold" | "reduce" | "add" | "close"
    advice_reason: str = ""
    advice_at: Optional[int] = None
