"""
宏观数据影响分析引擎（PRD F5B / Phase 3B）。

职责：
  - 识别 flash_news 中的宏观数据发布（CPI / FOMC 利率决议 / NFP 非农 / PPI / GDP 等）
  - 解析实际值 / 预期值，计算偏差
  - 按偏差等级映射到影响品种（黄金、美股、美元、加密）
  - 生成 macro_impact 记录 + WS 推送

阈值（config.py）：
  |偏差| < MACRO_DEVIATION_NEUTRAL(0.5%)        → 中性
  MACRO_DEVIATION_NEUTRAL ≤ |偏差| < MACRO_DEVIATION_LIGHT(1%)  → 轻微
  |偏差| ≥ MACRO_DEVIATION_LIGHT(1%)             → 明显

识别规则：macro_keywords + regex 提取数字。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import backend.config as config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 宏观事件识别规则
# ═══════════════════════════════════════════════════════════════════

# event_key → {keywords, impact_map}
# impact_map: 偏差方向 → [symbol, direction, strength]
MACRO_EVENTS = {
    "cpi": {
        "keywords": ["CPI", "消费者物价", "消费价格指数", "通胀数据"],
        "hawkish_impacts": [
            ("GLD", "bearish", 0.6),     # 黄金短期承压
            ("QQQ", "bearish", 0.5),     # 科技股承压
            ("SPY", "bearish", 0.4),
            ("BTC-USDT", "bearish", 0.5),
            ("TLT", "bearish", 0.7),     # 长债承压
            ("UUP", "bullish", 0.5),     # 美元走强
        ],
        "dovish_impacts": [
            ("GLD", "bullish", 0.6),
            ("QQQ", "bullish", 0.5),
            ("SPY", "bullish", 0.4),
            ("BTC-USDT", "bullish", 0.5),
            ("TLT", "bullish", 0.7),
            ("UUP", "bearish", 0.5),
        ],
    },
    "fomc": {
        "keywords": ["FOMC", "美联储利率决议", "加息", "降息", "联储利率"],
        "hawkish_impacts": [
            ("GLD", "bearish", 0.7),
            ("QQQ", "bearish", 0.6),
            ("SPY", "bearish", 0.5),       # v11.5 补漏：原表少 SPY
            ("BTC-USDT", "bearish", 0.6),
            ("TLT", "bearish", 0.8),
            ("UUP", "bullish", 0.7),
        ],
        "dovish_impacts": [
            ("GLD", "bullish", 0.7),
            ("QQQ", "bullish", 0.6),
            ("SPY", "bullish", 0.5),
            ("BTC-USDT", "bullish", 0.6),
            ("TLT", "bullish", 0.8),
            ("UUP", "bearish", 0.7),
        ],
    },
    "nfp": {
        "keywords": ["非农", "NFP", "就业报告", "Nonfarm Payrolls"],
        "hawkish_impacts": [  # 超预期强劲就业 → 加息预期上升
            ("GLD", "bearish", 0.5),
            ("TLT", "bearish", 0.6),
            ("UUP", "bullish", 0.6),
            ("SPY", "neutral", 0.3),
        ],
        "dovish_impacts": [  # 就业疲软 → 降息预期上升
            ("GLD", "bullish", 0.5),
            ("QQQ", "bullish", 0.5),
            ("BTC-USDT", "bullish", 0.4),
            ("TLT", "bullish", 0.6),
        ],
    },
    "ppi": {
        "keywords": ["PPI", "生产者物价", "工业品出厂价格"],
        "hawkish_impacts": [
            ("GLD", "bearish", 0.4),
            ("QQQ", "bearish", 0.3),
            ("TLT", "bearish", 0.5),
        ],
        "dovish_impacts": [
            ("QQQ", "bullish", 0.3),
            ("TLT", "bullish", 0.5),
        ],
    },
    "gdp": {
        "keywords": ["GDP", "国内生产总值", "经济增速"],
        "hawkish_impacts": [
            ("SPY", "bullish", 0.4),
            ("QQQ", "bullish", 0.3),
        ],
        "dovish_impacts": [
            ("SPY", "bearish", 0.5),
            ("GLD", "bullish", 0.4),
            ("TLT", "bullish", 0.5),
        ],
    },
}


# ═══════════════════════════════════════════════════════════════════
# 数值解析
# ═══════════════════════════════════════════════════════════════════


NUMBER_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%?")

# 常见模式：
#   "4月CPI实际值3.2%，预期3.1%"
#   "非农就业人数 25.3 万 vs 预期 20 万"
#   "FOMC 加息 25 个基点，预期 25 个基点"
_PATTERNS = [
    # 中文：实际/公布 X / 预期 Y（{0,80}? 防止回溯超时）
    re.compile(r"实际\s*[:：值]?\s*(-?\d+\.?\d*)\s*%?[^,，;；]{0,80}?预期\s*[:：值]?\s*(-?\d+\.?\d*)"),
    re.compile(r"公布\s*[:：值]?\s*(-?\d+\.?\d*)\s*%?[^,，;；]{0,80}?预期\s*[:：值]?\s*(-?\d+\.?\d*)"),
    re.compile(r"录得\s*(-?\d+\.?\d*)\s*%?[^,，;；]{0,80}?预期\s*[:：]?\s*(-?\d+\.?\d*)"),
    # 中文:数字 vs/对比 数字
    re.compile(r"(-?\d+\.?\d*)\s*%?\s*(?:vs|对比|相比)\s*(?:预期\s*)?(-?\d+\.?\d*)", re.I),
    # 英文:actual X% vs forecast/expected Y%（{0,100}? 防止回溯超时）
    re.compile(r"actual[:\s]+(-?\d+\.?\d*)\s*%?[^,;]{0,100}?(?:forecast|expected|consensus)[:\s]+(-?\d+\.?\d*)", re.I),
    re.compile(r"(?:rose|rises|fell|came in at|reported)[:\s]+(-?\d+\.?\d*)\s*%?[^,;]{0,100}?(?:forecast|expected|consensus|estimate)[:\s]+(?:of\s+)?(-?\d+\.?\d*)", re.I),
    re.compile(r"(-?\d+\.?\d*)\s*%?\s+vs[\.\s]+(?:expected|forecast|consensus|estimate|est\.?)\s+(?:of\s+)?(-?\d+\.?\d*)", re.I),
    # 通用最后兜底:相邻两个百分数
    re.compile(r"(-?\d+\.\d+)\s*%[^,，;；]{1,30}?(-?\d+\.\d+)\s*%"),
]


# 用于在没有"预期"关键词时识别"绝对值发布"（仅用作低强度兜底）
_SOLO_NUMBER = re.compile(r"(-?\d+\.\d+)\s*%")


def _extract_actual_forecast(text: str) -> Optional[Tuple[float, float]]:
    """优先抽 (actual, forecast)；找不到 forecast 时返回 (actual, actual) 作为低强度信号。"""
    for pat in _PATTERNS:
        m = pat.search(text or "")
        if m:
            try:
                actual = float(m.group(1))
                forecast = float(m.group(2))
                return actual, forecast
            except (ValueError, IndexError):
                continue
    # 兜底：识别到一个百分数 → 视为"无预期值的纯发布"
    m = _SOLO_NUMBER.search(text or "")
    if m:
        try:
            actual = float(m.group(1))
            return actual, actual  # 偏差为 0，但仍记录到 macro 字段
        except ValueError:
            pass
    return None


def _identify_event(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.upper()
    for event_key, meta in MACRO_EVENTS.items():
        for kw in meta["keywords"]:
            if kw.upper() in t:
                return event_key
    return None


# ═══════════════════════════════════════════════════════════════════
# 偏差计算 + 方向判定
# ═══════════════════════════════════════════════════════════════════


def _deviation_level(actual: float, forecast: float) -> Tuple[str, float]:
    """返回 (level, relative_dev)，level ∈ {neutral, light, significant}"""
    if forecast == 0:
        # 无预期基准：actual 非零视为明显偏差（rel=1.0），避免除以1.0导致虚高百分比
        rel = 1.0 if actual != 0 else 0.0
    else:
        rel = abs(actual - forecast) / abs(forecast)
    if rel < config.MACRO_DEVIATION_NEUTRAL:
        return "neutral", rel
    if rel < config.MACRO_DEVIATION_LIGHT:
        return "light", rel
    return "significant", rel


def _hawkish_direction(event_key: str, actual: float, forecast: float) -> bool:
    """
    判断偏差方向是"鹰派"（紧缩预期上升）还是"鸽派"（宽松）。
    CPI/PPI/NFP：实际 > 预期 → 鹰派
    FOMC 加息：实际加息幅度 > 预期 → 鹰派
    GDP：实际 > 预期 → 鹰派（经济强劲）
    """
    return actual > forecast


# ═══════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════


async def analyze_macro_news(news: Dict) -> Optional[Dict]:
    """
    判定是否为宏观数据新闻，如是则提取偏差并生成影响记录。
    返回 None（非宏观）或影响 dict。
    """
    title = news.get("title", "")
    content = news.get("content", "") or title
    if not title and not content:
        return None

    event_key = _identify_event(title + " " + content)
    if not event_key:
        return None

    parsed = _extract_actual_forecast(title + " " + content)
    if not parsed:
        # 识别到事件但完全无数字 → 仍标记为 macro_data 但不带 impacts
        return {
            "event": event_key, "actual": None, "forecast": None,
            "deviation": 0.0, "level": "neutral", "tone": "neutral",
            "impacts": [], "window_hours": config.MACRO_IMPACT_WINDOW_HOURS,
        }
    actual, forecast = parsed

    level, rel = _deviation_level(actual, forecast)
    # neutral 仍返回元数据（让前端可以高亮 macro 标签），只是 impacts 为空
    if level == "neutral":
        return {
            "event": event_key, "actual": actual, "forecast": forecast,
            "deviation": round(rel * 100, 2), "level": "neutral", "tone": "neutral",
            "impacts": [], "window_hours": config.MACRO_IMPACT_WINDOW_HOURS,
        }

    is_hawkish = _hawkish_direction(event_key, actual, forecast)
    impacts_cfg = MACRO_EVENTS[event_key][
        "hawkish_impacts" if is_hawkish else "dovish_impacts"
    ]
    # 明显偏差时，强度乘 1.3；轻微时 0.7
    scale = 1.3 if level == "significant" else 0.7
    impacts = []
    for sym, direction, strength in impacts_cfg:
        adj_strength = min(1.0, strength * scale)
        # 宏观 impacts 严格阈值：< 0.7 不列（避免污染候选池）
        if adj_strength < 0.7:
            continue
        impacts.append({
            "symbol": sym, "direction": direction,
            "horizon": "1-5d", "strength": round(adj_strength, 2),
            "reason": f"{event_key.upper()} 实际 {actual} / 预期 {forecast}",
        })

    return {
        "event": event_key,
        "actual": actual,
        "forecast": forecast,
        "deviation": round(rel * 100, 2),   # 百分比
        "level": level,
        "tone": "hawkish" if is_hawkish else "dovish",
        "impacts": impacts,
        "window_hours": config.MACRO_IMPACT_WINDOW_HOURS,
    }


async def save_and_broadcast(db, ws_hub, news: Dict, impact: Dict):
    """
    把宏观影响写入：
      1) flash_news 的 macro_* 专门字段（is_macro_data=1, macro_type, macro_actual, ...）
      2) ai_analysis JSON 合并 macro_impact 段
      3) WS 推送
    """
    import json
    actual = impact.get("actual")
    forecast = impact.get("forecast")
    dev_pct = impact.get("deviation") or 0.0
    if actual is not None and forecast is not None and forecast != 0:
        dev_pct = (actual - forecast) / abs(forecast) * 100
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT ai_analysis FROM flash_news WHERE id=?", (news["id"],)
            )
            row = await cur.fetchone()
            existing = {}
            if row and row["ai_analysis"]:
                try:
                    existing = json.loads(row["ai_analysis"])
                except Exception:
                    pass
            existing["macro_impact"] = impact
            if impact["tone"] != "neutral":
                existing.setdefault("overall_view",
                                    "bearish" if impact["tone"] == "hawkish" else "bullish")
            await conn.execute(
                """UPDATE flash_news
                   SET ai_analysis=?, is_macro_data=1, macro_type=?,
                       macro_actual=?, macro_forecast=?,
                       macro_deviation_pct=?, macro_impact_strength=?
                   WHERE id=?""",
                (json.dumps(existing, ensure_ascii=False),
                 impact["event"],
                 actual, forecast,
                 round(dev_pct, 4),
                 impact["level"],
                 news["id"]),
            )
            await conn.commit()
    except Exception as e:
        logger.debug(f"[macro] 回填 macro 字段失败: {e}")
    # WS 推送：用 broadcast_flash_news 通用通道（前端 flash_news handler 已支持）
    try:
        payload = {
            "type": "macro_impact",
            "data": {
                "news_id": news["id"],
                "title": news.get("title", ""),
                **impact,
            },
        }
        broadcast = getattr(ws_hub, "broadcast_flash_news", None) or getattr(ws_hub, "broadcast_news", None)
        if broadcast:
            await broadcast(payload)
    except Exception as e:
        logger.debug(f"[macro] WS 推送失败: {e}")
    logger.info(
        f"📊 宏观影响识别: {impact['event'].upper()} "
        f"{impact['tone']}({impact['level']}) 实际 {actual} vs 预期 {forecast} "
        f"→ {len(impact['impacts'])} 个品种"
    )
    # 把 impacts 推入候选池（避免宏观信号和候选池断链）
    try:
        await _auto_add_macro_impacts_to_pool(db, ws_hub, news, impact)
    except Exception as e:
        logger.debug(f"[macro] impacts 入池异常: {e}")
    # 宏观事件发生时，主动给所有 manual 股票追加一次诊断（让 LLM 考虑宏观影响）
    # 仅对 significant/light 级别事件，避免 neutral 浪费 LLM
    if impact.get("level") in ("significant", "light"):
        try:
            await _diagnose_manual_stocks_on_macro(db, impact)
        except Exception as e:
            logger.debug(f"[macro] manual 股票追加诊断异常: {e}")


async def _diagnose_manual_stocks_on_macro(db, impact: Dict):
    """宏观事件触发后，给所有 manual 来源的候选池股票做一次诊断。"""
    try:
        from backend.news import scheduler as _sched
        analyzer = getattr(_sched, "_ai_analyzer", None)
        if analyzer is None:
            return
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT symbol, market FROM watch_pool WHERE source='manual' AND status!='archived'"
            )
            rows = await cur.fetchall()
        manuals = [(r["symbol"], r["market"]) for r in rows]
        if not manuals:
            return
        logger.info(
            f"[macro→manual] {impact['event'].upper()} {impact['tone']} "
            f"→ 追加诊断 {len(manuals)} 只 manual 股票"
        )
        # 异步并发（限制 3 只并发避免 LLM 限频）
        import asyncio
        sem = asyncio.Semaphore(3)
        async def _diag_one(sym, mkt):
            async with sem:
                try:
                    await analyzer.diagnose_stock(sym, mkt, force=True)
                except Exception as e:
                    logger.debug(f"[macro→manual] {sym} 诊断失败: {e}")
        await asyncio.gather(*[_diag_one(s, m) for s, m in manuals])
    except Exception as e:
        logger.warning(f"[macro→manual] 整体异常: {e}")


def _market_of_symbol(sym: str) -> Optional[str]:
    s = (sym or "").upper()
    if s.endswith("-USDT") or s.endswith("-USD") or s.endswith("-USDC"):
        return "crypto"
    if s.endswith(".HK"):
        return "hk"
    if s.isdigit() and len(s) == 6:
        return "cn"
    if s.isalpha() and 1 <= len(s) <= 5:
        return "us"
    return None


async def _auto_add_macro_impacts_to_pool(db, ws_hub, news: Dict, impact: Dict):
    """
    宏观事件识别后，将 impacts 中 strength >= 0.7 的股票品种推入候选池。
    event_score 按 level 加权:
      - significant: 40-50
      - light: 25-35
      - neutral: 跳过
    crypto 品种跳过（候选池只收股票）。
    """
    if impact.get("level") == "neutral":
        return
    impacts = impact.get("impacts") or []
    if not impacts:
        return
    base = {"significant": 40, "light": 25}.get(impact.get("level"), 0)
    for im in impacts:
        sym = im.get("symbol")
        strength = float(im.get("strength") or 0)
        if strength < 0.7:
            continue
        market = _market_of_symbol(sym)
        if market in (None, "crypto"):
            continue
        score = min(50.0, base + strength * 10)
        try:
            pool_id = await db.add_to_pool(
                symbol=sym, market=market, source="macro_theme",
                score=score,
                reason=f"{impact['event'].upper()} {impact['tone']}({impact['level']}) → {im.get('direction')} strength={strength:.2f}",
            )
            try:
                await ws_hub.broadcast_pool_update("added", {
                    "id": pool_id, "symbol": sym, "market": market,
                    "source": "macro_theme", "score": score,
                    "reason": f"宏观: {impact['event'].upper()}",
                })
            except Exception:
                pass
            # v12.13: 同 (sym, event) 5 分钟内 INFO 去重（避免一次新闻流批量触发刷屏）
            now_ts = time.time()
            key = (sym, impact["event"])
            last = _macro_log_dedupe.get(key, 0)
            if now_ts - last >= 300:
                _macro_log_dedupe[key] = now_ts
                logger.info(f"[macro-pool] {sym} ({market}) 入池 score={score:.1f} via {impact['event'].upper()}")
                # 清旧 key 防内存膨胀
                if len(_macro_log_dedupe) > 200:
                    cutoff = now_ts - 600
                    for k, v in list(_macro_log_dedupe.items()):
                        if v < cutoff:
                            _macro_log_dedupe.pop(k, None)
            else:
                logger.debug(f"[macro-pool] {sym} ({market}) 5min 内重复入池，去重日志")
        except Exception as e:
            logger.debug(f"[macro-pool] {sym} 入池失败: {e}")


# v12.13: macro_impact 日志去重缓存（同 sym+event 5min 内不重复 INFO）
_macro_log_dedupe: Dict[Tuple[str, str], float] = {}
