# OpenChart Pro — 技术设计文档（TDD）

> **文档版本**：v3.1  
> **最后更新**：2026-04-14  
> **文档性质**：技术设计文档，定义"怎么做"，面向开发工程师  
> **配套文档**：《OpenChart Pro — PRD 产品需求文档 v3.0》  
> **本版对齐**：PRD 最终审计修复版（含 Phase 3A/3B 拆分、10 源新闻、评分公式量化、宏观影响阈值等）

---

## 目录

- [1. 技术栈与架构](#1-技术栈与架构)
- [2. 工程结构](#2-工程结构)
- [3. 全局配置](#3-全局配置)
- [4. 数据模型](#4-数据模型)
- [5. 数据库设计](#5-数据库设计)
- [6. 后端模块设计](#6-后端模块设计)
- [7. REST API 接口定义](#7-rest-api-接口定义)
- [8. WebSocket 协议](#8-websocket-协议)
- [9. 前端技术实现](#9-前端技术实现)
- [10. 依赖清单](#10-依赖清单)
- [11. 启动与初始化](#11-启动与初始化)
- [12. 实现约束](#12-实现约束)
- [13. 开发任务分解](#13-开发任务分解)

---

## 1. 技术栈与架构

### 1.1 技术栈

| 层级 | 技术选型 | 版本 | 选型理由 |
|------|---------|------|---------|
| 后端框架 | FastAPI | >=0.100 | 异步、类型友好、WebSocket 原生支持 |
| 实时通信 | WebSocket (FastAPI) | - | K 线/新闻/信号推送 |
| K 线图表 | KLineChart Pro | v10+ | 专业 K 线、UMD 引入简单 |
| 回测报告图表 | Chart.js | 4.x | 轻量通用图表 |
| 后端计算 | NumPy + Pandas | >=1.24 / >=2.0 | 指标计算向量化 |
| 回测引擎 | VectorBT | >=0.26 | 基础回测 |
| AI/LLM | DeepSeek / 通义千问 | - | OpenAI 兼容模式统一调用 |
| 任务调度 | APScheduler | >=3.10 | 新闻采集/评分/轮询定时任务 |
| 数据存储 | SQLite | 3.x | 轻量单文件，自托管友好 |
| HTTP 客户端 | aiohttp | >=3.9 | 异步请求数据源 |
| RSS 解析 | feedparser | >=6.0 | 新闻 RSS 采集 |
| 网页解析 | beautifulsoup4 | >=4.12 | 新闻爬取（巨潮/HKEX/ForexFactory） |
| 新闻去重 | simhash | >=2.1.2 | SimHash 内容相似度去重 |
| 前端样式 | 原生 CSS 暗色主题 | - | 无框架，轻量 |
| 包管理 | pip + npm | - | |

### 1.2 系统架构

```
┌────────────────────────────────────────────────────────────────────┐
│                   前端（Vanilla JS SPA）                            │
│  KLineChart Pro · Chart.js · 原生 CSS 暗色主题                     │
├────────────────────┬───────────────────────────────────────────────┤
│   REST API (HTTP)  │        WebSocket (双向实时通信)                │
├────────────────────┴───────────────────────────────────────────────┤
│                    FastAPI 后端服务 (:8888)                         │
│                                                                    │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────────┐  │
│  │ 数据源   │ │ 指标引擎  │ │ 新闻管线  │ │ 宏观影响分析器       │  │
│  │ (4 市场) │ │ (20+指标) │ │ (10 源)  │ │ (CPI/FOMC/NFP)     │  │
│  └────┬────┘ └──────────┘ └────┬─────┘ └──────────┬───────────┘  │
│       │                        │                   │              │
│  ┌────▼────┐ ┌──────────┐ ┌───▼───────┐ ┌────────▼─────────┐   │
│  │ 候选池   │ │ 策略信号  │ │ 持仓建议   │ │ 通知分发          │   │
│  │ (仅股票) │ │ 监控     │ │ 模块      │ │ (Toast/Webhook)  │   │
│  └─────────┘ └──────────┘ └──────────┘ └──────────────────┘    │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │         SQLite + APScheduler（定时任务）                      │  │
│  └─────────────────────────────────────────────────────────────┘  │
├────────────────────────────────────────────────────────────────────┤
│                       外部数据源 / API                              │
│  OKX WS · Binance · Yahoo Finance                                │
│  A 股聚合：东方财富 → 新浪财经 → 腾讯财经（自动降级）              │
│  10 个新闻源（RSS/REST/爬取）                                      │
│  DeepSeek / 通义千问 LLM API                                      │
└────────────────────────────────────────────────────────────────────┘
```

### 1.3 K 线缓存策略（Phase 1 增强）

所有市场的 K 线请求走 `data/cache.py` 包装层，行为：

| 场景 | 行为 |
|------|------|
| 懒加载历史（end_time_ms 非空） | 先查 SQLite，足够直接返回；不够时拉上游 + 存库 |
| 实时最新（end_time_ms=None） | 强制拉上游（最新 K 线可能未收盘），拉完后 upsert 入库 |
| 上游失败 | 降级返回 SQLite 已缓存的数据（即使略陈旧） |

好处：
- 大部分懒加载请求命中缓存（0 上游调用）
- 东方财富/Yahoo 限流时用户仍能看到历史 K 线
- 冷启动首批拉取后，后续同周期请求几乎全命中缓存

### 1.4 A 股多源自动降级（Phase 1 增强）

`data/cn_aggregator.py` 实现三级降级：

| 优先级 | 数据源 | 接口 | 备注 |
|-------|-------|-----|------|
| 主 | 东方财富（EastMoneyFetcher） | `push2his.eastmoney.com` | v1 默认主源 |
| 备 1 | 新浪财经（SinaCNFetcher） | `money.finance.sina.com.cn` | 限流时自动切 |
| 备 2 | 腾讯财经（TencentCNFetcher） | `ifzq.gtimg.cn` / `web.ifzq.gtimg.cn` | 最后一级降级 |

**健康度机制**：
- 每个源维护连续失败计数
- 连续失败 ≥ 3 次进入 60 秒冷却，期间不再尝试
- 请求按"可用优先"顺序试，命中即返回
- 冷却结束自动恢复尝试

### 1.3 后台任务调度

所有定时任务由 APScheduler 管理，随 FastAPI 启动：

| 任务 | 频率 | Phase | 说明 |
|------|------|-------|------|
| 新闻采集（10 源各自独立） | 1~30 分钟/源 | 3A | 各源按配置的间隔轮询 |
| 新闻规则引擎处理 | 新闻入库时触发 | 3A | 同步处理，<50ms/条 |
| LLM 深度分析 | ★★★★+ 新闻触发 | 3B | 异步，受日预算限制 |
| 候选池重评分 | 每 1 小时 | 3B | 全池重算 <30s |
| 候选池自动淘汰 | 每 1 小时 | 3B | 检查淘汰条件 |
| 异动排行拉取 | 盘中每 5 分钟 | 3B | 东方财富/Yahoo 排行榜 |
| 股票行情轮询 | 盘中每 3~10 秒 | 2 | 仅交易时段 |
| 持仓监控 | 每 5 分钟 | 5 | 检查新闻/指标变化 |
| 宏观数据采集 | ForexFactory 每 30 分钟 | 3B | 检查是否有新数据公布 |
| 加密 6 币种策略检查 | 每根 K 线收盘时 | 4 | 由 OKX WS 推送触发 |

---

## 2. 工程结构

```
openchart-pro/
├── backend/
│   ├── main.py                  # FastAPI 入口 + APScheduler 启动 + 初始化逻辑
│   ├── config.py                # 全局配置
│   ├── requirements.txt
│   │
│   ├── data/                    # 数据源模块 (Phase 1-2)
│   │   ├── __init__.py
│   │   ├── fetcher.py           # 统一接口（工厂模式）
│   │   ├── cache.py             # K 线缓存层（SQLite，所有市场共用）
│   │   ├── okx.py               # OKX WebSocket + REST（加密主力）
│   │   ├── binance.py           # Binance（加密备用降级）
│   │   ├── yahoo.py             # Yahoo Finance（美股/港股）
│   │   ├── cn_aggregator.py     # A 股聚合层（多源自动降级 + 健康度）
│   │   ├── eastmoney.py         # 东方财富（A 股主源，含异动排行）
│   │   ├── sina_cn.py           # 新浪财经（A 股备源 1）
│   │   ├── tencent_cn.py        # 腾讯财经（A 股备源 2）
│   │   └── models.py            # 核心数据模型
│   │
│   ├── indicators/              # 指标计算 (Phase 2)
│   │   ├── __init__.py
│   │   ├── builtin.py           # 20+ 内置指标（NumPy）
│   │   └── registry.py          # 指标注册表
│   │
│   ├── news/                    # 新闻管线 (Phase 3A + 3B)
│   │   ├── __init__.py
│   │   ├── collector.py         # 采集器基类 + 3 种适配器
│   │   ├── sources.py           # 10 个验证源配置
│   │   ├── dedup.py             # 去重（URL hash + SimHash）
│   │   ├── rule_engine.py       # 规则引擎（关键词 + 来源评分）
│   │   ├── impact_analyzer.py   # 宏观数据影响分析（CPI/FOMC/NFP）
│   │   ├── ai_analyzer.py       # LLM 深度解读（日预算 $5 硬上限）
│   │   └── scheduler.py         # 采集调度器
│   │
│   ├── watchpool/               # 候选池（仅股票）(Phase 3A + 3B)
│   │   ├── __init__.py
│   │   ├── manager.py           # CRUD + 状态机
│   │   ├── scorer.py            # 评分引擎（量化公式）
│   │   └── auto_maintain.py     # 自动推入 + 自动淘汰
│   │
│   ├── signals/                 # 策略信号 (Phase 4)
│   │   ├── __init__.py
│   │   ├── strategies.py        # 6 个内置策略（含置信度计算）
│   │   ├── monitor.py           # 监控引擎（加密固定 + 股票候选池）
│   │   ├── binding.py           # 策略绑定管理（多对多）
│   │   └── models.py            # Signal 数据结构
│   │
│   ├── portfolio/               # 持仓管理 (Phase 5)
│   │   ├── __init__.py
│   │   ├── manager.py           # 持仓 CRUD
│   │   ├── advisor.py           # 持仓建议引擎
│   │   └── tracker.py           # 持续监控
│   │
│   ├── alerts/                  # 通知分发
│   │   ├── __init__.py
│   │   └── notifiers.py         # Toast/Webhook/声音
│   │
│   ├── backtest/                # 基础回测 (Phase 5)
│   │   ├── __init__.py
│   │   ├── engine.py            # VectorBT 封装
│   │   └── report.py            # 报告生成
│   │
│   ├── crypto_dashboard/        # 加密仪表盘 (Phase 6)
│   │   ├── __init__.py
│   │   ├── onchain.py
│   │   ├── sentiment.py
│   │   └── calendar.py
│   │
│   ├── trading/                 # 交易预留 (Phase 7)
│   │   ├── __init__.py
│   │   ├── base.py              # BrokerAdapter 抽象接口
│   │   └── simulator.py         # 模拟下单
│   │
│   ├── ws/
│   │   └── hub.py               # WebSocket 推送中心
│   │
│   └── db/
│       └── database.py          # SQLite 管理
│
├── frontend/
│   ├── index.html               # 无右侧面板的简化布局
│   ├── css/app.css              # 暗色主题
│   ├── js/
│   │   ├── app.js               # 主逻辑 + 快捷键 + 首次引导
│   │   ├── chart.js             # KLineChart（含 loadMore 历史加载）
│   │   ├── indicators.js        # 指标面板
│   │   ├── news.js              # 新闻快讯面板（含宏观数据速报）
│   │   ├── watchpool.js         # 候选池面板（仅股票市场显示）
│   │   ├── signals.js           # 信号历史面板
│   │   ├── portfolio.js         # 持仓管理面板
│   │   ├── backtest.js          # 回测报告
│   │   ├── dashboard.js         # 加密仪表盘
│   │   ├── watchlist.js         # 自选列表（侧边栏，可折叠）
│   │   ├── search.js            # 搜索组件
│   │   ├── settings.js          # 设置对话框
│   │   ├── toast.js             # Toast 通知
│   │   └── websocket.js         # WebSocket 客户端
│   └── lib/
├── data/openchart.db
├── run.py
└── README.md
```

---

## 3. 全局配置

```python
# backend/config.py

# ═══ 服务器 ═══
HOST = "0.0.0.0"
PORT = 8888
DEBUG = True
DB_PATH = "./data/openchart.db"

# ═══ 加密货币（固定 6 币种，不使用候选池）═══
CRYPTO_SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT", "BNB-USDT", "XRP-USDT"]
CRYPTO_EXCHANGE = "okx"                # "okx" | "binance"

# OKX API
OKX_BASE_URL = "https://www.okx.com"
OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
# Binance（备用）
BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_WS = "wss://stream.binance.com:9443/ws"

# ═══ 股票数据源 ═══
YAHOO_POLL_INTERVAL = 10               # 秒
EASTMONEY_POLL_INTERVAL = 3            # 秒

# ═══ AI/LLM ═══
LLM_PROVIDER = "deepseek"             # "deepseek" | "qwen"
DEEPSEEK_API_KEY = ""
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"
QWEN_API_KEY = ""
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_MODEL = "qwen-turbo"
LLM_DAILY_BUDGET = 5.0                # 日预算硬上限（美元），超支停止 LLM 调用

# ═══ 新闻采集（v1 共 10 个验证源）═══
NEWS_DEDUP_WINDOW_HOURS = 24           # 去重窗口
NEWS_SIMHASH_THRESHOLD = 3            # SimHash 汉明距离阈值

# ═══ 第三方 API Key ═══
FINNHUB_API_KEY = ""
GLASSNODE_API_KEY = ""
CRYPTOQUANT_API_KEY = ""

# ═══ 候选池（仅股票市场使用）═══
WATCHPOOL_MAX_SIZE = 30
WATCHPOOL_MIN_SCORE = 40               # 最低评分阈值
WATCHPOOL_EXPIRE_NO_NEWS_DAYS = 30     # 30 天无新闻提及则淘汰
WATCHPOOL_EXPIRE_LOW_SCORE_DAYS = 14   # 连续 14 天低于阈值则淘汰
WATCHPOOL_RESCORE_INTERVAL = 3600      # 重评分间隔（秒）

# ═══ 信号 ═══
SIGNAL_MIN_CONFIDENCE = 60             # 最低置信度（0-100）
SIGNAL_DEDUP_WINDOW = 10              # 去重窗口（秒）

# ═══ 宏观数据（v1 仅 3 项）═══
MACRO_DEVIATION_NEUTRAL = 0.005        # <0.5% 判定为符合预期
MACRO_DEVIATION_LIGHT = 0.01           # 0.5%-1.0% 轻微偏离
# >=1.0% 明显偏离
MACRO_IMPACT_WINDOW_HOURS = 24         # 影响窗口（小时）

# ═══ 图表样式 ═══
CANDLE_COLOR_SCHEME = "international"
CANDLE_TYPE = "candle_solid"
SHOW_GRID = True
TIMEZONE = "Asia/Shanghai"

# ═══ 通知 ═══
WEBHOOK_URLS = []
ENABLE_BROWSER_NOTIFICATION = True
ENABLE_SOUND = True
SOUND_VOLUME = 70

# ═══ 回测 ═══
BACKTEST_INITIAL_CAPITAL = 100000
BACKTEST_COMMISSION_CRYPTO = 0.001
BACKTEST_COMMISSION_STOCK = 0.0003
BACKTEST_SLIPPAGE = 0.0005
```

---

## 4. 数据模型

```python
# backend/data/models.py

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum

class Market(Enum):
    CRYPTO = "crypto"
    US = "us"
    HK = "hk"
    CN = "cn"

class Interval(Enum):
    M1 = "1m"; M5 = "5m"; M15 = "15m"; M30 = "30m"
    H1 = "1H"; H4 = "4H"; D1 = "1D"; W1 = "1W"; MN = "1M"

@dataclass
class Candle:
    timestamp: int; open: float; high: float; low: float
    close: float; volume: float; turnover: float = 0.0

@dataclass
class Symbol:
    symbol: str; name: str; market: Market; exchange: str
    base: str = ""; quote: str = ""

# ═══ 新闻 ═══

@dataclass
class FlashNews:
    id: str
    title: str
    content: str
    source: str
    url: str
    published_at: int               # 源站发布时间（毫秒）
    collected_at: int               # 采集入库时间（毫秒）
    importance: int                  # 1-5 星
    sentiment: str                   # "bullish" | "bearish" | "neutral"
    categories: List[str]           # 关联品种代码
    impact_tags: List[str]          # 事件类型标签
    is_holding_related: bool
    l2_score: float
    # 加密影响（规则引擎产出）
    impact_on_crypto: Optional[List[Dict]] = None  # [{"symbol":"BTC-USDT","direction":"bullish","strength":4}]
    # 宏观数据字段（仅 is_macro_data=True 时有值）
    is_macro_data: bool = False
    macro_type: str = ""            # "CPI" | "FOMC" | "NFP"
    macro_actual: Optional[str] = None
    macro_forecast: Optional[str] = None
    macro_previous: Optional[str] = None
    macro_deviation_pct: Optional[float] = None
    macro_impact_strength: str = ""  # "neutral" | "light" | "strong"
    # LLM 深度解读（异步回填，Phase 3B）
    ai_analysis: Optional[Dict] = None

# ═══ 候选池（仅股票）═══

@dataclass
class WatchPoolItem:
    id: str
    symbol: str
    market: Market                   # 仅 us/hk/cn，不接受 crypto
    score: float                     # 0-100
    status: str                      # "candidate" | "monitoring" | "archived"
    source: str                      # "news" | "anomaly" | "macro_theme" | "manual"
    reason: str
    bound_strategies: List[str]      # 绑定的策略名（多对多）
    added_at: int
    last_scored_at: int
    last_news_mention_at: Optional[int] = None  # 最后一次新闻提及时间
    low_score_since: Optional[int] = None       # 开始连续低分的时间

# ═══ 策略绑定（多对多）═══

@dataclass
class StrategyBinding:
    id: str
    symbol: str
    market: Market
    strategy_name: str               # 策略名
    params: Dict = field(default_factory=dict)  # 策略参数（可选覆盖）
    enabled: bool = True
    created_at: int = 0

# ═══ 信号 ═══

@dataclass
class Signal:
    id: str
    symbol: str
    market: Market
    action: str                      # "buy" | "sell"
    strategy_name: str
    confidence: int                  # 0-100（见 PRD F7.5 公式）
    price: float
    suggested_qty: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    triggered_by: Dict = field(default_factory=dict)
    generated_at: int = 0

# ═══ 持仓 ═══

@dataclass
class Position:
    id: str
    symbol: str
    market: Market
    quantity: float
    avg_cost: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    latest_advice: str = ""          # "hold" | "reduce" | "add" | "close"
    advice_reason: str = ""
    advice_at: Optional[int] = None

# ═══ 回测 ═══

@dataclass
class BacktestResult:
    id: str; strategy_name: str; symbol: str; interval: str
    start_date: str; end_date: str
    summary: Dict[str, Any]
    equity_curve: List[Dict]; trades: List[Dict]; monthly_returns: Dict
```

---

## 5. 数据库设计

```sql
-- ═══ K 线历史（按市场+周期分表，按需创建）═══
CREATE TABLE klines_{market}_{interval} (
    symbol TEXT NOT NULL, timestamp INTEGER NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
    close REAL NOT NULL, volume REAL NOT NULL, turnover REAL DEFAULT 0,
    PRIMARY KEY (symbol, timestamp)
);

-- ═══ 自选列表 ═══
CREATE TABLE watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, market TEXT NOT NULL, name TEXT,
    sort_order INTEGER DEFAULT 0, added_at INTEGER NOT NULL,
    UNIQUE(symbol, market)
);

-- ═══ 新闻快讯 ═══
CREATE TABLE flash_news (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT,
    source TEXT NOT NULL,
    url TEXT,
    published_at INTEGER NOT NULL,   -- 源站发布时间
    collected_at INTEGER NOT NULL,   -- 采集入库时间
    importance INTEGER DEFAULT 1,
    sentiment TEXT DEFAULT 'neutral',
    categories TEXT DEFAULT '[]',    -- JSON
    impact_tags TEXT DEFAULT '[]',   -- JSON
    keywords TEXT DEFAULT '[]',      -- JSON
    is_holding_related INTEGER DEFAULT 0,
    l2_score REAL DEFAULT 0,
    impact_on_crypto TEXT,           -- JSON: 对加密 6 币种影响
    -- 宏观数据字段
    is_macro_data INTEGER DEFAULT 0,
    macro_type TEXT DEFAULT '',       -- CPI | FOMC | NFP
    macro_actual TEXT,
    macro_forecast TEXT,
    macro_previous TEXT,
    macro_deviation_pct REAL,        -- 偏差百分比
    macro_impact_strength TEXT DEFAULT '',  -- neutral | light | strong
    -- LLM 深度解读（异步回填）
    ai_analysis TEXT,
    -- 去重
    event_id TEXT,
    content_hash TEXT,
    simhash TEXT
);
CREATE INDEX idx_flash_time ON flash_news(published_at DESC);
CREATE INDEX idx_flash_importance ON flash_news(importance);

-- ═══ 候选池（仅股票）═══
CREATE TABLE watch_pool (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL CHECK(market IN ('us', 'hk', 'cn')),  -- 强制不接受 crypto
    score REAL DEFAULT 0,
    status TEXT DEFAULT 'candidate',
    source TEXT DEFAULT 'manual',
    reason TEXT DEFAULT '',
    added_at INTEGER NOT NULL,
    last_scored_at INTEGER,
    last_news_mention_at INTEGER,    -- 最后新闻提及时间
    low_score_since INTEGER,         -- 连续低分开始时间
    archived_at INTEGER,
    UNIQUE(symbol, market)
);

-- ═══ 策略绑定（多对多）═══
CREATE TABLE strategy_bindings (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    params TEXT DEFAULT '{}',        -- JSON: 策略参数
    enabled INTEGER DEFAULT 1,
    created_at INTEGER NOT NULL,
    UNIQUE(symbol, market, strategy_name)
);

-- ═══ 候选池评分历史 ═══
CREATE TABLE pool_score_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_item_id TEXT NOT NULL,
    score REAL NOT NULL,
    factors TEXT,                     -- JSON: 评分因子
    scored_at INTEGER NOT NULL
);

-- ═══ 策略信号 ═══
CREATE TABLE signals (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    action TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    confidence INTEGER DEFAULT 0,
    price REAL,
    suggested_qty REAL,
    stop_loss REAL,
    take_profit REAL,
    reason TEXT DEFAULT '',
    triggered_by TEXT DEFAULT '{}',
    status TEXT DEFAULT 'active',
    generated_at INTEGER NOT NULL,
    expires_at INTEGER
);
CREATE INDEX idx_signals_time ON signals(generated_at DESC);

-- ═══ 持仓 ═══
CREATE TABLE positions (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    quantity REAL NOT NULL,
    avg_cost REAL NOT NULL,
    opened_at INTEGER NOT NULL,
    notes TEXT DEFAULT '',
    UNIQUE(symbol, market)
);

-- ═══ 持仓建议历史 ═══
CREATE TABLE position_advices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    advice TEXT NOT NULL,
    reason TEXT NOT NULL,
    triggered_by TEXT DEFAULT '{}',
    advised_at INTEGER NOT NULL
);

-- ═══ LLM 调用成本追踪 ═══
CREATE TABLE llm_cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at INTEGER NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL NOT NULL,
    news_id TEXT                      -- 关联的新闻 ID
);

-- ═══ 回测报告 ═══
CREATE TABLE backtest_reports (
    id TEXT PRIMARY KEY,
    strategy_name TEXT NOT NULL,
    symbol TEXT NOT NULL, interval TEXT NOT NULL,
    start_date TEXT, end_date TEXT,
    config_json TEXT, result_json TEXT,
    created_at INTEGER NOT NULL
);

-- ═══ 用户配置 ═══
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

---

## 6. 后端模块设计

### 6.1 数据源模块 — data/

工厂模式分发，与之前版本相同。OKX（加密主力）/ Binance（备用）/ Yahoo Finance（美股/港股）/ 东方财富（A 股）。

**东方财富新增异动排行数据拉取**（Phase 3B 用于候选池推入）：

```python
# data/eastmoney.py 新增方法
class EastMoneyFetcher(DataFetcher):
    async def get_top_movers(self, market="cn", top_n=20) -> List[Dict]:
        """
        拉取东方财富已算好的排行榜数据（不做本地全市场计算）
        - 涨幅榜 Top N
        - 成交量排行 Top N
        - 资金净流入 Top N
        返回: [{"symbol": "600519", "name": "贵州茅台", "change_pct": 5.2,
                "volume_ratio": 3.1, "net_inflow": 52000000}]
        """
```

### 6.2 指标模块 — indicators/

与之前版本相同：20+ 内置指标（NumPy）+ 注册表。

**关键约束**：技术指标只对**自选列表 + 候选池 + 持仓品种 + 加密 6 币种**计算，不做全市场扫描。

### 6.3 新闻管线 — news/

#### 6.3.1 采集器 — collector.py

```python
class NewsCollector(ABC):
    source_name: str
    poll_interval: int

    @abstractmethod
    async def fetch(self) -> List[RawNews]: ...

class RSSCollector(NewsCollector): ...      # CoinDesk, Yahoo Finance, 金十 等
class RESTCollector(NewsCollector): ...     # OKX 公告, CryptoPanic, Finnhub, 东方财富
class ScraperCollector(NewsCollector): ...  # 巨潮, HKEX, ForexFactory
```

#### 6.3.2 数据源配置 — sources.py

**v1 首批 10 个验证源**（与 PRD F4 完全对齐）：

```python
NEWS_SOURCES = [
    {"name": "OKX公告",      "type": "rest",    "market": "crypto", "interval": 60},
    {"name": "CoinDesk",     "type": "rss",     "market": "crypto", "interval": 180},
    {"name": "CryptoPanic",  "type": "rest",    "market": "crypto", "interval": 120},
    {"name": "Yahoo Finance","type": "rss",     "market": "us",     "interval": 300},
    {"name": "Finnhub",      "type": "rest",    "market": "us",     "interval": 300},
    {"name": "东方财富7x24",  "type": "rest",    "market": "cn",     "interval": 120},
    {"name": "金十数据",      "type": "rss",     "market": "cn",     "interval": 180},
    {"name": "巨潮资讯",      "type": "scraper", "market": "cn",     "interval": 600},
    {"name": "HKEX披露易",    "type": "scraper", "market": "hk",     "interval": 600},
    {"name": "ForexFactory",  "type": "scraper", "market": "macro",  "interval": 1800},
]
```

#### 6.3.3 规则引擎 — rule_engine.py

```python
KEYWORD_WEIGHTS = {
    5: ["FOMC", "加息", "降息", "CPI", "非农", "黑客", "hack", "ETF批准", "退市", "破产"],
    3: ["财报", "earnings", "收购", "并购", "监管", "SEC", "证监会"],
    1: ["增持", "减持", "回购"],
    -2: ["推广", "广告", "赞助"],
}

SOURCE_TRUST = {
    "OKX公告": {"trust": 1.0, "bonus": 3},
    "巨潮资讯": {"trust": 1.0, "bonus": 3},
    "HKEX披露易": {"trust": 1.0, "bonus": 3},
    "CoinDesk": {"trust": 0.85, "bonus": 1},
    "东方财富7x24": {"trust": 0.85, "bonus": 1},
    "Finnhub": {"trust": 0.8, "bonus": 1},
    "CryptoPanic": {"trust": 0.7, "bonus": 0},
    "ForexFactory": {"trust": 0.9, "bonus": 2},
    # ...
}

def score_news(news, holdings, pool_symbols, crypto_symbols) -> FlashNews:
    """
    评分公式: score = (∑keyword_weight + source_bonus) × source_trust + holding_boost
    ★映射: ≥8→5星, 6-7→4星, 4-5→3星, 2-3→2星, <2→1星, <0→丢弃
    holding_boost: 涉及持仓/候选池/加密6币种 → +3

    同时产出:
    - categories: 匹配到的品种代码
    - sentiment: bullish/bearish/neutral（关键词模式判定）
    - impact_on_crypto: 对加密6币种的影响（如果新闻与加密相关）
    """
```

#### 6.3.4 宏观数据影响分析 — impact_analyzer.py

```python
import config

# v1 仅支持 3 项宏观数据
MACRO_IMPACT_MAP = {
    "CPI": {
        "above": {  # 实际 > 预期
            "crypto": "bearish", "tech": "bearish", "bank": "bullish", "consumer": "bearish"
        },
        "below": {
            "crypto": "bullish", "tech": "bullish", "bank": "bearish", "consumer": "bullish"
        },
    },
    "FOMC": {
        "surprise_hike": {"crypto": "strong_bearish", "tech": "strong_bearish", "bank": "bullish"},
        "surprise_cut": {"crypto": "strong_bullish", "tech": "strong_bullish", "bank": "bearish"},
        "expected": {"crypto": "neutral", "tech": "neutral", "bank": "neutral"},
    },
    "NFP": {
        "above": {"crypto": "bearish", "tech": "neutral", "bank": "bullish", "consumer": "bullish"},
        "below": {"crypto": "bullish", "tech": "neutral", "bank": "bearish", "consumer": "bearish"},
    },
}

SECTOR_MAP = {
    "NVDA": "tech", "AMD": "tech", "AAPL": "tech", "TSLA": "tech",
    "JPM": "bank", "GS": "bank",
    "600519": "consumer", "000858": "consumer",
    "0700.HK": "tech", "9988.HK": "tech",
    # 可扩展...
}

class MacroImpactAnalyzer:
    def analyze(self, macro_type: str, actual: float, forecast: float, previous: float) -> Dict:
        """
        1. 计算偏差: deviation = (actual - forecast) / forecast
        2. 判定强度:
           |deviation| < 0.005 → "neutral"（符合预期，不触发）
           0.005 ≤ |deviation| < 0.01 → "light"（轻微，仅通知）
           |deviation| ≥ 0.01 → "strong"（明显，触发信号）
        3. 查 MACRO_IMPACT_MAP → 加密 6 币种影响 + 持仓股票板块影响
        4. 影响窗口: 24 小时后失效
        """

    def get_impact_for_symbol(self, symbol: str, macro_type: str, direction: str) -> Dict:
        """查 SECTOR_MAP 获取品种所属板块，再查影响方向"""
```

#### 6.3.5 LLM 深度解读 — ai_analyzer.py

```python
class NewsAIAnalyzer:
    def __init__(self):
        self.today_cost = 0.0  # 当日累计成本

    async def can_call(self) -> bool:
        """检查日预算: today_cost < LLM_DAILY_BUDGET"""
        return self.today_cost < config.LLM_DAILY_BUDGET

    async def deep_analyze(self, news: FlashNews) -> Optional[Dict]:
        """
        仅 ★★★★+ 或用户点击时调用。
        调用前检查 can_call()，超预算则返回 None 并记录日志。
        每次调用后记录成本到 llm_cost_log 表。
        """

    async def generate_position_advice(self, position, recent_news, indicators) -> Dict:
        """为持仓品种生成操作建议"""
```

> LLM 统一用 `openai` 库 OpenAI 兼容模式。

### 6.4 候选池 — watchpool/（仅股票市场）

```python
class WatchPoolManager:
    async def add(self, symbol, market, score, source, reason):
        """添加品种。market 必须为 us/hk/cn，crypto 直接拒绝"""
        if market == "crypto":
            raise ValueError("加密货币不使用候选池")

class WatchPoolScorer:
    async def rescore_all(self):
        """
        每小时重算，公式（与 PRD F6.6 对齐）:
        score = news_heat×0.4 + momentum×0.3 + tech_signal×0.2 + anomaly_rank×0.1

        news_heat (0-100):
          = min(100, count_3star_24h × 20 + max_importance × 10)
          × time_decay(1h=1.0, 6h=0.7, 12h=0.5, 24h=0.3)

        momentum (0-100):
          = min(100, abs(change_24h_pct) × 10 + (volume/ma20_volume - 1) × 20)

        tech_signal (0-100):
          = 100 if high_confidence_signal, 50 if any_signal, 0 if none

        anomaly_rank (0-100):
          = 100 if in_top10_movers, 50 if in_top20, 0 if not
        """

class WatchPoolAutoMaintainer:
    async def auto_expire(self):
        """
        淘汰条件（与 PRD F6.5 对齐）:
        1. last_news_mention_at < now - 30天
        2. low_score_since 非空 且 now - low_score_since > 14天
        3. 淘汰后 7 日内若重新高分可自动恢复
        """

    async def auto_add_from_news(self, news: FlashNews):
        """★★★+ 新闻涉及的股票自动入池"""

    async def auto_add_from_rankings(self, rankings: List[Dict]):
        """异动排行榜 Top N 入池（东方财富/Yahoo 已算好的数据）"""

    async def auto_add_from_macro(self, macro_type, direction):
        """宏观数据发布后推荐受益板块龙头"""
```

### 6.5 策略信号 — signals/

#### 6.5.1 内置策略 — strategies.py

```python
class Strategy(ABC):
    name: str
    base_confidence: int  # 基础置信度分

    @abstractmethod
    def evaluate(self, symbol, candles, indicators, recent_news, macro_impact) -> Optional[Signal]: ...

    def calc_confidence(self, base, candles, indicators, recent_news) -> int:
        """
        通用置信度计算（与 PRD F7.5 对齐）:
        confidence = base
        + 10 if RSI in 30-70
        + 15 if price > MA200
        + 10 if volume > 1.5× MA20_volume
        - 10 if recent bearish news (★★★+)
        - 15 if macro bearish impact active
        return clamp(0, 100, confidence)
        """

class MACrossStrategy(Strategy):      # 基础分 55
class DonchianBreakout(Strategy):     # 基础分 50
class BollingerReversion(Strategy):   # 基础分 50
class RSIDivergence(Strategy):        # 基础分 50
class VolumeBreakout(Strategy):       # 基础分 55
class FlashEventStrategy(Strategy):   # 基础分 50 + importance×10
```

#### 6.5.2 策略绑定 — binding.py

```python
class StrategyBindingManager:
    async def bind(self, symbol, market, strategy_name, params=None):
        """绑定一只股票到一个策略"""

    async def batch_bind(self, symbols: List[str], market: str, strategy_name: str):
        """批量：一个策略绑定多只股票"""

    async def get_bindings(self, symbol=None, strategy_name=None) -> List[StrategyBinding]:
        """查询绑定关系"""

    async def unbind(self, symbol, market, strategy_name): ...
```

#### 6.5.3 监控引擎 — monitor.py

```python
class MonitorEngine:
    async def check_crypto(self):
        """
        加密 6 币种固定监控，系统启动时自动开始。
        默认绑定全部内置策略（用户可在设置中调整）。
        触发来源: OKX WebSocket 每根 K 线收盘时。
        """

    async def check_stocks(self):
        """
        股票: 遍历 strategy_bindings 表中 enabled=1 的绑定，
        对每个 (symbol, strategy) 组合运行策略评估。
        """

    async def on_macro_release(self, macro_type, actual, forecast, previous):
        """
        宏观数据公布事件:
        1. MacroImpactAnalyzer.analyze() → 影响判定
        2. 对加密 6 币种评估影响 → 可能触发信号
        3. 对持仓股票评估影响 → 可能触发建议
        4. 对候选池股票评估影响 → 可能触发信号
        5. WebSocket 推送宏观数据速报
        """
```

### 6.6 持仓管理 — portfolio/

与之前版本相同，增加宏观数据联动：

```python
class PositionAdvisor:
    async def check_all_positions(self):
        """
        每 5 分钟检查所有持仓。触发建议的条件:
        - 新闻利空 (★★★+ bearish 涉及持仓品种)
        - 宏观数据利空 (CPI/FOMC/NFP 对持仓品种所在板块利空)
        - 技术指标恶化 (RSI>80 超买, MACD 死叉等)
        - 链上异动 (加密: 巨鲸转入交易所)
        - 止损位接近

        同一建议 1 小时内不重复推送。
        """
```

### 6.7 其他模块

- **alerts/notifiers.py**：Toast + Webhook + 声音
- **backtest/**：VectorBT 基础回测（Phase 5）
- **crypto_dashboard/**：链上/情绪/日历（Phase 6）
- **trading/**：BrokerAdapter 抽象 + 模拟器（Phase 7）
- **ws/hub.py**：WebSocket 推送中心

---

## 7. REST API 接口定义

### 7.1 完整端点汇总

| 方法 | 路径 | Phase | 说明 |
|------|------|-------|------|
| GET | `/api/markets` | 1 | 市场列表 |
| GET | `/api/symbols` | 1 | 品种搜索 |
| GET | `/api/klines` | 1 | K 线数据（含 loadMore 分页） |
| GET | `/api/indicators` | 2 | 指标列表 |
| POST | `/api/indicators/calculate` | 2 | 指标计算 |
| GET | `/api/watchlist` | 2 | 自选列表 |
| POST | `/api/watchlist` | 2 | 添加自选 |
| DELETE | `/api/watchlist/{symbol}` | 2 | 删除自选 |
| PUT | `/api/watchlist/reorder` | 2 | 排序自选 |
| GET | `/api/news/flash` | 3A | 新闻列表（支持筛选星级/市场） |
| GET | `/api/news/flash/{id}` | 3A | 新闻详情 + AI 分析 |
| GET | `/api/news/sources` | 3A | 采集源健康状态 |
| GET | `/api/news/cost` | 3B | 今日 LLM 成本统计 |
| GET | `/api/pool` | 3A | 候选池列表 |
| POST | `/api/pool` | 3A | 手动添加候选（仅股票） |
| DELETE | `/api/pool/{id}` | 3A | 移出候选池 |
| POST | `/api/pool/{id}/bind` | 4 | 绑定策略 |
| DELETE | `/api/pool/{id}/bind/{strategy}` | 4 | 解绑策略 |
| POST | `/api/pool/batch-bind` | 4 | 批量绑定（一策略多股票） |
| GET | `/api/signals` | 4 | 信号列表 |
| GET | `/api/signals/{id}` | 4 | 信号详情 |
| GET | `/api/positions` | 5 | 持仓列表 |
| POST | `/api/positions` | 5 | 添加持仓 |
| PUT | `/api/positions/{id}` | 5 | 修改持仓 |
| DELETE | `/api/positions/{id}` | 5 | 删除持仓 |
| GET | `/api/positions/{id}/advices` | 5 | 持仓建议历史 |
| POST | `/api/backtest/run` | 5 | 运行回测 |
| GET | `/api/backtest/report/{id}` | 5 | 回测报告 |
| GET | `/api/dashboard/*` | 6 | 仪表盘系列 |
| GET | `/api/settings` | 1 | 获取配置 |
| PUT | `/api/settings` | 1 | 更新配置 |

### 7.2 关键接口示例

**批量绑定策略**：

```
POST /api/pool/batch-bind
Body: {
    "strategy_name": "ma_cross",
    "symbols": [
        {"symbol": "NVDA", "market": "us"},
        {"symbol": "AAPL", "market": "us"},
        {"symbol": "600519", "market": "cn"}
    ]
}
→ {"bound": 3, "failed": 0}
```

**LLM 成本查询**：

```
GET /api/news/cost
→ {"today_cost_usd": 3.2, "daily_budget": 5.0, "calls_today": 42,
   "budget_remaining_pct": 36, "status": "ok"}
```

---

## 8. WebSocket 协议

连接：`ws://{host}:{port}/ws`

### 8.1 客户端 → 服务器

```json
{"action": "subscribe", "symbol": "BTC-USDT", "interval": "1H"}
{"action": "unsubscribe", "symbol": "BTC-USDT"}
{"action": "switch", "symbol": "ETH-USDT", "interval": "4H"}
```

### 8.2 服务器 → 客户端（11 种消息类型）

```json
// 1. subscription_result
{"type": "subscription_result", "status": "ok", "symbol": "BTC-USDT"}

// 2. kline
{"type": "kline", "symbol": "BTC-USDT", "market": "crypto", "interval": "1H",
 "data": {"timestamp":..., "open":..., "high":..., "low":..., "close":..., "volume":...},
 "indicators": {"MA_20":..., "RSI_14":...}}

// 3. flash_news — 新闻快讯
{"type": "flash_news", "data": {"id":"...", "title":"...", "importance": 5,
 "sentiment": "bullish", "categories": ["BTC-USDT"], "source": "OKX公告",
 "impact_on_crypto": [{"symbol":"BTC-USDT","direction":"bullish","strength":4}]}}

// 4. macro_report — 宏观数据速报（新增，与 PRD §3.4 对齐）
{"type": "macro_report", "data": {
    "macro_type": "CPI", "actual": "3.1%", "forecast": "2.8%", "previous": "2.9%",
    "deviation_pct": 0.0107, "impact_strength": "strong",
    "impacts": {
        "crypto": {"direction": "bearish", "affected": ["BTC-USDT","ETH-USDT","SOL-USDT","DOGE-USDT","BNB-USDT","XRP-USDT"]},
        "tech": {"direction": "bearish"},
        "bank": {"direction": "bullish"}
    },
    "holding_impacts": [{"symbol":"NVDA","direction":"bearish","sector":"tech"}]
}}

// 5. pool_update — 候选池变动
{"type": "pool_update", "action": "added|removed|scored", "data": {...}}

// 6. signal — 策略信号
{"type": "signal", "data": {"id":"...", "symbol":"BTC-USDT", "action":"buy",
 "confidence": 82, "price": 68500, "stop_loss": 66000, "take_profit": 72000,
 "strategy_name": "donchian_breakout", "reason": "突破20日高点"}}

// 7. position_advice — 持仓建议
{"type": "position_advice", "data": {"symbol":"NVDA", "advice":"reduce",
 "reason":"CPI超预期+RSI超买", "urgency":"high"}}

// 8-9. backtest_progress / backtest_complete（保留）
// 10. dashboard_update（保留）
// 11. llm_budget_warning — LLM 预算告警
{"type": "llm_budget_warning", "data": {"today_cost": 4.2, "budget": 5.0, "pct": 84}}
```

---

## 9. 前端技术实现

### 9.1 布局实现（无右侧面板）

```html
<!-- 简化布局：侧边栏可折叠 + 主图表 + 底部标签页 -->
<div id="app">
  <header id="toolbar"><!-- 工具栏 44px --></header>
  <div id="main">
    <aside id="sidebar" class="collapsible"><!-- 160px 可折叠 --></aside>
    <section id="chart-area"><!-- 主图表 + 副图 --></section>
  </div>
  <div id="bottom-panel" class="resizable"><!-- 250px 可收起 --></div>
</div>
```

品种信息改为**鼠标悬停 Tooltip**（不再占固定面板空间）。

### 9.2 KLineChart 初始化（含历史自动加载）

```javascript
const chart = klinecharts.init('chart-container', { /* 暗色主题样式 */ });

// 历史自动加载（PRD F1.9）
chart.loadMore(async (timestamp) => {
    const moreData = await fetch(`/api/klines?symbol=${symbol}&interval=${interval}&before=${timestamp}&limit=200`);
    chart.applyMoreData(await moreData.json());
});
```

### 9.3 WebSocket 新增消息处理

```javascript
ws.on("flash_news", (msg) => addNewsToPanel(msg.data));
ws.on("macro_report", (msg) => showMacroImpactCard(msg.data));  // 宏观数据速报卡片
ws.on("pool_update", (msg) => updatePoolPanel(msg));
ws.on("signal", (msg) => { showSignalToast(msg.data); playSound(); });
ws.on("position_advice", (msg) => showAdviceToast(msg.data));
ws.on("llm_budget_warning", (msg) => showBudgetWarning(msg.data));
```

### 9.4 暗色主题

```css
:root {
    --bg-primary: #0D1117; --bg-secondary: #161B22; --bg-tertiary: #1C2333;
    --text-primary: #E6EDF3; --text-secondary: #7D8590;
    --color-up: #00C853; --color-down: #FF1744; --color-accent: #2196F3;
    --chart-grid: #1C2333; --chart-crosshair: #4B5563;
}
```

### 9.5 市场切换时候选池可见性

```javascript
function onMarketChange(market) {
    // 加密市场：隐藏候选池标签页，自选列表显示固定 6 币种
    document.getElementById('tab-watchpool').style.display = market === 'crypto' ? 'none' : 'block';
    // 加密市场：右键菜单隐藏"添加到候选池"
}
```

---

## 10. 依赖清单

```
# backend/requirements.txt
fastapi>=0.100.0
uvicorn[standard]>=0.23.0
websockets>=11.0
aiohttp>=3.9.0
numpy>=1.24.0
pandas>=2.0.0
vectorbt>=0.26.0
yfinance>=0.2.30
apscheduler>=3.10.0
aiosqlite>=0.19.0
pydantic>=2.0.0
python-dateutil>=2.8.0
httpx>=0.25.0
feedparser>=6.0.0
beautifulsoup4>=4.12.0
openai>=1.0.0
simhash>=2.1.2
```

---

## 11. 启动与初始化

```python
# run.py
import uvicorn, webbrowser, threading, time

def open_browser():
    time.sleep(2)
    webbrowser.open("http://localhost:8888")

if __name__ == "__main__":
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8888, reload=True, log_level="info")
```

### 11.1 main.py 启动逻辑

```python
@app.on_event("startup")
async def startup():
    # 1. 初始化数据库（建表）
    await init_database()

    # 2. 加载配置（DB 覆盖 config.py 默认值）
    await load_config_from_db()

    # 3. 加密 6 币种自动加载到自选列表
    for symbol in config.CRYPTO_SYMBOLS:
        await ensure_watchlist(symbol, "crypto")

    # 4. 加密 6 币种自动绑定全部内置策略
    for symbol in config.CRYPTO_SYMBOLS:
        for strategy in ALL_STRATEGIES:
            await ensure_strategy_binding(symbol, "crypto", strategy.name)

    # 5. 启动 APScheduler（新闻采集、候选池维护等定时任务）
    scheduler.start()

    # 6. 启动 OKX WebSocket 连接
    asyncio.create_task(start_okx_websocket())

    # 7. 重置当日 LLM 成本计数器
    reset_daily_llm_cost()
```

---

## 12. 实现约束

| # | 约束 | 说明 |
|---|------|------|
| 1 | OKX WS 心跳 | 每 25 秒 `"ping"` |
| 2 | 东方财富无认证 | 设置 User-Agent，合理频率避免封禁 |
| 3 | Yahoo Finance 限频 | 用缓存 |
| 4 | K 线分表 | 按 market + interval |
| 5 | 新闻采集频率 | 各源独立间隔（1-30 分钟），不高于配置值 |
| 6 | LLM 日预算 | $5 硬上限，超支停 LLM 但规则引擎不停 |
| 7 | 候选池拒绝 crypto | market CHECK 约束 + API 层校验双重保障 |
| 8 | 信号去重 | 同品种同方向 10 秒内不重复 |
| 9 | 持仓建议去重 | 同一建议 1 小时内不重复推送 |
| 10 | 宏观影响窗口 | 24 小时后失效，不再影响评分 |
| 11 | Symbol 统一 | 全平台 `BTC-USDT` 格式 |
| 12 | 静态文件挂载 | `app.mount("/")` 在所有路由之后 |
| 13 | LLM 统一调用 | `openai` 库兼容模式 |
| 14 | 技术指标范围 | 只对自选+候选池+持仓+加密6币种计算，不全市场扫描 |
| 15 | 新闻延迟诚实标注 | 前端每条新闻显示源站时间，不标"实时" |

**容错策略**：

| 场景 | 处理 |
|------|------|
| REST API 请求 | 10s 超时，3 次指数退避重试 |
| WebSocket 断线 | 无限重试（3s→30s 上限） |
| LLM API 失败 | 2 次重试（5s 间隔），仍失败则跳过 |
| LLM 超预算 | 当日停止 LLM 调用，规则引擎照常 |
| OKX 数据源 | 自动降级 Binance |
| 单个新闻源故障 | 跳过该源，其他源继续 |
| 全部新闻源故障 | 告警，候选池保持现有数据不清空 |
| SQLite 并发 | WAL 模式，单写多读 |

**交易时段**：

| 市场 | 时段（北京时间） |
|------|----------------|
| 加密 | 7×24 |
| A 股 | 工作日 9:15–11:30, 13:00–15:00 |
| 港股 | 工作日 9:30–12:00, 13:00–16:00 |
| 美股 | 工作日 21:30–04:00（夏）/ 22:30–05:00（冬） |

---

## 13. 开发任务分解

### Phase 1 — 核心基建

| # | 任务 | 文件 |
|---|------|------|
| 1 | 配置 + 数据库初始化 | `config.py`, `db/database.py` |
| 2 | FastAPI 框架 + 静态文件 + CORS | `main.py` |
| 3 | 数据模型 + 工厂 | `data/models.py`, `data/fetcher.py` |
| 4 | OKX 数据源 | `data/okx.py` |
| 5 | WebSocket 推送中心 | `ws/hub.py` |
| 6 | 页面布局（无右侧面板） + 暗色主题 | `index.html`, `app.css` |
| 7 | WebSocket 客户端 | `js/websocket.js` |
| 8 | KLineChart（含 loadMore） | `js/chart.js` |
| 9 | 主逻辑 + 快捷键 + 首次引导 | `js/app.js` |
| 10 | Toast 通知 | `js/toast.js` |
| 11 | 搜索组件 | `js/search.js` |
| 12 | 设置对话框 | `js/settings.js` |

### Phase 2 — 指标 + 多市场

| # | 任务 | 文件 |
|---|------|------|
| 13 | 20+ 内置指标 | `indicators/builtin.py`, `registry.py` |
| 14 | 指标选择面板 | `js/indicators.js` |
| 15 | 美股/港股数据 | `data/yahoo.py` |
| 16 | A 股数据 + 异动排行 | `data/eastmoney.py` |
| 17 | 自选列表（侧边栏可折叠） | `js/watchlist.js` |

### Phase 3A — 基础新闻 + 候选池框架

| # | 任务 | 文件 |
|---|------|------|
| 18 | 采集器框架 + 3 种适配器 | `news/collector.py` |
| 19 | 10 源配置 | `news/sources.py` |
| 20 | URL 去重 | `news/dedup.py`（URL hash 部分） |
| 21 | 规则引擎（关键词 + 来源评分） | `news/rule_engine.py` |
| 22 | 采集调度器 | `news/scheduler.py` |
| 23 | 候选池 CRUD + 状态机 | `watchpool/manager.py` |
| 24 | 新闻面板前端 | `js/news.js` |
| 25 | 候选池面板前端（仅股票市场显示） | `js/watchpool.js` |

### Phase 4 — 策略信号 + 加密监控

| # | 任务 | 文件 |
|---|------|------|
| 26 | 6 个内置策略（含置信度计算） | `signals/strategies.py` |
| 27 | 策略绑定管理（多对多 + 批量） | `signals/binding.py` |
| 28 | 监控引擎（加密固定 + 股票候选池） | `signals/monitor.py` |
| 29 | 加密 6 币种启动自动绑定 | `main.py` 启动逻辑 |
| 30 | 信号前端面板 | `js/signals.js` |

### Phase 3B — 新闻深度 + 宏观影响 + 候选池自动化

| # | 任务 | 文件 |
|---|------|------|
| 31 | SimHash 去重 | `news/dedup.py`（SimHash 部分） |
| 32 | LLM 深度解读 + 日预算控制 | `news/ai_analyzer.py` |
| 33 | 宏观数据影响分析 | `news/impact_analyzer.py` |
| 34 | 候选池评分引擎（量化公式） | `watchpool/scorer.py` |
| 35 | 候选池自动推入/淘汰 | `watchpool/auto_maintain.py` |
| 36 | 宏观数据速报 UI | `js/news.js` 扩展 |
| 37 | LLM 成本追踪 + 预算告警 | `llm_cost_log` 表 + API |

### Phase 5 — 持仓建议 + 回测

| # | 任务 | 文件 |
|---|------|------|
| 38 | 持仓 CRUD | `portfolio/manager.py` |
| 39 | 持仓建议引擎 | `portfolio/advisor.py` |
| 40 | 持仓监控（含宏观联动） | `portfolio/tracker.py` |
| 41 | 持仓前端面板 | `js/portfolio.js` |
| 42 | 基础回测 | `backtest/engine.py`, `report.py` |
| 43 | 回测前端 | `js/backtest.js` |

### Phase 6 — 加密仪表盘

| # | 任务 | 文件 |
|---|------|------|
| 44 | 链上 + 情绪 + 日历 | `crypto_dashboard/` |
| 45 | 仪表盘前端 | `js/dashboard.js` |

### Phase 7 — 自动交易（预留）

| # | 任务 | 文件 |
|---|------|------|
| 46 | BrokerAdapter 接口 + 模拟器 | `trading/base.py`, `simulator.py` |
| 47 | 风控预留 | `trading/risk.py` |
