"""
v12.15 教训采纳闭环 — 风控规则评估引擎

设计：
  - lesson_pattern + risk_rules 是数据；本模块是"执行体"
  - 每次开仓/加仓/巡检前调 evaluate_open_signal / evaluate_position_check
  - 任何一条 enabled 规则 hit → 拦截 + 写 risk_rule_hits 明细
  - 调用方根据返回值决定 reject / force_close / force_reduce

5 种 rule_type：
  1. rsi_block            — 开仓前阻拦：RSI 超买 + 距前高近
  2. drawdown_force_close — 持仓巡检：浮亏 ≥ 阈值 → 强平
  3. trend_block          — 开仓前阻拦：N 小时跌幅过门槛
  4. cooldown_override    — 同股冷却时长（覆盖 default config）
  5. prompt_principle     — 不参与硬过滤，仅注入 LLM prompt（reviewer 已处理）
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


async def load_active_rules(db, pool_id: str = None, rule_type: str = None) -> List[Dict]:
    """加载 enabled 规则。pool_id 不传 → 全部；传时返回 pool_id 匹配 + 'all' 的规则。"""
    sql = "SELECT * FROM risk_rules WHERE enabled=1"
    params: list = []
    if pool_id:
        sql += " AND pool_id IN (?, 'all')"
        params.append(pool_id)
    if rule_type:
        sql += " AND rule_type=?"
        params.append(rule_type)
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(sql, params)
            rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            try:
                r["params"] = json.loads(r.get("params") or "{}")
            except Exception:
                r["params"] = {}
        return rows
    except Exception as e:
        logger.debug(f"[risk-rules] load failed: {e}")
        return []


def _market_to_pool(market: str) -> str:
    return {"us": "us_hk", "hk": "us_hk", "cn": "cn", "crypto": "crypto"}.get(market, "us_hk")


async def _record_hit(db, rule: Dict, symbol: str, market: str, action: str,
                      signal_id: str = None, position_id: str = None,
                      price_at_hit: float = None):
    """规则命中时写明细 + 累加 hits 计数。"""
    now = int(time.time())
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """INSERT INTO risk_rule_hits
                   (rule_id, symbol, market, signal_id, position_id, action, price_at_hit, hit_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (rule["id"], symbol, market, signal_id, position_id, action, price_at_hit, now)
            )
            await conn.execute(
                "UPDATE risk_rules SET hits=hits+1, last_hit_at=? WHERE id=?",
                (now, rule["id"])
            )
            await conn.commit()
    except Exception as e:
        logger.debug(f"[risk-rules] record_hit failed: {e}")


# ─────────────────────────────────────────────────────────────────
# 评估器 1：rsi_block — RSI 超买 + 距前高近 → 拒绝开仓
# ─────────────────────────────────────────────────────────────────
async def _eval_rsi_block(db, rule: Dict, symbol: str, market: str) -> Optional[str]:
    """返回 reject 原因字符串；不命中返回 None。
    需要拉 K 线 + 计算 RSI / 20 日高。
    """
    p = rule.get("params") or {}
    max_rsi = float(p.get("max_rsi", 90))
    min_dist_pct = float(p.get("min_dist_to_high_pct", 3.0))
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                f"SELECT close, high FROM [klines_{market}_1h] "
                f"WHERE symbol=? ORDER BY timestamp DESC LIMIT 30",
                (symbol,)
            )
            rows = [dict(r) for r in await cur.fetchall()]
        if len(rows) < 14:
            return None
        closes = [r["close"] for r in rows][::-1]  # 时间正序
        # 简易 RSI (14)
        gains = []
        losses = []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0))
            losses.append(abs(min(d, 0)))
        if len(gains) < 14:
            return None
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        if avg_loss == 0:
            rsi = 100
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        cur_price = closes[-1]
        # 20 日高（取 1H 数据中最近 20 根的最高）— 精度够用
        high20 = max(r["high"] for r in rows[:20])
        dist_pct = (high20 - cur_price) / cur_price * 100 if cur_price > 0 else 999
        if rsi >= max_rsi and dist_pct < min_dist_pct:
            return f"RSI {rsi:.1f}≥{max_rsi} 且距前高 {dist_pct:.2f}%<{min_dist_pct}%"
    except Exception as e:
        logger.debug(f"[rsi_block] eval failed {symbol}: {e}")
    return None


# ─────────────────────────────────────────────────────────────────
# 评估器 2：drawdown_force_close — 持仓浮亏≥阈值 → 强平/强减
# ─────────────────────────────────────────────────────────────────
async def _eval_drawdown_force_close(db, rule: Dict, position: Dict,
                                     pnl_pct: float) -> Optional[Tuple[str, str]]:
    """返回 (action, reason) 或 None；action ∈ {'force_close', 'force_reduce_50'}"""
    p = rule.get("params") or {}
    threshold = float(p.get("loss_threshold_pct", 8.0))
    action_kind = p.get("action", "close")
    if pnl_pct >= -threshold:
        return None  # 浮亏未到阈值
    if action_kind == "close":
        return ("force_close", f"浮亏 {pnl_pct:.2f}%≤-{threshold}%（教训规则强平）")
    elif action_kind == "reduce_50":
        return ("force_reduce_50", f"浮亏 {pnl_pct:.2f}%≤-{threshold}%（教训规则减半）")
    return None


# ─────────────────────────────────────────────────────────────────
# 评估器 3：trend_block — N 小时跌幅过门槛 → 拒绝开仓
# ─────────────────────────────────────────────────────────────────
async def _eval_trend_block(db, rule: Dict, symbol: str, market: str) -> Optional[str]:
    p = rule.get("params") or {}
    lookback_h = int(p.get("lookback_h", 4))
    max_drop_pct = float(p.get("max_drop_pct", -3.0))
    try:
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - lookback_h * 3600 * 1000
        async with db.acquire() as conn:
            cur = await conn.execute(
                f"SELECT close FROM [klines_{market}_1h] "
                f"WHERE symbol=? AND timestamp>=? ORDER BY timestamp ASC LIMIT 1",
                (symbol, cutoff_ms),
            )
            first = await cur.fetchone()
            cur = await conn.execute(
                f"SELECT close FROM [klines_{market}_1h] "
                f"WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            )
            last = await cur.fetchone()
        if not first or not last or not first["close"]:
            return None
        trend = (last["close"] - first["close"]) / first["close"] * 100
        if trend < max_drop_pct:
            return f"{lookback_h}h 跌幅 {trend:.2f}%<{max_drop_pct}%（教训规则下跌趋势拦截）"
    except Exception as e:
        logger.debug(f"[trend_block] eval failed {symbol}: {e}")
    return None


# ─────────────────────────────────────────────────────────────────
# 评估器 4：cooldown_override — 返回该 (symbol, market) 应用的冷却秒数
# ─────────────────────────────────────────────────────────────────
async def get_cooldown_override(db, market: str) -> Optional[int]:
    """优先级：market 完全匹配 > 'all'；多条同优先级取最大秒数。"""
    rules = await load_active_rules(db, pool_id=_market_to_pool(market), rule_type="cooldown_override")
    matched_secs = []
    for r in rules:
        p = r.get("params") or {}
        rule_market = p.get("market", "all")
        if rule_market in (market, "all"):
            matched_secs.append(int(p.get("cooldown_sec", 0)))
    return max(matched_secs) if matched_secs else None


# ─────────────────────────────────────────────────────────────────
# 主入口 1：开仓信号评估（fast_path / simplified_verify / _handle_signal 用）
# ─────────────────────────────────────────────────────────────────
async def evaluate_open_signal(db, symbol: str, market: str,
                               signal_id: str = None) -> Optional[Tuple[Dict, str]]:
    """开仓信号通过教训规则评估；命中任一规则返回 (rule, reason)；通过返回 None。"""
    pool_id = _market_to_pool(market)
    rules = await load_active_rules(db, pool_id=pool_id)
    # v12.15.2 修复 Bug1：拉 price_at_hit 让 review_pending_hits 后续能判定 false_reject
    # 之前 _record_hit 收到 None → 直接标 false_reject=0 → 自动 disable 永不触发
    price_at_hit = None
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                f"SELECT close FROM [klines_{market}_1h] "
                f"WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            )
            row = await cur.fetchone()
        if row and row["close"]:
            price_at_hit = float(row["close"])
    except Exception as e:
        logger.debug(f"[evaluate_open_signal] price_at_hit 拉取失败 {symbol}: {e}")
    for rule in rules:
        rt = rule.get("rule_type")
        if rt == "rsi_block":
            reason = await _eval_rsi_block(db, rule, symbol, market)
            if reason:
                await _record_hit(db, rule, symbol, market, "reject_signal",
                                  signal_id=signal_id, price_at_hit=price_at_hit)
                return rule, f"[教训规则:rsi_block] {reason}"
        elif rt == "trend_block":
            reason = await _eval_trend_block(db, rule, symbol, market)
            if reason:
                await _record_hit(db, rule, symbol, market, "reject_signal",
                                  signal_id=signal_id, price_at_hit=price_at_hit)
                return rule, f"[教训规则:trend_block] {reason}"
    return None


# ─────────────────────────────────────────────────────────────────
# 主入口 2：持仓巡检评估（auto_trader._tp_sl_scan_once 用）
# ─────────────────────────────────────────────────────────────────
async def evaluate_position_check(db, position: Dict, market: str,
                                  pnl_pct: float) -> Optional[Tuple[Dict, str, str]]:
    """对持仓巡检评估教训规则；命中返回 (rule, action_kind, reason)。
    action_kind ∈ {'force_close', 'force_reduce_50'}
    """
    pool_id = _market_to_pool(market)
    rules = await load_active_rules(db, pool_id=pool_id, rule_type="drawdown_force_close")
    for rule in rules:
        out = await _eval_drawdown_force_close(db, rule, position, pnl_pct)
        if out:
            action, reason = out
            await _record_hit(
                db, rule, position.get("symbol"), market, action,
                position_id=position.get("id"),
                price_at_hit=position.get("current_price"),
            )
            return rule, action, f"[教训规则:drawdown_force_close] {reason}"
    return None


# ─────────────────────────────────────────────────────────────────
# Seed 初始化：把现有 hardcoded 规则迁移到 risk_rules 表
# 启动时调用一次（idempotent — 已存在不重写）
# ─────────────────────────────────────────────────────────────────
DEFAULT_SEEDS = [
    {
        "rule_type": "trend_block",
        "pool_id": "us_hk",
        "params": {"lookback_h": 4, "max_drop_pct": -3.0},
        "description": "美/港股 4h 跌幅 ≥ 3% → 拒绝 buy 信号（VIAV 当晚已跌 -9% 仍 confirm 教训）",
    },
    {
        "rule_type": "trend_block",
        "pool_id": "cn",
        "params": {"lookback_h": 4, "max_drop_pct": -3.0},
        "description": "A 股 4h 跌幅 ≥ 3% → 拒绝 buy 信号",
    },
    {
        "rule_type": "drawdown_force_close",
        "pool_id": "all",
        "params": {"loss_threshold_pct": 10.0, "action": "close"},
        "description": "浮亏 ≥ 10% 强制平仓（防 AI 沉没成本式 hold；QCOM#2 -3.58% SL 被动止损教训放严版）",
    },
    {
        "rule_type": "cooldown_override",
        "pool_id": "us_hk",
        "params": {"market": "us", "cooldown_sec": 3600},
        "description": "美股同股冷却 1h（QCOM 33min 二开亏 -3.58% 教训）",
    },
    {
        "rule_type": "cooldown_override",
        "pool_id": "us_hk",
        "params": {"market": "hk", "cooldown_sec": 3600},
        "description": "港股同股冷却 1h",
    },
    # P2 修复 (审计 #26): crypto pool 之前缺默认 trend_block 种子,
    # 加密 24/7 + 高波动比股票更需要趋势拒单 (例: 4h -5% 跌后 buy 信号常被噪音骗)
    {
        "rule_type": "trend_block",
        "pool_id": "crypto",
        "params": {"lookback_h": 4, "max_drop_pct": -5.0},
        "description": "加密 4h 跌幅 ≥ 5% → 拒绝 buy 信号 (24/7 高波动需更高阈值, 防左侧追多)",
    },
    {
        "rule_type": "cooldown_override",
        "pool_id": "crypto",
        "params": {"market": "crypto", "cooldown_sec": 1800},
        "description": "加密同币种冷却 30min (24/7 不同于股票交易时段限制,30min 足够)",
    },
]


async def seed_default_rules(db):
    """启动时调一次；把内置硬规则迁移到 risk_rules 表。已存在的 description 不覆盖。"""
    now = int(time.time())
    inserted = 0
    try:
        async with db.acquire() as conn:
            for seed in DEFAULT_SEEDS:
                # 是否已存在同 rule_type + pool_id + params 的规则？(用 description 兜底匹配)
                cur = await conn.execute(
                    "SELECT id FROM risk_rules WHERE rule_type=? AND pool_id=? AND description=?",
                    (seed["rule_type"], seed["pool_id"], seed["description"])
                )
                existing = await cur.fetchone()
                if existing:
                    continue
                await conn.execute(
                    """INSERT INTO risk_rules
                       (rule_type, pool_id, params, source_kind, description,
                        enabled, created_at, updated_at)
                       VALUES (?, ?, ?, 'migrated', ?, 1, ?, ?)""",
                    (seed["rule_type"], seed["pool_id"], json.dumps(seed["params"]),
                     seed["description"], now, now)
                )
                inserted += 1
            await conn.commit()
        if inserted > 0:
            logger.info(f"[risk-rules] 启动 seed — 新增 {inserted} 条默认规则")
    except Exception as e:
        logger.warning(f"[risk-rules] seed 失败: {e}")


# ─────────────────────────────────────────────────────────────────
# False reject 后台监控 — 回查 K 线判定规则命中是否"误拦"
# ─────────────────────────────────────────────────────────────────
async def review_pending_hits(db):
    """v12.15 扫所有 false_reject IS NULL 且 hit_at < now-2h 的命中记录。
    回查 K 线：命中后 2h 价格变化决定是否"假阳"
      - reject_signal: 拦截后 2h 涨 ≥1.5% → 假阳（不拦应该赢）
      - force_close / force_reduce: 强平后 2h 涨 ≥3% → 假阳（不强平能继续涨）
    更新 risk_rule_hits.false_reject + 累加 risk_rules.false_reject_count
    """
    now_ts = int(time.time())
    cutoff_review = now_ts - 2 * 3600  # 命中 2h+ 前的可以回查
    cutoff_lookback = now_ts - 7 * 86400  # 仅回查近 7 天，老数据无意义
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                """SELECT id, rule_id, symbol, market, action, price_at_hit, hit_at
                   FROM risk_rule_hits
                   WHERE false_reject IS NULL AND hit_at < ? AND hit_at > ?
                   ORDER BY hit_at ASC LIMIT 100""",
                (cutoff_review, cutoff_lookback),
            )
            hits = [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        logger.debug(f"[review-hits] 查询失败: {e}")
        return 0
    if not hits:
        return 0
    reviewed = 0
    for h in hits:
        symbol = h["symbol"]
        market = h["market"]
        if market not in ("us", "hk", "cn", "crypto"):
            continue
        price_at = h.get("price_at_hit")
        if not price_at or price_at <= 0:
            # 没有 price_at_hit → 无法判定，标 0 跳过
            try:
                async with db.acquire() as conn:
                    await conn.execute(
                        "UPDATE risk_rule_hits SET false_reject=0, reviewed_at=? WHERE id=?",
                        (now_ts, h["id"]),
                    )
                    await conn.commit()
            except Exception:
                pass
            continue
        # 拉命中后 2h 内的最高价（如果是 reject buy，看后续是否涨）
        target_ts_ms = (h["hit_at"] + 2 * 3600) * 1000
        try:
            async with db.acquire() as conn:
                cur = await conn.execute(
                    f"SELECT MAX(high) AS hi, MIN(low) AS lo FROM [klines_{market}_15m] "
                    f"WHERE symbol=? AND timestamp BETWEEN ? AND ?",
                    (symbol, h["hit_at"] * 1000, target_ts_ms),
                )
                row = await cur.fetchone()
            if not row or row["hi"] is None:
                continue
            hi = float(row["hi"])
            lo = float(row["lo"])
        except Exception as e:
            logger.debug(f"[review-hits] {symbol} K 线查询失败: {e}")
            continue
        # 判定假阳
        false_reject = 0
        if h["action"] == "reject_signal":
            # 买入信号被拦：如果后续 2h 涨 ≥1.5%，算假阳
            if hi > price_at * 1.015:
                false_reject = 1
        elif h["action"] in ("force_close", "force_reduce_50"):
            # 强平/强减：如果后续 2h 仍涨 ≥3%，算假阳（早卖了）
            if hi > price_at * 1.03:
                false_reject = 1
        try:
            async with db.acquire() as conn:
                await conn.execute(
                    "UPDATE risk_rule_hits SET false_reject=?, reviewed_at=? WHERE id=?",
                    (false_reject, now_ts, h["id"]),
                )
                if false_reject:
                    await conn.execute(
                        "UPDATE risk_rules SET false_reject_count=false_reject_count+1 WHERE id=?",
                        (h["rule_id"],),
                    )
                await conn.commit()
            reviewed += 1
        except Exception as e:
            logger.debug(f"[review-hits] update failed: {e}")
    if reviewed > 0:
        logger.info(f"[review-hits] 回查 {reviewed} 条命中")
    # 自动 disable：false_reject_rate > 30% 且 hits ≥ 5
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                """SELECT id, hits, false_reject_count, description FROM risk_rules
                   WHERE enabled=1 AND hits >= 5 AND
                         (CAST(false_reject_count AS REAL) / hits) > 0.30"""
            )
            bad = [dict(r) for r in await cur.fetchall()]
            for r in bad:
                rate = r["false_reject_count"] / r["hits"] * 100
                await conn.execute(
                    "UPDATE risk_rules SET enabled=0, updated_at=? WHERE id=?",
                    (now_ts, r["id"]),
                )
                logger.warning(
                    f"[risk-rules] 自动禁用规则 #{r['id']}（{r['description'][:40]}）— "
                    f"假阳率 {rate:.1f}% ({r['false_reject_count']}/{r['hits']})"
                )
            await conn.commit()
    except Exception as e:
        logger.debug(f"[review-hits] 自动禁用扫描失败: {e}")
    return reviewed


async def review_hits_loop(db, interval_sec: int = 1800):
    """每 30 min 跑一次回查 + 自动禁用扫描。"""
    import asyncio
    await asyncio.sleep(120)  # 启动 2min 后再开始
    while True:
        try:
            await review_pending_hits(db)
        except Exception as e:
            logger.warning(f"[review-hits-loop] 异常: {e}")
        await asyncio.sleep(interval_sec)
