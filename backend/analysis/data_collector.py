"""按需分析 — 数据收集层 (v12.22.0)

收集单个 symbol 的全维数据,供 LLM 分析师做专业判断:
  - T0 实时价格快照 (执行前用于漂移检查)
  - 5 周期 K 线 + 技术指标
  - 近 7 天相关新闻
  - 衍生品数据 (仅加密)
  - 基本面数据 (仅股票)
  - 持仓状态 (DB 实际或用户覆盖)
  - 同类对比 (仅加密 6 主流币波动)
  - missing_data 列表 (LLM prompt 据此告知用户)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── 配置常量 ────────────────────────────────────────────────────
KLINE_INTERVALS = ["15m", "1H", "4H", "1D", "1W"]
KLINE_LIMIT_PER_INTERVAL = 200
NEWS_DAYS_LOOKBACK = 7
NEWS_MAX_ITEMS = 30
PEERS_FOR_CRYPTO = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT", "DOGE-USDT"]


# ─── 主入口 ──────────────────────────────────────────────────────
async def collect_all(
    db,
    symbol: str,
    market: str,
    position_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    收集单个 symbol 的所有数据。

    参数:
      db: DatabaseManager 实例
      symbol: 标的代码 (e.g. "ETH" / "AAPL" / "0700")
      market: "crypto" | "us" | "hk" | "cn"
      position_override: 用户填的持仓信息 (覆盖 DB 实际持仓);None=用 DB 实际

    返回:
      {
          "symbol": str,
          "market": str,
          "t0_snapshot": {"price": float, "ts": str (ISO+TZ), "ts_ms": int},
          "klines": {"15m": [...candles], "1H": [...], ...},
          "indicators": {"15m": {ma5, ma20, rsi, macd, ...}, ...},
          "news": [{title, source, sentiment, ts, importance}, ...],
          "fundamentals": dict | None,    # 仅股票
          "derivatives": dict | None,     # 仅加密
          "position": dict | None,        # 持仓信息 (DB 或 override)
          "peers": list | None,           # 仅加密
          "missing_data": ["fundamentals", ...],  # 缺失项明确列出
      }
    """
    market = (market or "").lower()
    if market not in ("crypto", "us", "hk", "cn"):
        raise ValueError(f"unsupported market: {market}")

    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol cannot be empty")

    # symbol 规整 (与 backend/main.py:_normalize_symbol 同逻辑,避免循环引用直接内联):
    #   - crypto: "ETH" → "ETH-USDT" (内部统一存现货代码,合约下游再加 -SWAP)
    #   - 港股: 纯数字补足 4 位 + .HK (981 → 0981.HK, 700 → 0700.HK, 9988 → 9988.HK)
    #   - A 股: 6 位数字保持不变,去除可能的 .SH/.SZ 后缀
    #   - 美股: 大写,去除可能的 .US 后缀
    if market == "crypto":
        symbol = symbol.upper()
        if "-" not in symbol:
            symbol = f"{symbol}-USDT"
    elif market == "hk":
        s = symbol.upper().strip()
        core = s.replace(".HK", "")
        if core.isdigit():
            symbol = core.zfill(4) + ".HK"
        else:
            symbol = s if s.endswith(".HK") else s + ".HK"
    elif market == "cn":
        symbol = symbol.upper().replace(".SH", "").replace(".SZ", "")
    elif market == "us":
        symbol = symbol.upper().replace(".US", "")

    missing_data: List[str] = []

    # ─── 并行收集独立数据源 ───
    t0_task = _collect_t0_snapshot(db, symbol, market)
    klines_task = _collect_klines(db, symbol, market)
    news_task = _collect_news(db, symbol, market)
    position_task = _collect_position(db, symbol, market, position_override)

    # 仅加密
    derivatives_task: Optional[asyncio.Task] = None
    peers_task: Optional[asyncio.Task] = None
    if market == "crypto":
        derivatives_task = asyncio.create_task(_collect_derivatives(symbol))
        peers_task = asyncio.create_task(_collect_peers(db, symbol))

    # 仅股票
    fundamentals_task: Optional[asyncio.Task] = None
    if market in ("us", "hk", "cn"):
        fundamentals_task = asyncio.create_task(_collect_fundamentals(db, symbol, market))

    # ─── await 全部 ───
    t0_snapshot = await t0_task
    klines = await klines_task
    news = await news_task
    position = await position_task
    derivatives = await derivatives_task if derivatives_task else None
    peers = await peers_task if peers_task else None
    fundamentals = await fundamentals_task if fundamentals_task else None

    # ─── 计算指标 ───
    indicators = _compute_indicators(klines)

    # ─── 标记缺失数据 ───
    if not t0_snapshot.get("price"):
        missing_data.append("realtime_price")
    if not any(klines.values()):
        missing_data.append("klines")
    if not news:
        missing_data.append("news")
    if market == "crypto":
        if not derivatives or not any(derivatives.values()):
            missing_data.append("derivatives")
    if market in ("us", "hk", "cn") and not fundamentals:
        missing_data.append("fundamentals")

    return {
        "symbol": symbol,
        "market": market,
        "t0_snapshot": t0_snapshot,
        "klines": klines,
        "indicators": indicators,
        "news": news,
        "fundamentals": fundamentals,
        "derivatives": derivatives,
        "position": position,
        "peers": peers,
        "missing_data": missing_data,
        "collected_at": int(time.time()),
    }


# ─── T0 快照 ─────────────────────────────────────────────────────
async def _collect_t0_snapshot(db, symbol: str, market: str) -> Dict[str, Any]:
    """实时价格 + 时间戳 (执行前漂移检查的基准)"""
    from datetime import datetime, timezone, timedelta
    bj_tz = timezone(timedelta(hours=8))

    price: Optional[float] = None
    if market == "crypto":
        try:
            from backend.data.fetcher import get_fetcher
            from backend.data.models import Market
            okx = get_fetcher(Market.CRYPTO)
            ticker = await okx.get_ticker(symbol)
            if ticker and ticker.get("last"):
                price = float(ticker["last"])
        except Exception as e:
            logger.debug(f"[t0] crypto ticker {symbol} 失败: {e}")

    if price is None:
        # K 线兜底 (取最近 1H / 15m / 1D 任一)
        for interval in ("15m", "1H", "1D"):
            try:
                async with db.acquire() as conn:
                    cur = await conn.execute(
                        f"SELECT close FROM [klines_{market}_{interval.lower()}] "
                        "WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                        (symbol,),
                    )
                    row = await cur.fetchone()
                if row and row["close"]:
                    price = float(row["close"])
                    break
            except Exception:
                continue

    ts_ms = int(time.time() * 1000)
    ts_iso = datetime.fromtimestamp(ts_ms / 1000, bj_tz).isoformat()
    return {"price": price, "ts": ts_iso, "ts_ms": ts_ms}


# ─── K 线收集 ────────────────────────────────────────────────────
async def _collect_klines(db, symbol: str, market: str) -> Dict[str, List[Dict]]:
    """5 周期 K 线 (DB 表 klines_<market>_<interval>),每周期 200 根"""
    out: Dict[str, List[Dict]] = {}
    for interval in KLINE_INTERVALS:
        try:
            async with db.acquire() as conn:
                cur = await conn.execute(
                    f"SELECT timestamp, open, high, low, close, volume "
                    f"FROM [klines_{market}_{interval.lower()}] "
                    "WHERE symbol=? ORDER BY timestamp DESC LIMIT ?",
                    (symbol, KLINE_LIMIT_PER_INTERVAL),
                )
                rows = await cur.fetchall()
            # rows 是 desc 顺序; 反转为 asc (最旧→最新),便于指标计算
            candles = [
                {
                    "ts": r["timestamp"],
                    "o": float(r["open"]),
                    "h": float(r["high"]),
                    "l": float(r["low"]),
                    "c": float(r["close"]),
                    "v": float(r["volume"] or 0),
                }
                for r in reversed(rows)
            ]
            if candles:
                out[interval] = candles
        except Exception as e:
            logger.debug(f"[klines] {symbol}({market}) {interval} 失败: {e}")
            continue
    return out


# ─── 指标计算 ────────────────────────────────────────────────────
def _compute_indicators(klines: Dict[str, List[Dict]]) -> Dict[str, Dict[str, Any]]:
    """每周期算 MA/EMA/RSI/MACD/BOLL/ATR 当前值 (取最新 K 线对应的指标)"""
    out: Dict[str, Dict[str, Any]] = {}
    for interval, candles in klines.items():
        if len(candles) < 30:
            # 数据太少,只算能算的
            out[interval] = {"insufficient_data": True, "bar_count": len(candles)}
            continue

        try:
            from backend.indicators.builtin import (
                calc_ma, calc_ema, calc_rsi, calc_macd, calc_boll, calc_atr,
            )
        except Exception as e:
            logger.warning(f"[indicators] 导入 builtin 失败: {e}")
            return {}

        close = [c["c"] for c in candles]
        high = [c["h"] for c in candles]
        low = [c["l"] for c in candles]

        ind: Dict[str, Any] = {}
        try:
            ma5 = calc_ma(close, 5)
            ma20 = calc_ma(close, 20)
            ma60 = calc_ma(close, 60) if len(close) >= 60 else []
            ma200 = calc_ma(close, 200) if len(close) >= 200 else []
            ind["ma5"] = _last_val(ma5)
            ind["ma20"] = _last_val(ma20)
            ind["ma60"] = _last_val(ma60)
            ind["ma200"] = _last_val(ma200)
        except Exception as e:
            logger.debug(f"[indicators] {interval} MA 失败: {e}")

        try:
            ema12 = calc_ema(close, 12)
            ema26 = calc_ema(close, 26)
            ind["ema12"] = _last_val(ema12)
            ind["ema26"] = _last_val(ema26)
        except Exception:
            pass

        try:
            rsi = calc_rsi(close, 14)
            ind["rsi14"] = _last_val(rsi)
        except Exception:
            pass

        try:
            macd_d = calc_macd(close)
            ind["macd"] = _last_val(macd_d.get("dif"))
            ind["macd_signal"] = _last_val(macd_d.get("dea"))
            ind["macd_hist"] = _last_val(macd_d.get("histogram"))
        except Exception:
            pass

        try:
            boll_d = calc_boll(close)
            ind["boll_mid"] = _last_val(boll_d.get("middle"))
            ind["boll_upper"] = _last_val(boll_d.get("upper"))
            ind["boll_lower"] = _last_val(boll_d.get("lower"))
        except Exception:
            pass

        try:
            atr = calc_atr(high, low, close, 14)
            ind["atr14"] = _last_val(atr)
            cur = close[-1] if close else None
            if ind["atr14"] and cur:
                ind["atr_pct"] = round(ind["atr14"] / cur * 100, 3)
        except Exception:
            pass

        # 当前价 + 成交量统计 (供 prompt 用)
        ind["current_close"] = close[-1] if close else None
        if len(candles) >= 20:
            recent_vols = [c["v"] for c in candles[-20:]]
            avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0
            ind["avg_vol_20"] = round(avg_vol, 2)
            ind["latest_vol"] = candles[-1]["v"]
            if avg_vol > 0:
                ind["vol_ratio"] = round(candles[-1]["v"] / avg_vol, 2)

        out[interval] = ind
    return out


def _last_val(arr) -> Optional[float]:
    """取数组最后一个非空浮点值"""
    if arr is None:
        return None
    try:
        for v in reversed(arr):
            if v is not None:
                try:
                    fv = float(v)
                    if fv == fv:  # 排除 NaN
                        return round(fv, 6)
                except (TypeError, ValueError):
                    continue
    except TypeError:
        pass
    return None


# ─── 新闻收集 ────────────────────────────────────────────────────
async def _collect_news(db, symbol: str, market: str) -> List[Dict[str, Any]]:
    """近 7 天相关新闻 (按 categories 含 symbol 过滤;关键词 fallback;市场 fallback)"""
    cutoff_ms = int(time.time() * 1000) - NEWS_DAYS_LOOKBACK * 86400 * 1000
    out: List[Dict[str, Any]] = []
    seen_ids = set()
    try:
        # 1) symbol 精确过滤 (categories 字段)
        # crypto 如 "ETH-USDT" 在 categories 里通常是 "ETH-USDT" 或 "ETH"
        # 股票如 "AAPL" 直接在 categories
        symbol_keys = [symbol]
        if market == "crypto" and "-" in symbol:
            base = symbol.split("-")[0]
            symbol_keys.append(base)

        for sk in symbol_keys:
            news_items = await db.get_flash_news(
                market=None,  # 不限 market,只按 symbol
                importance_min=1,
                limit=NEWS_MAX_ITEMS,
                symbol=sk,
            )
            for n in news_items:
                if n.get("published_at", 0) < cutoff_ms:
                    continue
                if n["id"] in seen_ids:
                    continue
                seen_ids.add(n["id"])
                out.append(_normalize_news(n))

        # 2) 关键词 fallback (categories 可能未存或格式不一,按 title/content LIKE 兜底)
        # 仅在 categories 没拉到足够时才查,避免重复结果
        if len(out) < 5:
            base_kw = symbol.split("-")[0] if "-" in symbol else symbol
            kw_items = await db.get_flash_news(
                market=market if market != "crypto" else None,
                importance_min=1,
                limit=NEWS_MAX_ITEMS,
                keyword=base_kw,
            )
            for n in kw_items:
                if n.get("published_at", 0) < cutoff_ms:
                    continue
                if n["id"] in seen_ids:
                    continue
                seen_ids.add(n["id"])
                out.append(_normalize_news(n))

        # 3) 仍然没拉到 → 按 market 拉一些重要新闻做背景
        if not out:
            news_items = await db.get_flash_news(
                market=market,
                importance_min=2,
                limit=10,
            )
            for n in news_items:
                if n.get("published_at", 0) < cutoff_ms:
                    continue
                if n["id"] in seen_ids:
                    continue
                seen_ids.add(n["id"])
                out.append(_normalize_news(n))
    except Exception as e:
        logger.debug(f"[news] {symbol}({market}) 失败: {e}")

    # 按 published_at 降序,取前 30
    out.sort(key=lambda x: x.get("ts_ms", 0), reverse=True)
    return out[:NEWS_MAX_ITEMS]


def _normalize_news(n: Dict) -> Dict[str, Any]:
    """精简新闻字段 (LLM 不需要全部 raw)"""
    from datetime import datetime, timezone, timedelta
    bj_tz = timezone(timedelta(hours=8))
    ts_ms = int(n.get("published_at") or 0)
    return {
        "id": n.get("id"),
        "title": n.get("title", ""),
        "content": (n.get("content") or "")[:500],  # 截断防 prompt 爆炸
        "source": n.get("source", ""),
        "importance": int(n.get("importance") or 1),
        "sentiment": n.get("sentiment", "neutral"),
        "ts_ms": ts_ms,
        "ts": datetime.fromtimestamp(ts_ms / 1000, bj_tz).isoformat() if ts_ms else "",
    }


# ─── 衍生品数据 (仅加密) ─────────────────────────────────────────
async def _collect_derivatives(symbol: str) -> Dict[str, Any]:
    """加密衍生品: 资金费率 / 持仓量 / 多空比 / 恐惧贪婪"""
    try:
        from backend.crypto_dashboard.sentiment import SentimentData
    except Exception as e:
        logger.warning(f"[derivatives] SentimentData 导入失败: {e}")
        return {}

    sd = SentimentData()
    base = symbol.split("-")[0] if "-" in symbol else symbol  # ETH-USDT → ETH
    swap_inst = f"{base}-USDT-SWAP"

    out: Dict[str, Any] = {}

    # 真正并行: 把每个 coroutine 包成 task 后再 gather (return_exceptions=True 兜底)
    results = await asyncio.gather(
        sd.get_funding_rate(swap_inst),
        sd.get_open_interest(swap_inst),
        sd.get_long_short_ratio(base),
        sd.get_fear_greed_index(),
        return_exceptions=True,
    )
    funding, oi, lsr, fng = [
        (r if not isinstance(r, BaseException) else None) for r in results
    ]

    if funding and funding.get("source") != "error":
        cur = funding.get("current") or {}
        out["funding_rate"] = {
            "current_pct": cur.get("rate_pct"),
            "annualized_pct": cur.get("annualized_pct"),
            "next_funding_time": funding.get("next_funding_time"),
            "history_len": len(funding.get("history") or []),
        }
    if oi and oi.get("source") != "error":
        out["open_interest"] = {
            "oi": oi.get("oi"),
            "oi_ccy": oi.get("oi_ccy"),
            "ts": oi.get("timestamp"),
        }
    if lsr and lsr.get("source") != "error":
        cur = lsr.get("current") or {}
        out["long_short_ratio"] = {
            "ratio": cur.get("ratio"),
            "long_pct": cur.get("long_pct"),
            "short_pct": cur.get("short_pct"),
            "signal": lsr.get("signal"),
        }
    if fng and fng.get("source") != "error":
        out["fear_greed"] = {
            "value": fng.get("value"),
            "label": fng.get("label"),
            "label_cn": fng.get("label_cn"),
        }

    return out


# ─── 同类对比 (仅加密) ───────────────────────────────────────────
async def _collect_peers(db, symbol: str) -> List[Dict[str, Any]]:
    """6 主流币 24h 涨跌 + 当前价,给 LLM 看市场整体氛围"""
    out: List[Dict[str, Any]] = []
    base_self = symbol.split("-")[0] if "-" in symbol else symbol
    for peer in PEERS_FOR_CRYPTO:
        peer_base = peer.split("-")[0]
        if peer_base == base_self:
            continue  # 跳过自己
        try:
            # 取 1D K 线最近 2 根算涨跌
            async with db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT close FROM [klines_crypto_1d] "
                    "WHERE symbol=? ORDER BY timestamp DESC LIMIT 2",
                    (peer,),
                )
                rows = await cur.fetchall()
            if len(rows) >= 2:
                today = float(rows[0]["close"])
                yesterday = float(rows[1]["close"])
                change_pct = (today - yesterday) / yesterday * 100 if yesterday > 0 else 0
                out.append({
                    "symbol": peer,
                    "price": today,
                    "change_24h_pct": round(change_pct, 2),
                })
        except Exception:
            continue
    return out


# ─── 基本面 (仅股票) ────────────────────────────────────────────
async def _collect_fundamentals(db, symbol: str, market: str) -> Optional[Dict[str, Any]]:
    """从 symbol_fundamentals 表读已缓存的基本面 (7 天 TTL — 原表 24h 刷新,这里宽松)"""
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT * FROM symbol_fundamentals WHERE symbol=? AND market=?",
                (symbol, market),
            )
            row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        # 7 天 TTL 检查 (原表 24h 自动刷新,这里给个宽松上限避免极端陈旧数据)
        if int(time.time()) - int(d.get("updated_at") or 0) > 86400 * 7:
            return None
        return {
            "name": d.get("name", ""),
            "price": d.get("price"),
            "market_cap": d.get("market_cap"),
            "pe": d.get("pe"),
            "pb": d.get("pb"),
            "turnover_rate": d.get("turnover_rate"),
            "avg_turnover": d.get("avg_turnover"),
            "is_st": bool(d.get("is_st")),
            "updated_at": d.get("updated_at"),
        }
    except Exception as e:
        logger.debug(f"[fundamentals] {symbol}({market}) 失败: {e}")
        return None


# ─── 持仓信息 ────────────────────────────────────────────────────
async def _collect_position(
    db,
    symbol: str,
    market: str,
    override: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    优先级:
      1) override 非空 → 用用户填的
      2) DB 现货 positions 表
      3) DB 合约 swap_positions 表 (仅 crypto)
    """
    if override:
        # 用户填的:校验字段
        try:
            qty = float(override.get("quantity") or 0)
            avg = float(override.get("avg_cost") or 0)
            side = override.get("side") or "long"
            if qty > 0 and avg > 0:
                return {
                    "source": "user_override",
                    "type": "spot",  # 用户覆盖默认按现货语义
                    "side": side,
                    "quantity": qty,
                    "avg_cost": avg,
                    "stop_loss": float(override["stop_loss"]) if override.get("stop_loss") else None,
                    "take_profit": float(override["take_profit"]) if override.get("take_profit") else None,
                }
        except (TypeError, ValueError):
            pass
        return None

    # 1) 现货 positions
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT * FROM positions WHERE symbol=? AND market=?",
                (symbol, market),
            )
            row = await cur.fetchone()
        if row:
            d = dict(row)
            return {
                "source": "db_spot",
                "type": "spot",
                "id": d["id"],
                "side": d.get("side") or "long",
                "quantity": float(d["quantity"]),
                "avg_cost": float(d["avg_cost"]),
                "opened_at": d.get("opened_at"),
                "stop_loss": d.get("ai_stop_loss"),
                "take_profit": d.get("ai_take_profit"),
                "auto_traded": bool(d.get("auto_traded")),
            }
    except Exception as e:
        logger.debug(f"[position-spot] {symbol}({market}) 失败: {e}")

    # 2) 合约 swap_positions (仅加密)
    if market == "crypto":
        try:
            base = symbol.split("-")[0] if "-" in symbol else symbol
            swap_inst = f"{base}-USDT-SWAP"
            async with db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT * FROM swap_positions WHERE symbol=? AND status='open' "
                    "ORDER BY opened_at DESC LIMIT 1",
                    (swap_inst,),
                )
                row = await cur.fetchone()
            if row:
                d = dict(row)
                return {
                    "source": "db_swap",
                    "type": "swap",
                    "id": d["id"],
                    "swap_inst": d["symbol"],
                    "side": d.get("pos_side") or "long",
                    "quantity": float(d["qty"]),
                    "avg_cost": float(d["avg_open_price"]),
                    "leverage": int(d.get("leverage") or 1),
                    "margin_usd": float(d.get("margin_usd") or 0),
                    "stop_loss": d.get("stop_loss"),
                    "take_profit": d.get("take_profit"),
                    "liq_price": d.get("liq_price"),
                    "unrealized_pnl_usd": float(d.get("unrealized_pnl_usd") or 0),
                }
        except Exception as e:
            logger.debug(f"[position-swap] {symbol}({market}) 失败: {e}")

    return None
