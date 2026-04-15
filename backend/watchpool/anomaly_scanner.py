"""
异动排行扫描（PRD F6.2）。

每 5 分钟（仅交易时段）拉数据源已经算好的排行榜：
  - A 股：东方财富涨幅榜 / 资金净流入榜
  - 美股：Yahoo Day Gainers
  - 港股：东方财富港股涨幅榜

把 Top N 入候选池（source="anomaly"），评分基于排名。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

_BJ_TZ = timezone(timedelta(hours=8))
_ET_TZ = timezone(timedelta(hours=-5))  # 简化：不切夏令时


# ═══════════════════════════════════════════════════════════════════
# 交易时段判断（避免非盘时段无意义请求）
# ═══════════════════════════════════════════════════════════════════


def _is_a_stock_trading() -> bool:
    """A 股交易时间：工作日 9:30-11:30, 13:00-15:00 北京时间。"""
    now = datetime.now(_BJ_TZ)
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    minutes = h * 60 + m
    return (9 * 60 + 30) <= minutes <= (11 * 60 + 30) or (13 * 60) <= minutes <= (15 * 60)


def _is_us_stock_trading() -> bool:
    """美股交易时间：工作日 9:30-16:00 美东时间。"""
    now = datetime.now(_ET_TZ)
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    minutes = h * 60 + m
    return (9 * 60 + 30) <= minutes <= (16 * 60)


def _is_hk_stock_trading() -> bool:
    """港股 9:30-12:00, 13:00-16:00 北京时间。"""
    now = datetime.now(_BJ_TZ)
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    minutes = h * 60 + m
    return (9 * 60 + 30) <= minutes <= (12 * 60) or (13 * 60) <= minutes <= (16 * 60)


# ═══════════════════════════════════════════════════════════════════
# 排行榜采集
# ═══════════════════════════════════════════════════════════════════


async def fetch_a_stock_top_movers(top_n: int = 20) -> List[Dict[str, Any]]:
    """
    东方财富 A 股涨幅榜（沪深 A 股，按涨幅降序）。
    返回: [{"symbol", "name", "change_pct", "turnover", "rank"}]
    """
    # fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23 = 沪深A股
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=" + str(top_n) + "&po=1&np=1&ut=&fltt=2&invt=2"
        "&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
        "&fields=f12,f14,f3,f6,f8,f62"
    )
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json(content_type=None)
    except Exception as e:
        logger.warning(f"[anomaly] A股涨幅榜拉取失败: {e}")
        return []

    items: List[Dict[str, Any]] = []
    try:
        diff = d.get("data", {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        for i, row in enumerate(diff):
            symbol = row.get("f12", "")  # 代码
            name = row.get("f14", "")
            change_pct = row.get("f3", 0)
            turnover = row.get("f6", 0)
            net_inflow = row.get("f62", 0)  # 主力净流入
            if not symbol:
                continue
            items.append({
                "symbol": symbol,
                "name": name,
                "market": "cn",
                "change_pct": float(change_pct) if change_pct not in ("-", None) else 0,
                "turnover": float(turnover) if turnover not in ("-", None) else 0,
                "net_inflow": float(net_inflow) if net_inflow not in ("-", None) else 0,
                "rank": i + 1,
            })
    except Exception as e:
        logger.warning(f"[anomaly] A股涨幅榜解析失败: {e}")
    return items


async def fetch_us_stock_top_movers(top_n: int = 20) -> List[Dict[str, Any]]:
    """
    Yahoo Finance Day Gainers (美股涨幅榜)。
    """
    url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?count={top_n}&scrIds=day_gainers"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json(content_type=None)
    except Exception as e:
        logger.warning(f"[anomaly] 美股涨幅榜拉取失败: {e}")
        return []

    items: List[Dict[str, Any]] = []
    try:
        result = d.get("finance", {}).get("result", [])
        if not result:
            return []
        quotes = result[0].get("quotes", [])
        for i, q in enumerate(quotes):
            symbol = q.get("symbol", "")
            if not symbol:
                continue
            items.append({
                "symbol": symbol,
                "name": q.get("shortName", "") or q.get("longName", ""),
                "market": "us",
                "change_pct": float(q.get("regularMarketChangePercent", 0) or 0),
                "turnover": float(q.get("regularMarketVolume", 0) or 0),
                "net_inflow": 0,
                "rank": i + 1,
            })
    except Exception as e:
        logger.warning(f"[anomaly] 美股涨幅榜解析失败: {e}")
    return items


async def fetch_hk_stock_top_movers(top_n: int = 20) -> List[Dict[str, Any]]:
    """东方财富 港股涨幅榜（fs=m:128+t:3 = 港股主板）。"""
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=" + str(top_n) + "&po=1&np=1&ut=&fltt=2&invt=2"
        "&fid=f3&fs=m:128+t:3,m:128+t:4,m:128+t:1,m:128+t:2"
        "&fields=f12,f14,f3,f6"
    )
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json(content_type=None)
    except Exception as e:
        logger.warning(f"[anomaly] 港股涨幅榜拉取失败: {e}")
        return []
    items: List[Dict[str, Any]] = []
    try:
        diff = d.get("data", {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        for i, row in enumerate(diff):
            sym = row.get("f12", "")
            if not sym:
                continue
            # 港股 symbol 格式：5 位数字 → 转为 NNNNN.HK
            symbol = f"{sym.zfill(5)}.HK" if sym.isdigit() else sym
            items.append({
                "symbol": symbol,
                "name": row.get("f14", ""),
                "market": "hk",
                "change_pct": float(row.get("f3", 0) or 0),
                "turnover": float(row.get("f6", 0) or 0),
                "net_inflow": 0,
                "rank": i + 1,
            })
    except Exception as e:
        logger.warning(f"[anomaly] 港股涨幅榜解析失败: {e}")
    return items


# ═══════════════════════════════════════════════════════════════════
# 调度器
# ═══════════════════════════════════════════════════════════════════


class AnomalyScanner:
    """
    异动扫描器：每 5 分钟（仅交易时段）拉各市场涨幅榜，
    满足阈值的入候选池（source='anomaly'）。
    """

    # 入池阈值
    THRESHOLD_CHANGE_PCT = 5.0   # 涨幅 ≥5% 才入池
    THRESHOLD_NET_INFLOW = 5e7    # 主力净流入 ≥5000 万（仅 A 股有此字段）
    TOP_N = 30                    # 各市场扫描 Top 30，再筛阈值

    def __init__(self, db, ws_hub):
        self.db = db
        self.ws_hub = ws_hub
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self, check_interval_sec: int = 300):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(check_interval_sec))
        logger.info(f"AnomalyScanner started (每 {check_interval_sec}s 扫描，仅交易时段)")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("AnomalyScanner stopped")

    async def _loop(self, interval: int):
        while self._running:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"异动扫描异常: {e}")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def scan_once(self):
        """单次扫描：按当前交易时段决定扫哪些市场。"""
        tasks = []
        if _is_a_stock_trading():
            tasks.append(("cn", fetch_a_stock_top_movers(self.TOP_N)))
        if _is_us_stock_trading():
            tasks.append(("us", fetch_us_stock_top_movers(self.TOP_N)))
        if _is_hk_stock_trading():
            tasks.append(("hk", fetch_hk_stock_top_movers(self.TOP_N)))

        if not tasks:
            logger.debug("[anomaly] 当前所有市场都在非交易时段，跳过扫描")
            return

        for market, coro in tasks:
            try:
                items = await coro
                added = await self._process_market(market, items)
                if added:
                    logger.info(f"[anomaly] {market} 市场新增 {added} 只异动股入候选池")
            except Exception as e:
                logger.exception(f"[anomaly] {market} 处理失败: {e}")

    async def _process_market(self, market: str, items: List[Dict[str, Any]]) -> int:
        """筛选 + 入池。"""
        added = 0
        for it in items:
            try:
                # 阈值过滤
                if it["change_pct"] < self.THRESHOLD_CHANGE_PCT:
                    continue
                # A 股可额外要求资金净流入
                if market == "cn" and it.get("net_inflow", 0) < 0:
                    # 涨幅大但主力净流出：不入池
                    continue
                # 评分：涨幅越大、排名越靠前评分越高
                score = min(100, 50 + it["change_pct"] * 2 + (30 - min(it["rank"], 30)) * 0.5)
                pool_id = await self.db.add_to_pool(
                    symbol=it["symbol"],
                    market=market,
                    source="anomaly",
                    score=score,
                    reason=f"涨幅榜 #{it['rank']} | {it['name']} +{it['change_pct']:.2f}%",
                )
                await self.ws_hub.broadcast_pool_update(
                    "added",
                    {
                        "id": pool_id,
                        "symbol": it["symbol"],
                        "market": market,
                        "source": "anomaly",
                        "score": round(score, 1),
                        "reason": f"涨幅 +{it['change_pct']:.2f}% (rank #{it['rank']})",
                    },
                )
                added += 1
            except Exception as e:
                logger.debug(f"[anomaly] 入池 {it.get('symbol')} 失败: {e}")
        return added
