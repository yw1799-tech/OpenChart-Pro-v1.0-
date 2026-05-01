"""
v12.13 市场情绪/资金流上下文模块。

提供给 LLM verify_signal 的"市场天气"段：
  - A 股：北向资金当日净流入（东方财富 push2 接口；5min 缓存）
  - 美股+加密：CNN Fear & Greed Index（1h 缓存）

设计：
  - 共享缓存（避免每次 verify 都拉外部 API）
  - 失败 fail-soft（拉不到就返回"未知"，不阻断 verify）
  - 仅返回字符串文本，由调用方拼到 prompt
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# ─ 共享 session ─
_SESSION: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/120.0",
            },
        )
    return _SESSION


async def close_session():
    global _SESSION
    if _SESSION and not _SESSION.closed:
        await _SESSION.close()
        _SESSION = None


# ════════════════════════════════════════════════════════════════
# A 股北向资金（东方财富 push2 接口，A 股交易时段每分钟更新）
# ════════════════════════════════════════════════════════════════

_NORTHBOUND_CACHE: dict = {"ts": 0.0, "text": ""}
_NORTHBOUND_TTL = 5 * 60  # 5 分钟

# 接口返回每分钟的（沪股通+深股通）净买入累计
_EM_URL = (
    "https://push2.eastmoney.com/api/qt/kamtbs.rtmin/get"
    "?fields1=f1,f3,f5,f8&fields2=f51,f52,f53,f54,f55,f56,f62,f63,f64,f65"
    "&ut=b2884a393a59ad64002292a3e90d46a5"
)


async def _fetch_northbound_flow() -> str:
    """拉北向资金当前实时净流入（亿元）。失败返 '（暂无北向数据）'。"""
    try:
        s = await _get_session()
        async with s.get(_EM_URL) as r:
            if r.status != 200:
                return "（北向数据暂不可用）"
            data = await r.json(content_type=None)
        # data.s2n 是每分钟数组，每条 "HH:MM,...,沪股通净额(万元),...,深股通净额(万元),..."
        # 字段顺序（按 fields2 文档）：
        #   f51:时间 f52:沪股通买入 f53:沪股通卖出 f54:沪股通净额
        #   f55:深股通买入 f56:深股通卖出 f57:深股通净额
        #   f62:北向资金净额（综合）
        s2n = (data.get("data") or {}).get("s2n") or []
        if not s2n:
            return "（北向无数据；可能未到交易时段）"
        # 找最新非全 0 的一行（有真实数据）
        latest = None
        for row in reversed(s2n):
            parts = row.split(",")
            if len(parts) < 8:
                continue
            try:
                # 第 7 字段是北向综合净流入（万元）— 实测 EM 接口顺序
                vals = [float(x) for x in parts[1:]]
                if any(abs(v) > 0.01 for v in vals):
                    latest = (parts[0], vals)
                    break
            except ValueError:
                continue
        if not latest:
            return "（北向数据全 0；A 股可能未开盘或集合竞价中）"
        time_str, vals = latest
        # vals 顺序对应 fields2 后续；取最后一个非零代表"北向综合净流入"
        # 实测：vals[6] 通常是综合净额（万元）→ 转亿元
        net_yi = vals[6] / 10000.0 if len(vals) > 6 else 0.0
        sign = "+" if net_yi >= 0 else ""
        flow_state = (
            "强流入（看多 A 股）" if net_yi >= 30 else
            "净流入（偏多）" if net_yi >= 5 else
            "弱流入" if net_yi >= 0 else
            "弱流出" if net_yi >= -5 else
            "净流出（偏空）" if net_yi >= -30 else
            "强流出（看空 A 股）"
        )
        return f"截至 {time_str} 北向资金净流入 {sign}{net_yi:.2f} 亿元（{flow_state}）"
    except Exception as e:
        logger.debug(f"[market-ctx] 北向资金拉取失败: {e}")
        return "（北向数据拉取失败）"


async def get_northbound_flow_text() -> str:
    """返回缓存的北向资金描述（5min TTL）。"""
    now = time.time()
    if now - _NORTHBOUND_CACHE["ts"] < _NORTHBOUND_TTL and _NORTHBOUND_CACHE["text"]:
        return _NORTHBOUND_CACHE["text"]
    text = await _fetch_northbound_flow()
    _NORTHBOUND_CACHE["ts"] = now
    _NORTHBOUND_CACHE["text"] = text
    return text


# ════════════════════════════════════════════════════════════════
# CNN Fear & Greed Index（美股市场情绪，1h 缓存）
# ════════════════════════════════════════════════════════════════

_FNG_CACHE: dict = {"ts": 0.0, "text": ""}
_FNG_TTL = 60 * 60  # 1 小时

_CNN_FNG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/current"


async def _fetch_fear_greed() -> str:
    """拉 CNN F&G 当前值。"""
    try:
        s = await _get_session()
        async with s.get(_CNN_FNG_URL, headers={"Referer": "https://www.cnn.com/"}) as r:
            if r.status != 200:
                return "（CNN F&G 暂不可用）"
            data = await r.json(content_type=None)
        score = float(data.get("score") or 0)
        rating = data.get("rating") or "neutral"
        prev_close = float(data.get("previous_close") or 0)
        prev_week = float(data.get("previous_1_week") or 0)
        # 中文映射
        rating_cn = {
            "extreme fear": "极度恐惧",
            "fear": "恐惧",
            "neutral": "中性",
            "greed": "贪婪",
            "extreme greed": "极度贪婪",
        }.get(rating.lower(), rating)
        # 极端值反向提示（>80 极贪 BUY 谨慎；<20 极恐 SELL 谨慎）
        warn = ""
        if score >= 80:
            warn = "，⚠️ 极度贪婪：BUY 信号需谨慎（高位风险）"
        elif score <= 20:
            warn = "，⚠️ 极度恐惧：SELL 信号需谨慎（低位风险）"
        delta_week = score - prev_week
        delta_str = f"周变化 {'+' if delta_week >= 0 else ''}{delta_week:.0f}"
        return f"CNN F&G = {score:.0f}（{rating_cn}），昨日 {prev_close:.0f}，{delta_str}{warn}"
    except Exception as e:
        logger.debug(f"[market-ctx] CNN F&G 拉取失败: {e}")
        return "（CNN F&G 拉取失败）"


async def get_fear_greed_text() -> str:
    """返回缓存的 CNN F&G 描述（1h TTL）。"""
    now = time.time()
    if now - _FNG_CACHE["ts"] < _FNG_TTL and _FNG_CACHE["text"]:
        return _FNG_CACHE["text"]
    text = await _fetch_fear_greed()
    _FNG_CACHE["ts"] = now
    _FNG_CACHE["text"] = text
    return text


# ════════════════════════════════════════════════════════════════
# 统一入口：按市场返回完整"市场天气"段
# ════════════════════════════════════════════════════════════════


async def build_market_context(market: str) -> str:
    """根据市场返回市场情绪文本。
    cn → 北向资金
    us / hk → CNN F&G（hk 与 us 强相关）
    crypto → CNN F&G + 加密恐贪（已经在 verify 路径加进去了，这里只补 CNN）
    """
    parts = []
    market = (market or "").lower()
    try:
        if market == "cn":
            nb = await get_northbound_flow_text()
            parts.append(f"📈 A 股北向资金：{nb}")
        elif market in ("us", "hk", "crypto"):
            fg = await get_fear_greed_text()
            parts.append(f"📊 美股 CNN 恐贪指数：{fg}")
    except Exception as e:
        logger.debug(f"[market-ctx] {market} 构建失败: {e}")
    return "\n".join(parts) if parts else "（暂无市场情绪数据）"
