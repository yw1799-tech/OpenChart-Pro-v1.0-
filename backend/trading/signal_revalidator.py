"""
v12.18.0 信号 4 层重验器 — 给"开市后 pending 信号"用。

设计逻辑：成本递增 4 层闸门，任一层 reject 即终止；
全部通过才返回 pass，由调用方触发实际开仓。

  Tier 1 (free, <10ms): 价差闸门
    当前价 vs 信号价偏离过大 → 行情已变 → reject
  Tier 2 (free, ~50ms): 策略重跑
    用最新 K 线重跑同一策略 → 不再触发或反向 → reject
  Tier 3 (free, ~100ms): 新闻闸门
    pending 期间出现 ★3+ 反向新闻 → reject
  Tier 4 (LLM, ~3s, $0.01): AI 重验（仅边缘案例）
    原 ai_confidence < 70 OR pending > 24h → 调 LLM
    否则跳过（高置信短窗 pending 直接放行）

返回 (tier, reason, new_ai_conf, new_ai_sl, new_ai_tp):
  tier: 'pass' / 'gap' / 'strategy' / 'news' / 'ai' / 'ai_error'
  reason: 人类可读拒/通过原因
  new_ai_*: 仅 Tier 4 跑 LLM 时非 None（覆盖原值）
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# 各市场价差阈值（pending 期间允许的最大偏离 %）
GAP_THRESHOLD_PCT = {
    "us": 3.0,
    "hk": 3.0,
    "cn": 3.0,
    "crypto": 5.0,    # 加密波动大，阈值放宽
}

# Tier 4 触发条件
AI_REVERIFY_CONF_THRESHOLD = 70    # ai_conf < 70 → 走 AI 重验
AI_REVERIFY_AGE_HOURS = 24         # pending > 24h → 走 AI 重验


async def revalidate_signal(
    db,
    sig: Dict[str, Any],
    monitor_engine=None,
) -> Tuple[str, str, Optional[int], Optional[float], Optional[float]]:
    """主入口。返回 (tier, reason, new_ai_conf, new_ai_sl, new_ai_tp)。"""
    symbol = sig["symbol"]
    market = sig["market"]
    action = sig["action"]
    signal_price = float(sig.get("price") or 0)
    strategy_name = sig.get("strategy_name") or ""
    interval = sig.get("interval") or "1H"

    # ─── Tier 1: 价差闸门 ─────────────────────────────────
    try:
        current = await _get_current_price(market, symbol)
        if current and signal_price > 0:
            gap_pct = abs(current - signal_price) / signal_price * 100
            threshold = GAP_THRESHOLD_PCT.get(market, 3.0)
            if gap_pct > threshold:
                return (
                    "gap",
                    f"价差 {gap_pct:.2f}% 超阈值 {threshold}%（信号价 {signal_price:.4f} → 当前 {current:.4f}）",
                    None, None, None,
                )
    except Exception as e:
        logger.debug(f"[reval-T1] {symbol} 价差检查异常: {e}")
        # 价格拉不到不阻塞 — 继续走下一层

    # ─── Tier 2: 策略重跑 ─────────────────────────────────
    # 跳过特殊策略：resonance（合成）/ 缺名
    if strategy_name and strategy_name not in ("resonance", "", None):
        try:
            t2_verdict, t2_reason = await _rerun_strategy(
                db, symbol, market, action, strategy_name, interval, sig,
            )
            if t2_verdict == "reject":
                return ("strategy", t2_reason, None, None, None)
            # t2_verdict == "pass" 或 "skip"（数据不足等）→ 继续
        except Exception as e:
            logger.debug(f"[reval-T2] {symbol} 策略重跑异常: {e}")

    # ─── Tier 3: 新闻闸门 ─────────────────────────────────
    try:
        t3_verdict, t3_reason = await _check_news_invalidation(db, symbol, action, sig)
        if t3_verdict == "reject":
            return ("news", t3_reason, None, None, None)
    except Exception as e:
        logger.debug(f"[reval-T3] {symbol} 新闻检查异常: {e}")

    # ─── Tier 4: AI 重验（仅边缘案例）───────────────────
    original_conf = int(sig.get("ai_confidence") or 0)
    pending_h = (time.time() * 1000 - (sig.get("generated_at") or 0)) / 3600000
    needs_ai = (original_conf < AI_REVERIFY_CONF_THRESHOLD) or (pending_h > AI_REVERIFY_AGE_HOURS)

    if not needs_ai:
        return (
            "pass",
            f"通过 Tier 1-3（原 conf={original_conf} ≥ {AI_REVERIFY_CONF_THRESHOLD} + pending {pending_h:.1f}h ≤ {AI_REVERIFY_AGE_HOURS}h，跳过 Tier 4）",
            None, None, None,
        )

    if monitor_engine is None:
        return (
            "pass",
            f"通过 Tier 1-3（无 monitor_engine 注入，跳过 Tier 4 AI 重验）",
            None, None, None,
        )

    try:
        new_conf, new_verdict, new_reason, new_sl, new_tp = await _ai_reverify(
            db, sig, monitor_engine,
        )
        if new_verdict in ("llm_error", "", None):
            return ("ai_error", f"AI 重验调用失败: {new_reason or '未知'}", None, None, None)
        if new_verdict != "confirm" or new_conf < 60:
            return (
                "ai",
                f"AI 重验失效: verdict={new_verdict} conf={new_conf}（原 conf={original_conf}）",
                new_conf, new_sl, new_tp,
            )
        return (
            "pass",
            f"通过 4 层（AI 重验 conf {original_conf}→{new_conf}）",
            new_conf, new_sl, new_tp,
        )
    except Exception as e:
        logger.warning(f"[reval-T4] {symbol} AI 重验异常: {e}")
        return ("ai_error", f"AI 重验异常: {type(e).__name__}: {e}", None, None, None)


# ═══════════════════════════════════════════════════════════════════
# Tier 实现
# ═══════════════════════════════════════════════════════════════════


async def _get_current_price(market: str, symbol: str) -> Optional[float]:
    """通过 fetcher.get_ticker() 拉当前价。"""
    try:
        from backend.data.fetcher import get_fetcher
        from backend.data.models import Market
        f = get_fetcher(Market(market))
        ticker = await f.get_ticker(symbol)
        if ticker and ticker.get("last"):
            return float(ticker["last"])
        return None
    except Exception as e:
        logger.debug(f"[reval-price] {symbol} ticker 拉取失败: {e}")
        return None


async def _rerun_strategy(
    db, symbol: str, market: str, original_action: str,
    strategy_name: str, interval: str, sig: Dict[str, Any],
) -> Tuple[str, str]:
    """重跑同一策略 — 返回 (verdict, reason)。
    verdict: 'reject' / 'pass' / 'skip'
    """
    from backend.signals.strategies import get_strategy
    from backend.data.cache import cached_get_klines
    from backend.data.models import Market, Interval

    # 拉策略 params
    params = await _get_binding_params(db, symbol, market, strategy_name, interval)
    strategy = get_strategy(strategy_name, **params)
    if not strategy:
        return ("skip", f"策略 {strategy_name} 实例化失败")

    try:
        mkt_enum = Market(market)
        iv_enum = Interval(interval)
    except ValueError:
        return ("skip", f"无效市场/周期: {market}/{interval}")

    candles = await cached_get_klines(db=db, market=mkt_enum, symbol=symbol,
                                       interval=iv_enum, limit=300)
    if not candles or len(candles) < 30:
        return ("skip", "K 线数据不足")

    # 跑策略（兼容 sync/async evaluate）
    result = strategy.evaluate(symbol, mkt_enum, candles)
    if asyncio.iscoroutine(result):
        result = await result

    if result is None:
        return ("reject", f"策略 {strategy_name} 不再触发（行情已变）")
    new_action = getattr(result, "action", None)
    if new_action != original_action:
        return ("reject", f"策略 {strategy_name} 反向触发: 原 {original_action} → 现 {new_action}")
    return ("pass", f"策略 {strategy_name} 仍触发同向 {new_action}")


async def _get_binding_params(
    db, symbol: str, market: str, strategy_name: str, interval: str
) -> Dict[str, Any]:
    """从 strategy_bindings 表拉策略 params。"""
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT params FROM strategy_bindings "
                "WHERE symbol=? AND market=? AND strategy_name=? AND interval=?",
                (symbol, market, strategy_name, interval),
            )
            row = await cur.fetchone()
            if row and row["params"]:
                return json.loads(row["params"])
    except Exception:
        pass
    return {}


async def _check_news_invalidation(
    db, symbol: str, action: str, sig: Dict[str, Any]
) -> Tuple[str, str]:
    """v12.18 检查 pending 期间是否出现反向 ★3+ 新闻。
    BUY 信号 + 利空新闻 → reject
    SELL 信号 + 利好新闻 → reject
    """
    sig_ts_ms = int(sig.get("generated_at") or 0)
    if sig_ts_ms <= 0:
        return ("skip", "无 generated_at")

    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                """SELECT id, title, importance, sentiment, published_at
                   FROM flash_news
                   WHERE published_at > ? AND categories LIKE ?
                     AND importance >= 3
                   ORDER BY published_at DESC LIMIT 20""",
                (sig_ts_ms, f'%"{symbol}"%'),
            )
            rows = await cur.fetchall()
    except Exception as e:
        logger.debug(f"[reval-news] {symbol} 查询失败: {e}")
        return ("skip", "查询新闻失败")

    contradict_keyword = "bearish" if action == "buy" else "bullish"
    for r in rows:
        sentiment = (r["sentiment"] or "").lower()
        if sentiment == contradict_keyword:
            title = (r["title"] or "")[:60]
            return (
                "reject",
                f"pending 期间出现反向新闻 ★{r['importance']} ({sentiment}): {title}",
            )
    return ("pass", "无反向 ★3+ 新闻")


async def _ai_reverify(
    db, sig: Dict[str, Any], monitor_engine,
) -> Tuple[int, str, str, Optional[float], Optional[float]]:
    """Tier 4 调用 monitor._ai_verify_signal 重新走一遍 LLM。
    返回 (new_conf, new_verdict, new_reason, new_sl, new_tp)。
    注意：_ai_verify_signal 直接 UPDATE signals 表，所以调完后从 DB 拉新值。
    """
    from backend.data.models import Market, Signal

    # 重建 Signal dataclass（_ai_verify_signal 接受 Signal 对象）
    try:
        market = Market(sig["market"])
    except ValueError:
        return (0, "llm_error", "无效市场", None, None)

    # 备份原值（在 _ai_verify_signal 覆盖之前）
    original_verdict = sig.get("ai_verdict") or ""
    original_conf = int(sig.get("ai_confidence") or 0)
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """UPDATE signals SET original_ai_verdict=COALESCE(NULLIF(original_ai_verdict,''),?),
                                       original_ai_confidence=CASE WHEN original_ai_confidence>0 THEN original_ai_confidence ELSE ? END
                   WHERE id=?""",
                (original_verdict, original_conf, sig["id"]),
            )
            await conn.commit()
    except Exception as e:
        logger.debug(f"[reval-T4] 备份原值失败: {e}")

    signal_obj = Signal(
        id=sig["id"],
        symbol=sig["symbol"],
        market=market,
        action=sig["action"],
        strategy_name=sig.get("strategy_name") or "",
        confidence=int(sig.get("confidence") or 0),
        price=float(sig.get("price") or 0),
        stop_loss=sig.get("stop_loss"),
        take_profit=sig.get("take_profit"),
        reason=sig.get("reason") or "",
        triggered_by=_safe_json_loads(sig.get("triggered_by")),
        generated_at=int(sig.get("generated_at") or 0),
    )
    # _ai_verify_signal 用 getattr fallback 取 interval
    setattr(signal_obj, "interval", sig.get("interval") or "1H")

    await monitor_engine._ai_verify_signal(signal_obj)

    # 从 DB 重读最新值
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT ai_verdict, ai_confidence, ai_reason, ai_stop_loss, ai_take_profit "
            "FROM signals WHERE id=?",
            (sig["id"],),
        )
        row = await cur.fetchone()
    if not row:
        return (0, "llm_error", "重验后查询信号失败", None, None)
    return (
        int(row["ai_confidence"] or 0),
        row["ai_verdict"] or "",
        row["ai_reason"] or "",
        row["ai_stop_loss"],
        row["ai_take_profit"],
    )


def _safe_json_loads(s):
    if not s:
        return {}
    try:
        return json.loads(s) if isinstance(s, str) else (s or {})
    except Exception:
        return {}
