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
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
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
    {"id": "crypto", "name": "加密货币", "icon": "bitcoin", "description": "BTC, ETH 等主流加密货币", "default_symbol": "BTC-USDT", "currency": "USDT"},
    {"id": "us", "name": "美股", "icon": "dollar-sign", "description": "NYSE, NASDAQ 美国股票", "default_symbol": "AAPL", "currency": "USD"},
    {"id": "hk", "name": "港股", "icon": "landmark", "description": "HKEX 香港股票", "default_symbol": "0700.HK", "currency": "HKD"},
    {"id": "cn", "name": "A股", "icon": "bar-chart-2", "description": "SSE, SZSE 中国A股", "default_symbol": "600519", "currency": "CNY"},
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
):
    """获取K线数据"""
    # 自动推断market，防止前端没传正确的market
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
        candles = await fetcher.get_klines(symbol, iv, limit)
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
        indicators.append({
            "name": ind_id,
            "label": meta["name"],
            "category": meta["category"],
            "overlay": meta["overlay"],
            "params": [
                {"key": p["name"], "type": p["type"], "default": p["default"]}
                for p in meta["params"]
            ],
            "outputs": meta["outputs"],
        })
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
                    {"t": timestamps[i], "v": float(arr[i]) if arr[i] is not None else None}
                    for i in range(len(arr))
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
            return {k: _sanitize_list(v) if isinstance(v, (list, tuple, dict)) else _sanitize_value(v) for k, v in lst.items()}
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
                        {"t": timestamps[i] if i < len(timestamps) else i,
                         "v": _sanitize_value(v)}
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
                alert_id, req.symbol, req.market, req.condition_type,
                json.dumps(req.condition), req.message, req.label,
                json.dumps(req.notify_methods), req.repeat_mode, req.cooldown,
                now, now,
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
            cursor = await db.execute(
                "SELECT * FROM alert_history ORDER BY triggered_at DESC LIMIT ?", (limit,)
            )
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
    engine = BacktestEngine({
        "initial_capital": bt_config.get("initial_capital", 100000),
        "commission": bt_config.get("commission", 0.001),
        "slippage": bt_config.get("slippage", 0.0005),
    })

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
                    backtest_id, req.strategy_code, req.symbol, req.interval,
                    req.start_date, req.end_date,
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
                cursor = await db.execute("SELECT key, value FROM settings WHERE key LIKE 'deepseek_%' OR key LIKE 'qwen_%' OR key = 'llm_provider'")
                rows = await cursor.fetchall()
                for row in rows:
                    llm_settings[row[0]] = row[1].strip('"') if row[1].startswith('"') else row[1]
                await db.close()
            except Exception:
                pass

            provider = llm_settings.get('llm_provider', 'deepseek')
            if provider == 'qwen':
                api_key = llm_settings.get('qwen_api_key', '')
                base_url = llm_settings.get('qwen_base_url', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
                model = llm_settings.get('qwen_model', 'qwen-turbo')
            else:
                api_key = llm_settings.get('deepseek_api_key', '')
                base_url = llm_settings.get('deepseek_base_url', 'https://api.deepseek.com')
                model = llm_settings.get('deepseek_model', 'deepseek-chat')

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

    # 1. 获取K线数据
    try:
        mkt = MktEnum(market)
        iv = IntEnum(req.interval)
        fetcher = get_fetcher(mkt)
        candles = await fetcher.get_klines(req.symbol, iv, 200)
    except Exception as e:
        return {"error": f"获取K线失败: {e}"}

    if not candles or len(candles) < 30:
        return {"error": "K线数据不足，至少需要30根"}

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
    if ma5 > ma20: tech_signals.append("MA5>MA20 多头排列")
    else: tech_signals.append("MA5<MA20 空头排列")
    if ma60 and price > ma60: tech_signals.append("价格在MA60之上")
    elif ma60: tech_signals.append("价格在MA60之下")
    if dif > dea and macd_data["dif"][-2] <= macd_data["dea"][-2]: tech_signals.append("MACD金叉")
    elif dif < dea and macd_data["dif"][-2] >= macd_data["dea"][-2]: tech_signals.append("MACD死叉")
    if hist > 0: tech_signals.append("MACD柱状线为正")
    else: tech_signals.append("MACD柱状线为负")
    if rsi14 > 70: tech_signals.append("RSI超买区(>70)")
    elif rsi14 < 30: tech_signals.append("RSI超卖区(<30)")
    elif rsi14 > 50: tech_signals.append("RSI偏多")
    else: tech_signals.append("RSI偏空")
    if price > boll_upper: tech_signals.append("突破布林上轨")
    elif price < boll_lower: tech_signals.append("跌破布林下轨")

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

    # 3. 获取新闻
    news_summary = []
    try:
        from backend.screener.news import NewsCollector
        collector = NewsCollector()
        news = await collector.fetch_all(hours=48)
        symbol_lower = req.symbol.lower().replace("-usdt","").replace("-usd","")
        relevant = [n for n in news if symbol_lower in (n.get("title","")+" "+n.get("content","")).lower()][:5]
        if not relevant:
            relevant = news[:5]
        news_summary = [{"title": n.get("title",""), "source": n.get("source","")} for n in relevant]
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
                return_exceptions=True
            )
            if not isinstance(fg, Exception) and fg:
                onchain_summary["恐惧贪婪指数"] = f"{fg.get('value', '?')} ({fg.get('label_cn', fg.get('label', ''))})"
            if not isinstance(fr, Exception) and fr:
                current = fr.get("current", {})
                rate = current.get("fundingRate", current.get("rate", 0))
                onchain_summary["资金费率"] = f"{float(rate)*100:.4f}%"
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
        onchain_summary["距涨停"] = f"{((prev_close*1.1 - price)/price*100):+.2f}%"
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
            async for row in await db.execute("SELECT key, value FROM settings WHERE key LIKE 'deepseek_%' OR key LIKE 'qwen_%' OR key = 'llm_provider'"):
                llm_settings[row[0]] = row[1].strip('"') if row[1].startswith('"') else row[1]

        provider = llm_settings.get('llm_provider', 'deepseek')
        if provider == 'qwen':
            api_key = llm_settings.get('qwen_api_key', '')
            base_url = llm_settings.get('qwen_base_url', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
            model = llm_settings.get('qwen_model', 'qwen-turbo')
        else:
            api_key = llm_settings.get('deepseek_api_key', '')
            base_url = llm_settings.get('deepseek_base_url', 'https://api.deepseek.com/v1')
            model = llm_settings.get('deepseek_model', 'deepseek-chat')

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
                onchain_text = f"\n## {extra_section_title}\n" + "\n".join(f"- {k}: {v}" for k, v in onchain_summary.items())

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
{chr(10).join('- ' + s for s in tech_signals)}
{onchain_text}

## 近期相关新闻与舆情
{chr(10).join('- ' + n['title'] for n in news_summary) if news_summary else '- 无相关新闻'}

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
                client.chat.completions.create(model=model, messages=[{"role":"user","content":prompt}], max_tokens=1000, temperature=0.3),
                timeout=30
            )
            result_text = (response.choices[0].message.content or "").strip()
            # 提取JSON
            if "```" in result_text:
                lines = result_text.split("\n")
                json_lines = []
                in_block = False
                for line in lines:
                    if line.strip().startswith("```") and not in_block: in_block = True; continue
                    elif line.strip() == "```" and in_block: break
                    elif in_block: json_lines.append(line)
                result_text = "\n".join(json_lines).strip()
            if result_text and result_text[0] not in ('{','['):
                start = result_text.find('{')
                if start != -1: result_text = result_text[start:]
            llm_verdict = json.loads(result_text)
    except Exception as e:
        logger.warning(f"AI研判LLM调用失败: {e}")
        llm_verdict = {"direction": "观望", "confidence": 50, "reasoning": f"LLM分析暂不可用: {str(e)[:50]}", "risk_warning": "请自行判断"}

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
