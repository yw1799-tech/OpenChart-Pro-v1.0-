"""
OpenChart Pro — FastAPI 入口
所有 API 路由 + WebSocket + 静态文件服务
"""

import os
import json
import uuid
import time
import asyncio
import logging
import aiosqlite
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, APIRouter, Query, Body, Path, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import backend.config as config
from backend.ws.hub import hub

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic 请求/响应模型
# ---------------------------------------------------------------------------


class IndicatorCalcRequest(BaseModel):
    symbol: str
    interval: str
    indicators: List[Dict[str, Any]]  # [{"name": "MA", "params": {"period": 20}}]
    limit: int = 500


class FormulaRequest(BaseModel):
    code: str
    symbol: Optional[str] = None
    interval: Optional[str] = None


class AlertCreate(BaseModel):
    symbol: str
    market: str = "crypto"
    condition_type: str
    condition: Dict[str, Any]
    message: str = ""
    label: str = ""
    notify_methods: List[str] = ["browser", "sound"]
    repeat_mode: str = "once"
    cooldown: int = 300


class AlertUpdate(BaseModel):
    enabled: Optional[bool] = None
    condition: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    label: Optional[str] = None
    notify_methods: Optional[List[str]] = None
    repeat_mode: Optional[str] = None
    cooldown: Optional[int] = None


class BacktestRunRequest(BaseModel):
    symbol: str
    market: str = "crypto"
    interval: str = "1D"
    start_date: str = ""
    end_date: str = ""
    strategy_code: str = ""
    strategy_type: str = "openscript"
    config: Dict[str, Any] = {}


class BacktestOptimizeRequest(BaseModel):
    symbol: str
    market: str = "crypto"
    interval: str = "1D"
    start_date: str = ""
    end_date: str = ""
    strategy_code: str = ""
    strategy_type: str = "openscript"
    config: Dict[str, Any] = {}
    param_grid: Dict[str, Any] = {}


class ScreenerFilterRequest(BaseModel):
    markets: List[str] = ["crypto"]
    filters: List[Dict[str, Any]] = []
    sort_by: str = "change_pct"
    sort_order: str = "desc"
    limit: int = 50


class ScreenerAIRequest(BaseModel):
    market: str = "crypto"
    hours: int = 24
    min_score: int = 60


class ScreenerAIRecommendRequest(BaseModel):
    market: str = "crypto"


class ScreenerTechSignalsRequest(BaseModel):
    market: str = "crypto"
    signals: List[str] = ["macd_cross", "rsi_oversold", "volume_breakout"]
    symbols: List[str] = []  # 前端可传入指定品种列表


class WatchlistAddRequest(BaseModel):
    symbol: str
    market: str = "crypto"
    note: str = ""


class WatchlistUpdateRequest(BaseModel):
    note: Optional[str] = None
    sort_order: Optional[int] = None


class SettingsUpdate(BaseModel):
    settings: Dict[str, Any]


# ---------------------------------------------------------------------------
# 数据库辅助
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "openchart.db")


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    """初始化 SQLite 表结构"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT DEFAULT 'crypto',
                condition_type TEXT NOT NULL,
                condition TEXT NOT NULL,
                message TEXT DEFAULT '',
                label TEXT DEFAULT '',
                notify_methods TEXT DEFAULT '["browser","sound"]',
                repeat_mode TEXT DEFAULT 'once',
                cooldown INTEGER DEFAULT 300,
                enabled INTEGER DEFAULT 1,
                triggered_count INTEGER DEFAULT 0,
                last_triggered INTEGER,
                created_at INTEGER,
                updated_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS alert_history (
                id TEXT PRIMARY KEY,
                alert_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                message TEXT,
                triggered_at INTEGER,
                price REAL,
                details TEXT
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL UNIQUE,
                market TEXT DEFAULT 'crypto',
                note TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                created_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS backtest_reports (
                id TEXT PRIMARY KEY,
                strategy_name TEXT,
                symbol TEXT,
                interval TEXT,
                start_date TEXT,
                end_date TEXT,
                summary TEXT,
                equity_curve TEXT,
                benchmark_curve TEXT,
                drawdown_curve TEXT,
                trades TEXT,
                monthly_returns TEXT,
                optimization TEXT,
                created_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS formulas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                code TEXT NOT NULL,
                mode TEXT DEFAULT 'openscript',
                type TEXT DEFAULT 'indicator',
                created_at INTEGER NOT NULL,
                updated_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS news_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT,
                url TEXT,
                sentiment REAL,
                symbols TEXT,
                published_at INTEGER,
                analyzed_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS screener_tasks (
                task_id TEXT PRIMARY KEY,
                market TEXT NOT NULL,
                status TEXT DEFAULT 'collecting_news',
                progress TEXT,
                result_json TEXT,
                error TEXT,
                created_at INTEGER NOT NULL,
                completed_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        await db.commit()

        # 插入默认设置
        default_settings = {
            "candle_color_scheme": config.CANDLE_COLOR_SCHEME,
            "candle_type": config.CANDLE_TYPE,
            "show_grid": json.dumps(config.SHOW_GRID),
            "timezone": config.TIMEZONE,
            "crypto_exchange": config.CRYPTO_EXCHANGE,
            "enable_browser_notification": json.dumps(config.ENABLE_BROWSER_NOTIFICATION),
            "enable_sound": json.dumps(config.ENABLE_SOUND),
            "sound_volume": str(config.SOUND_VOLUME),
        }
        for k, v in default_settings.items():
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")
    yield


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(title="OpenChart Pro", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Router: Markets & Symbols & Klines
# ---------------------------------------------------------------------------
market_router = APIRouter(prefix="/api", tags=["市场数据"])

MARKETS = [
    {
        "id": "crypto",
        "name": "加密货币",
        "icon": "bitcoin",
        "description": "BTC, ETH 等主流加密货币",
        "default_symbol": "BTC-USDT",
        "currency": "USDT",
    },
    {
        "id": "us",
        "name": "美股",
        "icon": "dollar-sign",
        "description": "NYSE, NASDAQ 美国股票",
        "default_symbol": "AAPL",
        "currency": "USD",
    },
    {
        "id": "hk",
        "name": "港股",
        "icon": "landmark",
        "description": "HKEX 香港股票",
        "default_symbol": "0700.HK",
        "currency": "HKD",
    },
    {
        "id": "cn",
        "name": "A股",
        "icon": "bar-chart-2",
        "description": "SSE, SZSE 中国A股",
        "default_symbol": "600519",
        "currency": "CNY",
    },
]


@market_router.get("/markets")
async def get_markets():
    return {"markets": MARKETS}


@market_router.get("/symbols")
async def search_symbols(
    market: str = Query("crypto"),
    q: str = Query("", description="搜索关键词"),
):
    """搜索品种列表（从交易所API获取）"""
    from backend.data.models import Market as MktEnum

    try:
        mkt = MktEnum(market)
    except ValueError:
        raise HTTPException(400, f"Unknown market: {market}")

    try:
        from backend.data.fetcher import get_fetcher

        fetcher = get_fetcher(mkt)
        symbols = await fetcher.get_symbols(q)
        return {
            "symbols": [
                {
                    "symbol": s.symbol,
                    "name": s.name,
                    "market": s.market.value,
                    "exchange": s.exchange,
                    "base": s.base,
                    "quote": s.quote,
                }
                for s in symbols
                if s.market == mkt  # 只返回当前市场的品种
            ]
        }
    except Exception as e:
        logger.warning(f"get_symbols failed for {market}: {e}")
        return {"symbols": []}


def _guess_market(symbol: str, market_hint: str) -> str:
    """根据symbol格式自动推断市场，避免用OKX查美股代码"""
    if market_hint and market_hint != "crypto":
        return market_hint
    # 如果显式传了crypto且symbol含-，那就是crypto
    if "-" in symbol and not symbol.endswith(".HK"):
        return market_hint  # BTC-USDT 格式，保持crypto
    # 港股: 数字+.HK
    if symbol.upper().endswith(".HK"):
        return "hk"
    # A股: 纯6位数字
    if symbol.isdigit() and len(symbol) == 6:
        return "cn"
    # 港股: 纯4-5位数字（00700, 09988等）
    if symbol.isdigit() and len(symbol) in (4, 5):
        return "hk"
    # 美股: 纯字母
    if symbol.isalpha() and symbol.isupper() and len(symbol) <= 5:
        return "us"
    return market_hint


@market_router.get("/klines")
async def get_klines(
    symbol: str = Query(...),
    interval: str = Query("1H"),
    limit: int = Query(500, ge=1, le=5000),
    market: str = Query("crypto"),
    end_time: Optional[int] = Query(None, description="毫秒时间戳，返回此时间戳之前的K线（用于向左懒加载）"),
):
    """获取K线数据，支持 end_time 参数向历史分页"""
    market = _guess_market(symbol, market)

    from backend.data.models import Market as MktEnum, Interval as IntEnum

    try:
        mkt = MktEnum(market)
    except ValueError:
        raise HTTPException(400, f"Unknown market: {market}")

    try:
        iv = IntEnum(interval)
    except ValueError:
        raise HTTPException(400, f"Unknown interval: {interval}")

    try:
        from backend.data.fetcher import get_fetcher

        fetcher = get_fetcher(mkt)
        candles = await fetcher.get_klines(symbol, iv, limit, end_time_ms=end_time)
        return {
            "symbol": symbol,
            "market": market,
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
    except Exception as e:
        logger.error(f"get_klines error: {e}")
        raise HTTPException(502, f"Failed to fetch klines: {e}")


# ---------------------------------------------------------------------------
# Router: Indicators
# ---------------------------------------------------------------------------
indicator_router = APIRouter(prefix="/api/indicators", tags=["指标"])


@indicator_router.get("")
async def list_indicators():
    """从注册表返回全部指标元数据"""
    from backend.indicators.registry import INDICATOR_REGISTRY

    seen = set()
    indicators = []
    for ind_id, meta in INDICATOR_REGISTRY.items():
        if id(meta) in seen:
            continue
        seen.add(id(meta))
        indicators.append(
            {
                "name": ind_id,
                "label": meta["name"],
                "category": meta["category"],
                "overlay": meta["overlay"],
                "params": [{"key": p["name"], "type": p["type"], "default": p["default"]} for p in meta["params"]],
                "outputs": meta["outputs"],
            }
        )
    return {"indicators": indicators}


@indicator_router.post("/calculate")
async def calculate_indicators(req: IndicatorCalcRequest):
    """计算指标数据 — 使用注册表实际计算"""
    import numpy as np
    from backend.indicators.registry import calculate_indicator

    # 1) 获取K线数据
    from backend.data.models import Market as MktEnum, Interval as IntEnum
    from backend.data.fetcher import get_fetcher

    try:
        mkt = MktEnum(req.symbol.split("-")[-1] if "-" not in req.symbol else "crypto")
    except ValueError:
        mkt = MktEnum("crypto")

    try:
        iv = IntEnum(req.interval)
    except ValueError:
        raise HTTPException(400, f"Unknown interval: {req.interval}")

    try:
        fetcher = get_fetcher(mkt)
        candles = await fetcher.get_klines(req.symbol, iv, req.limit)
    except Exception as e:
        logger.error(f"获取K线数据失败: {e}")
        raise HTTPException(502, f"获取K线数据失败: {e}")

    if not candles:
        return {"symbol": req.symbol, "interval": req.interval, "results": {}}

    # 2) 构建 OHLCV 数据
    ohlcv_data = {
        "open": np.array([c.open for c in candles], dtype=np.float64),
        "high": np.array([c.high for c in candles], dtype=np.float64),
        "low": np.array([c.low for c in candles], dtype=np.float64),
        "close": np.array([c.close for c in candles], dtype=np.float64),
        "volume": np.array([c.volume for c in candles], dtype=np.float64),
    }
    timestamps = [c.timestamp for c in candles]

    # 3) 逐指标计算
    results = {}
    for ind in req.indicators:
        name = ind.get("name", "")
        params = ind.get("params", {})
        try:
            raw = calculate_indicator(name, ohlcv_data, params)
            # 统一转换为可JSON序列化格式
            if isinstance(raw, dict):
                converted = {}
                for k, v in raw.items():
                    arr = np.where(np.isnan(v), None, v)
                    converted[k] = [
                        {"t": timestamps[i], "v": float(arr[i]) if arr[i] is not None else None}
                        for i in range(len(arr))
                    ]
                results[name] = converted
            elif isinstance(raw, np.ndarray):
                arr = np.where(np.isnan(raw), None, raw)
                results[name] = [
                    {"t": timestamps[i], "v": float(arr[i]) if arr[i] is not None else None} for i in range(len(arr))
                ]
            else:
                results[name] = []
        except Exception as e:
            logger.warning(f"计算指标 {name} 失败: {e}")
            results[name] = {"error": str(e)}

    return {
        "symbol": req.symbol,
        "interval": req.interval,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Router: Formula（公式编辑器）
# ---------------------------------------------------------------------------
formula_router = APIRouter(prefix="/api/formula", tags=["公式编辑器"])


@formula_router.post("/validate")
async def validate_formula(req: FormulaRequest):
    """验证公式语法 — 使用 OpenScript 解析器"""
    from backend.indicators.formula.executor import validate_and_preview

    try:
        result = validate_and_preview(req.code)
        return {
            "valid": result["valid"],
            "errors": result["errors"],
            "warnings": [],
            "inputs": result.get("inputs", []),
            "meta": result.get("meta", {}),
        }
    except Exception as e:
        return {"valid": False, "errors": [str(e)], "warnings": []}


@formula_router.post("/execute")
async def execute_formula(req: FormulaRequest):
    """执行公式并返回结果 — 使用 OpenScript 执行器"""
    import numpy as np
    from backend.indicators.formula.executor import execute_openscript, ExecutionError

    # 获取K线数据
    ohlcv_data = {"open": [], "high": [], "low": [], "close": [], "volume": []}
    timestamps = []

    if req.symbol and req.interval:
        from backend.data.models import Market as MktEnum, Interval as IntEnum
        from backend.data.fetcher import get_fetcher

        try:
            mkt = MktEnum("crypto")
            iv = IntEnum(req.interval)
            fetcher = get_fetcher(mkt)
            candles = await fetcher.get_klines(req.symbol, iv, 500)
            if candles:
                ohlcv_data = {
                    "open": [c.open for c in candles],
                    "high": [c.high for c in candles],
                    "low": [c.low for c in candles],
                    "close": [c.close for c in candles],
                    "volume": [c.volume for c in candles],
                }
                timestamps = [c.timestamp for c in candles]
        except Exception as e:
            logger.warning(f"公式执行-获取K线失败: {e}")

    if not ohlcv_data["close"]:
        # 提供测试数据以便验证
        n = 100
        ohlcv_data = {
            "open": list(np.linspace(100, 110, n)),
            "high": list(np.linspace(101, 111, n)),
            "low": list(np.linspace(99, 109, n)),
            "close": list(np.linspace(100, 110, n)),
            "volume": list(np.linspace(1000, 2000, n)),
        }
        timestamps = list(range(n))

    def _sanitize_value(v):
        """将NaN/Inf等非JSON兼容值转为None"""
        if v is None:
            return None
        if isinstance(v, float):
            if np.isnan(v) or np.isinf(v):
                return None
            return v
        if isinstance(v, (np.floating, np.integer)):
            fv = float(v)
            if np.isnan(fv) or np.isinf(fv):
                return None
            return fv
        return v

    def _sanitize_list(lst):
        """递归清理列表中的非JSON值"""
        if isinstance(lst, (list, tuple)):
            return [_sanitize_value(v) if not isinstance(v, (list, tuple, dict)) else _sanitize_list(v) for v in lst]
        if isinstance(lst, dict):
            return {
                k: _sanitize_list(v) if isinstance(v, (list, tuple, dict)) else _sanitize_value(v)
                for k, v in lst.items()
            }
        return _sanitize_value(lst)

    try:
        result = execute_openscript(req.code, ohlcv_data, timeout=5.0)
        # 转换numpy数组为普通JSON兼容列表
        plots = []
        for p in result.get("plots", []):
            plot_item = {**p}
            if "data" in plot_item:
                raw_data = plot_item["data"]
                if hasattr(raw_data, "tolist"):
                    raw_data = raw_data.tolist()
                if isinstance(raw_data, list):
                    plot_item["data"] = [
                        {"t": timestamps[i] if i < len(timestamps) else i, "v": _sanitize_value(v)}
                        for i, v in enumerate(raw_data)
                    ]
            plots.append(_sanitize_list(plot_item))

        return {
            "result": plots,
            "drawings": _sanitize_list(result.get("drawings", [])),
            "shapes": _sanitize_list(result.get("shapes", [])),
            "alerts": _sanitize_list(result.get("alerts", [])),
            "meta": result.get("meta", {}),
            "logs": [],
        }
    except (ExecutionError, Exception) as e:
        return {"result": [], "logs": [f"执行错误: {str(e)}"], "error": str(e)}


# ---------------------------------------------------------------------------
# Router: Alerts
# ---------------------------------------------------------------------------
alert_router = APIRouter(prefix="/api/alerts", tags=["警报"])


@alert_router.get("")
async def list_alerts():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM alerts ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return {
            "alerts": [
                {
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "market": r["market"],
                    "condition_type": r["condition_type"],
                    "condition": json.loads(r["condition"]),
                    "message": r["message"],
                    "label": r["label"],
                    "notify_methods": json.loads(r["notify_methods"]),
                    "repeat_mode": r["repeat_mode"],
                    "cooldown": r["cooldown"],
                    "enabled": bool(r["enabled"]),
                    "triggered_count": r["triggered_count"],
                    "last_triggered": r["last_triggered"],
                }
                for r in rows
            ]
        }
    finally:
        await db.close()


@alert_router.post("")
async def create_alert(req: AlertCreate):
    alert_id = str(uuid.uuid4())
    now = int(time.time())
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO alerts (id, symbol, market, condition_type, condition, message,
               label, notify_methods, repeat_mode, cooldown, enabled, triggered_count,
               last_triggered, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,1,0,NULL,?,?)""",
            (
                alert_id,
                req.symbol,
                req.market,
                req.condition_type,
                json.dumps(req.condition),
                req.message,
                req.label,
                json.dumps(req.notify_methods),
                req.repeat_mode,
                req.cooldown,
                now,
                now,
            ),
        )
        await db.commit()
        return {"id": alert_id, "status": "created"}
    finally:
        await db.close()


@alert_router.put("/{alert_id}")
async def update_alert(alert_id: str, req: AlertUpdate):
    db = await get_db()
    try:
        fields = []
        values = []
        if req.enabled is not None:
            fields.append("enabled = ?")
            values.append(int(req.enabled))
        if req.condition is not None:
            fields.append("condition = ?")
            values.append(json.dumps(req.condition))
        if req.message is not None:
            fields.append("message = ?")
            values.append(req.message)
        if req.label is not None:
            fields.append("label = ?")
            values.append(req.label)
        if req.notify_methods is not None:
            fields.append("notify_methods = ?")
            values.append(json.dumps(req.notify_methods))
        if req.repeat_mode is not None:
            fields.append("repeat_mode = ?")
            values.append(req.repeat_mode)
        if req.cooldown is not None:
            fields.append("cooldown = ?")
            values.append(req.cooldown)

        if not fields:
            raise HTTPException(400, "No fields to update")

        fields.append("updated_at = ?")
        values.append(int(time.time()))
        values.append(alert_id)

        await db.execute(f"UPDATE alerts SET {', '.join(fields)} WHERE id = ?", values)
        await db.commit()
        return {"id": alert_id, "status": "updated"}
    finally:
        await db.close()


@alert_router.delete("/{alert_id}")
async def delete_alert(alert_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
        await db.commit()
        return {"id": alert_id, "status": "deleted"}
    finally:
        await db.close()


@alert_router.get("/history")
async def get_alert_history(
    symbol: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
):
    db = await get_db()
    try:
        if symbol:
            cursor = await db.execute(
                "SELECT * FROM alert_history WHERE symbol = ? ORDER BY triggered_at DESC LIMIT ?",
                (symbol, limit),
            )
        else:
            cursor = await db.execute("SELECT * FROM alert_history ORDER BY triggered_at DESC LIMIT ?", (limit,))
        rows = await cursor.fetchall()
        return {
            "history": [
                {
                    "id": r["id"],
                    "alert_id": r["alert_id"],
                    "symbol": r["symbol"],
                    "message": r["message"],
                    "triggered_at": r["triggered_at"],
                    "price": r["price"],
                    "details": json.loads(r["details"]) if r["details"] else None,
                }
                for r in rows
            ]
        }
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Router: Backtest
# ---------------------------------------------------------------------------
backtest_router = APIRouter(prefix="/api/backtest", tags=["回测"])


@backtest_router.post("/run")
async def run_backtest(req: BacktestRunRequest):
    """启动回测 — 调用 BacktestEngine"""
    from backend.backtest.engine import BacktestEngine

    backtest_id = str(uuid.uuid4())

    bt_config = req.config or {}
    engine = BacktestEngine(
        {
            "initial_capital": bt_config.get("initial_capital", 100000),
            "commission": bt_config.get("commission", 0.001),
            "slippage": bt_config.get("slippage", 0.0005),
        }
    )

    try:
        report = await engine.run(
            symbol=req.symbol,
            interval=req.interval,
            start_date=req.start_date,
            end_date=req.end_date,
            strategy_code=req.strategy_code,
            strategy_type=req.strategy_type,
        )

        # 保存到数据库
        now = int(time.time())
        db = await get_db()
        try:
            await db.execute(
                """INSERT INTO backtest_reports
                   (id, strategy_name, symbol, interval, start_date, end_date,
                    summary, equity_curve, benchmark_curve, drawdown_curve,
                    trades, monthly_returns, optimization, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    backtest_id,
                    req.strategy_code,
                    req.symbol,
                    req.interval,
                    req.start_date,
                    req.end_date,
                    json.dumps(report.get("summary", {})),
                    json.dumps(report.get("equity_curve", [])),
                    json.dumps(report.get("benchmark_curve", [])),
                    json.dumps(report.get("drawdown_curve", [])),
                    json.dumps(report.get("trades", [])),
                    json.dumps(report.get("monthly_returns", {})),
                    None,
                    now,
                ),
            )
            await db.commit()
        finally:
            await db.close()

        return {
            "id": backtest_id,
            "status": "completed",
            "report": report,
        }
    except Exception as e:
        logger.error(f"回测执行失败: {e}")
        return {
            "id": backtest_id,
            "status": "error",
            "message": f"回测执行失败: {str(e)}",
        }


@backtest_router.get("/report/{backtest_id}")
async def get_backtest_report(backtest_id: str):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM backtest_reports WHERE id = ?", (backtest_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Report not found")
        return {
            "id": row["id"],
            "strategy_name": row["strategy_name"],
            "symbol": row["symbol"],
            "interval": row["interval"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "summary": json.loads(row["summary"]) if row["summary"] else {},
            "equity_curve": json.loads(row["equity_curve"]) if row["equity_curve"] else [],
            "benchmark_curve": json.loads(row["benchmark_curve"]) if row["benchmark_curve"] else [],
            "drawdown_curve": json.loads(row["drawdown_curve"]) if row["drawdown_curve"] else [],
            "trades": json.loads(row["trades"]) if row["trades"] else [],
            "monthly_returns": json.loads(row["monthly_returns"]) if row["monthly_returns"] else {},
            "optimization": json.loads(row["optimization"]) if row["optimization"] else None,
        }
    finally:
        await db.close()


@backtest_router.post("/optimize")
async def run_optimization(req: BacktestOptimizeRequest):
    """参数优化 — 调用 BacktestEngine.optimize"""
    from backend.backtest.engine import BacktestEngine

    task_id = str(uuid.uuid4())

    engine = BacktestEngine()
    try:
        result = await engine.optimize(
            symbol=req.symbol,
            interval=req.interval,
            start_date=req.start_date,
            end_date=req.end_date,
            strategy_code=req.strategy_code,
            param_grid=req.param_grid,
        )
        return {"id": task_id, "status": "completed", "result": result}
    except Exception as e:
        logger.error(f"参数优化失败: {e}")
        return {"id": task_id, "status": "error", "message": f"优化失败: {str(e)}"}


# ---------------------------------------------------------------------------
# Router: Screener
# ---------------------------------------------------------------------------
screener_router = APIRouter(prefix="/api/screener", tags=["筛选器"])


@screener_router.post("/filter")
async def screener_filter(req: ScreenerFilterRequest):
    """规则筛选 — 调用 ScreenerEngine"""
    from backend.screener.rules import ScreenerEngine

    engine = ScreenerEngine()
    try:
        result = await engine.screen(
            markets=req.markets,
            filters=req.filters,
            sort_by=req.sort_by,
            sort_order=req.sort_order,
            limit=req.limit,
        )
        return {
            "count": result.get("count", 0),
            "results": result.get("results", []),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"筛选执行失败: {e}")
        return {"count": 0, "results": [], "error": str(e)}


# AI分析任务缓存
_ai_tasks: Dict[str, Dict] = {}


@screener_router.post("/ai-analyze")
async def screener_ai_analyze(req: ScreenerAIRequest):
    """AI 分析 — 调用 AIAnalyzer"""
    task_id = str(uuid.uuid4())
    _ai_tasks[task_id] = {"status": "running", "result": None}

    async def _run_analysis():
        try:
            _ai_tasks[task_id]["status"] = "collecting_news"
            _ai_tasks[task_id]["progress"] = "正在采集新闻..."
            # 从数据库settings表读取LLM配置
            from backend.screener.ai_analyzer import AIAnalyzer

            llm_settings = {}
            try:
                db = await get_db()
                cursor = await db.execute(
                    "SELECT key, value FROM settings WHERE key LIKE 'deepseek_%' OR key LIKE 'qwen_%' OR key = 'llm_provider'"
                )
                rows = await cursor.fetchall()
                for row in rows:
                    llm_settings[row[0]] = row[1].strip('"') if row[1].startswith('"') else row[1]
                await db.close()
            except Exception:
                pass

            provider = llm_settings.get("llm_provider", "deepseek")
            if provider == "qwen":
                api_key = llm_settings.get("qwen_api_key", "")
                base_url = llm_settings.get("qwen_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
                model = llm_settings.get("qwen_model", "qwen-turbo")
            else:
                api_key = llm_settings.get("deepseek_api_key", "")
                base_url = llm_settings.get("deepseek_base_url", "https://api.deepseek.com")
                model = llm_settings.get("deepseek_model", "deepseek-chat")

            if not api_key:
                _ai_tasks[task_id] = {"status": "error", "result": None, "error": "LLM API Key 未配置，请在设置中填入"}
                return

            analyzer = AIAnalyzer(base_url=base_url, api_key=api_key, model=model)
            _ai_tasks[task_id]["status"] = "analyzing"
            _ai_tasks[task_id]["progress"] = "AI正在分析..."
            recommendations = await analyzer.get_recommendations(
                market=req.market,
                hours=req.hours,
                min_score=req.min_score,
            )
            _ai_tasks[task_id] = {
                "status": "done",
                "progress": "分析完成",
                "result": recommendations,
            }
        except Exception as e:
            logger.error(f"AI分析失败: {e}")
            _ai_tasks[task_id] = {
                "status": "error",
                "result": None,
                "error": str(e),
            }

    # 后台运行
    asyncio.create_task(_run_analysis())
    return {"task_id": task_id, "status": "pending"}


@screener_router.get("/ai-status/{task_id}")
async def screener_ai_status(task_id: str):
    """查询 AI 分析状态"""
    task = _ai_tasks.get(task_id)
    if task is None:
        return {"task_id": task_id, "status": "not_found", "result": None}
    return {"task_id": task_id, **task}


# ---- AI 驱动推荐 (新版选股) ----

# 缓存：避免频繁调用 LLM
_ai_recommend_cache: Dict[str, Dict] = {}  # {market: {data, timestamp}}


@screener_router.post("/ai-recommend")
async def screener_ai_recommend(req: ScreenerAIRecommendRequest):
    """
    AI 驱动推荐 — 根据新闻/政策自动推荐品种。
    如果有 LLM Key 则调用 AI 分析；否则返回基于新闻关键词的基础推荐。
    结果缓存 5 分钟。
    """
    import time as _time

    market = req.market

    # 检查缓存
    cached = _ai_recommend_cache.get(market)
    if cached and (_time.time() - cached.get("timestamp", 0)) < 1800:  # 30分钟缓存
        return cached["data"]

    try:
        # 读取 LLM 配置
        llm_settings = {}
        try:
            db = await get_db()
            cursor = await db.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'deepseek_%' OR key LIKE 'qwen_%' OR key = 'llm_provider'"
            )
            rows = await cursor.fetchall()
            for row in rows:
                llm_settings[row[0]] = row[1].strip('"') if row[1].startswith('"') else row[1]
            await db.close()
        except Exception:
            pass

        provider = llm_settings.get("llm_provider", "deepseek")
        if provider == "qwen":
            api_key = llm_settings.get("qwen_api_key", "")
            base_url = llm_settings.get("qwen_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
            model = llm_settings.get("qwen_model", "qwen-turbo")
        else:
            api_key = llm_settings.get("deepseek_api_key", "")
            base_url = llm_settings.get("deepseek_base_url", "https://api.deepseek.com")
            model = llm_settings.get("deepseek_model", "deepseek-chat")

        has_llm = bool(api_key and len(api_key) > 5)

        # 1. 采集新闻（根据市场类型选择新闻源）
        news_items = []
        try:
            from backend.screener.news import NewsCollector

            collector = NewsCollector()
            news_items = await collector.fetch_all(market=market)
            await collector.close()
        except Exception as e:
            logger.warning(f"新闻采集失败: {e}")

        # 策略：先用默认热门品种，获取实时价格，然后用AI给推荐理由和评分
        recommendations = _get_default_hot_symbols(market)
        source = "热门品种推荐"

        # 并行获取实时价格（asyncio.gather加速）
        from backend.data.models import Market as MktEnum, Interval
        from backend.data.fetcher import get_fetcher

        market_enum = (
            MktEnum.CRYPTO
            if market == "crypto"
            else MktEnum.US
            if market == "us"
            else MktEnum.HK
            if market == "hk"
            else MktEnum.CN
        )
        fetcher = get_fetcher(market_enum)

        async def _fetch_price(symbol, market, fetcher):
            try:
                fetch_sym = symbol
                if market == "hk" and not symbol.endswith(".HK"):
                    code = symbol.lstrip("0") or "0"
                    fetch_sym = code.zfill(4) + ".HK"
                klines = await fetcher.get_klines(fetch_sym, Interval.D1, limit=2)
                if klines and len(klines) >= 2:
                    chg = (
                        round((klines[-1].close - klines[-2].close) / klines[-2].close * 100, 2)
                        if klines[-2].close
                        else 0
                    )
                    return symbol, round(klines[-1].close, 2), chg
                elif klines and len(klines) == 1:
                    return symbol, round(klines[-1].close, 2), 0
            except Exception:
                pass
            return symbol, None, 0

        # 分批获取价格（每批5个，间隔0.5秒，避免被限流）
        price_map = {}
        batch_size = 5
        for i in range(0, len(recommendations), batch_size):
            batch = recommendations[i : i + batch_size]
            tasks = [_fetch_price(rec["symbol"], market, fetcher) for rec in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    continue
                sym, price, chg = result
                price_map[sym] = (price, chg)
            if i + batch_size < len(recommendations):
                await asyncio.sleep(0.5)

        for rec in recommendations:
            price, chg = price_map.get(rec["symbol"], (None, 0))
            if price is not None:
                rec["price"] = price
                rec["change_pct"] = chg

        # 如果有LLM Key + 有新闻 → AI做真正的初筛
        if has_llm and news_items:
            try:
                from openai import AsyncOpenAI

                client = AsyncOpenAI(api_key=api_key, base_url=base_url)

                market_names = {"crypto": "加密货币", "us": "美股", "hk": "港股", "cn": "A股"}
                market_name = market_names.get(market, market)
                symbols_str = ", ".join([f"{r['symbol']}({r['name']})" for r in recommendations])
                news_str = "\n".join([f"- {n.get('title', '')}" for n in news_items[:25]])

                # 获取价格信息
                price_info = []
                for r in recommendations:
                    if r.get("price"):
                        price_info.append(f"{r['symbol']}({r['name']}): 现价{r['price']}, 涨跌{r['change_pct']}%")
                price_str = "\n".join(price_info) if price_info else "暂无价格数据"

                prompt = f"""你是专业的金融分析师和交易顾问。请根据以下信息，从{market_name}市场中筛选出**当前最值得关注和可能有操作机会**的品种。

## 你的任务
你不是推荐热门股，而是帮用户做**第一轮初筛**——从候选品种中找出**当前有操作机会、值得进一步研究**的标的。
用户只想看到"可能可以买入"或"值得密切关注"的品种，**不要推荐需要回避的品种**。

筛选标准（按优先级）：
1. 政策利好直接受益的板块龙头
2. 近期有正面催化事件（财报超预期、产品发布、行业政策等）
3. 技术面出现买入信号（超跌反弹、突破关键位、放量上攻等）
4. 估值合理或被低估，有安全边际

**排除标准**：面临重大利空、监管风险、业绩暴雷、高位滞涨的品种不要推荐。

## 候选品种池（含当前价格和涨跌）
{price_str}

## 最新市场新闻
{news_str}

## 输出要求
从候选品种中筛选出3-6个最有操作价值的，按推荐优先级排序。
返回JSON数组，每个元素：
- symbol: 品种代码
- score: 0-100综合评分（80+强烈推荐，60-79值得关注，<60观望）
- action: 只能是"buy"(建议买入)或"watch"(关注等待机会)，不推荐就不要列出
- reason: 推荐/回避理由（结合具体新闻和盘面，30字以内）
- hot_topic: 核心催化因素（如"AI政策利好"、"财报超预期"等，8字以内）
- risk: 主要风险（10字以内）

只返回JSON数组，不要其他文字。"""

                resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=1500,
                        temperature=0.3,
                    ),
                    timeout=20,
                )
                import json as _json

                content = resp.choices[0].message.content.strip()
                if "```" in content:
                    content = content.split("```")[1].replace("json", "").strip()
                ai_results = _json.loads(content)

                # 用AI结果更新推荐，并过滤只保留AI选出的
                ai_map = {r["symbol"]: r for r in ai_results}
                rec_map = {r["symbol"]: r for r in recommendations}

                filtered = []
                for ai_rec in ai_results:
                    sym = ai_rec.get("symbol", "")
                    base = rec_map.get(sym, {})
                    filtered.append(
                        {
                            "symbol": sym,
                            "name": base.get("name", ""),
                            "market": market,
                            "price": base.get("price"),
                            "change_pct": base.get("change_pct", 0),
                            "score": ai_rec.get("score", 50),
                            "hot_topic": ai_rec.get("hot_topic", ""),
                            "reason": ai_rec.get("reason", ""),
                            "action": ai_rec.get("action", "watch"),
                            "risk": ai_rec.get("risk", ""),
                            "signals": [],
                        }
                    )

                if filtered:
                    # 过滤掉action=avoid/sell的，只保留有操作价值的
                    recommendations = [r for r in filtered if r.get("action") not in ("avoid", "sell")]
                    if not recommendations:
                        recommendations = filtered  # 如果全被过滤了，保留原始结果
                    source = f"{provider.upper()} AI 初筛"
                else:
                    # AI没返回有效结果，用默认排序
                    recommendations.sort(key=lambda x: abs(x.get("change_pct", 0)), reverse=True)
                    source = f"{provider.upper()} AI 分析"
            except Exception as e:
                logger.warning(f"AI初筛失败(降级为基础推荐): {e}")

        # 清理NaN/None值，防止JSON序列化错误
        import math

        for rec in recommendations:
            for k in ("price", "change_pct", "score"):
                v = rec.get(k)
                if v is not None and isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    rec[k] = 0
            if rec.get("price") is None:
                rec["price"] = 0
            if rec.get("change_pct") is None:
                rec["change_pct"] = 0

        result = {
            "recommendations": recommendations[:12],
            "updated_at": datetime.now().isoformat(),
            "source": source,
        }

        _ai_recommend_cache[market] = {"data": result, "timestamp": _time.time()}
        return result

    except Exception as e:
        import traceback

        print(f"[AI推荐] 异常: {e}")
        traceback.print_exc()
        fallback = _get_default_hot_symbols(market)
        return {
            "recommendations": fallback,
            "updated_at": datetime.now().isoformat(),
            "source": "默认热门品种",
        }


@screener_router.post("/tech-signals")
async def screener_tech_signals(req: ScreenerTechSignalsRequest):
    """
    技术面信号扫描 — 自动扫描该市场热门品种，返回有技术信号的品种。
    """
    import numpy as np
    from backend.screener.rules import _rsi, _macd, _sma
    from backend.data.models import Market as MktEnum, Interval
    from backend.data.fetcher import get_fetcher

    try:
        symbol_list = req.symbols if req.symbols else _get_hot_symbol_list(req.market)
        market_enum = (
            MktEnum.CRYPTO
            if req.market == "crypto"
            else MktEnum.US
            if req.market == "us"
            else MktEnum.HK
            if req.market == "hk"
            else MktEnum.CN
        )
        fetcher = get_fetcher(market_enum)

        signals = []
        for idx, symbol in enumerate(symbol_list[:15]):  # 限制扫描数量避免超时
            if idx > 0 and idx % 5 == 0:
                await asyncio.sleep(0.5)  # 每5个请求暂停0.5秒避免限流
            try:
                # 港股加.HK后缀
                fetch_sym = symbol
                if req.market == "hk" and not symbol.endswith(".HK"):
                    code = symbol.lstrip("0") or "0"
                    while len(code) < 4:
                        code = "0" + code
                    fetch_sym = code + ".HK"
                klines = await fetcher.get_klines(fetch_sym, Interval.D1, limit=50)
                print(f"[TechScan] {fetch_sym}: {len(klines) if klines else 0} klines")
                if not klines or len(klines) < 20:
                    continue

                close = np.array([k.close for k in klines], dtype=np.float64)
                volume = np.array([k.volume for k in klines], dtype=np.float64)
                latest = float(close[-1])
                prev = float(close[-2]) if len(close) > 1 else latest
                change_pct = round((latest / prev - 1) * 100, 2) if prev > 0 else 0

                # RSI
                rsi_val = _rsi(close, 14)
                rsi_val = round(float(rsi_val), 1) if not np.isnan(rsi_val) else None

                # MACD
                dif, dea = _macd(close)
                macd_trend = "--"
                signal_type = None

                if not np.isnan(dif[-1]) and not np.isnan(dif[-2]):
                    # MACD金叉
                    if dif[-2] <= dea[-2] and dif[-1] > dea[-1]:
                        signal_type = "MACD金叉"
                        macd_trend = "多"
                    # MACD死叉
                    elif dif[-2] >= dea[-2] and dif[-1] < dea[-1]:
                        signal_type = "MACD死叉"
                        macd_trend = "空"
                    elif dif[-1] > dea[-1]:
                        macd_trend = "多"
                    else:
                        macd_trend = "空"

                # RSI 超卖/超买
                if rsi_val is not None:
                    if rsi_val <= 30:
                        signal_type = signal_type or "RSI超卖"
                    elif rsi_val >= 70:
                        signal_type = signal_type or "RSI超买"

                # 量比
                vol_ma = _sma(volume, 20)
                vol_ratio = (
                    round(float(volume[-1] / vol_ma[-1]), 1) if not np.isnan(vol_ma[-1]) and vol_ma[-1] > 0 else 1.0
                )

                # 放量突破
                if vol_ratio >= 2.0 and change_pct > 0:
                    if signal_type:
                        signal_type += "+放量"
                    else:
                        signal_type = "放量突破"

                # 均线突破
                ma20 = _sma(close, 20)
                if not np.isnan(ma20[-1]) and not np.isnan(ma20[-2]):
                    if close[-2] <= ma20[-2] and close[-1] > ma20[-1]:
                        signal_type = (signal_type + "+突破MA20") if signal_type else "突破MA20"

                # 趋势判断：连涨/连跌
                if len(close) >= 3:
                    if close[-1] > close[-2] > close[-3]:
                        signal_type = signal_type or "连续上涨"
                    elif close[-1] < close[-2] < close[-3]:
                        signal_type = signal_type or "连续下跌"

                # MA5/MA10金叉死叉
                if len(close) >= 10:
                    ma5 = _sma(close, 5)
                    ma10 = _sma(close, 10)
                    if (
                        not np.isnan(ma5[-1])
                        and not np.isnan(ma10[-1])
                        and not np.isnan(ma5[-2])
                        and not np.isnan(ma10[-2])
                    ):
                        if ma5[-2] <= ma10[-2] and ma5[-1] > ma10[-1]:
                            signal_type = (signal_type + "+MA金叉") if signal_type else "MA5/10金叉"
                        elif ma5[-2] >= ma10[-2] and ma5[-1] < ma10[-1]:
                            signal_type = (signal_type + "+MA死叉") if signal_type else "MA5/10死叉"

                # 如果没有信号，标记为"--"但仍然显示
                if not signal_type:
                    signal_type = "暂无明显信号"

                if True:  # 始终添加（让用户看到所有品种的技术面状态）
                    signals.append(
                        {
                            "symbol": symbol,
                            "price": round(latest, 4),
                            "change_pct": change_pct,
                            "signal_type": signal_type,
                            "rsi": rsi_val,
                            "macd_trend": macd_trend,
                            "volume_ratio": vol_ratio,
                            "market": req.market,
                        }
                    )

            except Exception as e:
                logger.debug(f"技术扫描 {symbol} 异常: {e}")
                continue

        # 按信号重要性排序（有多个信号的优先）
        signals.sort(key=lambda x: len(x.get("signal_type", "")), reverse=True)
        return {"signals": signals[:20]}

    except Exception as e:
        logger.error(f"技术信号扫描失败: {e}")
        return {"signals": [], "error": str(e)}


def _build_keyword_recommendations(market: str, news_items: list) -> list:
    """基于新闻关键词匹配生成基础推荐（不依赖 LLM）"""
    # 市场对应的热门品种和关键词映射
    keyword_map = {
        "crypto": {
            "BTC": ["比特币", "bitcoin", "btc", "加密", "数字货币", "矿", "减半"],
            "ETH": ["以太坊", "ethereum", "eth", "智能合约", "defi"],
            "SOL": ["solana", "sol", "高性能"],
            "BNB": ["binance", "bnb", "币安"],
            "XRP": ["ripple", "xrp", "跨境支付"],
            "DOGE": ["dogecoin", "doge", "马斯克", "meme"],
        },
        "us": {
            "NVDA": ["英伟达", "nvidia", "ai芯片", "gpu", "算力", "人工智能"],
            "TSLA": ["特斯拉", "tesla", "电动车", "自动驾驶", "马斯克"],
            "AAPL": ["苹果", "apple", "iphone"],
            "MSFT": ["微软", "microsoft", "azure", "copilot"],
            "GOOGL": ["谷歌", "google", "alphabet", "搜索"],
            "AMZN": ["亚马逊", "amazon", "aws", "电商"],
            "META": ["meta", "facebook", "元宇宙", "社交"],
            "AMD": ["amd", "芯片", "处理器"],
        },
        "hk": {
            "00700": ["腾讯", "微信", "游戏"],
            "09988": ["阿里巴巴", "淘宝", "电商"],
            "09888": ["百度", "搜索", "AI"],
            "01810": ["小米", "手机"],
            "09618": ["京东", "电商"],
            "03690": ["美团", "外卖"],
        },
        "cn": {
            "600519": ["茅台", "白酒"],
            "000858": ["五粮液", "白酒"],
            "300750": ["宁德时代", "电池", "新能源"],
            "601012": ["隆基", "光伏", "太阳能"],
            "002594": ["比亚迪", "电动车", "新能源"],
            "000001": ["平安", "金融", "银行"],
        },
    }

    market_symbols = keyword_map.get(market, keyword_map.get("crypto", {}))
    scores = {sym: 0 for sym in market_symbols}

    # 扫描新闻标题匹配关键词
    for news in news_items or []:
        title = (news.get("title", "") + " " + news.get("summary", "")).lower()
        for sym, keywords in market_symbols.items():
            for kw in keywords:
                if kw.lower() in title:
                    scores[sym] += 10
                    break

    # 按得分排序，未匹配的也包含（作为热门品种）
    sorted_syms = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    recs = []
    for sym, score in sorted_syms:
        base_score = max(50, min(90, 50 + score))
        recs.append(
            {
                "symbol": sym,
                "name": "",
                "market": market,
                "price": None,
                "change_pct": 0,
                "score": base_score,
                "hot_topic": "热门品种" if score == 0 else "新闻热点",
                "reason": "基于新闻关键词匹配" if score > 0 else "市场热门品种",
                "action": "watch",
                "signals": [],
            }
        )

    return recs


def _get_default_hot_symbols(market: str) -> list:
    """返回默认热门品种列表（覆盖主要行业龙头，30+品种供AI筛选）"""
    defaults = {
        "crypto": [
            {"symbol": "BTC-USDT", "name": "比特币", "hot_topic": "数字黄金"},
            {"symbol": "ETH-USDT", "name": "以太坊", "hot_topic": "智能合约"},
            {"symbol": "SOL-USDT", "name": "Solana", "hot_topic": "高性能链"},
            {"symbol": "BNB-USDT", "name": "币安币", "hot_topic": "交易所"},
            {"symbol": "XRP-USDT", "name": "瑞波", "hot_topic": "跨境支付"},
            {"symbol": "DOGE-USDT", "name": "狗狗币", "hot_topic": "Meme"},
            {"symbol": "ADA-USDT", "name": "艾达币", "hot_topic": "PoS公链"},
            {"symbol": "AVAX-USDT", "name": "雪崩", "hot_topic": "DeFi生态"},
            {"symbol": "DOT-USDT", "name": "波卡", "hot_topic": "跨链"},
            {"symbol": "MATIC-USDT", "name": "Polygon", "hot_topic": "L2扩容"},
            {"symbol": "LINK-USDT", "name": "Chainlink", "hot_topic": "预言机"},
            {"symbol": "UNI-USDT", "name": "Uniswap", "hot_topic": "DEX龙头"},
            {"symbol": "ATOM-USDT", "name": "Cosmos", "hot_topic": "跨链生态"},
            {"symbol": "FIL-USDT", "name": "Filecoin", "hot_topic": "存储"},
            {"symbol": "APT-USDT", "name": "Aptos", "hot_topic": "Move公链"},
        ],
        "us": [
            {"symbol": "NVDA", "name": "英伟达", "hot_topic": "AI芯片"},
            {"symbol": "TSLA", "name": "特斯拉", "hot_topic": "电动车"},
            {"symbol": "AAPL", "name": "苹果", "hot_topic": "消费电子"},
            {"symbol": "MSFT", "name": "微软", "hot_topic": "云+AI"},
            {"symbol": "GOOGL", "name": "谷歌", "hot_topic": "搜索+AI"},
            {"symbol": "AMZN", "name": "亚马逊", "hot_topic": "电商+云"},
            {"symbol": "META", "name": "Meta", "hot_topic": "社交+AI"},
            {"symbol": "AMD", "name": "AMD", "hot_topic": "芯片"},
            {"symbol": "NFLX", "name": "奈飞", "hot_topic": "流媒体"},
            {"symbol": "JPM", "name": "摩根大通", "hot_topic": "银行龙头"},
            {"symbol": "BAC", "name": "美国银行", "hot_topic": "金融"},
            {"symbol": "V", "name": "Visa", "hot_topic": "支付"},
            {"symbol": "MA", "name": "万事达", "hot_topic": "支付"},
            {"symbol": "UNH", "name": "联合健康", "hot_topic": "医疗保险"},
            {"symbol": "JNJ", "name": "强生", "hot_topic": "医药"},
            {"symbol": "PFE", "name": "辉瑞", "hot_topic": "制药"},
            {"symbol": "WMT", "name": "沃尔玛", "hot_topic": "零售"},
            {"symbol": "HD", "name": "家得宝", "hot_topic": "家居零售"},
            {"symbol": "CRM", "name": "Salesforce", "hot_topic": "SaaS"},
            {"symbol": "ORCL", "name": "甲骨文", "hot_topic": "数据库+云"},
            {"symbol": "AVGO", "name": "博通", "hot_topic": "AI芯片"},
            {"symbol": "MU", "name": "美光", "hot_topic": "存储芯片"},
            {"symbol": "INTC", "name": "英特尔", "hot_topic": "CPU"},
            {"symbol": "COIN", "name": "Coinbase", "hot_topic": "加密交易所"},
            {"symbol": "PLTR", "name": "Palantir", "hot_topic": "大数据+AI"},
            {"symbol": "SNOW", "name": "Snowflake", "hot_topic": "数据云"},
            {"symbol": "SQ", "name": "Block", "hot_topic": "金融科技"},
            {"symbol": "SHOP", "name": "Shopify", "hot_topic": "电商SaaS"},
        ],
        "hk": [
            {"symbol": "00700", "name": "腾讯", "hot_topic": "社交+游戏"},
            {"symbol": "09988", "name": "阿里巴巴", "hot_topic": "电商+云"},
            {"symbol": "09888", "name": "百度", "hot_topic": "搜索+AI"},
            {"symbol": "01810", "name": "小米", "hot_topic": "手机+IoT"},
            {"symbol": "09618", "name": "京东", "hot_topic": "电商"},
            {"symbol": "03690", "name": "美团", "hot_topic": "本地生活"},
            {"symbol": "02318", "name": "中国平安", "hot_topic": "保险"},
            {"symbol": "00388", "name": "港交所", "hot_topic": "交易所"},
            {"symbol": "01024", "name": "快手", "hot_topic": "短视频"},
            {"symbol": "02015", "name": "理想汽车", "hot_topic": "新能源车"},
            {"symbol": "09866", "name": "蔚来", "hot_topic": "新能源车"},
            {"symbol": "00941", "name": "中国移动", "hot_topic": "电信"},
            {"symbol": "01211", "name": "比亚迪股份", "hot_topic": "电动车"},
            {"symbol": "02269", "name": "药明生物", "hot_topic": "生物医药"},
            {"symbol": "09999", "name": "网易", "hot_topic": "游戏"},
            {"symbol": "01299", "name": "友邦保险", "hot_topic": "保险"},
            {"symbol": "00175", "name": "吉利汽车", "hot_topic": "汽车"},
            {"symbol": "09868", "name": "小鹏汽车", "hot_topic": "新能源车"},
            {"symbol": "02020", "name": "安踏体育", "hot_topic": "运动消费"},
            {"symbol": "00005", "name": "汇丰控股", "hot_topic": "银行"},
        ],
        "cn": [
            {"symbol": "600519", "name": "贵州茅台", "hot_topic": "白酒龙头"},
            {"symbol": "000858", "name": "五粮液", "hot_topic": "白酒"},
            {"symbol": "000568", "name": "泸州老窖", "hot_topic": "白酒"},
            {"symbol": "300750", "name": "宁德时代", "hot_topic": "新能源电池"},
            {"symbol": "002594", "name": "比亚迪", "hot_topic": "电动车"},
            {"symbol": "601012", "name": "隆基绿能", "hot_topic": "光伏"},
            {"symbol": "600438", "name": "通威股份", "hot_topic": "光伏+饲料"},
            {"symbol": "000001", "name": "平安银行", "hot_topic": "金融"},
            {"symbol": "601318", "name": "中国平安", "hot_topic": "保险"},
            {"symbol": "601398", "name": "工商银行", "hot_topic": "银行"},
            {"symbol": "600036", "name": "招商银行", "hot_topic": "银行"},
            {"symbol": "002415", "name": "海康威视", "hot_topic": "安防+AI"},
            {"symbol": "300059", "name": "东方财富", "hot_topic": "券商+互联网"},
            {"symbol": "688981", "name": "中芯国际", "hot_topic": "半导体"},
            {"symbol": "002230", "name": "科大讯飞", "hot_topic": "AI语音"},
            {"symbol": "600276", "name": "恒瑞医药", "hot_topic": "创新药"},
            {"symbol": "300760", "name": "迈瑞医疗", "hot_topic": "医疗器械"},
            {"symbol": "603259", "name": "药明康德", "hot_topic": "CXO"},
            {"symbol": "000651", "name": "格力电器", "hot_topic": "家电"},
            {"symbol": "000333", "name": "美的集团", "hot_topic": "家电"},
            {"symbol": "600887", "name": "伊利股份", "hot_topic": "乳业"},
            {"symbol": "600048", "name": "保利发展", "hot_topic": "地产"},
            {"symbol": "601668", "name": "中国建筑", "hot_topic": "基建"},
            {"symbol": "002371", "name": "北方华创", "hot_topic": "半导体设备"},
            {"symbol": "688008", "name": "澜起科技", "hot_topic": "芯片"},
            {"symbol": "300274", "name": "阳光电源", "hot_topic": "光伏储能"},
            {"symbol": "002459", "name": "晶澳科技", "hot_topic": "光伏组件"},
            {"symbol": "600893", "name": "航发动力", "hot_topic": "军工"},
            {"symbol": "002179", "name": "中航光电", "hot_topic": "军工电子"},
            {"symbol": "300033", "name": "同花顺", "hot_topic": "金融IT"},
        ],
    }

    result = []
    for item in defaults.get(market, defaults["crypto"]):
        result.append(
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "market": market,
                "price": None,
                "change_pct": 0,
                "score": 60,
                "hot_topic": item["hot_topic"],
                "reason": "市场热门品种",
                "action": "watch",
                "signals": [],
            }
        )
    return result


def _get_hot_symbol_list(market: str) -> list:
    """返回热门品种代码列表（用于技术扫描）"""
    lists = {
        "crypto": [
            "BTC-USDT",
            "ETH-USDT",
            "SOL-USDT",
            "BNB-USDT",
            "XRP-USDT",
            "DOGE-USDT",
            "ADA-USDT",
            "AVAX-USDT",
            "DOT-USDT",
            "MATIC-USDT",
            "LINK-USDT",
            "UNI-USDT",
            "ATOM-USDT",
            "FIL-USDT",
            "APT-USDT",
        ],
        "us": [
            "NVDA",
            "TSLA",
            "AAPL",
            "MSFT",
            "GOOGL",
            "AMZN",
            "META",
            "AMD",
            "NFLX",
            "JPM",
            "BAC",
            "V",
            "MA",
            "UNH",
            "JNJ",
            "PFE",
            "WMT",
            "HD",
            "CRM",
            "ORCL",
            "AVGO",
            "MU",
            "INTC",
            "COIN",
            "PLTR",
            "SNOW",
            "SQ",
            "SHOP",
        ],
        "hk": [
            "00700",
            "09988",
            "09888",
            "01810",
            "09618",
            "03690",
            "02318",
            "00388",
            "01024",
            "02015",
            "09866",
            "00941",
            "01211",
            "02269",
            "09999",
            "01299",
            "00175",
            "09868",
            "02020",
            "00005",
        ],
        "cn": [
            "600519",
            "000858",
            "000568",
            "300750",
            "002594",
            "601012",
            "600438",
            "000001",
            "601318",
            "601398",
            "600036",
            "002415",
            "300059",
            "688981",
            "002230",
            "600276",
            "300760",
            "603259",
            "000651",
            "000333",
            "600887",
            "600048",
            "601668",
            "002371",
            "688008",
            "300274",
            "002459",
            "600893",
            "002179",
            "300033",
        ],
    }
    return lists.get(market, lists["crypto"])


# ---------------------------------------------------------------------------
# Router: AI Judge (AI研判)
# ---------------------------------------------------------------------------
aijudge_router = APIRouter(prefix="/api/aijudge", tags=["AI研判"])


class AIJudgeRequest(BaseModel):
    symbol: str
    market: str = "crypto"
    interval: str = "1D"


@aijudge_router.post("/analyze")
async def ai_judge_analyze(req: AIJudgeRequest):
    """AI综合研判：K线+指标+新闻→开仓建议"""
    import numpy as np
    from backend.data.models import Market as MktEnum, Interval as IntEnum
    from backend.data.fetcher import get_fetcher
    from backend.indicators.builtin import calc_rsi, calc_macd, calc_ma, calc_boll, calc_atr

    # 自动推断market
    market = _guess_market(req.symbol, req.market)

    # 1. 获取K线数据（港股需要转换symbol: 00700→0700.HK）
    fetch_symbol = req.symbol
    if market == "hk" and not req.symbol.endswith(".HK"):
        code = req.symbol.lstrip("0") or "0"
        fetch_symbol = code.zfill(4) + ".HK"

    try:
        mkt = MktEnum(market)
        iv = IntEnum(req.interval)
        fetcher = get_fetcher(mkt)
        candles = await fetcher.get_klines(fetch_symbol, iv, 500)
    except Exception as e:
        return {"error": f"获取K线失败: {e}"}

    if not candles or len(candles) < 20:
        return {"error": f"K线数据不足（仅{len(candles) if candles else 0}根），请尝试更大周期"}

    # 2. 计算技术指标
    close = np.array([c.close for c in candles], dtype=np.float64)
    high = np.array([c.high for c in candles], dtype=np.float64)
    low = np.array([c.low for c in candles], dtype=np.float64)
    volume = np.array([c.volume for c in candles], dtype=np.float64)

    price = float(close[-1])
    ma5 = float(calc_ma(close, 5)[-1])
    ma20 = float(calc_ma(close, 20)[-1])
    ma60 = float(calc_ma(close, 60)[-1]) if len(close) >= 60 else None
    rsi14 = float(calc_rsi(close, 14)[-1])
    macd_data = calc_macd(close)
    dif = float(macd_data["dif"][-1])
    dea = float(macd_data["dea"][-1])
    hist = float(macd_data["histogram"][-1])
    boll = calc_boll(close, 20, 2)
    boll_upper = float(boll["upper"][-1])
    boll_lower = float(boll["lower"][-1])
    boll_mid = float(boll["middle"][-1])
    atr14 = float(calc_atr(high, low, close, 14)[-1])

    # 涨跌幅
    change_1d = (close[-1] - close[-2]) / close[-2] * 100 if len(close) > 1 else 0
    change_7d = (close[-1] - close[-7]) / close[-7] * 100 if len(close) > 7 else 0

    # 技术信号判断
    tech_signals = []
    if ma5 > ma20:
        tech_signals.append("MA5>MA20 多头排列")
    else:
        tech_signals.append("MA5<MA20 空头排列")
    if ma60 and price > ma60:
        tech_signals.append("价格在MA60之上")
    elif ma60:
        tech_signals.append("价格在MA60之下")
    if dif > dea and macd_data["dif"][-2] <= macd_data["dea"][-2]:
        tech_signals.append("MACD金叉")
    elif dif < dea and macd_data["dif"][-2] >= macd_data["dea"][-2]:
        tech_signals.append("MACD死叉")
    if hist > 0:
        tech_signals.append("MACD柱状线为正")
    else:
        tech_signals.append("MACD柱状线为负")
    if rsi14 > 70:
        tech_signals.append("RSI超买区(>70)")
    elif rsi14 < 30:
        tech_signals.append("RSI超卖区(<30)")
    elif rsi14 > 50:
        tech_signals.append("RSI偏多")
    else:
        tech_signals.append("RSI偏空")
    if price > boll_upper:
        tech_signals.append("突破布林上轨")
    elif price < boll_lower:
        tech_signals.append("跌破布林下轨")

    indicators_summary = {
        "价格": f"{price:,.2f}",
        "MA5": f"{ma5:,.2f}",
        "MA20": f"{ma20:,.2f}",
        "MA60": f"{ma60:,.2f}" if ma60 else "N/A",
        "RSI(14)": f"{rsi14:.1f}",
        "MACD DIF": f"{dif:.2f}",
        "MACD DEA": f"{dea:.2f}",
        "MACD柱": f"{hist:.2f}",
        "布林上轨": f"{boll_upper:,.2f}",
        "布林中轨": f"{boll_mid:,.2f}",
        "布林下轨": f"{boll_lower:,.2f}",
        "ATR(14)": f"{atr14:.2f}",
        "1日涨跌": f"{change_1d:+.2f}%",
        "7日涨跌": f"{change_7d:+.2f}%",
    }

    # 3. 获取新闻（分层筛选：品种相关 > 行业相关 > 市场通用）
    news_summary = []
    try:
        from backend.screener.news import NewsCollector

        collector = NewsCollector()
        news = await collector.fetch_all(market=market)
        await collector.close()

        symbol_lower = req.symbol.lower().replace("-usdt", "").replace("-usd", "").replace(".hk", "")
        # 品种名称映射（用于关键词匹配）
        name_map = {
            "00700": ["腾讯", "tencent"],
            "09988": ["阿里", "alibaba"],
            "09888": ["百度", "baidu"],
            "01810": ["小米", "xiaomi"],
            "02015": ["理想", "li auto", "lixiang"],
            "09866": ["蔚来", "nio"],
            "01211": ["比亚迪", "byd"],
            "03690": ["美团", "meituan"],
            "09618": ["京东", "jd"],
            "600519": ["茅台", "maotai"],
            "300750": ["宁德", "catl"],
            "002594": ["比亚迪", "byd"],
            "000001": ["平安", "pingan"],
            "601398": ["工商银行", "icbc"],
            "nvda": ["nvidia", "英伟达"],
            "tsla": ["tesla", "特斯拉"],
            "aapl": ["apple", "苹果"],
            "msft": ["microsoft", "微软"],
            "googl": ["google", "谷歌"],
            "meta": ["facebook", "meta"],
            "btc": ["bitcoin", "比特币"],
            "eth": ["ethereum", "以太坊"],
            "sol": ["solana"],
        }
        keywords = name_map.get(symbol_lower, [symbol_lower])

        # 分层筛选
        direct_match = []  # 直接提到品种的
        market_news = []  # 同市场的通用新闻

        for n in news:
            text = (n.get("title", "") + " " + n.get("content", "")).lower()
            # 品种/公司名直接匹配
            if any(kw in text for kw in keywords):
                direct_match.append(n)
            else:
                market_news.append(n)

        # 组合：先放品种相关(最多5条)，再补市场通用(补到10条)
        relevant = direct_match[:5]
        remaining = 10 - len(relevant)
        if remaining > 0:
            relevant.extend(market_news[:remaining])

        news_summary = [{"title": n.get("title", ""), "source": n.get("source", "")} for n in relevant]
    except Exception:
        pass

    # 4. 市场情绪与额外数据
    onchain_summary = {}
    market_context = ""

    if market == "crypto":
        # 加密货币：恐惧贪婪、资金费率、多空比、链上数据
        try:
            from backend.crypto_dashboard.sentiment import SentimentData

            sentiment = SentimentData()
            fg, fr, ls = await asyncio.gather(
                sentiment.get_fear_greed_index(),
                sentiment.get_funding_rate(),
                sentiment.get_long_short_ratio(),
                return_exceptions=True,
            )
            if not isinstance(fg, Exception) and fg:
                onchain_summary["恐惧贪婪指数"] = f"{fg.get('value', '?')} ({fg.get('label_cn', fg.get('label', ''))})"
            if not isinstance(fr, Exception) and fr:
                current = fr.get("current", {})
                rate = current.get("fundingRate", current.get("rate", 0))
                onchain_summary["资金费率"] = f"{float(rate) * 100:.4f}%"
            if not isinstance(ls, Exception) and ls:
                current = ls.get("current", {})
                ratio = current.get("ratio", ls.get("ratio", "?"))
                onchain_summary["多空比"] = str(ratio)
                signal = current.get("signal", ls.get("signal", ""))
                if signal:
                    onchain_summary["多空信号"] = signal
        except Exception as e:
            logger.debug(f"链上数据获取失败: {e}")
        market_context = "加密货币"

    elif market == "us":
        # 美股：成交量趋势、波动率分析
        vol_avg20 = float(np.mean(volume[-20:])) if len(volume) >= 20 else 0
        vol_ratio = float(volume[-1] / vol_avg20) if vol_avg20 > 0 else 1
        onchain_summary["成交量/20日均量"] = f"{vol_ratio:.2f}倍"
        onchain_summary["成交量状态"] = "放量" if vol_ratio > 1.5 else "缩量" if vol_ratio < 0.7 else "正常"
        # 波动率
        returns = np.diff(np.log(close[-30:])) if len(close) >= 30 else np.array([])
        if len(returns) > 0:
            annual_vol = float(np.std(returns) * np.sqrt(252) * 100)
            onchain_summary["年化波动率"] = f"{annual_vol:.1f}%"
        market_context = "美股"

    elif market == "hk":
        vol_avg20 = float(np.mean(volume[-20:])) if len(volume) >= 20 else 0
        vol_ratio = float(volume[-1] / vol_avg20) if vol_avg20 > 0 else 1
        onchain_summary["成交量/20日均量"] = f"{vol_ratio:.2f}倍"
        onchain_summary["成交量状态"] = "放量" if vol_ratio > 1.5 else "缩量" if vol_ratio < 0.7 else "正常"
        returns = np.diff(np.log(close[-30:])) if len(close) >= 30 else np.array([])
        if len(returns) > 0:
            annual_vol = float(np.std(returns) * np.sqrt(252) * 100)
            onchain_summary["年化波动率"] = f"{annual_vol:.1f}%"
        market_context = "港股"

    elif market == "cn":
        vol_avg20 = float(np.mean(volume[-20:])) if len(volume) >= 20 else 0
        vol_ratio = float(volume[-1] / vol_avg20) if vol_avg20 > 0 else 1
        onchain_summary["成交量/20日均量"] = f"{vol_ratio:.2f}倍"
        onchain_summary["成交量状态"] = "放量" if vol_ratio > 1.5 else "缩量" if vol_ratio < 0.7 else "正常"
        # A股特有：涨停跌停价
        prev_close = float(close[-2]) if len(close) >= 2 else price
        onchain_summary["涨停价"] = f"{prev_close * 1.1:,.2f}"
        onchain_summary["跌停价"] = f"{prev_close * 0.9:,.2f}"
        onchain_summary["距涨停"] = f"{((prev_close * 1.1 - price) / price * 100):+.2f}%"
        returns = np.diff(np.log(close[-30:])) if len(close) >= 30 else np.array([])
        if len(returns) > 0:
            annual_vol = float(np.std(returns) * np.sqrt(252) * 100)
            onchain_summary["年化波动率"] = f"{annual_vol:.1f}%"
        market_context = "A股"

    # 5. 调用LLM综合研判
    llm_verdict = None
    try:
        import sqlite3, aiosqlite

        llm_settings = {}
        async with aiosqlite.connect(DB_PATH) as db:
            async for row in await db.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'deepseek_%' OR key LIKE 'qwen_%' OR key = 'llm_provider'"
            ):
                llm_settings[row[0]] = row[1].strip('"') if row[1].startswith('"') else row[1]

        provider = llm_settings.get("llm_provider", "deepseek")
        if provider == "qwen":
            api_key = llm_settings.get("qwen_api_key", "")
            base_url = llm_settings.get("qwen_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
            model = llm_settings.get("qwen_model", "qwen-turbo")
        else:
            api_key = llm_settings.get("deepseek_api_key", "")
            base_url = llm_settings.get("deepseek_base_url", "https://api.deepseek.com/v1")
            model = llm_settings.get("deepseek_model", "deepseek-chat")

        if api_key:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key, base_url=base_url)

            # 构建市场数据部分
            extra_section_title = {
                "crypto": "链上数据 & 市场情绪",
                "us": "成交量 & 波动率分析",
                "hk": "成交量 & 波动率分析",
                "cn": "量价分析 & 涨跌停",
            }.get(market, "市场数据")

            onchain_text = ""
            if onchain_summary:
                onchain_text = f"\n## {extra_section_title}\n" + "\n".join(
                    f"- {k}: {v}" for k, v in onchain_summary.items()
                )

            role_desc = {
                "crypto": "加密货币量化交易分析师",
                "us": "美股投资分析师",
                "hk": "港股投资分析师",
                "cn": "A股投资分析师（注意涨跌停板制度和T+1交易规则）",
            }.get(market, "量化交易分析师")

            prompt = f"""你是一名专业的{role_desc}。请根据以下多维数据对 {req.symbol} 进行综合研判，给出明确的开仓建议。

## 当前技术指标 (周期: {req.interval})
{json.dumps(indicators_summary, ensure_ascii=False, indent=2)}

## 技术信号
{chr(10).join("- " + s for s in tech_signals)}
{onchain_text}

## 近期相关新闻与舆情
{chr(10).join("- " + n["title"] for n in news_summary) if news_summary else "- 无相关新闻"}

请综合以上「技术面」「链上数据」「市场情绪」「新闻舆情」四个维度进行分析。

请用以下JSON格式回复（所有字段必填，中文回复）：
{{
  "direction": "做多/做空/观望",
  "confidence": 1到100的整数,
  "entry_price": 建议入场价(数字),
  "stop_loss": 建议止损价(数字),
  "take_profit": 建议止盈价(数字),
  "position_pct": 建议仓位百分比1到100(整数),
  "timeframe": "建议持仓周期，如3-5天",
  "reasoning": "开仓理由，包含：1)趋势判断 2)关键指标支撑 3)新闻面影响，300字以内",
  "entry_logic": "具体入场逻辑，如：在价格回踩MA20时入场做多",
  "exit_logic": "出场逻辑，如：跌破MA60止损，触及前高止盈",
  "risk_warning": "主要风险因素",
  "key_levels": {{ "support": [支撑位1, 支撑位2], "resistance": [压力位1, 压力位2] }}
}}"""

            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": prompt}], max_tokens=1000, temperature=0.3
                ),
                timeout=30,
            )
            result_text = (response.choices[0].message.content or "").strip()
            # 提取JSON
            if "```" in result_text:
                lines = result_text.split("\n")
                json_lines = []
                in_block = False
                for line in lines:
                    if line.strip().startswith("```") and not in_block:
                        in_block = True
                        continue
                    elif line.strip() == "```" and in_block:
                        break
                    elif in_block:
                        json_lines.append(line)
                result_text = "\n".join(json_lines).strip()
            if result_text and result_text[0] not in ("{", "["):
                start = result_text.find("{")
                if start != -1:
                    result_text = result_text[start:]
            llm_verdict = json.loads(result_text)
    except Exception as e:
        logger.warning(f"AI研判LLM调用失败: {e}")
        llm_verdict = {
            "direction": "观望",
            "confidence": 50,
            "reasoning": f"LLM分析暂不可用: {str(e)[:50]}",
            "risk_warning": "请自行判断",
        }

    return {
        "symbol": req.symbol,
        "market": market,
        "interval": req.interval,
        "price": price,
        "indicators": indicators_summary,
        "tech_signals": tech_signals,
        "onchain": onchain_summary,
        "news": news_summary,
        "verdict": llm_verdict,
    }


# ---------------------------------------------------------------------------
# Router: Dashboard
# ---------------------------------------------------------------------------
dashboard_router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"])


@dashboard_router.get("/fear-greed")
async def get_fear_greed():
    """恐惧贪婪指数 — 调用 SentimentData"""
    from backend.crypto_dashboard.sentiment import SentimentData

    sd = SentimentData()
    try:
        return await sd.get_fear_greed_index()
    except Exception as e:
        logger.error(f"获取恐惧贪婪指数失败: {e}")
        return {"value": None, "label": "Unknown", "label_cn": "获取失败", "history": [], "source": "error"}


@dashboard_router.get("/funding-rate")
async def get_funding_rate(symbol: str = Query("BTC-USDT-SWAP")):
    """资金费率 — 调用 SentimentData"""
    from backend.crypto_dashboard.sentiment import SentimentData

    sd = SentimentData()
    try:
        return await sd.get_funding_rate(symbol)
    except Exception as e:
        logger.error(f"获取资金费率失败: {e}")
        return {"symbol": symbol, "current": None, "history": [], "source": "error"}


@dashboard_router.get("/open-interest")
async def get_open_interest(symbol: str = Query("BTC-USDT-SWAP")):
    """未平仓合约 — 调用 SentimentData"""
    from backend.crypto_dashboard.sentiment import SentimentData

    sd = SentimentData()
    try:
        return await sd.get_open_interest(symbol)
    except Exception as e:
        logger.error(f"获取持仓量失败: {e}")
        return {"symbol": symbol, "oi": None, "source": "error"}


@dashboard_router.get("/long-short-ratio")
async def get_long_short_ratio(coin: str = Query("BTC")):
    """多空比 — 调用 SentimentData"""
    from backend.crypto_dashboard.sentiment import SentimentData

    sd = SentimentData()
    try:
        return await sd.get_long_short_ratio(coin)
    except Exception as e:
        logger.error(f"获取多空比失败: {e}")
        return {"coin": coin, "current": None, "history": [], "source": "error"}


@dashboard_router.get("/exchange-flow")
async def get_exchange_flow(coin: str = Query("BTC")):
    """交易所资金流向 — 调用 OnChainData"""
    from backend.crypto_dashboard.onchain import OnChainData

    oc = OnChainData()
    try:
        return await oc.get_exchange_flow(coin)
    except Exception as e:
        logger.error(f"获取交易所流向失败: {e}")
        return {"netflow": [], "inflow": [], "outflow": [], "source": "error"}


@dashboard_router.get("/whale-transactions")
async def get_whale_transactions(coin: str = Query("BTC"), limit: int = Query(20)):
    """巨鲸交易 — 调用 OnChainData"""
    from backend.crypto_dashboard.onchain import OnChainData

    oc = OnChainData()
    try:
        txs = await oc.get_whale_transactions(coin)
        return {"coin": coin, "transactions": txs[:limit]}
    except Exception as e:
        logger.error(f"获取巨鲸交易失败: {e}")
        return {"coin": coin, "transactions": []}


@dashboard_router.get("/calendar")
async def get_calendar():
    """经济日历 — 调用 EconomicCalendar"""
    from backend.crypto_dashboard.calendar import EconomicCalendar

    cal = EconomicCalendar()
    try:
        macro = await cal.get_macro_events()
        crypto = await cal.get_crypto_events()
        return {"events": macro + crypto}
    except Exception as e:
        logger.error(f"获取经济日历失败: {e}")
        return {"events": []}


@dashboard_router.get("/onchain")
async def get_onchain(coin: str = Query("BTC"), metric: str = Query("active_addresses")):
    """链上数据 — 调用 OnChainData"""
    from backend.crypto_dashboard.onchain import OnChainData

    oc = OnChainData()
    try:
        if metric == "active_addresses":
            result = await oc.get_active_addresses(coin)
            return {"coin": coin, "metric": metric, "data": result}
        elif metric == "nupl":
            result = await oc.get_nupl()
            return {"coin": coin, "metric": metric, "data": result}
        elif metric == "miner":
            result = await oc.get_miner_data()
            return {"coin": coin, "metric": metric, "data": result}
        else:
            # 默认返回活跃地址
            result = await oc.get_active_addresses(coin)
            return {"coin": coin, "metric": metric, "data": result}
    except Exception as e:
        logger.error(f"获取链上数据失败: {e}")
        return {"coin": coin, "metric": metric, "data": []}


# ---------------------------------------------------------------------------
# Router: Watchlist
# ---------------------------------------------------------------------------
watchlist_router = APIRouter(prefix="/api/watchlist", tags=["自选列表"])


@watchlist_router.get("")
async def get_watchlist():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM watchlist ORDER BY sort_order ASC, created_at DESC")
        rows = await cursor.fetchall()
        return {
            "watchlist": [
                {
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "market": r["market"],
                    "note": r["note"],
                    "sort_order": r["sort_order"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }
    finally:
        await db.close()


@watchlist_router.post("")
async def add_to_watchlist(req: WatchlistAddRequest):
    item_id = str(uuid.uuid4())
    now = int(time.time())
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO watchlist (id, symbol, market, note, sort_order, created_at) VALUES (?,?,?,?,0,?)",
            (item_id, req.symbol, req.market, req.note, now),
        )
        await db.commit()
        return {"id": item_id, "status": "added"}
    finally:
        await db.close()


class WatchlistReorderRequest(BaseModel):
    items: List[Dict[str, Any]]  # [{"id": "xxx", "sort_order": 0}, ...]


@watchlist_router.put("/reorder")
async def reorder_watchlist(req: WatchlistReorderRequest):
    """重新排序自选列表"""
    db = await get_db()
    try:
        for item in req.items:
            item_id = item.get("id", "")
            sort_order = item.get("sort_order", 0)
            if item_id:
                await db.execute(
                    "UPDATE watchlist SET sort_order = ? WHERE id = ?",
                    (sort_order, item_id),
                )
        await db.commit()
        return {"status": "ok", "updated": len(req.items)}
    finally:
        await db.close()


@watchlist_router.put("/{item_id}")
async def update_watchlist_item(item_id: str, req: WatchlistUpdateRequest):
    db = await get_db()
    try:
        fields = []
        values = []
        if req.note is not None:
            fields.append("note = ?")
            values.append(req.note)
        if req.sort_order is not None:
            fields.append("sort_order = ?")
            values.append(req.sort_order)
        if not fields:
            raise HTTPException(400, "No fields to update")
        values.append(item_id)
        await db.execute(f"UPDATE watchlist SET {', '.join(fields)} WHERE id = ?", values)
        await db.commit()
        return {"id": item_id, "status": "updated"}
    finally:
        await db.close()


@watchlist_router.delete("/{item_id}")
async def remove_from_watchlist(item_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM watchlist WHERE id = ?", (item_id,))
        await db.commit()
        return {"id": item_id, "status": "deleted"}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Router: Settings
# ---------------------------------------------------------------------------
settings_router = APIRouter(prefix="/api/settings", tags=["设置"])


@settings_router.get("")
async def get_settings():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT key, value FROM settings")
        rows = await cursor.fetchall()
        settings = {}
        for r in rows:
            val = r["value"]
            # 尝试解析 JSON 值
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
            settings[r["key"]] = val
        return {"settings": settings}
    finally:
        await db.close()


@settings_router.put("")
async def update_settings(req: SettingsUpdate):
    db = await get_db()
    try:
        for key, value in req.settings.items():
            str_val = json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                (key, str_val, str_val),
            )
        await db.commit()
        return {"status": "ok", "updated": list(req.settings.keys())}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Router: 缠论分析
# ---------------------------------------------------------------------------
chanlun_router = APIRouter(prefix="/api/chanlun", tags=["缠论"])


@chanlun_router.get("")
async def get_chanlun(
    symbol: str = Query(...),
    interval: str = Query("1H"),
    market: str = Query("crypto"),
    limit: int = Query(1000, ge=50, le=5000),
):
    """
    缠论分析接口 - 获取K线数据并返回笔/线段/中枢/买卖点

    返回:
        bi_list: 笔列表 [{begin_x, begin_y, end_x, end_y, dir, is_sure}]
        seg_list: 线段列表 [{begin_x, begin_y, end_x, end_y, dir, is_sure}]
        zs_list: 中枢列表 [{begin_x, end_x, zg, zd, dir, level}]
        bsp_list: 买卖点列表 [{x, y, type, is_buy}]

    其中 x 是 bar_index（K线数组索引），y 是价格
    """
    import sys as _sys
    import os as _os

    # 自动推断market
    market = _guess_market(symbol, market)

    from backend.data.models import Market as MktEnum, Interval as IntEnum

    try:
        mkt = MktEnum(market)
    except ValueError:
        raise HTTPException(400, f"Unknown market: {market}")
    try:
        iv = IntEnum(interval)
    except ValueError:
        raise HTTPException(400, f"Unknown interval: {interval}")

    # 1. 获取K线数据
    try:
        from backend.data.fetcher import get_fetcher

        fetcher = get_fetcher(mkt)
        candles = await fetcher.get_klines(symbol, iv, limit)
    except Exception as e:
        logger.error(f"[Chanlun] 获取K线失败: {e}")
        raise HTTPException(502, f"获取K线数据失败: {e}")

    if not candles:
        return {"bi_list": [], "seg_list": [], "zs_list": [], "bsp_list": []}

    # 2. 转换为通用格式
    candle_dicts = [
        {
            "timestamp": c.timestamp,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        for c in candles
    ]

    # 3. 调用缠论引擎
    _engine_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "chanlun_engine")
    if _engine_dir not in _sys.path:
        _sys.path.insert(0, _engine_dir)

    try:
        from backend.chanlun_engine.chanlun_service import analyze

        result = analyze(candle_dicts)
    except Exception as e:
        logger.error(f"[Chanlun] 缠论分析失败: {e}", exc_info=True)
        raise HTTPException(500, f"缠论分析失败: {e}")

    return result


@chanlun_router.post("/from-data")
async def chanlun_from_data(req: Dict[str, Any]):
    """直接用前端传来的K线数据做缠论分析，确保数据一致"""
    import sys as _sys, os as _os

    candles = req.get("candles", [])
    if not candles or len(candles) < 30:
        return {"bi_list": [], "seg_list": [], "zs_list": [], "bsp_list": []}
    _engine_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "chanlun_engine")
    if _engine_dir not in _sys.path:
        _sys.path.insert(0, _engine_dir)
    try:
        from backend.chanlun_engine.chanlun_service import analyze

        return analyze(candles)
    except Exception as e:
        return {"bi_list": [], "seg_list": [], "zs_list": [], "bsp_list": [], "error": str(e)}


# ---------------------------------------------------------------------------
# 艾略特波浪分析端点
# ---------------------------------------------------------------------------


@chanlun_router.post("/elliott-wave/from-data")
async def elliott_wave_from_data(req: Dict[str, Any]):
    """用前端传来的可见区域K线数据做艾略特波浪分析"""
    candles = req.get("candles", [])
    bar_offset = int(req.get("bar_offset", 0))
    if not candles or len(candles) < 30:
        return {"patterns": [], "predictions": []}
    try:
        from backend.elliott_wave.service import analyze

        return analyze(candles, bar_offset=bar_offset)
    except Exception as e:
        logger.error(f"艾略特波浪分析失败: {e}", exc_info=True)
        return {"patterns": [], "predictions": [], "error": str(e)}


@chanlun_router.get("/verdict")
async def chanlun_verdict(
    symbol: str = Query(...),
    market: str = Query("crypto"),
):
    """
    缠论多级别综合研判 - 并行分析三个周期，给出操作建议

    加密货币: 1H / 4H / 1D
    股票:     30m / 1D / 1W
    """
    import sys as _sys
    import os as _os
    from datetime import datetime as _dt

    market = _guess_market(symbol, market)

    from backend.data.models import Market as MktEnum, Interval as IntEnum

    try:
        mkt = MktEnum(market)
    except ValueError:
        raise HTTPException(400, f"Unknown market: {market}")

    # 根据市场选择三个分析周期
    if market == "crypto":
        timeframes = [("1H", IntEnum.H1), ("4H", IntEnum.H4), ("1D", IntEnum.D1)]
    else:
        timeframes = [("30m", IntEnum.M30), ("1D", IntEnum.D1), ("1W", IntEnum.W1)]

    _engine_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "chanlun_engine")
    if _engine_dir not in _sys.path:
        _sys.path.insert(0, _engine_dir)

    from backend.data.fetcher import get_fetcher
    from backend.chanlun_engine.chanlun_service import analyze as chanlun_analyze

    fetcher = get_fetcher(mkt)

    # 并行获取三个周期的K线数据
    # 港股symbol转换
    fetch_symbol = symbol
    if market == "hk" and not symbol.endswith(".HK"):
        code = symbol.lstrip("0") or "0"
        fetch_symbol = code.zfill(4) + ".HK"

    async def _fetch_and_analyze(tf_label, tf_enum):
        try:
            candles = await fetcher.get_klines(fetch_symbol, tf_enum, 1000)
            print(f"[ChanlunVerdict] {tf_label}: {len(candles) if candles else 0} candles")
            if not candles:
                return tf_label, None, []
            candle_dicts = [
                {
                    "timestamp": c.timestamp,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for c in candles
            ]
            result = chanlun_analyze(candle_dicts)
            return tf_label, result, candle_dicts
        except Exception as e:
            import traceback

            print(f"[ChanlunVerdict] {tf_label} 分析失败: {e}")
            traceback.print_exc()
            return tf_label, None, []

    # 串行获取，每个间隔0.5秒，避免OKX限流
    results = []
    for label, iv in timeframes:
        r = await _fetch_and_analyze(label, iv)
        results.append(r)
        await asyncio.sleep(0.5)

    # 获取当前价格（用最小周期的最后一根K线）
    current_price = 0.0
    for _, _, candle_dicts in results:
        if candle_dicts:
            current_price = candle_dicts[-1]["close"]
            break

    # 综合分析
    def _analyze_level(tf_label, result, candle_dicts):
        """分析单个级别的走势状态"""
        if not result or not result.get("bi_list"):
            return None

        bi_list = result["bi_list"]
        zs_list = result.get("zs_list", [])
        bsp_list = result.get("bsp_list", [])

        last_bi = bi_list[-1] if bi_list else None
        if not last_bi:
            return None

        last_dir = "up" if last_bi["dir"] == 1 else "down"

        # 判断走势类型
        # 统计连续同向笔的数量来判断趋势
        trend = "震荡"
        if len(bi_list) >= 3:
            recent_bis = bi_list[-5:]  # 最近5笔
            up_count = sum(1 for b in recent_bis if b["dir"] == 1)
            down_count = len(recent_bis) - up_count
            # 检查是否有中枢被突破
            if zs_list:
                last_zs = None
                # 找笔级别的最后一个中枢
                bi_zs = [z for z in zs_list if z.get("level") == "bi"]
                if bi_zs:
                    last_zs = bi_zs[-1]
                elif zs_list:
                    last_zs = zs_list[-1]

                if last_zs:
                    if current_price > last_zs["zg"]:
                        trend = "上涨趋势" if up_count > down_count else "上涨突破"
                    elif current_price < last_zs["zd"]:
                        trend = "下跌趋势" if down_count > up_count else "下跌突破"
                    else:
                        trend = "中枢震荡"
            else:
                if up_count >= 4:
                    trend = "上涨趋势"
                elif down_count >= 4:
                    trend = "下跌趋势"

        level_info = {
            "tf": tf_label,
            "last_bi_dir": last_dir,
            "last_bi_sure": last_bi.get("is_sure", False),
            "last_bi_from": last_bi["begin_y"],
            "last_bi_to": last_bi["end_y"],
            "trend": trend,
        }

        return level_info

    def _extract_zhongshu(tf_label, result):
        """提取中枢信息"""
        if not result:
            return []
        zs_list = result.get("zs_list", [])
        zs_info = []
        # 只取笔中枢的最后2个
        bi_zs = [z for z in zs_list if z.get("level") == "bi"]
        for zs in bi_zs[-2:]:
            position = ""
            if current_price > zs["zg"]:
                position = "价格在中枢上方"
            elif current_price < zs["zd"]:
                diff_pct = (zs["zd"] - current_price) / current_price * 100
                if diff_pct > 10:
                    position = "价格远在中枢下方"
                else:
                    position = "价格在中枢下方"
            else:
                position = "价格在中枢内"
            zs_info.append(
                {
                    "tf": tf_label,
                    "zg": round(zs["zg"], 2),
                    "zd": round(zs["zd"], 2),
                    "position": position,
                }
            )
        return zs_info

    def _extract_bsp(tf_label, result):
        """提取有效买卖点"""
        if not result:
            return []
        bsp_list = result.get("bsp_list", [])
        bi_list = result.get("bi_list", [])
        active = []
        # 只关注最近的买卖点（最后3个）
        for bsp in bsp_list[-3:]:
            active.append(
                {
                    "tf": tf_label,
                    "type": bsp["type"],
                    "is_buy": bsp["is_buy"],
                    "price": round(bsp["y"], 2),
                    "desc": f"{tf_label} {'买点' if bsp['is_buy'] else '卖点'} {bsp['type']}",
                }
            )
        return active

    def _generate_verdict(levels, zhongshu_all, active_bsp_all):
        """综合三级别生成操作建议"""
        action = "wait"
        confidence = 50
        reasoning_parts = []
        next_signals = []
        key_prices = []
        exit_strategy = ""

        valid_levels = [l for l in levels if l is not None]
        if not valid_levels:
            return {
                "action": "wait",
                "action_cn": "数据不足",
                "confidence": 0,
                "reasoning": "无法获取有效的缠论分析数据",
                "levels": [],
                "zhongshu": [],
                "active_bsp": [],
                "next_signals": [],
                "key_prices": [],
                "exit_strategy": "",
            }

        tf_labels = [timeframes[0][0], timeframes[1][0], timeframes[2][0]]
        small = valid_levels[0] if len(valid_levels) > 0 else None
        medium = valid_levels[1] if len(valid_levels) > 1 else None
        large = valid_levels[2] if len(valid_levels) > 2 else None

        # 统计方向
        dirs = [l["last_bi_dir"] for l in valid_levels]
        down_count = dirs.count("down")
        up_count = dirs.count("up")

        # 统计确认状态
        sure_count = sum(1 for l in valid_levels if l.get("last_bi_sure"))

        # 查找有效买卖点
        buy_points = [b for b in active_bsp_all if b["is_buy"]]
        sell_points = [b for b in active_bsp_all if not b["is_buy"]]

        # 判断逻辑
        # 1. 三级别同向下 + 无买点 → wait
        if down_count == 3 and not buy_points:
            action = "wait"
            confidence = 80 + sure_count * 5
            reasoning_parts.append("三级别同时处于下跌笔中")
            if sure_count < 3:
                reasoning_parts.append("走势未完美（有未完成笔）")
            reasoning_parts.append("没有任何级别的有效买点")
            exit_strategy = "等待小级别下跌笔完成并出现背驰信号"

        # 2. 三级别同向上 → hold_long
        elif up_count == 3:
            action = "hold_long"
            confidence = 75 + sure_count * 5
            reasoning_parts.append("三级别同时处于上涨笔中")
            if sell_points:
                reasoning_parts.append(f"注意已有卖点信号")
                action = "sell" if any(s["tf"] == tf_labels[0] for s in sell_points) else "hold_long"
            exit_strategy = "等待操作级别第一类卖点出现"

        # 3. 小级别买点 + 中级别下跌未完 → buy（轻仓）
        elif small and small["last_bi_dir"] == "up" and buy_points:
            small_buy = [b for b in buy_points if b["tf"] == tf_labels[0]]
            medium_buy = [b for b in buy_points if b["tf"] == tf_labels[1]]
            if small_buy and medium_buy:
                # 小+中级别买点共振 → buy（重仓）
                action = "buy"
                confidence = 85 + sure_count * 3
                reasoning_parts.append(f"{tf_labels[0]}和{tf_labels[1]}买点共振")
                reasoning_parts.append("多级别共振确认，可重仓参与")
                exit_strategy = f"等待{tf_labels[0]}第一类卖点出现"
            elif small_buy:
                action = "buy"
                confidence = 60 + sure_count * 5
                reasoning_parts.append(f"{tf_labels[0]}出现买点")
                if medium and medium["last_bi_dir"] == "down":
                    reasoning_parts.append(f"但{tf_labels[1]}仍在下跌中，建议轻仓")
                    confidence -= 10
                exit_strategy = f"等待{tf_labels[0]}第一类卖点或买点被破坏则退出"

        # 4. 小级别出现卖点 → sell
        elif sell_points and any(s["tf"] == tf_labels[0] for s in sell_points):
            action = "sell"
            confidence = 70 + sure_count * 5
            reasoning_parts.append(f"{tf_labels[0]}出现卖点信号")
            if medium and medium["last_bi_dir"] == "down":
                reasoning_parts.append(f"{tf_labels[1]}走势向下确认")
                confidence += 10
            exit_strategy = "已出现卖点，应考虑减仓或清仓"

        # 5. 混合方向 → 具体分析
        else:
            if down_count > up_count:
                action = "wait"
                confidence = 55 + sure_count * 5
                reasoning_parts.append(f"多数级别向下（{down_count}跌/{up_count}涨）")
                if buy_points:
                    reasoning_parts.append("有买点但需更多确认")
                else:
                    reasoning_parts.append("等待买点出现")
            elif up_count > down_count:
                action = "hold_long"
                confidence = 60 + sure_count * 5
                reasoning_parts.append(f"多数级别向上（{up_count}涨/{down_count}跌）")
                exit_strategy = "关注下跌级别的走势完成情况"
            else:
                action = "wait"
                confidence = 45
                reasoning_parts.append("级别方向分歧，走势不明朗")
                reasoning_parts.append("等待方向一致后再操作")

        # 生成等待信号
        if action == "wait":
            if small and small["last_bi_dir"] == "down":
                next_signals.append(
                    {
                        "desc": f"{tf_labels[0]}第一类买点",
                        "condition": f"{tf_labels[0]}下跌笔完成+背驰",
                        "importance": "操作级别信号",
                    }
                )
            if medium and medium["last_bi_dir"] == "down":
                next_signals.append(
                    {
                        "desc": f"{tf_labels[1]}第一类买点",
                        "condition": f"{tf_labels[1]}下跌笔完成+背驰",
                        "importance": "确认信号",
                    }
                )
            if large and large["last_bi_dir"] == "down":
                next_signals.append(
                    {
                        "desc": f"{tf_labels[2]}背驰信号",
                        "condition": f"{tf_labels[2]}下跌力度衰减+MACD面积缩小",
                        "importance": "大级别转折",
                    }
                )
        elif action in ("hold_long", "buy"):
            if small:
                next_signals.append(
                    {
                        "desc": f"{tf_labels[0]}第一类卖点",
                        "condition": f"{tf_labels[0]}上涨笔完成+背驰",
                        "importance": "减仓信号",
                    }
                )
            if medium and medium["last_bi_dir"] == "up":
                next_signals.append(
                    {
                        "desc": f"{tf_labels[1]}第一类卖点",
                        "condition": f"{tf_labels[1]}上涨背驰",
                        "importance": "清仓信号",
                    }
                )

        # 关键价位
        for l in valid_levels:
            key_prices.append(
                {
                    "price": round(l["last_bi_from"], 2),
                    "desc": f"{l['tf']}笔起点",
                    "type": "reference",
                }
            )
            key_prices.append(
                {
                    "price": round(l["last_bi_to"], 2),
                    "desc": f"{l['tf']}笔终点",
                    "type": "support" if l["last_bi_dir"] == "down" else "resistance",
                }
            )

        # 加入中枢关键价位
        for zs in zhongshu_all:
            key_prices.append({"price": zs["zg"], "desc": f"{zs['tf']}中枢ZG", "type": "resistance"})
            key_prices.append({"price": zs["zd"], "desc": f"{zs['tf']}中枢ZD", "type": "support"})

        # 去重并排序
        seen = set()
        unique_prices = []
        for kp in key_prices:
            if kp["price"] not in seen:
                seen.add(kp["price"])
                unique_prices.append(kp)
        key_prices = sorted(unique_prices, key=lambda x: x["price"])

        # 限制confidence范围
        confidence = max(0, min(100, confidence))

        action_cn_map = {
            "wait": "空仓等待",
            "buy": "买入",
            "sell": "卖出",
            "hold_long": "持有观望",
            "hold_short": "持空观望",
        }

        return {
            "action": action,
            "action_cn": action_cn_map.get(action, action),
            "confidence": confidence,
            "reasoning": "，".join(reasoning_parts) if reasoning_parts else "分析中",
            "levels": valid_levels,
            "zhongshu": zhongshu_all,
            "active_bsp": active_bsp_all,
            "next_signals": next_signals,
            "key_prices": key_prices,
            "exit_strategy": exit_strategy or "根据同级别买卖点信号操作",
        }

    # 构建分析数据
    levels = []
    zhongshu_all = []
    active_bsp_all = []

    for tf_label, result, candle_dicts in results:
        level = _analyze_level(tf_label, result, candle_dicts)
        levels.append(level)
        zhongshu_all.extend(_extract_zhongshu(tf_label, result))
        active_bsp_all.extend(_extract_bsp(tf_label, result))

    verdict = _generate_verdict(levels, zhongshu_all, active_bsp_all)

    return {
        "symbol": symbol,
        "current_price": round(current_price, 2),
        **verdict,
        "updated_at": _dt.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# 注册所有路由
# ---------------------------------------------------------------------------
app.include_router(market_router)
app.include_router(indicator_router)
app.include_router(formula_router)
app.include_router(alert_router)
app.include_router(backtest_router)
app.include_router(screener_router)
app.include_router(dashboard_router)
app.include_router(watchlist_router)
app.include_router(aijudge_router)
app.include_router(settings_router)
app.include_router(chanlun_router)

# ---------------------------------------------------------------------------
# 斐波那契分析路由
# ---------------------------------------------------------------------------
fibonacci_router = APIRouter(prefix="/api/fibonacci", tags=["斐波那契"])


@fibonacci_router.post("/analyze")
async def fibonacci_analyze(req: Dict[str, Any]):
    """
    自动斐波那契回撤/扩展分析 — 接收前端POST的K线数据，确保bar_index一致。

    请求体:
        candles: K线数据数组
        mode: "retracement" 或 "extension"
        deviation: ZigZag偏差乘数（默认3.0）
        depth: pivot检测深度（默认10）
    """
    import numpy as np
    import pandas as pd
    from backend.indicators.auto_fibonacci import AutoFibonacci

    candles = req.get("candles", [])
    mode = req.get("mode", "retracement")
    deviation = float(req.get("deviation", 3.0))
    depth = int(req.get("depth", 10))

    if not candles or len(candles) < 30:
        return {"error": "K线数据不足（至少需要30根）", "levels": [], "pivots": []}

    # 构建DataFrame
    df = pd.DataFrame(candles)
    # 确保列名统一
    col_map = {}
    for col in df.columns:
        cl = col.lower()
        if cl in ("high", "h"):
            col_map["high"] = col
        elif cl in ("low", "l"):
            col_map["low"] = col
        elif cl in ("close", "c"):
            col_map["close"] = col
        elif cl in ("open", "o"):
            col_map["open"] = col

    high_col = col_map.get("high", "high")
    low_col = col_map.get("low", "low")
    close_col = col_map.get("close", "close")

    try:
        fib = AutoFibonacci(deviation=deviation, depth=depth, dynamic_deviation=True)
        fib.fit(df, high_col=high_col, low_col=low_col, close_col=close_col)

        # 序列化pivot点
        pivots_out = [{"x": p.index, "y": p.price, "is_high": p.is_high} for p in fib.pivots]

        if mode == "extension":
            ext = fib.get_extension()
            if ext is None:
                return {
                    "mode": "extension",
                    "error": "ZigZag枢轴点不足（至少需要3个）",
                    "levels": [],
                    "pivots": pivots_out,
                }
            levels = [{"ratio": lv.ratio, "price": lv.price, "label": lv.label} for lv in ext.levels]
            return {
                "mode": "extension",
                "trend": ext.trend.value,
                "point_a": {"x": ext.point_a.index, "y": ext.point_a.price},
                "point_b": {"x": ext.point_b.index, "y": ext.point_b.price},
                "point_c": {"x": ext.point_c.index, "y": ext.point_c.price},
                "start": {"x": ext.point_a.index, "y": ext.point_a.price},
                "end": {"x": ext.point_b.index, "y": ext.point_b.price},
                "trend_range": ext.trend_range,
                "retracement_ratio": ext.retracement_ratio,
                "levels": levels,
                "pivots": pivots_out,
            }
        else:
            ret = fib.get_retracement()
            if ret is None:
                return {
                    "mode": "retracement",
                    "error": "ZigZag枢轴点不足（至少需要2个）",
                    "levels": [],
                    "pivots": pivots_out,
                }
            levels = [{"ratio": lv.ratio, "price": lv.price, "label": lv.label} for lv in ret.levels]
            return {
                "mode": "retracement",
                "trend": ret.trend.value,
                "start": {"x": ret.start_point.index, "y": ret.start_point.price},
                "end": {"x": ret.end_point.index, "y": ret.end_point.price},
                "price_range": ret.price_range,
                "levels": levels,
                "pivots": pivots_out,
            }
    except Exception as e:
        logger.error(f"[Fibonacci] 分析失败: {e}", exc_info=True)
        return {"error": f"斐波那契分析失败: {str(e)}", "levels": [], "pivots": []}


app.include_router(fibonacci_router)


# ---------------------------------------------------------------------------
# WebSocket 端点
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await hub.handle_client(websocket)


# ---------------------------------------------------------------------------
# 静态文件服务（必须放在所有路由注册之后）
# ---------------------------------------------------------------------------
_frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")

# ---------------------------------------------------------------------------
# 直接运行入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=config.HOST, port=config.PORT, reload=config.DEBUG)
