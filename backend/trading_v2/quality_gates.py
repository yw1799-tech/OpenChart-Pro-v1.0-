"""
Layer 1: 质量门 (v12.23.0 Phase 1)

设计哲学: "宁可错过 100 个平庸机会, 不要错入 10 个 trap"

5 道门 (顺序短路):
  Gate 1: 位置门      — 信号价距 20 日高 < 5pct → reject (防追高)
  Gate 2: 已涨幅度门  — 当日开盘到信号触发 涨幅 > 4pct → reject (防末端追入)
  Gate 3: 风险回报门  — AI SL/TP 都给出 且 R:R ≥ 1.5 → 否则 reject
  Gate 4: 大盘环境门  — 美股 SPY 当日跌 > 1.5pct → 美股 BUY 全部 reject
  Gate 5: 财报临近门  — 距下次财报 < 3 天 → reject (财报波动大易扫损)
                       (Phase 1 stub, Phase 2 接入财报日历后启用)

每道门返回 (passed: bool, reason: str). reason 用于 reject 日志.

阈值初值: 基于 7 天小样本直觉 + 业界惯例.
将由 Phase 0 数据回测 (30 天) 校准.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 阈值常量 (Phase 0 数据出来后调优)
# ═══════════════════════════════════════════════════════════════════

# Gate 1: 位置门
# v12.23.1 审计 P1: 0.05 默认
# v12.24.3 误改 0.03 → v12.24.4 回退 0.05
#   重新分桶 30 天数据发现 3-5% 区间是亏区 (-0.05% 均), 5% 阈值才正确
#   把 3% 5% 混桶时误读 (24 笔均 0.11% 看似"勉强赚", 拆开后 <3% 微赚 / 3-5% 微亏)
DIST_TO_HIGH_MIN_PCT = 0.05

# Gate 2: 已涨幅度门
# 30 天仅触发 4/42 = 10% 笔, 阈值合理
INTRADAY_PUMP_MAX_PCT = 0.04

# Gate 3: 风险回报门
# v12.23.1 审计 P1: 1.5 → 3.0 (旧 SL 抗噪 ≥ 2.5% + TP 25% 默认 → R:R = 10:1, 1.5 形同虚设)
# v12.24.3 误改 3.0 → 2.0 → v12.24.4 回退 3.0
#   重新分桶 30 天数据发现 R:R 2.0-3.0 区间是亏区 (-0.30% 均, 9 笔)
#   把 R:R<3.0 混桶时误读 (25 笔均 0.51% 看似"赚", 拆开后 <2.0 赚 / 2.0-3.0 亏)
#   R:R ≥ 5.0 的 2 笔仍全亏 (-1.02%) → AI 给的高 R:R 不可靠 (考虑后续上限化)
MIN_RISK_REWARD = 3.0

# Gate 4: 大盘环境门
# v12.23.1 审计 P1: -1.5% → -2.0% (SPY 日内 1% 波动家常便饭, 1.5% 阈值偏严)
# 30 天 SPY 没跌过 -2%, 触发 0 次, 数据样本不足无法校准
SPY_DROP_THRESHOLD_PCT = -0.020

# Gate 5: 财报临近门 (Phase 1 stub, Phase 2 接入财报日历后启用)
EARNINGS_BLACKOUT_DAYS = 3

# 市场白名单 (防 SQL 注入双保险, signal.market 来源是枚举但显式校验更稳妥)
ALLOWED_MARKETS = frozenset(["us", "hk", "cn", "crypto"])


# ═══════════════════════════════════════════════════════════════════
# 主入口: 串行评估 5 道门, 短路返回
# ═══════════════════════════════════════════════════════════════════


async def run_all_gates(
    db,
    signal: Dict[str, Any],
    ctx: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """对一个 BUY 信号串行跑 5 道门, 任一 fail 立即返回.

    Args:
        db: DatabaseManager
        signal: signals 表的 row dict (含 symbol/market/price/strategy_name 等)
        ctx: 上下文 (可选, 含预拉的 K 线/SPY 等, 避免重复查 DB)

    Returns:
        (passed, gate_name, metadata)
        passed=True → 全过, gate_name='all_pass', metadata={...各门 metric}
        passed=False → 第一道 fail 的门名, reason 在 metadata['reason']
    """
    if (signal.get("action") or "").lower() != "buy":
        # 暂只评 BUY (短信号路径不动)
        return True, "not_buy_skip", {}

    symbol = signal.get("symbol", "")
    market = signal.get("market", "")
    price = float(signal.get("price") or 0)
    if not symbol or not market or price <= 0:
        return False, "invalid_signal", {"reason": "信号字段不全"}

    metadata: Dict[str, Any] = {"symbol": symbol, "market": market, "price": price}

    # Gate 1: 位置门
    g1 = await _gate_position(db, symbol, market, price)
    metadata["gate_1_position"] = g1
    if not g1["passed"]:
        return False, "gate_1_position", {**metadata, "reason": g1["reason"]}

    # Gate 2: 已涨幅度门
    g2 = await _gate_intraday_pump(db, symbol, market, price)
    metadata["gate_2_pump"] = g2
    if not g2["passed"]:
        return False, "gate_2_pump", {**metadata, "reason": g2["reason"]}

    # Gate 3: 风险回报门 (用 signal 自带 SL/TP)
    g3 = _gate_risk_reward(signal)
    metadata["gate_3_rr"] = g3
    if not g3["passed"]:
        return False, "gate_3_rr", {**metadata, "reason": g3["reason"]}

    # Gate 4: 大盘环境门 (仅美股)
    if market == "us":
        g4 = await _gate_market_regime(db)
        metadata["gate_4_spy"] = g4
        if not g4["passed"]:
            return False, "gate_4_spy", {**metadata, "reason": g4["reason"]}

    # Gate 5: 财报临近门 (Phase 1 stub, 永远 pass)
    g5 = _gate_earnings_blackout(symbol, market)
    metadata["gate_5_earnings"] = g5
    if not g5["passed"]:
        return False, "gate_5_earnings", {**metadata, "reason": g5["reason"]}

    return True, "all_pass", metadata


# ═══════════════════════════════════════════════════════════════════
# Gate 1: 位置门
# ═══════════════════════════════════════════════════════════════════


async def _gate_position(db, symbol: str, market: str, price: float) -> Dict[str, Any]:
    """信号价距 20 日高 < DIST_TO_HIGH_MIN_PCT (默认 5pct) → 拒."""
    if market not in ALLOWED_MARKETS:
        return {"passed": True, "reason": f"未知市场 {market},跳过", "skipped": True}
    try:
        async with db.acquire() as conn:
            cutoff_ms = int(time.time() * 1000) - 20 * 86400 * 1000
            cur = await conn.execute(
                f"SELECT MAX(high) AS hi, MIN(low) AS lo FROM [klines_{market}_1d] "
                "WHERE symbol=? AND timestamp >= ?",
                (symbol, cutoff_ms),
            )
            row = await cur.fetchone()
        if not row or not row["hi"]:
            # K 线缺失 → 谨慎放过 (不阻断, 但记录)
            return {"passed": True, "reason": "K线缺失,跳过位置门", "skipped": True}
        hi20 = float(row["hi"])
        if hi20 <= 0:
            return {"passed": True, "reason": "hi20=0,跳过", "skipped": True}
        dist_pct = (hi20 - price) / hi20  # 距离 20 日高的百分比 (越大越安全)
        passed = dist_pct >= DIST_TO_HIGH_MIN_PCT
        reason = (
            f"距20日高 {dist_pct*100:.2f}% < {DIST_TO_HIGH_MIN_PCT*100:.0f}% (追高)"
            if not passed else "ok"
        )
        return {
            "passed": passed,
            "reason": reason,
            "hi20": round(hi20, 4),
            "dist_pct": round(dist_pct, 4),
        }
    except Exception as e:
        logger.debug(f"[gate-1] {symbol}({market}) 异常: {e}")
        return {"passed": True, "reason": f"异常跳过: {e}", "skipped": True}


# ═══════════════════════════════════════════════════════════════════
# Gate 2: 已涨幅度门
# ═══════════════════════════════════════════════════════════════════


async def _gate_intraday_pump(db, symbol: str, market: str, price: float) -> Dict[str, Any]:
    """当日开盘到信号触发涨幅 > INTRADAY_PUMP_MAX_PCT (默认 4pct) → 拒.

    简化版: 用最新 1D K 线的 open 字段作为"当日开盘价".
    更精确版本 (Phase 2): 用 1H 或 15m K 线算精确"信号前 30min 涨幅".
    """
    if market not in ALLOWED_MARKETS:
        return {"passed": True, "reason": f"未知市场 {market},跳过", "skipped": True}
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                f"SELECT open AS day_open FROM [klines_{market}_1d] "
                "WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            )
            row = await cur.fetchone()
        if not row or not row["day_open"]:
            return {"passed": True, "reason": "无当日开盘价,跳过", "skipped": True}
        day_open = float(row["day_open"])
        if day_open <= 0:
            return {"passed": True, "reason": "day_open=0,跳过", "skipped": True}
        pump_pct = (price - day_open) / day_open
        passed = pump_pct <= INTRADAY_PUMP_MAX_PCT
        reason = (
            f"当日已涨 {pump_pct*100:.2f}% > {INTRADAY_PUMP_MAX_PCT*100:.0f}% (动量末端)"
            if not passed else "ok"
        )
        return {
            "passed": passed,
            "reason": reason,
            "day_open": round(day_open, 4),
            "pump_pct": round(pump_pct, 4),
        }
    except Exception as e:
        logger.debug(f"[gate-2] {symbol}({market}) 异常: {e}")
        return {"passed": True, "reason": f"异常跳过: {e}", "skipped": True}


# ═══════════════════════════════════════════════════════════════════
# Gate 3: 风险回报门
# ═══════════════════════════════════════════════════════════════════


def _gate_risk_reward(signal: Dict[str, Any]) -> Dict[str, Any]:
    """R:R 评估 — 优先用 AI 验证后的 ai_stop_loss/ai_take_profit, 退化用策略原始 stop_loss/take_profit.

    审计 P0 修复 (v12.23.1):
      - 字段优先级: ai_stop_loss > stop_loss (生产路径用 ai_*, 策略原始字段大量为 None)
      - 缺 SL/TP 时改为 passed=True + skipped=True (与 gate_1/2 K 线缺失一致),
        而不是 reject — 因为 chanlun 等策略 by design 不带 SL/TP, 一刀切 reject 会
        100% 错杀这类策略. SL/TP 兜底由后续 _execute_open 的 v12.22.6 逻辑负责.
      - R:R 阈值 1.5 → 3.0 (审计 P1 — 1.5 在 v12.22.6 兜底 (SL 2.5%, TP 25%) 下名存实亡)
    """
    # 优先 AI 给的, 退化策略原始
    sl = signal.get("ai_stop_loss")
    if sl is None:
        sl = signal.get("stop_loss")
    tp = signal.get("ai_take_profit")
    if tp is None:
        tp = signal.get("take_profit")
    price = float(signal.get("price") or 0)

    try:
        sl_f = float(sl) if sl is not None else None
        tp_f = float(tp) if tp is not None else None
    except (TypeError, ValueError):
        return {"passed": True, "reason": "SL/TP 字段类型错误,跳过", "skipped": True}

    if sl_f is None or sl_f <= 0 or tp_f is None or tp_f <= 0:
        # 缺 SL/TP — 跳过本门 (chanlun 等策略 by design 无 SL/TP, 兜底由 _execute_open 处理)
        return {
            "passed": True,
            "reason": "SL/TP 缺失,跳过 R:R 评估 (兜底由开仓时 SL 抗噪机制处理)",
            "skipped": True,
            "ai_sl": sl_f, "ai_tp": tp_f,
        }
    if price <= 0:
        return {"passed": True, "reason": "信号价 0,跳过", "skipped": True}

    risk = price - sl_f
    reward = tp_f - price
    if risk <= 0 or reward <= 0:
        return {
            "passed": False,
            "reason": f"SL/TP 方向不合理 (risk={risk:.4f}, reward={reward:.4f})",
            "ai_sl": sl_f, "ai_tp": tp_f,
        }
    rr = reward / risk
    passed = rr >= MIN_RISK_REWARD
    reason = (
        f"R:R {rr:.2f} < {MIN_RISK_REWARD} — 性价比不足"
        if not passed else "ok"
    )
    return {
        "passed": passed,
        "reason": reason,
        "rr": round(rr, 2),
        "risk_pct": round(risk / price * 100, 2),
        "reward_pct": round(reward / price * 100, 2),
    }


# ═══════════════════════════════════════════════════════════════════
# Gate 4: 大盘环境门
# ═══════════════════════════════════════════════════════════════════


async def _gate_market_regime(db) -> Dict[str, Any]:
    """美股 SPY 当日跌 > SPY_DROP_THRESHOLD_PCT (默认 -1.5pct) → 美股 BUY 全部拒.

    SPY = SPDR S&P 500 ETF, 反映美股大盘.
    """
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT open, close FROM [klines_us_1d] "
                "WHERE symbol='SPY' ORDER BY timestamp DESC LIMIT 1"
            )
            row = await cur.fetchone()
        if not row or not row["open"]:
            return {"passed": True, "reason": "SPY K 线缺失,跳过", "skipped": True}
        spy_open = float(row["open"])
        spy_close = float(row["close"])
        if spy_open <= 0:
            return {"passed": True, "reason": "SPY open=0,跳过", "skipped": True}
        spy_change = (spy_close - spy_open) / spy_open
        passed = spy_change >= SPY_DROP_THRESHOLD_PCT
        reason = (
            f"SPY 当日 {spy_change*100:.2f}% < {SPY_DROP_THRESHOLD_PCT*100:.1f}% (大盘恐慌,暂停 BUY)"
            if not passed else "ok"
        )
        return {
            "passed": passed,
            "reason": reason,
            "spy_change_pct": round(spy_change * 100, 2),
        }
    except Exception as e:
        logger.debug(f"[gate-4] 异常: {e}")
        return {"passed": True, "reason": f"异常跳过: {e}", "skipped": True}


# ═══════════════════════════════════════════════════════════════════
# Gate 5: 财报临近门 (Phase 1 stub)
# ═══════════════════════════════════════════════════════════════════


def _gate_earnings_blackout(symbol: str, market: str) -> Dict[str, Any]:
    """距下次财报 < EARNINGS_BLACKOUT_DAYS (默认 3 天) → 拒.

    Phase 1 stub: 暂不接入财报日历, 永远 pass.
    Phase 2 待办:
      - 美股: yfinance 或 finnhub 拉取 earnings_calendar
      - 港股 / A 股: 东方财富 / Wind 等
      - 缓存到 symbol_earnings 表 (24h TTL)
    """
    return {
        "passed": True,
        "reason": "stub_phase1_pass",
        "skipped": True,
    }
