"""
OpenChart Pro — FastAPI 主入口 (v3.0 Phase 1)

本文件只承载 Phase 1 + Phase 2 的基础路由：
  - 市场/品种/K 线
  - 指标
  - 自选列表
  - 设置
  - WebSocket

Phase 3A/4/5/6 的路由会在对应模块开发完成时以独立 router 方式注册到此处。

启动顺序 (TDD §11.1)：
  1. 初始化 SQLite 连接池 + 建表
  2. 加载 DB 覆盖配置到内存
  3. 加密 6 币种自动加入自选列表（PRD F1.8 首次使用引导）
  4. 加密 6 币种自动绑定全部内置策略（Phase 4 启用后生效）
  5. 启动 APScheduler（Phase 3A 新闻采集等）
  6. 启动 OKX WebSocket 订阅（Phase 1）
  7. 重置当日 LLM 成本计数器（Phase 3B）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import backend.config as config
from backend.data.cache import cached_get_klines
from backend.data.fetcher import get_fetcher
from backend.data.models import Interval, Market
from backend.db.database import DatabaseManager
from backend.indicators.registry import calculate_indicator, get_indicator_info, list_indicators
from backend.news.ai_analyzer import NewsAIAnalyzer
from backend.news.scheduler import NewsScheduler, attach_ai_analyzer
from backend.news.symbol_registry import registry as symbol_registry
from backend.portfolio.manager import PortfolioManager
from backend.portfolio.tracker import PortfolioTracker
from backend.signals.binding import StrategyBindingManager
from backend.signals.monitor import MonitorEngine
from backend.signals.strategies import list_strategies
from backend.trading.simulator import simulator as trading_simulator
from backend.watchpool.anomaly_scanner import AnomalyScanner
from backend.ws.hub import hub

# ═══════════════════════════════════════════════════════════════════
# 全局状态与日志
# ═══════════════════════════════════════════════════════════════════

logger = logging.getLogger("openchart")
logging.basicConfig(
    level=logging.INFO if not config.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# 全局数据库单例
db = DatabaseManager(config.DB_PATH, pool_size=5)

# 运行时配置快照
_runtime_config: Dict[str, Any] = {}

# 订阅中的 WebSocket 任务
_ws_subscriptions: Dict[tuple, asyncio.Task] = {}

# 新闻采集调度器（Phase 3A，启动时初始化）
news_scheduler: Optional[NewsScheduler] = None

# 策略监控引擎 (Phase 4, 启动时初始化)
monitor_engine: Optional[MonitorEngine] = None

# 持仓追踪器 (Phase 5, 启动时初始化)
portfolio_tracker: Optional[PortfolioTracker] = None
portfolio_manager: Optional[PortfolioManager] = None

# 异动扫描器 (Phase 3A 通道②)
anomaly_scanner: Optional[AnomalyScanner] = None

# LLM 新闻深度解读 (Phase 3B)
ai_analyzer: Optional[NewsAIAnalyzer] = None


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════


async def _load_runtime_config():
    """从 DB 加载配置值到内存，形成 runtime 视图（DB 覆盖 config.py 默认值）。"""
    global _runtime_config
    db_config = await db.get_all_config()

    # 先把 config.py 的默认值铺底
    defaults = {k: v for k, v in vars(config).items() if not k.startswith("_") and k.isupper()}
    _runtime_config = dict(defaults)

    # DB 值覆盖（JSON 序列化存储复杂类型）
    for key, raw_value in db_config.items():
        try:
            _runtime_config[key] = json.loads(raw_value)
        except (json.JSONDecodeError, TypeError):
            _runtime_config[key] = raw_value

    logger.info(f"Runtime config loaded: {len(_runtime_config)} keys")


def get_rt(key: str, default: Any = None) -> Any:
    """读取运行时配置值。"""
    return _runtime_config.get(key, default)


async def _ensure_crypto_watchlist():
    """加密 6 币种加入自选列表（PRD F1.8 首次使用引导）。"""
    existing = await db.get_watchlist(market="crypto")
    existing_symbols = {item["symbol"] for item in existing}

    for symbol in config.CRYPTO_SYMBOLS:
        if symbol not in existing_symbols:
            await db.add_to_watchlist(symbol=symbol, market="crypto", name=symbol)
            logger.info(f"Auto-added {symbol} to crypto watchlist")


def _get_pool_symbols() -> set:
    """同步获取候选池中所有股票符号（供 NewsScheduler 加分判定用）。
    简化实现：从 _runtime_config 取最新缓存（实际查询交给后台异步预热）。
    """
    return _runtime_config.get("_pool_symbols_cache", set())


async def _refresh_pool_symbols_cache():
    """每 5 分钟刷新候选池符号缓存。"""
    while True:
        try:
            items = await db.get_pool_items(status="monitoring", limit=200)
            _runtime_config["_pool_symbols_cache"] = {item["symbol"] for item in items}
        except Exception as e:
            logger.warning(f"刷新候选池缓存失败: {e}")
        await asyncio.sleep(300)


async def _refresh_symbol_registry_loop():
    """
    每 3 分钟刷新 SymbolRegistry 动态词典。
    用户 watchlist / 候选池 / 持仓中的品种会被自动加入识别表，
    新闻提到这些品种就能立即识别 → categories → 自动入候选池。
    """
    # 启动时立即跑一次
    try:
        await symbol_registry.refresh_from_db(db)
    except Exception as e:
        logger.warning(f"SymbolRegistry 首次刷新失败: {e}")
    while True:
        await asyncio.sleep(180)
        try:
            await symbol_registry.refresh_from_db(db)
        except Exception as e:
            logger.warning(f"SymbolRegistry 周期刷新失败: {e}")


def _parse_market(market_str: str) -> Market:
    """
    字符串到 Market 枚举。
    兼容前端历史命名：'a' → 'cn' （A 股的旧别名）
    """
    m = market_str.lower()
    if m == "a":  # 前端历史别名
        m = "cn"
    try:
        return Market(m)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid market: {market_str}")


def _parse_interval(interval_str: str) -> Interval:
    """字符串到 Interval 枚举。"""
    try:
        return Interval(interval_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid interval: {interval_str}")


# ═══════════════════════════════════════════════════════════════════
# FastAPI 应用 + 生命周期
# ═══════════════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动 + 关闭钩子。"""
    # ─── 启动 ───
    logger.info("OpenChart Pro starting...")

    # 1. 数据库
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    await db.init_db()
    logger.info(f"Database initialized: {config.DB_PATH}")

    # 2. 加载运行时配置
    await _load_runtime_config()

    # 3. 加密 6 币种首次使用引导
    await _ensure_crypto_watchlist()

    # 4. Phase 4 策略监控引擎 + Phase 5 持仓追踪器
    global monitor_engine, portfolio_tracker, portfolio_manager
    portfolio_manager = PortfolioManager(db)

    async def _news_for_symbol(symbol: str):
        """提供给 monitor / tracker 的新闻查询函数。"""
        try:
            items = await db.get_flash_news(importance_min=2, limit=20)
            return [n for n in items if symbol in (n.get("categories") or [])]
        except Exception:
            return []

    # MonitorEngine 的 news_provider 必须是同步函数（无 event loop），简化处理
    monitor_engine = MonitorEngine(db=db, ws_hub=hub, news_provider=lambda s: [])
    await monitor_engine.ensure_crypto_bindings()
    monitor_engine.start(check_interval_sec=60)
    logger.info("MonitorEngine started + crypto bindings ensured")

    portfolio_tracker = PortfolioTracker(db=db, ws_hub=hub, news_provider=lambda s: [])
    portfolio_tracker.start(check_interval_sec=300)
    logger.info("PortfolioTracker started")

    # 6.5 Phase 3A 通道②：异动扫描器（每 5 分钟拉数据源涨幅榜，仅交易时段）
    global anomaly_scanner
    anomaly_scanner = AnomalyScanner(db=db, ws_hub=hub)
    anomaly_scanner.start(check_interval_sec=300)
    logger.info("AnomalyScanner started")

    # 7. Phase 7 交易模拟器（默认开启 dry-run 模式）
    await trading_simulator.connect()

    # 5. Phase 3B LLM 深度解读引擎（先初始化以便注入到 NewsScheduler）
    global ai_analyzer
    ai_analyzer = NewsAIAnalyzer(db=db)
    attach_ai_analyzer(ai_analyzer)

    # 6. Phase 3A 新闻采集调度器
    global news_scheduler
    news_scheduler = NewsScheduler(
        db=db,
        ws_hub=hub,
        holding_provider=lambda: set(),
        pool_provider=_get_pool_symbols,
    )
    news_scheduler.start()
    asyncio.create_task(_refresh_pool_symbols_cache())
    asyncio.create_task(_refresh_symbol_registry_loop())
    logger.info(f"NewsScheduler + SymbolRegistry + AI Analyzer started (registry size={symbol_registry.size()})")

    # 6. Phase 3B LLM 成本计数器（占位）
    # TODO Phase 3B: reset_daily_llm_cost()

    logger.info("OpenChart Pro ready on http://%s:%d", config.HOST, config.PORT)

    yield  # ← 应用运行中

    # ─── 关闭 ───
    logger.info("OpenChart Pro shutting down...")
    if anomaly_scanner:
        await anomaly_scanner.stop()
    if portfolio_tracker:
        await portfolio_tracker.stop()
    if monitor_engine:
        await monitor_engine.stop()
    if news_scheduler:
        await news_scheduler.stop()
    await trading_simulator.close()
    # 取消所有 WebSocket 订阅
    for task in _ws_subscriptions.values():
        task.cancel()
    await db.close()


app = FastAPI(title="OpenChart Pro", version="3.0.0", lifespan=lifespan)

# CORS（开发环境允许所有，生产建议收紧）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════
# Pydantic 请求模型
# ═══════════════════════════════════════════════════════════════════


class WatchlistAddRequest(BaseModel):
    symbol: str
    market: str
    name: str = ""


class WatchlistReorderRequest(BaseModel):
    market: str
    symbols: List[str]  # 按新顺序排列的品种代码列表


class SettingsUpdateRequest(BaseModel):
    # 部分更新，任意字段都可选
    class Config:
        extra = "allow"


class IndicatorCalcRequest(BaseModel):
    symbol: str
    market: str
    interval: str
    indicators: List[Dict[str, Any]]  # [{"name": "MACD", "params": {...}}]
    limit: int = 500


# ═══════════════════════════════════════════════════════════════════
# 路由：市场 / 品种 / K线  (Phase 1)
# ═══════════════════════════════════════════════════════════════════

market_router = APIRouter(prefix="/api", tags=["市场"])


@market_router.get("/markets")
async def get_markets():
    """返回平台支持的四大市场及默认品种。"""
    return [
        {"id": "crypto", "name": "加密货币", "default_symbol": "BTC-USDT", "currency": "USDT"},
        {"id": "us", "name": "美股", "default_symbol": "AAPL", "currency": "USD"},
        {"id": "hk", "name": "港股", "default_symbol": "0700.HK", "currency": "HKD"},
        {"id": "cn", "name": "A股", "default_symbol": "600519", "currency": "CNY"},
    ]


@market_router.get("/symbols")
async def search_symbols(market: str = Query(...), q: str = Query("", alias="q")):
    """按市场搜索品种。支持代码和名称模糊匹配。"""
    m = _parse_market(market)
    fetcher = get_fetcher(m)
    symbols = await fetcher.get_symbols(query=q)
    return [
        {
            "symbol": s.symbol,
            "name": s.name,
            "market": s.market.value,
            "exchange": s.exchange,
            "base": s.base,
            "quote": s.quote,
        }
        for s in symbols
    ]


@market_router.get("/klines")
async def get_klines(
    symbol: str = Query(...),
    interval: str = Query("1H"),
    limit: int = Query(500, ge=1, le=2000),
    market: Optional[str] = Query(None),
    end_time: Optional[int] = Query(None, description="毫秒时间戳，用于往左分页加载历史"),
    before: Optional[int] = Query(None, description="别名，等同 end_time"),
):
    """
    获取 K 线数据。
    - market 可选，不传时按 symbol 推断（BTC-USDT → crypto，600519 → cn 等）
    - end_time 参数用于前端"往左拖动自动加载"(PRD F1.9)
      - 接受毫秒时间戳，返回该时间之前的最近 limit 根 K 线
      - before 是兼容别名
    """
    # 市场推断
    if market:
        m = _parse_market(market)
    else:
        m = _infer_market(symbol)

    i = _parse_interval(interval)
    end_time_ms = end_time or before  # 兼容两种命名
    # 走缓存层：优先命中 SQLite，未命中再调上游 Fetcher，上游失败时降级返回缓存
    candles = await cached_get_klines(
        db=db, market=m, symbol=symbol, interval=i, limit=limit, end_time_ms=end_time_ms
    )

    return {
        "symbol": symbol,
        "market": m.value,
        "interval": interval,
        "candles": [
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "turnover": c.turnover,
            }
            for c in candles
        ],
    }


def _infer_market(symbol: str) -> Market:
    """根据品种代码模式推断市场（兜底用）。"""
    s = symbol.upper()
    if "-USDT" in s or "-USD" in s or "-USDC" in s:
        return Market.CRYPTO
    if s.endswith(".HK"):
        return Market.HK
    if s.isdigit() and len(s) == 6:
        return Market.CN
    return Market.US


# ═══════════════════════════════════════════════════════════════════
# 路由：指标  (Phase 2)
# ═══════════════════════════════════════════════════════════════════

indicator_router = APIRouter(prefix="/api/indicators", tags=["指标"])


@indicator_router.get("")
async def get_indicator_list():
    """返回所有内置指标列表及其参数定义。"""
    return list_indicators()


@indicator_router.post("/calculate")
async def calculate_indicators(req: IndicatorCalcRequest):
    """批量计算指标。先拉 K 线，然后逐个计算请求的指标。"""
    m = _parse_market(req.market)
    i = _parse_interval(req.interval)

    fetcher = get_fetcher(m)
    candles = await fetcher.get_klines(symbol=req.symbol, interval=i, limit=req.limit)
    if not candles:
        raise HTTPException(status_code=404, detail="No kline data")

    # 构造 OHLCV numpy 数组
    ohlcv = {
        "open": np.array([c.open for c in candles], dtype=np.float64),
        "high": np.array([c.high for c in candles], dtype=np.float64),
        "low": np.array([c.low for c in candles], dtype=np.float64),
        "close": np.array([c.close for c in candles], dtype=np.float64),
        "volume": np.array([c.volume for c in candles], dtype=np.float64),
    }

    results: Dict[str, Any] = {}
    for item in req.indicators:
        name = item.get("name", "").upper()
        params = item.get("params", {})
        info = get_indicator_info(name)
        if not info:
            results[name] = {"error": f"Unknown indicator: {name}"}
            continue
        try:
            output = calculate_indicator(name, ohlcv, params)
            # numpy array -> list / dict values -> list
            if isinstance(output, dict):
                results[name] = {k: v.tolist() if hasattr(v, "tolist") else v for k, v in output.items()}
            elif hasattr(output, "tolist"):
                results[name] = output.tolist()
            else:
                results[name] = output
        except Exception as e:
            logger.exception(f"Indicator {name} failed")
            results[name] = {"error": str(e)}

    return results


# ═══════════════════════════════════════════════════════════════════
# 路由：自选列表  (Phase 2)
# ═══════════════════════════════════════════════════════════════════

watchlist_router = APIRouter(prefix="/api/watchlist", tags=["自选列表"])


@watchlist_router.get("")
async def get_watchlist(market: Optional[str] = Query(None)):
    """获取自选列表。可按市场过滤。"""
    return await db.get_watchlist(market=market)


@watchlist_router.post("")
async def add_watchlist(req: WatchlistAddRequest):
    """添加品种到自选列表。"""
    _parse_market(req.market)  # 校验市场合法
    item_id = await db.add_to_watchlist(symbol=req.symbol, market=req.market, name=req.name)
    return {"id": item_id, "added": True}


@watchlist_router.delete("/{symbol}")
async def remove_watchlist(symbol: str, market: str = Query(...)):
    """从自选列表删除品种。"""
    _parse_market(market)
    await db.remove_from_watchlist(symbol=symbol, market=market)
    return {"removed": True}


@watchlist_router.put("/reorder")
async def reorder_watchlist(req: WatchlistReorderRequest):
    """重新排序自选列表（按传入的 symbols 顺序）。"""
    _parse_market(req.market)
    items = [
        {"symbol": sym, "market": req.market, "sort_order": idx}
        for idx, sym in enumerate(req.symbols)
    ]
    await db.update_watchlist_order(items)
    return {"reordered": True}


# ═══════════════════════════════════════════════════════════════════
# 路由：设置  (Phase 1)
# ═══════════════════════════════════════════════════════════════════

settings_router = APIRouter(prefix="/api/settings", tags=["设置"])


# API Key 类字段在 GET 时脱敏
_SENSITIVE_KEYS = {
    "DEEPSEEK_API_KEY",
    "QWEN_API_KEY",
    "FINNHUB_API_KEY",
    "GLASSNODE_API_KEY",
    "CRYPTOQUANT_API_KEY",
    "OKX_API_KEY",
    "OKX_SECRET_KEY",
    "OKX_PASSPHRASE",
}


def _mask_secret(value: str) -> str:
    """脱敏 API Key：仅显示前 4 + 后 4 位，中间用 **** 替代。"""
    if not value or len(value) <= 8:
        return "****" if value else ""
    return f"{value[:4]}****{value[-4:]}"


@settings_router.get("")
async def get_settings():
    """获取当前运行时配置（敏感字段脱敏）。"""
    result = {}
    for key, value in _runtime_config.items():
        if key in _SENSITIVE_KEYS and isinstance(value, str):
            result[key.lower()] = _mask_secret(value)
        else:
            result[key.lower()] = value
    return result


@settings_router.put("")
async def update_settings(req: SettingsUpdateRequest):
    """
    部分更新配置。写入 DB 并热更新内存。
    敏感字段如果传入 "****" 脱敏值，视为未修改，保留原值。
    """
    updates = req.model_dump(exclude_unset=True)

    for key_lower, value in updates.items():
        key_upper = key_lower.upper()
        # 脱敏值不覆盖
        if key_upper in _SENSITIVE_KEYS and isinstance(value, str) and "****" in value:
            continue
        # 序列化复杂类型
        if isinstance(value, (dict, list, bool)):
            stored = json.dumps(value)
        else:
            stored = str(value)
        await db.set_config(key_upper, stored)
        _runtime_config[key_upper] = value

    return {"success": True, "updated": list(updates.keys())}


# ═══════════════════════════════════════════════════════════════════
# WebSocket 端点  (Phase 1)
# ═══════════════════════════════════════════════════════════════════


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket 双向通信端点。
    客户端消息格式（与前端 websocket.js 保持一致）：
      {"type": "subscribe" | "unsubscribe" | "switch", "symbol": "...", "interval": "..."}
    服务端推送消息类型见 TDD §8.2（kline/flash_news/signal/...）
    """
    await hub.handle_client(websocket)


# ═══════════════════════════════════════════════════════════════════
# 路由：新闻快讯  (Phase 3A)
# ═══════════════════════════════════════════════════════════════════

news_router = APIRouter(prefix="/api/news", tags=["新闻"])


@news_router.get("/flash")
async def list_flash_news(
    market: Optional[str] = Query(None),
    importance_min: int = Query(1, ge=1, le=5),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """新闻快讯列表，按发布时间倒序。"""
    items = await db.get_flash_news(
        market=market, importance_min=importance_min, limit=limit, offset=offset
    )
    return {"count": len(items), "items": items}


@news_router.get("/flash/{news_id}")
async def get_flash_news_detail(news_id: str):
    """单条新闻详情（含 AI 分析字段）。"""
    item = await db.get_flash_news_by_id(news_id)
    if not item:
        raise HTTPException(status_code=404, detail="News not found")
    return item


@news_router.get("/sources")
async def get_news_sources_health():
    """各采集源的健康度状态（监控用）。"""
    if not news_scheduler:
        return []
    return news_scheduler.get_health()


@news_router.post("/flash/{news_id}/analyze")
async def trigger_news_ai_analysis(news_id: str):
    """
    用户主动触发 LLM 深度解读（Phase 3B）。
    幂等：已有 ai_analysis 直接返回，否则同步调 LLM。
    """
    if not ai_analyzer:
        raise HTTPException(status_code=503, detail="AI 分析引擎未启动")
    item = await db.get_flash_news_by_id(news_id)
    if not item:
        raise HTTPException(status_code=404, detail="News not found")
    if item.get("ai_analysis"):
        return {"cached": True, "ai_analysis": item["ai_analysis"]}
    result = await ai_analyzer.deep_analyze_news(item)
    if result is None:
        raise HTTPException(
            status_code=503,
            detail="LLM 调用失败（API Key 未配置 / 网络异常 / 解析失败），请在设置中检查 LLM 配置",
        )
    return {"cached": False, "ai_analysis": result}


@news_router.get("/cost")
async def get_llm_cost_status():
    """今日 LLM 累计成本 + 预算状态（Phase 3B F5.11）。"""
    if not ai_analyzer:
        return {"status": "disabled", "today_cost_usd": 0, "daily_budget": config.LLM_DAILY_BUDGET}
    return await ai_analyzer.get_cost_status()


# ═══════════════════════════════════════════════════════════════════
# 路由：候选池  (Phase 3A) — 仅股票市场
# ═══════════════════════════════════════════════════════════════════

pool_router = APIRouter(prefix="/api/pool", tags=["候选池"])


class PoolAddRequest(BaseModel):
    symbol: str
    market: str
    reason: str = ""
    score: float = 50.0


@pool_router.get("")
async def list_pool_items(
    status: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """查询候选池条目，按评分降序。"""
    items = await db.get_pool_items(status=status, market=market, limit=limit)
    return {"count": len(items), "items": items}


@pool_router.post("")
async def add_to_pool(req: PoolAddRequest):
    """手动添加股票到候选池（加密品种会被 DB CHECK 约束拒绝）。"""
    if req.market == "crypto":
        raise HTTPException(
            status_code=400,
            detail="加密货币不使用候选池：6 币种已通过 OKX WebSocket 自动监控",
        )
    if req.market not in ("us", "hk", "cn"):
        raise HTTPException(status_code=400, detail=f"Invalid market: {req.market}")
    try:
        item_id = await db.add_to_pool(
            symbol=req.symbol, market=req.market, source="manual",
            score=req.score, reason=req.reason or "用户手动添加",
        )
        await hub.broadcast_pool_update("added", {
            "id": item_id, "symbol": req.symbol, "market": req.market,
            "source": "manual", "score": req.score,
        })
        return {"id": item_id, "added": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@pool_router.delete("/{item_id}")
async def remove_pool_item(item_id: str):
    """从候选池移除条目（硬删除）。"""
    ok = await db.remove_from_pool(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Pool item not found")
    await hub.broadcast_pool_update("removed", {"id": item_id})
    return {"removed": True}


# ═══════════════════════════════════════════════════════════════════
# 路由：策略信号  (Phase 4)
# ═══════════════════════════════════════════════════════════════════

signal_router = APIRouter(prefix="/api/signals", tags=["策略信号"])


@signal_router.get("")
async def list_signals(
    symbol: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    status: Optional[str] = Query("active"),
    limit: int = Query(100, ge=1, le=500),
):
    """信号列表，按生成时间倒序。"""
    async with db.acquire() as conn:
        sql = "SELECT * FROM signals WHERE 1=1"
        params: list = []
        if symbol:
            sql += " AND symbol = ?"; params.append(symbol)
        if market:
            sql += " AND market = ?"; params.append(market)
        if status:
            sql += " AND status = ?"; params.append(status)
        sql += " ORDER BY generated_at DESC LIMIT ?"; params.append(limit)
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        items = []
        for r in rows:
            d = dict(r)
            try:
                d["triggered_by"] = json.loads(d.get("triggered_by") or "{}")
            except json.JSONDecodeError:
                d["triggered_by"] = {}
            items.append(d)
        return {"count": len(items), "items": items}


@signal_router.get("/{signal_id}")
async def get_signal_detail(signal_id: str):
    async with db.acquire() as conn:
        cursor = await conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Signal not found")
        d = dict(row)
        try:
            d["triggered_by"] = json.loads(d.get("triggered_by") or "{}")
        except json.JSONDecodeError:
            d["triggered_by"] = {}
        return d


# ═══════════════════════════════════════════════════════════════════
# 路由：策略 + 绑定  (Phase 4)
# ═══════════════════════════════════════════════════════════════════

strategy_router = APIRouter(prefix="/api/strategies", tags=["策略"])


class StrategyBindRequest(BaseModel):
    symbol: str
    market: str
    strategy_name: str
    params: Optional[Dict[str, Any]] = None


class BatchBindRequest(BaseModel):
    strategy_name: str
    targets: List[Dict[str, str]]  # [{"symbol":..., "market":...}]
    params: Optional[Dict[str, Any]] = None


@strategy_router.get("")
async def get_strategy_list():
    """所有内置策略元信息。"""
    return list_strategies()


@strategy_router.get("/bindings")
async def list_strategy_bindings(
    symbol: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    strategy_name: Optional[str] = Query(None),
):
    """查询策略绑定。"""
    if not monitor_engine:
        return []
    return await monitor_engine.bindings.get_bindings(
        symbol=symbol, market=market, strategy_name=strategy_name, enabled_only=False
    )


@strategy_router.post("/bind")
async def bind_strategy(req: StrategyBindRequest):
    """单个绑定：一只品种 + 一个策略。"""
    if not monitor_engine:
        raise HTTPException(status_code=503, detail="MonitorEngine 未启动")
    binding_id = await monitor_engine.bindings.bind(
        symbol=req.symbol, market=req.market,
        strategy_name=req.strategy_name, params=req.params,
    )
    return {"id": binding_id, "bound": True}


@strategy_router.post("/batch-bind")
async def batch_bind_strategy(req: BatchBindRequest):
    """批量绑定：一个策略绑多只品种。"""
    if not monitor_engine:
        raise HTTPException(status_code=503, detail="MonitorEngine 未启动")
    return await monitor_engine.bindings.batch_bind(
        strategy_name=req.strategy_name, targets=req.targets, params=req.params,
    )


@strategy_router.delete("/bind")
async def unbind_strategy(
    symbol: str = Query(...), market: str = Query(...), strategy_name: str = Query(...)
):
    """解绑。"""
    if not monitor_engine:
        raise HTTPException(status_code=503, detail="MonitorEngine 未启动")
    ok = await monitor_engine.bindings.unbind(symbol, market, strategy_name)
    return {"unbound": ok}


# ═══════════════════════════════════════════════════════════════════
# 路由：持仓管理  (Phase 5)
# ═══════════════════════════════════════════════════════════════════

position_router = APIRouter(prefix="/api/positions", tags=["持仓"])


class PositionAddRequest(BaseModel):
    symbol: str
    market: str
    quantity: float
    avg_cost: float
    notes: str = ""


class PositionUpdateRequest(BaseModel):
    quantity: Optional[float] = None
    avg_cost: Optional[float] = None
    notes: Optional[str] = None


@position_router.get("")
async def list_positions():
    if not portfolio_manager:
        return []
    return await portfolio_manager.get_all()


@position_router.post("")
async def add_position(req: PositionAddRequest):
    if not portfolio_manager:
        raise HTTPException(status_code=503, detail="PortfolioManager 未启动")
    _parse_market(req.market)  # 校验
    pid = await portfolio_manager.add_position(
        symbol=req.symbol, market=req.market,
        quantity=req.quantity, avg_cost=req.avg_cost, notes=req.notes,
    )
    return {"id": pid, "added": True}


@position_router.put("/{position_id}")
async def update_position(position_id: str, req: PositionUpdateRequest):
    if not portfolio_manager:
        raise HTTPException(status_code=503, detail="PortfolioManager 未启动")
    await portfolio_manager.update_position(
        position_id, quantity=req.quantity, avg_cost=req.avg_cost, notes=req.notes,
    )
    return {"updated": True}


@position_router.delete("/{position_id}")
async def remove_position(position_id: str):
    if not portfolio_manager:
        raise HTTPException(status_code=503, detail="PortfolioManager 未启动")
    ok = await portfolio_manager.remove_position(position_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Position not found")
    return {"removed": True}


@position_router.get("/{position_id}/advices")
async def get_position_advices(position_id: str, limit: int = Query(50, ge=1, le=200)):
    if not portfolio_manager:
        return []
    return await portfolio_manager.get_advice_history(position_id, limit=limit)


# ═══════════════════════════════════════════════════════════════════
# 路由：加密仪表盘  (Phase 6, 复用 crypto_dashboard 模块)
# ═══════════════════════════════════════════════════════════════════

dashboard_router = APIRouter(prefix="/api/dashboard", tags=["加密仪表盘"])


@dashboard_router.get("/fear-greed")
async def get_fear_greed():
    """恐惧贪婪指数（数据源 Alternative.me，免费）。"""
    try:
        from backend.crypto_dashboard.sentiment import SentimentData
        sd = SentimentData()
        return await sd.get_fear_greed_index()
    except Exception as e:
        return {"error": str(e), "value": None, "label": "unknown"}


@dashboard_router.get("/funding-rate")
async def get_funding_rate(symbol: str = Query("BTC-USDT-SWAP")):
    try:
        from backend.crypto_dashboard.sentiment import SentimentData
        sd = SentimentData()
        return await sd.get_funding_rate(symbol)
    except Exception as e:
        return {"error": str(e)}


@dashboard_router.get("/open-interest")
async def get_open_interest(symbol: str = Query("BTC-USDT-SWAP")):
    try:
        from backend.crypto_dashboard.sentiment import SentimentData
        sd = SentimentData()
        return await sd.get_open_interest(symbol)
    except Exception as e:
        return {"error": str(e)}


@dashboard_router.get("/long-short-ratio")
async def get_long_short_ratio(coin: str = Query("BTC")):
    try:
        from backend.crypto_dashboard.sentiment import SentimentData
        sd = SentimentData()
        return await sd.get_long_short_ratio(coin)
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════
# 路由：交易模拟器  (Phase 7, 仅模拟下单)
# ═══════════════════════════════════════════════════════════════════

trading_router = APIRouter(prefix="/api/trading", tags=["交易"])


class SimOrderRequest(BaseModel):
    symbol: str
    side: str  # buy / sell
    quantity: float
    price: float = 0.0
    order_type: str = "market"


@trading_router.post("/simulate-order")
async def simulate_order(req: SimOrderRequest):
    """模拟下单（dry-run，不真实下单）。"""
    order = await trading_simulator.place_order(
        symbol=req.symbol, side=req.side,
        quantity=req.quantity, price=req.price,
        order_type=req.order_type,
    )
    return order


@trading_router.get("/sim-orders")
async def get_sim_orders(limit: int = Query(50, ge=1, le=500)):
    return await trading_simulator.list_orders(limit=limit)


@trading_router.get("/sim-positions")
async def get_sim_positions():
    return await trading_simulator.get_positions()


# 注册路由（必须在 StaticFiles 挂载之前）
app.include_router(market_router)
app.include_router(indicator_router)
app.include_router(watchlist_router)
app.include_router(settings_router)
app.include_router(news_router)
app.include_router(pool_router)
app.include_router(signal_router)
app.include_router(strategy_router)
app.include_router(position_router)
app.include_router(dashboard_router)
app.include_router(trading_router)


# ═══════════════════════════════════════════════════════════════════
# 健康检查 + 静态文件
# ═══════════════════════════════════════════════════════════════════


@app.get("/api/health")
async def health():
    """简单的健康检查端点，用于监控和一键启动验证。"""
    return {
        "status": "ok",
        "version": "3.0.0",
        "phase": "1",
        "time": int(time.time()),
    }


# 前端静态文件挂载 (必须在所有 API 路由之后！)
# PRD/TDD §12 约束 #12：mount("/") 必须在所有 router 注册之后
frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
else:
    logger.warning(f"Frontend directory not found: {frontend_dir}")
