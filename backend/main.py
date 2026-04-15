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

# 运行时配置快照（启动时从 DB 加载，PUT /api/settings 时热更新）
# 与 config.py 默认值合并：DB 值优先
_runtime_config: Dict[str, Any] = {}

# 订阅中的 WebSocket 任务，key = (market, symbol, interval) -> asyncio.Task
_ws_subscriptions: Dict[tuple, asyncio.Task] = {}


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

    # 4. Phase 4 策略自动绑定（占位，策略模块实装后填充）
    # TODO Phase 4: auto-bind built-in strategies to CRYPTO_SYMBOLS

    # 5. Phase 3A 采集调度器（占位）
    # TODO Phase 3A: scheduler.start()

    # 6. Phase 3B LLM 成本计数器（占位）
    # TODO Phase 3B: reset_daily_llm_cost()

    logger.info("OpenChart Pro ready on http://%s:%d", config.HOST, config.PORT)

    yield  # ← 应用运行中

    # ─── 关闭 ───
    logger.info("OpenChart Pro shutting down...")
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


# 注册路由（必须在 StaticFiles 挂载之前）
app.include_router(market_router)
app.include_router(indicator_router)
app.include_router(watchlist_router)
app.include_router(settings_router)


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
