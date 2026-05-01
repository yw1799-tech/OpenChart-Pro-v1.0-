"""
监控引擎（PRD F7 / TDD §6.5.3）。

加密 6 币种：系统启动时自动绑定全部内置策略，每根 K 线收盘时检查（实装时连 OKX WebSocket 即可）。
股票候选池：候选池 monitoring 状态的品种，按其绑定的策略评估。

信号去重：同品种同方向 SIGNAL_DEDUP_WINDOW (默认 10s) 内不重复触发。
信号阈值：confidence < SIGNAL_MIN_CONFIDENCE (默认 60) 不触发。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

try:
    import exchange_calendars as _ec
    _CAL_CN = _ec.get_calendar("XSHG")
    _CAL_HK = _ec.get_calendar("XHKG")
    _CAL_US = _ec.get_calendar("XNYS")  # XNYS 与 XNAS 交易日历一致
except Exception:  # pragma: no cover
    _CAL_CN = _CAL_HK = _CAL_US = None

import backend.config as config
from backend.data.cache import cached_get_klines
from backend.data.models import Interval, Market, Signal
from backend.signals.binding import StrategyBindingManager
from backend.signals.strategies import ALL_STRATEGIES, Strategy, get_strategy

logger = logging.getLogger(__name__)


# 周期 → 毫秒。用于判断末根 K 线是否已收盘。
INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H": 3_600_000,
    "1h": 3_600_000,
    "2H": 7_200_000,
    "4H": 14_400_000,
    "4h": 14_400_000,
    "1D": 86_400_000,
    "1d": 86_400_000,
    "1W": 604_800_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,
}


def _filter_unclosed(candles: List[Any], interval_str: str) -> List[Any]:
    """
    如果末根 K 线尚未收盘（candle.timestamp + interval_ms > now），剔除它。
    所有策略都基于"已收盘"K 线评估，避免在未完成周期内误触信号。
    """
    if not candles:
        return candles
    ms = INTERVAL_MS.get(interval_str)
    if not ms:
        return candles
    last = candles[-1]
    # 兼容 dict / 对象形式；时间戳支持秒 / 毫秒
    if isinstance(last, dict):
        ts = last.get("timestamp") or last.get("time") or last.get("t") or 0
    else:
        ts = getattr(last, "timestamp", 0) or getattr(last, "time", 0) or 0
    try:
        ts = int(ts)
    except Exception:
        return candles
    if ts <= 0:
        return candles
    # 归一化到毫秒
    if ts < 10_000_000_000:  # 秒级
        ts *= 1000
    now_ms = int(time.time() * 1000)
    # 末根未收盘：candle_open_ts + interval_ms > now
    if ts + ms > now_ms:
        return candles[:-1]
    return candles


def _is_session_today(cal, local_now: datetime) -> bool:
    """调用 exchange_calendars.is_session（传入 date）。未加载日历时回退到周内判定。"""
    if cal is None:
        return local_now.weekday() < 5
    try:
        return bool(cal.is_session(local_now.date()))
    except Exception:
        return local_now.weekday() < 5


def is_market_tradable(market_str: str) -> bool:
    """
    当前是否处于"可挂单"时段（含集合竞价 / 美股盘前盘后；加密 24/7）。
    用于策略信号生成（监控阶段）—— 集合竞价也允许产生信号、做 verify。
    """
    m = (market_str or "").lower()
    if m in ("crypto", ""):
        return True
    try:
        if m in ("cn", "a", "ashare"):
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            if not _is_session_today(_CAL_CN, now):
                return False
            t = now.time()
            # A 股集合竞价 09:15 起；连续 09:30-11:30 / 13:00-15:00；无零售盘前盘后
            return (dtime(9, 15) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))
        if m in ("hk", "hkex"):
            now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
            if not _is_session_today(_CAL_HK, now):
                return False
            t = now.time()
            # 港股集合竞价 09:00 起；连续 09:30-12:00 / 13:00-16:00；收市竞价 16:00-16:10
            return (dtime(9, 0) <= t <= dtime(12, 0)) or (dtime(13, 0) <= t <= dtime(16, 10))
        if m in ("us", "nasdaq", "nyse"):
            now = datetime.now(ZoneInfo("America/New_York"))
            if not _is_session_today(_CAL_US, now):
                return False
            t = now.time()
            # 美股盘前 04:00-09:30 / 常规 09:30-16:00 / 盘后 16:00-20:00
            return dtime(4, 0) <= t <= dtime(20, 0)
    except Exception as e:
        logger.debug(f"is_market_tradable({market_str}) 异常: {e}")
        return True
    return True


def is_market_executable(market_str: str) -> bool:
    """
    当前是否处于"可下单成交"时段——只有连续竞价时段才算（去掉集合竞价 + 美股盘前盘后）。
    用于 auto_trader 实际执行下单。集合竞价时段信号会被记 'pending'，等开市后自动 fire。

    A 股：09:30-11:30 / 13:00-14:57（去掉 9:15-9:30 集合竞价 + 14:57-15:00 收盘竞价）
    港股：09:30-12:00 / 13:00-16:00（去掉 9:00-9:30 集合竞价 + 16:00-16:10 收市竞价）
    美股：09:30-16:00 ET（去掉盘前 4:00-9:30 + 盘后 16:00-20:00）
    加密：24/7
    """
    m = (market_str or "").lower()
    if m in ("crypto", ""):
        return True
    try:
        if m in ("cn", "a", "ashare"):
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            if not _is_session_today(_CAL_CN, now):
                return False
            t = now.time()
            return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t < dtime(14, 57))
        if m in ("hk", "hkex"):
            now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
            if not _is_session_today(_CAL_HK, now):
                return False
            t = now.time()
            return (dtime(9, 30) <= t <= dtime(12, 0)) or (dtime(13, 0) <= t <= dtime(16, 0))
        if m in ("us", "nasdaq", "nyse"):
            now = datetime.now(ZoneInfo("America/New_York"))
            if not _is_session_today(_CAL_US, now):
                return False
            t = now.time()
            return dtime(9, 30) <= t <= dtime(16, 0)
    except Exception as e:
        logger.debug(f"is_market_executable({market_str}) 异常: {e}")
        return True
    return True


class MonitorEngine:
    """
    策略信号监控引擎。

    工作模式：
    - 加密 6 币种：定时（每个周期）拉一次 K 线，跑全部策略
    - 股票候选池：定时拉 K 线，跑各自绑定的策略
    """

    def __init__(self, db, ws_hub, news_provider=None):
        self.db = db
        self.ws_hub = ws_hub
        self.news_provider = news_provider or (lambda symbol: [])
        self.bindings = StrategyBindingManager(db)
        self._dedupe: Dict[tuple, int] = {}
        self._dedupe_lock = asyncio.Lock()  # 多并发 _evaluate_symbol 写 dict 防竞态
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._broadcast_lock = asyncio.Lock()
        self._last_broadcast_ms = 0
        # 保存 create_task 引用避免被 GC（否则后台任务可能被中止，异常被吞噬）
        self._bg_tasks: set = set()

    def _spawn(self, coro):
        """安全创建后台任务：保留引用 + 异常日志 + 完成后清理。"""
        t = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        def _done(task):
            self._bg_tasks.discard(task)
            if not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.warning(f"后台任务异常: {type(exc).__name__}: {exc}")
        t.add_done_callback(_done)
        return t

    # ───────── 启动时初始化加密 6 币种绑定 ─────────

    # 加密监控周期：15m + 1H + 4H + 1D（四周期覆盖短中长各级）
    # 绑定数：6 币 × 7 策略 × 4 周期 = 168 条
    # 注：15m 信号频繁，对 LLM 预算压力大，但缠论策略自身过滤严格（需形成买卖点）
    CRYPTO_INTERVALS = ["15m", "1H", "4H", "1D"]
    # 股票监控周期（候选池高分股）：日线最稳
    STOCK_INTERVALS = ["1D"]
    # 美股"热门"额外周期：1H（yfinance 1h 数据盘中实时更新，弥补 1D 闭市后才更新的缺陷）
    # 仅对 持仓 / watchlist / score≥60 的美股开 1H，避免监控过载
    US_HOT_EXTRA_INTERVALS = ["1H"]
    US_HOT_MIN_SCORE = 60
    # 股票自动绑定的策略（v11.5 加 chanlun，原本只在加密自动绑，股票需手动绑导致无缠论信号）
    # v12.13: 加 flash_event（消费 LLM 解读后的 ai_analysis.impacts，新闻驱动股票本应是大头）
    STOCK_AUTO_STRATEGIES = ["ma_cross", "bollinger_reversion", "volume_breakout", "chanlun", "flash_event"]
    # 股票自动入监控的评分门槛（基线 40，news/news_ai/macro/manual 强制绑定不受此限制）
    STOCK_MONITOR_MIN_SCORE = 40
    # 入池即绑的 source 白名单：明确信号驱动的，不管 score 多少都要监控
    STOCK_MONITOR_FORCE_SOURCES = ("manual", "news", "news_ai", "macro_theme")

    async def ensure_crypto_bindings(self):
        """v12.16 (Step 3): 加密绑定改用 STRATEGY_MARKET_MATRIX 替代"全策略全周期"。
        旧逻辑：6 币 × 7 策略 × 4 周期 = 168 绑定（很多噪音）
        新逻辑：6 币 × 矩阵中加密专属配置（按策略选择最佳周期 + params）
        """
        from backend.signals.strategies import get_strategy_matrix_for_market
        cfg_list = get_strategy_matrix_for_market("crypto")
        cnt = 0
        for symbol in config.CRYPTO_SYMBOLS:
            for cfg in cfg_list:
                try:
                    await self.bindings.bind(
                        symbol=symbol, market="crypto",
                        strategy_name=cfg["strategy_name"], interval=cfg["interval"],
                        params=cfg["params"],
                        enabled=True,
                    )
                    cnt += 1
                except Exception as e:
                    logger.debug(f"绑定 {symbol}/{cfg['strategy_name']}/{cfg['interval']} 失败: {e}")
        logger.info(f"加密自动绑定 (v12.16 matrix): {cnt} 条 ({len(config.CRYPTO_SYMBOLS)} 币 × {len(cfg_list)} 策略-周期)")

    async def auto_bind_stock_pool(self):
        """
        自动绑定候选池股票的策略：
          - 强制来源 (manual/news/news_ai/macro_theme)：无门槛立即绑（明确驱动信号）
          - 其他来源 (anomaly)：score >= STOCK_MONITOR_MIN_SCORE (默认 40) 才绑
        周期：
          - 默认 1D（所有股票）
          - 美股额外加 1H（仅持仓 / watchlist / score≥60 / strong_buy/buy 评级）
            → 弥补 yfinance 1D K 线只在闭市后更新的缺陷，盘中也能触发策略信号
        """
        try:
            items = await self.db.get_pool_items(limit=500)
        except Exception as e:
            logger.debug(f"auto_bind_stock_pool 拉取候选池失败: {e}")
            return 0
        # 拉取美股 1H 加权对象：持仓 + watchlist
        us_hot_symbols = set()
        try:
            async with self.db.acquire() as conn:
                # 美股持仓
                cur = await conn.execute("SELECT symbol FROM positions WHERE market='us' AND quantity > 0")
                for r in await cur.fetchall():
                    us_hot_symbols.add(r["symbol"])
                # 美股 watchlist
                cur = await conn.execute("SELECT symbol FROM watchlist WHERE market='us'")
                for r in await cur.fetchall():
                    us_hot_symbols.add(r["symbol"])
        except Exception as e:
            logger.debug(f"美股 hot 集合查询失败: {e}")

        # v12.16 (Step 3): 用 STRATEGY_MARKET_MATRIX 替代"固定策略列表 × 固定周期"
        from backend.signals.strategies import get_strategy_matrix_for_market
        cnt = 0
        cnt_forced = 0
        import json as _json
        for it in items:
            is_forced = it.get("source") in self.STOCK_MONITOR_FORCE_SOURCES
            if not is_forced and (it.get("score") or 0) < self.STOCK_MONITOR_MIN_SCORE:
                continue
            mkt = it.get("market")
            cfg_list = get_strategy_matrix_for_market(mkt)  # 含该市场所有策略 + 周期
            # 美股 hot 子集（持仓/watchlist/高分/buy 评级）需要额外 1H 周期 — 矩阵中已含 1H
            # 非 hot 美股先不加 1H（保留原本节流逻辑：只跑 1D 减监控压力）
            is_us_hot = False
            if mkt == "us":
                is_us_hot = (
                    it["symbol"] in us_hot_symbols
                    or (it.get("score") or 0) >= self.US_HOT_MIN_SCORE
                )
                if not is_us_hot and it.get("ai_diagnosis"):
                    try:
                        diag = _json.loads(it["ai_diagnosis"])
                        if diag.get("rating") in ("buy", "strong_buy"):
                            is_us_hot = True
                    except Exception:
                        pass
            for cfg in cfg_list:
                # 美股非热门跳过 1H（节流）— 仅保留 1D
                if mkt == "us" and not is_us_hot and cfg["interval"] != "1D":
                    continue
                try:
                    await self.bindings.bind(
                        symbol=it["symbol"], market=mkt,
                        strategy_name=cfg["strategy_name"],
                        interval=cfg["interval"],
                        params=cfg["params"],
                        enabled=True,
                    )
                    cnt += 1
                    if is_forced:
                        cnt_forced += 1
                except Exception as e:
                    logger.debug(f"股票绑定失败 {it['symbol']}/{cfg['strategy_name']}: {e}")

        if cnt:
            logger.info(
                f"股票候选池自动绑定 (v12.16 matrix): {cnt} 条 (含强制来源 {cnt_forced} 条)"
            )
        return cnt

    # ───────── 后台监控循环 ─────────

    def start(self, check_interval_sec: int = 60):
        """启动后台监控（默认每 60 秒检查一次所有绑定）。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(check_interval_sec))
        # v12.15.3: 启动恢复 — 扫近 6h 的 stuck signal（重启时 verify task 被 cancel 留下的孤儿）
        asyncio.create_task(self._recover_stuck_signals())
        # v12.15.3: 周期 stuck 扫描（每 5min 兜底，防 verify task 静默失败）
        asyncio.create_task(self._stuck_signal_loop())
        logger.info("MonitorEngine started + stuck-signal recovery armed")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MonitorEngine stopped")

    async def _monitor_loop(self, interval_sec: int):
        while self._running:
            try:
                await self.check_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"监控循环异常: {e}")
            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                break

    async def _recover_stuck_signals(self):
        """v12.15.3 启动恢复：扫近 6h 的 ai_verdict 为空的信号，重新分发处理。
        修复重启时 _spawn(_ai_verify_signal) 的异步 task 被 cancel 导致永久 stuck 教训。
        策略：
          - 美股/港股/A股 SELL 信号 + 无持仓 → 直接标 skipped
          - 其它 → 重新 spawn _ai_verify_signal（让 LLM 重新验证）
        """
        await asyncio.sleep(15)  # 等系统稳定再恢复
        await self._scan_and_recover_stuck(lookback_h=6, label="stuck-recovery")

    async def _stuck_signal_loop(self, interval_sec: int = 300):
        """v12.15.3 每 5min 兜底扫一次 stuck 信号"""
        await asyncio.sleep(180)
        while self._running:
            try:
                await self._scan_and_recover_stuck(lookback_h=2, label="stuck-loop", min_age_min=10)
            except Exception as e:
                logger.debug(f"[stuck-loop] 异常: {e}")
            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                break

    async def _scan_and_recover_stuck(self, lookback_h: int = 6,
                                      label: str = "recover", min_age_min: int = 0):
        """共用扫描逻辑：min_age_min 过滤掉太新的（防与正常 verify 撞车）"""
        try:
            now_ms = int(time.time() * 1000)
            cutoff_old = now_ms - lookback_h * 3600 * 1000
            cutoff_new = now_ms - min_age_min * 60 * 1000 if min_age_min else now_ms
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT id, symbol, market, action, confidence,
                              strategy_name, interval, price, generated_at,
                              ai_stop_loss, ai_take_profit
                       FROM signals
                       WHERE (ai_verdict IS NULL OR ai_verdict = '')
                         AND generated_at > ? AND generated_at < ?
                       ORDER BY generated_at DESC LIMIT 50""",
                    (cutoff_old, cutoff_new),
                )
                rows = [dict(r) for r in await cur.fetchall()]
            if not rows:
                if label == "stuck-recovery":
                    logger.info(f"[{label}] 无 stuck 信号需恢复")
                return
            sell_skipped = 0
            re_verify = 0
            for row in rows:
                mkt = row["market"]
                action = row["action"]
                # 股票 SELL + 无持仓 → 直接标 skipped
                if mkt != "crypto" and action == "sell":
                    try:
                        async with self.db.acquire() as conn:
                            cur = await conn.execute(
                                "SELECT 1 FROM positions WHERE symbol=? AND market=? AND quantity > 0 LIMIT 1",
                                (row["symbol"], mkt),
                            )
                            has_pos = await cur.fetchone()
                        if not has_pos:
                            await self._mark_verify_skipped(
                                row["id"],
                                f"[{label}] 无持仓，股票 SELL 信号无意义（自动交易仅做多）",
                            )
                            sell_skipped += 1
                            continue
                    except Exception as e:
                        logger.debug(f"[{label}] 持仓查询失败 {row['id'][:8]}: {e}")
                        continue
                # 其它情况：重新 spawn LLM verify
                try:
                    from backend.data.models import Signal as SigCls, Market
                    mkt_enum = Market(mkt) if mkt in ("us","hk","cn","crypto") else None
                    if not mkt_enum:
                        continue
                    sig_obj = SigCls(
                        id=row["id"], symbol=row["symbol"], market=mkt_enum,
                        action=action, confidence=int(row["confidence"] or 0),
                        strategy_name=row["strategy_name"] or "",
                        price=float(row["price"] or 0),
                        stop_loss=row.get("ai_stop_loss"),
                        take_profit=row.get("ai_take_profit"),
                        triggered_by={},
                        generated_at=int(row["generated_at"] or 0),
                    )
                    # interval 不在 dataclass 里 — _ai_verify_signal 用 getattr fallback 到 1H
                    sig_obj.interval = row.get("interval") or "1H"
                    self._spawn(self._ai_verify_signal(sig_obj))
                    re_verify += 1
                except Exception as e:
                    logger.debug(f"[{label}] 恢复 {row['id'][:8]} 失败: {e}")
            logger.info(
                f"[{label}] 处理 {len(rows)} 条 stuck — SELL 无持仓 skipped {sell_skipped}，"
                f"重新分发 verify {re_verify}"
            )
        except Exception as e:
            logger.warning(f"[{label}] 异常: {e}")

    # ───────── 主检查逻辑 ─────────

    async def check_all(self):
        """遍历所有启用绑定，按 (symbol,market,interval) 分组运行策略（限并发 8）。"""
        bindings = await self.bindings.get_bindings(enabled_only=True)
        if not bindings:
            return

        by_group: Dict[tuple, List[Dict]] = {}
        for b in bindings:
            iv = b.get("interval") or "1H"
            key = (b["symbol"], b["market"], iv)
            by_group.setdefault(key, []).append(b)

        # 限制并发避免 108 个 K 线请求同时打爆 + 信号洪流
        sem = asyncio.Semaphore(8)
        async def _wrap(sym, mkt, iv, bs):
            async with sem:
                await self._evaluate_symbol(sym, mkt, iv, bs)
        await asyncio.gather(
            *[_wrap(sym, mkt, iv, bs) for (sym, mkt, iv), bs in by_group.items()],
            return_exceptions=True,
        )

    async def _evaluate_symbol(self, symbol: str, market_str: str, interval_str: str, bindings: List[Dict]):
        """对单个 (品种, 周期) 跑所有绑定的策略。"""
        # 非可交易时段不触发信号（加密 24/7；A/港/美按各自窗口含盘前盘后）
        if not is_market_tradable(market_str):
            return
        # v12.16 财报窗口过滤：美股临近财报（前 3 日 / 后 1 日）不发信号
        try:
            from backend.signals.strategies import is_in_earnings_window
            if await is_in_earnings_window(symbol, market_str):
                logger.info(f"[earnings-window] {symbol}({market_str}) 临近财报，跳过信号生成")
                return
        except Exception as e:
            logger.debug(f"[earnings-window] {symbol} 检查异常: {e}")
        try:
            market = Market(market_str)
        except ValueError:
            return
        # 周期字符串 → Interval enum
        try:
            interval = Interval(interval_str)
        except ValueError:
            logger.debug(f"无效周期: {interval_str}")
            return

        try:
            candles = await cached_get_klines(
                db=self.db, market=market, symbol=symbol, interval=interval, limit=300
            )
        except Exception as e:
            logger.debug(f"拉取 K 线失败 {symbol}/{interval_str}: {e}")
            return
        if not candles or len(candles) < 30:
            return

        # 剔除未收盘末根 K 线，确保所有策略都只看已收盘周期
        # 原因：21:43 触发 4H 信号这种 bug 的根源是策略评估了尚未完成的 4H K 线
        closed_candles = _filter_unclosed(candles, interval_str)
        if len(closed_candles) < 30:
            return
        candles = closed_candles

        # 取该品种最近新闻（供 FlashEventStrategy 和通用 modifier 使用）
        # v12.13: news_provider 现支持 async（main.py 提供真实拉取 + 30s 缓存版本）
        # 老版本 news_provider 是同步 lambda 返回空 [] → 导致所有策略 recent_news=[]，flash_event 0 信号
        recent_news = []
        try:
            res = self.news_provider(symbol)
            if asyncio.iscoroutine(res):
                recent_news = await res or []
            else:
                recent_news = res or []
        except Exception as e:
            logger.debug(f"[monitor] {symbol} news_provider 失败: {e}")

        # AI 诊断否决：近期 rating=sell 直接拒绝反向信号；reduce 扣分
        #   - 股票 (us/hk/cn) 读 watch_pool.ai_diagnosis（7 天内有效）
        #   - 加密 (crypto) 读 crypto_diagnosis（2 小时内有效，因加密波动快）
        ai_veto_buy = False      # rating=sell → 否决 BUY
        ai_veto_sell = False     # rating=strong_buy/buy → 否决 SELL（避免逆势）
        ai_demerit = 0
        try:
            async with self.db.acquire() as conn:
                if market_str in ("us", "hk", "cn"):
                    cur = await conn.execute(
                        "SELECT ai_diagnosis, ai_diagnosed_at FROM watch_pool "
                        "WHERE symbol=? AND market=? AND status != 'archived' "
                        "ORDER BY ai_diagnosed_at DESC LIMIT 1",
                        (symbol, market_str),
                    )
                    row = await cur.fetchone()
                    max_age = 7 * 86400
                elif market_str == "crypto":
                    cur = await conn.execute(
                        "SELECT diagnosis AS ai_diagnosis, diagnosed_at AS ai_diagnosed_at "
                        "FROM crypto_diagnosis WHERE symbol=?",
                        (symbol,),
                    )
                    row = await cur.fetchone()
                    max_age = 2 * 3600   # 加密 2 小时内
                else:
                    row = None
                    max_age = 0
            if row and row["ai_diagnosis"]:
                age = time.time() - (row["ai_diagnosed_at"] or 0)
                if age < max_age:
                    diag = json.loads(row["ai_diagnosis"])
                    rating = (diag or {}).get("rating")
                    # v12.11: rating 为空 / "error" / 包含 "llm_error" 字样不生效（防 LLM 误判后 7 天信号沉底）
                    rating_norm = (rating or "").strip().lower() if isinstance(rating, str) else ""
                    if rating_norm in ("", "error", "llm_error", "none", "unknown"):
                        logger.debug(f"[ai-veto] {symbol}/{market_str} rating 异常 ({rating!r})，否决不生效")
                    elif rating_norm == "sell":
                        ai_veto_buy = True
                    elif rating_norm in ("strong_buy", "buy"):
                        ai_veto_sell = True
                    elif rating_norm == "reduce":
                        ai_demerit = 20
        except Exception as e:
            logger.debug(f"[ai-veto] {symbol}/{market_str} 查诊断失败: {e}")

        # 先收集本轮所有候选信号，再做矛盾消解
        candidate_signals = []
        for binding in bindings:
            try:
                strat_name = binding["strategy_name"]
                params = binding.get("params") or {}
                strategy = get_strategy(strat_name, **params)
                if not strategy:
                    continue
                # v12.16 (Step 5): evaluate 支持同步/异步双模 — 加密专属策略需 await 外部 API
                _r = strategy.evaluate(symbol, market, candles, recent_news)
                if asyncio.iscoroutine(_r):
                    signal = await _r
                else:
                    signal = _r
                if not signal:
                    continue
                signal.interval = interval_str
                # 应用 AI 诊断否决
                if ai_veto_buy and signal.action == "buy":
                    logger.info(f"[ai-veto] {symbol}/{interval_str} BUY 被 AI 诊断 sell 否决")
                    continue
                if ai_veto_sell and signal.action == "sell":
                    logger.info(f"[ai-veto] {symbol}/{interval_str} SELL 被 AI 诊断 buy 否决")
                    continue
                if ai_demerit:
                    signal.confidence = max(0, signal.confidence - ai_demerit)
                if signal.confidence < config.SIGNAL_MIN_CONFIDENCE:
                    continue
                candidate_signals.append((signal, strat_name))
            except Exception as e:
                logger.exception(f"策略 {binding.get('strategy_name')} 评估异常: {e}")

        # 矛盾消解：同品种同周期出 BUY + SELL → 只保留置信度最高的方向
        if len(candidate_signals) > 1:
            buys = [(s, n) for s, n in candidate_signals if s.action == 'buy']
            sells = [(s, n) for s, n in candidate_signals if s.action == 'sell']
            if buys and sells:
                best_buy = max(buys, key=lambda x: x[0].confidence)
                best_sell = max(sells, key=lambda x: x[0].confidence)
                if best_buy[0].confidence == best_sell[0].confidence:
                    # 置信度相同 = 方向不明确 → 全部丢弃，不推信号
                    candidate_signals = []
                    logger.info(f"[signal-conflict] {symbol}/{interval_str} BUY({best_buy[0].confidence}) == SELL({best_sell[0].confidence}) → 方向矛盾丢弃")
                elif best_buy[0].confidence > best_sell[0].confidence:
                    candidate_signals = [x for x in candidate_signals if x[0].action == 'buy']
                    logger.info(f"[signal-conflict] {symbol}/{interval_str} BUY({best_buy[0].confidence}) > SELL({best_sell[0].confidence}) → 保留 BUY")
                else:
                    candidate_signals = [x for x in candidate_signals if x[0].action == 'sell']
                    logger.info(f"[signal-conflict] {symbol}/{interval_str} SELL({best_sell[0].confidence}) > BUY({best_buy[0].confidence}) → 保留 SELL")

        # v12.16 (Step 1): 共振合并 — 同 K 线 ≥2 策略同方向 → 合并为 super signal
        # 替换原始单策略信号（避免 cooldown 互相干扰），triggered_by 保留追溯
        candidate_signals = self._synthesize_resonance_signals(
            candidate_signals, interval_str
        )

        # 按 interval 动态决定去重窗口：一根 K 线内同一策略同方向最多触发 1 次
        # 1D → 4h / 4H → 1h / 1H → 30min / 15m → 10min / 5m → 5min
        _DEDUP_BY_INTERVAL = {
            "1D": 4 * 3600 * 1000,   # 一根日 K 线内不重复
            "4H": 1 * 3600 * 1000,
            "1H": 30 * 60 * 1000,
            "30m": 15 * 60 * 1000,
            "15m": 10 * 60 * 1000,
            "5m": 5 * 60 * 1000,
            "1m": 3 * 60 * 1000,
        }
        default_window_ms = max(config.SIGNAL_DEDUP_WINDOW, 300) * 1000
        dedup_window_ms = _DEDUP_BY_INTERVAL.get(interval_str, default_window_ms)

        for signal, strat_name in candidate_signals:
            try:
                # 去重 key 含 interval
                key = (signal.market.value if hasattr(signal.market, "value") else str(signal.market),
                       signal.symbol, signal.action, strat_name, interval_str)
                now_ms = int(time.time() * 1000)
                # 加锁：多并发 _evaluate_symbol 同时改 _dedupe 防 dict race
                async with self._dedupe_lock:
                    last = self._dedupe.get(key, 0)
                    if last == 0:
                        try:
                            cutoff_ms = now_ms - dedup_window_ms
                            async with self.db.acquire() as conn:
                                cur = await conn.execute(
                                    """SELECT MAX(generated_at) AS last FROM signals
                                       WHERE symbol=? AND market=? AND action=? AND strategy_name=?
                                         AND interval=? AND generated_at > ?""",
                                    (signal.symbol, key[0], signal.action, strat_name, interval_str, cutoff_ms),
                                )
                                row = await cur.fetchone()
                            if row and row["last"]:
                                last = int(row["last"])
                                self._dedupe[key] = last
                        except Exception:
                            pass
                    if now_ms - last < dedup_window_ms:
                        continue
                    self._dedupe[key] = now_ms
                    # 防止 _dedupe 无限增长：超过 2000 条时清理 1 小时前的旧条目
                    if len(self._dedupe) > 2000:
                        cutoff = now_ms - 3600 * 1000
                        self._dedupe = {k: v for k, v in self._dedupe.items() if v >= cutoff}
                # 入库 + 推送（锁外做，避免持锁 IO）
                await self._save_and_broadcast(signal)
            except Exception as e:
                logger.exception(f"策略 {strat_name} 评估异常: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # v12.16 (Step 1): 策略共振合并器
    # ═══════════════════════════════════════════════════════════════════
    # "黄金组合"定义：同 K 线触发任一组合 → Level 3 (conf=100, sizing×1.5)
    GOLDEN_COMBOS = [
        # 趋势 + 量能：经典动量起爆模式
        {"ma_cross", "volume_breakout", "macd_cross"},
        # 反转：超卖底部 + 量能确认（v12.16: 删 rsi_divergence 后改用 ema_triple）
        {"bollinger_reversion", "volume_breakout", "ema_triple"},
        # 突破共振：突破 + 盘整结束 + 趋势确认
        {"donchian_breakout", "squeeze_breakout", "adx_trend_follow"},
        # 加密"smart money"组合：衍生品三件套
        {"funding_extreme", "oi_breakout", "long_short_ratio"},
        # 加密 F&G 极值 + 衍生品共振（额外新增）
        {"fear_greed_reversal", "funding_extreme"},
        # 缠论 + 量能：段级买卖点 + 资金确认
        {"chanlun", "volume_breakout"},
        # A 股政策 + 资金组合
        {"northbound_flow_top", "sector_momentum", "limit_up_followup"},
        # v12.16.5: RSI 趋势回踩 + 趋势确认 + 量能（经典 3 重共振，趋势内抄底）
        {"ma_cross", "rsi_pullback", "volume_breakout"},
        # v12.16.5: RSI 真背离 + 量能确认（反转高胜率，避免单一背离误信号）
        {"rsi_real_divergence", "volume_breakout"},
        # v12.16.6: 美股财报后高开 + 趋势 + 量能（财报季高胜率突破延续）
        {"gap_up_continuation", "ma_cross", "volume_breakout"},
        # v12.16.6: 美股高开 + MACD 动量（盘后预期外利好的延续）
        {"gap_up_continuation", "macd_cross"},
        # v12.16.6: 港股南向资金 + 趋势 + 量能（机构 + 散户共振）
        {"southbound_inflow", "ma_cross", "volume_breakout"},
        # v12.16.6: 新闻驱动 + 量能 + 趋势（防情绪噪音 + 假突破）
        {"flash_event", "volume_breakout", "ma_cross"},
        # v12.17.0: 美股盘前/开盘突破 + 趋势 + 量能（开盘强势确认）
        {"premarket_breakout", "ma_cross", "volume_breakout"},
        # v12.17.0: 美股相对强势 + 三重过滤（强势股回调买入）
        {"relative_strength_top", "triple_screen"},
        # v12.17.0: A 股龙虎榜 + 板块联动（机构 + 题材双重确认）
        {"lhb_follow", "sector_momentum"},
        # v12.17.0: A 股融资 + 北向 + 板块（杠杆资金 + 外资 + 题材三共振）
        {"margin_breakout", "northbound_flow_top", "sector_momentum"},
        # v12.17.0: 加密巨鲸大单 + 量能 + 趋势（聪明钱 + 趋势确认）
        {"whale_activity", "volume_breakout", "ma_cross"},
        # v12.17.0: 加密稳定币流入 + F&G 极值（流动性 + 情绪共振）
        {"stablecoin_flow", "fear_greed_reversal"},
        # v12.17.0: 量价背离 + RSI 真背离（双背离反转高胜率）
        {"volume_price_divergence", "rsi_real_divergence"},
        # v12.17.0: 三重过滤 + RSI 趋势回踩（多周期 + 单周期共振）
        {"triple_screen", "rsi_pullback"},
    ]

    def _is_golden_combo(self, strat_set: set) -> bool:
        """命中任一黄金组合（含子集匹配 — 触发的策略数 ≥ 组合定义全集即算）"""
        for combo in self.GOLDEN_COMBOS:
            if combo.issubset(strat_set):
                return True
        return False

    def _synthesize_resonance_signals(self, candidate_signals: List[Tuple],
                                     interval_str: str) -> List[Tuple]:
        """
        多策略共振合并 — 同 symbol+market+action 内 ≥2 策略 → 合并 super signal
        返回值替换 candidate_signals（避免单策略 + super signal 双重入库）
        Level 1: 任意 2 策略 → conf = max + 15
        Level 2: 任意 3 策略 → conf = 100, sizing_boost = 1.2
        Level 3: 黄金组合命中 → conf = 100, sizing_boost = 1.5
        """
        if not candidate_signals:
            return candidate_signals
        # 按 (symbol, market, action) 分组
        from collections import defaultdict
        groups = defaultdict(list)
        for sig, strat_name in candidate_signals:
            mkt = sig.market.value if hasattr(sig.market, "value") else str(sig.market)
            key = (sig.symbol, mkt, sig.action)
            groups[key].append((sig, strat_name))

        out: List[Tuple] = []
        for key, group in groups.items():
            if len(group) <= 1:
                # 单策略 — 原样保留
                out.extend(group)
                continue
            # 共振：去重策略名（避免同策略多次评估重复计数）
            strat_names = sorted({n for _, n in group})
            if len(strat_names) <= 1:
                # 多条但同策略（罕见）— 保留 conf 最高那条
                best = max(group, key=lambda x: x[0].confidence)
                out.append(best)
                continue
            # Level 检测
            if self._is_golden_combo(set(strat_names)):
                level, sizing_boost = 3, 1.5
                target_conf = 100
            elif len(strat_names) >= 3:
                level, sizing_boost = 2, 1.2
                target_conf = 100
            else:
                level, sizing_boost = 1, 1.0
                max_conf = max(s.confidence for s, _ in group)
                target_conf = min(100, max_conf + 15)

            # 构造 super signal — 用第一条信号的 base 字段，覆盖关键字段
            base_sig, _ = group[0]
            try:
                import dataclasses, uuid
                # 黄金组合命中的命名以最具代表性的策略为主
                super_sig = dataclasses.replace(
                    base_sig,
                    id=str(uuid.uuid4()),  # 新 id（避免与原始信号 id 冲突）
                    confidence=target_conf,
                    strategy_name="resonance",
                    reason=f"🌟 L{level} 共振 ({len(strat_names)} 策略): {' + '.join(strat_names)}",
                    triggered_by={
                        "resonance_level": level,
                        "strategies": strat_names,
                        "sizing_boost": sizing_boost,
                        "is_golden": level == 3,
                        "original_confs": {n: s.confidence for s, n in group},
                        "interval": interval_str,
                    },
                )
            except Exception as e:
                logger.warning(f"[resonance] 合并失败 {key}: {e} → fallback 保留 conf 最高单策略")
                out.append(max(group, key=lambda x: x[0].confidence))
                continue
            logger.info(
                f"🌟 [resonance-L{level}] {key[0]}/{key[2]} {len(strat_names)} 策略共振 "
                f"({' + '.join(strat_names)}) → conf={target_conf} boost={sizing_boost}"
            )
            out.append((super_sig, "resonance"))
        return out

    async def _save_and_broadcast(self, signal: Signal):
        """保存信号到 DB + WebSocket 推送。"""
        # v12.11: 推导 side（long/short）—— 加密 sell 默认 short（开空），股票 sell 一律 long（平多）
        # 策略可在 signal.triggered_by['side'] 显式指定覆盖
        sig_side = "long"
        try:
            tb = signal.triggered_by or {}
            if isinstance(tb, dict) and tb.get("side") in ("long", "short"):
                sig_side = tb["side"]
            elif signal.market.value == "crypto" and signal.action == "sell":
                sig_side = "short"  # 加密 sell 视为开空（自动反向开仓允许）
        except Exception:
            pass

        async with self.db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO signals (
                    id, symbol, market, action, side, strategy_name, interval, confidence,
                    price, suggested_qty, stop_loss, take_profit,
                    reason, triggered_by, status, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    signal.id,
                    signal.symbol,
                    signal.market.value,
                    signal.action,
                    sig_side,
                    signal.strategy_name,
                    getattr(signal, "interval", "1H"),
                    signal.confidence,
                    signal.price,
                    signal.suggested_qty,
                    signal.stop_loss,
                    signal.take_profit,
                    signal.reason,
                    json.dumps(signal.triggered_by or {}),
                    signal.generated_at,
                ),
            )
            await conn.commit()

        # WebSocket 推送（节流：最少 200ms 间隔，避免前端被洪流卡死）
        async with self._broadcast_lock:
            now_ms = int(time.time() * 1000)
            gap = now_ms - self._last_broadcast_ms
            if gap < 200:
                await asyncio.sleep((200 - gap) / 1000)
            self._last_broadcast_ms = int(time.time() * 1000)
        try:
            await self.ws_hub.broadcast_signal({
                "id": signal.id,
                "symbol": signal.symbol,
                "market": signal.market.value,
                "action": signal.action,
                "strategy_name": signal.strategy_name,
                "interval": getattr(signal, "interval", "1H"),
                "confidence": signal.confidence,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "reason": signal.reason,
                "generated_at": signal.generated_at,
            })
        except Exception as e:
            logger.warning(f"信号推送失败: {e}")

        logger.info(
            f"📡 信号触发 {signal.symbol} {signal.action} "
            f"@{signal.price:.4f} confidence={signal.confidence} "
            f"strategy={signal.strategy_name}"
        )

        # AI 二次验证触发规则（省 LLM 成本，按候选池 rating 分层阈值）：
        #   - strong_buy: confidence ≥ 65（主动抓入场机会）
        #   - buy:        confidence ≥ 70
        #   - reduce:     confidence ≥ 70（仅 SELL 方向，快出场）
        #   - sell:       confidence ≥ 75（仅 SELL 方向）
        #   - hold:       confidence ≥ 80（过滤噪音）
        #   - 无 rating / 不在池 / 加密: 默认 SIGNAL_MIN_CONFIDENCE
        #   + 股票 SELL 无持仓 → 跳过
        #   + 10 分钟内同 symbol+action 验证复用
        mkt = signal.market.value if hasattr(signal.market, "value") else str(signal.market)
        pool_rating = None
        diag_age_h = None
        try:
            async with self.db.acquire() as conn:
                if mkt == "crypto":
                    # 加密从 crypto_diagnosis 表读 rating + 诊断时间
                    cur = await conn.execute(
                        "SELECT rating, diagnosed_at FROM crypto_diagnosis WHERE symbol=? LIMIT 1",
                        (signal.symbol,),
                    )
                    row = await cur.fetchone()
                    if row:
                        pool_rating = row["rating"]
                        ts = row["diagnosed_at"] or 0
                        if ts:
                            diag_age_h = (time.time() - ts) / 3600
                else:
                    # 股票从 watch_pool 读（JSON 提取 rating）
                    cur = await conn.execute(
                        "SELECT (CASE WHEN json_valid(ai_diagnosis) THEN "
                        "json_extract(ai_diagnosis, '$.rating') ELSE NULL END) AS r, "
                        "ai_diagnosed_at "
                        "FROM watch_pool WHERE symbol=? AND market=? AND status!='archived' LIMIT 1",
                        (signal.symbol, mkt),
                    )
                    row = await cur.fetchone()
                    if row:
                        pool_rating = row["r"]
                        ts = row["ai_diagnosed_at"] or 0
                        if ts:
                            diag_age_h = (time.time() - ts) / 3600
        except Exception:
            pass

        default_thr = config.SIGNAL_MIN_CONFIDENCE
        if pool_rating == "strong_buy":
            required_conf = 65
        elif pool_rating == "buy":
            required_conf = 70
        elif pool_rating == "reduce" and signal.action == "sell":
            required_conf = 70
        elif pool_rating == "sell" and signal.action == "sell":
            required_conf = 75
        elif pool_rating == "hold":
            required_conf = 80
        else:
            required_conf = default_thr

        # v12.13 修复：默认 False 避免 else 分支后引用未定义变量（之前导致 chanlun/volume_breakout 等评估异常）
        should_verify = False
        if signal.confidence >= required_conf:
            should_verify = True
            # 股票 SELL 无持仓
            if mkt != "crypto" and signal.action == "sell":
                try:
                    async with self.db.acquire() as conn:
                        cur = await conn.execute(
                            "SELECT 1 FROM positions WHERE symbol=? AND market=? AND quantity > 0 LIMIT 1",
                            (signal.symbol, mkt),
                        )
                        if not await cur.fetchone():
                            should_verify = False
                            await self._mark_verify_skipped(
                                signal.id,
                                "无持仓，股票 SELL 信号无意义（自动交易仅做多）",
                            )
                            logger.debug(f"[verify-skip] {signal.symbol}({mkt}) SELL 无持仓")
                except Exception:
                    pass
        else:
            # v12.13 信号置信度低于阈值 → 显式标 skipped（前端区分"无需验证"vs"还在排队"）
            await self._mark_verify_skipped(
                signal.id,
                f"信号置信度 {signal.confidence} < 阈值 {required_conf}（rating={pool_rating or '-'}）",
            )

        # === v2 修订：按池中 rating + age 决定 verify 路径 ===
        # v12.14 (B5 修复): 诊断时效从 24h/12h 收紧到 2h/1h —— 8h+ 前的 buy 与 0min 后的 K 线已经脱节
        # strong_buy + age<1h + BUY 信号 → 跳过 LLM verify，直接 confirm（强买直通）
        # buy + age<2h + BUY 信号 → 简化 verify（K 线趋势 + 近 24h ★3+ 利空新闻 双门槛）
        # 其他（含老诊断）→ 完整 LLM verify
        handled_by_fast_path = False
        if should_verify and signal.action == "buy" and pool_rating in ("strong_buy", "buy") and diag_age_h is not None:
            if pool_rating == "strong_buy" and diag_age_h < 1:
                await self._fast_confirm(signal, "强买直通", 85, f"基于诊断 strong_buy（{diag_age_h:.2f}h 前），跳过 LLM 验证")
                handled_by_fast_path = True
            elif pool_rating == "buy" and diag_age_h < 2:
                await self._simplified_verify_buy(signal, diag_age_h)
                handled_by_fast_path = True

        # v12.13 修复：verify-reuse 块要在 fast_path if 外（之前缩进错让"非 strong_buy/buy"信号永远跑不到 verify）
        if should_verify and not handled_by_fast_path:
            # 规则 3：近 10 分钟同 symbol+market+action 已有验证结果 → 复用
            reused = False
            try:
                cutoff_ms = int(time.time() * 1000) - 10 * 60 * 1000
                async with self.db.acquire() as conn:
                    cur = await conn.execute(
                        """SELECT ai_verdict, ai_confidence, ai_reason,
                                  ai_stop_loss, ai_take_profit, ai_news_ids
                           FROM signals
                           WHERE symbol=? AND market=? AND action=?
                             AND ai_verdict != '' AND ai_verified_at > ?
                             AND id != ?
                           ORDER BY ai_verified_at DESC LIMIT 1""",
                        (signal.symbol, mkt, signal.action, cutoff_ms, signal.id),
                    )
                    prev = await cur.fetchone()
                if prev:
                    # 直接复制上次验证结果到当前信号
                    async with self.db.acquire() as conn2:
                        await conn2.execute(
                            """UPDATE signals SET ai_verdict=?, ai_confidence=?, ai_reason=?,
                                   ai_verified_at=?, ai_stop_loss=?, ai_take_profit=?, ai_news_ids=?
                               WHERE id=?""",
                            (prev["ai_verdict"], prev["ai_confidence"],
                             f"[复用 10 分钟内同股同向验证] {prev['ai_reason'] or ''}",
                             int(time.time() * 1000),
                             prev["ai_stop_loss"], prev["ai_take_profit"],
                             prev["ai_news_ids"] or "",
                             signal.id),
                        )
                        await conn2.commit()
                    # WS 推送一份带 verdict 的信号更新
                    try:
                        broadcast = getattr(self.ws_hub, "broadcast_signal", None)
                        if broadcast:
                            await broadcast({
                                "id": signal.id, "symbol": signal.symbol,
                                "_ai_update": True,
                                "ai_verdict": prev["ai_verdict"],
                                "ai_confidence": prev["ai_confidence"],
                                "ai_reason": f"[复用] {prev['ai_reason'] or ''}",
                                "ai_stop_loss": prev["ai_stop_loss"],
                                "ai_take_profit": prev["ai_take_profit"],
                            })
                    except Exception:
                        pass
                    logger.info(
                        f"[verify-reuse] {signal.symbol}/{signal.action} "
                        f"复用 10 分钟内 {prev['ai_verdict']}(conf {prev['ai_confidence']})"
                    )
                    # 复用 confirm 同样要触发 auto_trader，否则复用信号永远不开单
                    try:
                        if prev["ai_verdict"] == "confirm":
                            from backend.main import auto_trader
                            if auto_trader and auto_trader.enabled:
                                self._spawn(auto_trader.on_signal_verified(signal.id))
                    except Exception as e:
                        logger.debug(f"[verify-reuse auto-trader hook] 异常: {e}")
                    reused = True
            except Exception as e:
                logger.debug(f"[verify-reuse] 失败，走正常 LLM: {e}")
            if not reused:
                self._spawn(self._ai_verify_signal(signal))

    # tech_snapshot 5 分钟缓存：同 symbol+interval 的 verify/simplified_verify 共享计算
    # （原来每次 verify 都重新拉 100 根 K 线 + 重算 8 个指标，浪费严重）
    _TECH_CACHE_TTL_MS = 5 * 60 * 1000
    _tech_snapshot_cache: Dict[str, Tuple[int, str]] = {}

    async def _build_tech_snapshot(self, signal) -> str:
        """构建技术面快照文本，供 LLM 参考。包含 RSI/MACD/MA/ATR/支撑阻力/量能/形态。"""
        cache_key = f"{signal.symbol}|{signal.market.value}|{getattr(signal, 'interval', '1H')}"
        now_ms = int(time.time() * 1000)
        hit = self._tech_snapshot_cache.get(cache_key)
        if hit and (now_ms - hit[0] < self._TECH_CACHE_TTL_MS):
            return hit[1]
        try:
            import numpy as np
            from backend.data.models import Interval
            interval_str = getattr(signal, "interval", "1H")
            try:
                interval_enum = Interval(interval_str)
            except ValueError:
                interval_enum = Interval.H1
            candles = await cached_get_klines(
                db=self.db, market=signal.market, symbol=signal.symbol,
                interval=interval_enum, limit=100,
            )
            if not candles or len(candles) < 30:
                msg = "（K 线数据不足，无法计算技术面）"
                self._tech_snapshot_cache[cache_key] = (now_ms, msg)
                return msg

            closes = np.array([c.close for c in candles], dtype=np.float64)
            highs = np.array([c.high for c in candles], dtype=np.float64)
            lows = np.array([c.low for c in candles], dtype=np.float64)
            volumes = np.array([c.volume for c in candles], dtype=np.float64)
            last_close = float(closes[-1])
            prev_close = float(closes[-2])
            pct_change = (last_close - prev_close) / prev_close * 100 if prev_close else 0

            from backend.indicators.builtin import calc_ma, calc_rsi, calc_macd, calc_volume_ma

            # 均线
            ma5 = calc_ma(closes, 5)[-1]
            ma10 = calc_ma(closes, 10)[-1]
            ma20 = calc_ma(closes, 20)[-1]
            ma50 = calc_ma(closes, 50)[-1] if len(closes) >= 50 else float('nan')

            # 趋势判定
            def _ma_trend(m5, m10, m20):
                if any(np.isnan(x) for x in [m5, m10, m20]): return "未知"
                if m5 > m10 > m20: return "多头排列"
                if m5 < m10 < m20: return "空头排列"
                return "纠缠"

            # RSI
            rsi = calc_rsi(closes, 14)[-1]
            rsi_state = "未知"
            if not np.isnan(rsi):
                if rsi > 70: rsi_state = "超买"
                elif rsi < 30: rsi_state = "超卖"
                elif rsi > 50: rsi_state = "中性偏强"
                else: rsi_state = "中性偏弱"

            # MACD（calc_macd 返回 dict）
            macd_out = calc_macd(closes)
            dif = macd_out["dif"]
            dea = macd_out["dea"]
            hist = macd_out["histogram"]
            dif_last = float(dif[-1])
            dea_last = float(dea[-1])
            hist_last = float(hist[-1])
            hist_prev = float(hist[-2]) if len(hist) >= 2 else 0.0
            macd_state = "金叉" if hist_last > 0 and hist_prev < 0 else \
                         "死叉" if hist_last < 0 and hist_prev > 0 else \
                         ("多头强化" if hist_last > 0 and hist_last > hist_prev else
                          "多头减弱" if hist_last > 0 else
                          "空头强化" if hist_last < hist_prev else "空头减弱")

            # 量能
            vma20 = calc_volume_ma(volumes, 20)[-1]
            last_vol = float(volumes[-1])
            vol_ratio = last_vol / vma20 if vma20 > 0 else 0

            # ATR（近 14 根）
            trs = []
            for i in range(-14, 0):
                if i == -14:
                    trs.append(float(highs[i] - lows[i]))
                else:
                    tr = max(
                        float(highs[i] - lows[i]),
                        abs(float(highs[i] - closes[i - 1])),
                        abs(float(lows[i] - closes[i - 1])),
                    )
                    trs.append(tr)
            atr14 = sum(trs) / len(trs)

            # v12.13: ADX 趋势强度（DI+/DI- + ADX 14 周期）
            # 用途：让 LLM 判断当前是趋势市还是震荡市
            #   ADX > 25：趋势强（趋势策略 ma_cross/breakout/chanlun 加分）
            #   ADX < 20：震荡市（反转策略 bollinger_reversion/rsi_divergence 加分）
            adx_val = float('nan')
            di_plus_val = float('nan')
            di_minus_val = float('nan')
            try:
                if len(closes) >= 28:
                    period = 14
                    plus_dm = np.zeros(len(closes))
                    minus_dm = np.zeros(len(closes))
                    tr_arr = np.zeros(len(closes))
                    for i in range(1, len(closes)):
                        up = highs[i] - highs[i-1]
                        dn = lows[i-1] - lows[i]
                        plus_dm[i] = up if (up > dn and up > 0) else 0.0
                        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
                        tr_arr[i] = max(highs[i] - lows[i],
                                        abs(highs[i] - closes[i-1]),
                                        abs(lows[i] - closes[i-1]))
                    # Wilder smoothing
                    def _wilder(arr, p):
                        s = np.zeros_like(arr)
                        s[p] = arr[1:p+1].sum()
                        for i in range(p+1, len(arr)):
                            s[i] = s[i-1] - s[i-1]/p + arr[i]
                        return s
                    tr_smooth = _wilder(tr_arr, period)
                    plus_smooth = _wilder(plus_dm, period)
                    minus_smooth = _wilder(minus_dm, period)
                    with np.errstate(divide='ignore', invalid='ignore'):
                        di_plus = 100 * plus_smooth / tr_smooth
                        di_minus = 100 * minus_smooth / tr_smooth
                        dx = 100 * np.abs(di_plus - di_minus) / (di_plus + di_minus)
                    dx = np.nan_to_num(dx, nan=0.0)
                    # ADX = DX 的 Wilder 平滑
                    adx_arr = np.zeros_like(dx)
                    adx_arr[2*period] = dx[period+1:2*period+1].mean()
                    for i in range(2*period+1, len(dx)):
                        adx_arr[i] = (adx_arr[i-1] * (period-1) + dx[i]) / period
                    adx_val = float(adx_arr[-1])
                    di_plus_val = float(di_plus[-1])
                    di_minus_val = float(di_minus[-1])
            except Exception:
                pass

            # 趋势市判定 + 推荐策略提示
            adx_state = "未知"
            if not np.isnan(adx_val):
                if adx_val >= 25:
                    adx_state = f"强趋势 (ADX={adx_val:.1f}≥25; 趋势策略加分)"
                elif adx_val >= 20:
                    adx_state = f"弱趋势 (ADX={adx_val:.1f})"
                else:
                    adx_state = f"震荡市 (ADX={adx_val:.1f}<20; 反转策略加分)"

            # VWAP（成交量加权均价 — 仅当日内 K 线有意义；日线 K 线 VWAP=均价无信息）
            vwap_line = ""
            if interval_str in ("1m", "5m", "15m", "30m", "1H"):
                try:
                    typical = (highs + lows + closes) / 3.0
                    # 取最后 N 根算"今日 VWAP"（按 interval 估算 N）
                    bars_per_day = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "1H": 6}.get(interval_str, 24)
                    n = min(bars_per_day, len(closes))
                    cum_pv = (typical[-n:] * volumes[-n:]).sum()
                    cum_v = volumes[-n:].sum()
                    if cum_v > 0:
                        vwap_val = float(cum_pv / cum_v)
                        vwap_diff_pct = (last_close - vwap_val) / vwap_val * 100
                        vwap_pos = "上方（多头主导）" if last_close > vwap_val else "下方（空头主导）"
                        vwap_line = f"\n- VWAP（近 {n} 根）：{vwap_val:.4f}，价格在 VWAP {vwap_pos} {vwap_diff_pct:+.2f}%"
                except Exception:
                    pass

            # 历史波动率（HV）— 20 日年化波动率，VIX 替代品
            #   HV ≥ 35（年化）：高波动期，BUY 信号谨慎；HV ≤ 15：低波动趋势期
            hv_line = ""
            try:
                if len(closes) >= 21:
                    log_ret = np.diff(np.log(closes[-21:]))
                    daily_std = float(log_ret.std())
                    # 按 interval 推算年化系数
                    annualize = {"1D": np.sqrt(252), "1H": np.sqrt(252*6.5),
                                 "4H": np.sqrt(252*1.6), "15m": np.sqrt(252*26)}.get(interval_str, np.sqrt(252))
                    hv_pct = daily_std * annualize * 100
                    hv_state = "高波动" if hv_pct >= 35 else "低波动" if hv_pct <= 15 else "正常"
                    hv_line = f"\n- 历史波动率(20根)：{hv_pct:.1f}% 年化 ({hv_state})"
            except Exception:
                pass

            # 支撑/阻力（近 20/50 根高低）
            hi20 = float(highs[-20:].max())
            lo20 = float(lows[-20:].min())
            hi50 = float(highs[-50:].max()) if len(highs) >= 50 else hi20
            lo50 = float(lows[-50:].min()) if len(lows) >= 50 else lo20
            dist_to_hi20 = (hi20 - last_close) / last_close * 100
            dist_to_lo20 = (last_close - lo20) / last_close * 100

            # 近期形态（连续 3 根阴阳）
            last3 = closes[-3:]
            last3_open = np.array([c.open for c in candles[-3:]])
            recent_pattern = ""
            if all(last3 > last3_open):
                recent_pattern = "连续 3 根阳线"
            elif all(last3 < last3_open):
                recent_pattern = "连续 3 根阴线"
            else:
                recent_pattern = "阴阳交替"

            snapshot = f"""【技术面快照（基于最近 {len(candles)} 根 {interval_str} K 线）】
- 当前价：{last_close:.4f} (相对上根 {'+' if pct_change >= 0 else ''}{pct_change:.2f}%)
- 均线排列：MA5={ma5:.4f} / MA10={ma10:.4f} / MA20={ma20:.4f} / MA50={ma50:.4f}{' (多头排列)' if _ma_trend(ma5, ma10, ma20) == '多头排列' else ' (空头排列)' if _ma_trend(ma5, ma10, ma20) == '空头排列' else ' (纠缠)'}
- 价格 vs MA20：{((last_close - ma20) / ma20 * 100):+.2f}% ({'上方' if last_close > ma20 else '下方'})
- RSI(14)：{rsi:.1f} ({rsi_state})
- MACD：DIF={dif_last:.4f} / DEA={dea_last:.4f} / HIST={hist_last:.4f} → {macd_state}
- 量能：当前 vol={last_vol:.0f} vs 20日均量={vma20:.0f}，放量 {vol_ratio:.2f}x {'✅' if vol_ratio >= 1.5 else '⚠️' if vol_ratio < 0.7 else ''}
- 波动率 ATR(14)：{atr14:.4f}
- 趋势强度 ADX(14)：{adx_state}; DI+={di_plus_val:.1f} / DI-={di_minus_val:.1f}{vwap_line}{hv_line}
- 20日区间：高 {hi20:.4f} / 低 {lo20:.4f} | 距 20日高 {dist_to_hi20:.2f}%（阻力）| 距 20日低 {dist_to_lo20:.2f}%（支撑）
- 50日区间：高 {hi50:.4f} / 低 {lo50:.4f}
- 近 3 根形态：{recent_pattern}"""
            self._tech_snapshot_cache[cache_key] = (now_ms, snapshot)
            # 定期清理过期 cache 避免无限增长（>500 条时批量删过期的）
            if len(self._tech_snapshot_cache) > 500:
                cutoff = now_ms - self._TECH_CACHE_TTL_MS
                expired = [k for k, v in self._tech_snapshot_cache.items() if v[0] < cutoff]
                for k in expired:
                    self._tech_snapshot_cache.pop(k, None)
            return snapshot
        except Exception as e:
            logger.warning(f"[tech-snapshot] {signal.symbol} 构建失败: {e}")
            return "（技术面快照构建失败）"

    async def _fast_confirm(self, signal, source_label: str, ai_conf: int, reason: str):
        """
        快速通道：跳过 LLM verify 直接标 confirm。
        用于候选池 rating=strong_buy（age<12h）的 BUY 信号 → 强买直通。
        附带从 K 线/系统 SL/TP 推算 ai_stop_loss / ai_take_profit，让前端"详情"模态框可显示。
        v12.15: 在 fast-path 里也评估教训规则（避开 LLM 的同时仍执行硬规则）
        """
        # v12.15 教训采纳闭环 — fast_path 评估 risk_rules（仅 BUY 开仓信号）
        if signal.action == "buy":
            try:
                from backend.trading.risk_engine import evaluate_open_signal
                mkt = signal.market.value if hasattr(signal.market, "value") else str(signal.market)
                hit = await evaluate_open_signal(self.db, signal.symbol, mkt, signal_id=signal.id)
                if hit:
                    rule, hit_reason = hit
                    verdict_text = f"[教训规则拦截] {hit_reason}"
                    try:
                        async with self.db.acquire() as conn:
                            await conn.execute(
                                """UPDATE signals SET ai_verdict='reject', ai_confidence=30,
                                   ai_reason=?, ai_verified_at=? WHERE id=?""",
                                (verdict_text[:300], int(time.time() * 1000), signal.id),
                            )
                            await conn.commit()
                    except Exception:
                        pass
                    try:
                        broadcast = getattr(self.ws_hub, "broadcast_signal", None)
                        if broadcast:
                            await broadcast({
                                "id": signal.id, "symbol": signal.symbol,
                                "_ai_update": True,
                                "ai_verdict": "reject", "ai_confidence": 30,
                                "ai_reason": verdict_text,
                                "verdict_source": "教训规则拦截",
                            })
                    except Exception:
                        pass
                    logger.info(f"⚡ [risk-rules-block] {signal.symbol}/{signal.action} {hit_reason}")
                    return
            except Exception as e:
                logger.debug(f"[risk-rules] fast_confirm 评估异常: {e}")

        verdict_text = f"[{source_label}] {reason}"
        # 从信号自带 SL/TP 复制为 AI SL/TP（fast-path 不重新计算，沿用策略层给的）
        ai_sl = float(signal.stop_loss) if getattr(signal, "stop_loss", None) is not None else None
        ai_tp = float(signal.take_profit) if getattr(signal, "take_profit", None) is not None else None
        # 拉近 24h 相关新闻 ID 用于前端溯源（与 full verify 一致）
        news_ids_json = "[]"
        try:
            mkt = signal.market.value if hasattr(signal.market, "value") else str(signal.market)
            cutoff_ms = int(time.time() * 1000) - 24 * 3600 * 1000
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT id FROM flash_news WHERE published_at > ? AND categories LIKE ? "
                    "ORDER BY published_at DESC LIMIT 10",
                    (cutoff_ms, f'%"{signal.symbol}"%'),
                )
                ids = [r["id"] for r in await cur.fetchall()]
            news_ids_json = json.dumps(ids, ensure_ascii=False)
        except Exception:
            pass
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """UPDATE signals SET ai_verdict='confirm', ai_confidence=?, ai_reason=?,
                       ai_verified_at=?, ai_stop_loss=?, ai_take_profit=?, ai_news_ids=?
                       WHERE id=?""",
                    (ai_conf, verdict_text[:300], int(time.time() * 1000),
                     ai_sl, ai_tp, news_ids_json, signal.id),
                )
                await conn.commit()
        except Exception as e:
            logger.warning(f"[fast-confirm] {signal.symbol} 回填失败: {e}")
            return
        # WS 推送
        try:
            broadcast = getattr(self.ws_hub, "broadcast_signal", None)
            if broadcast:
                await broadcast({
                    "id": signal.id, "symbol": signal.symbol,
                    "_ai_update": True,
                    "ai_verdict": "confirm",
                    "ai_confidence": ai_conf,
                    "ai_reason": verdict_text,
                    "ai_stop_loss": ai_sl,
                    "ai_take_profit": ai_tp,
                    "verdict_source": source_label,  # 前端按此显示中文标签
                })
        except Exception:
            pass
        logger.info(f"⚡ [fast-confirm] {signal.symbol}/{signal.action} 「{source_label}」conf={ai_conf}")
        # 触发 auto_trader
        try:
            from backend.main import auto_trader
            if auto_trader and auto_trader.enabled:
                self._spawn(auto_trader.on_signal_verified(signal.id))
        except Exception as e:
            logger.debug(f"[fast-confirm auto-trader hook] 异常: {e}")

    async def _simplified_verify_buy(self, signal, diag_age_h: float):
        """
        简化验证：仅查 24h 利空新闻；趋势检查由 risk_rule trend_block 负责（_fast_confirm 入口）
        v12.15.2: 删除硬编码 4h 趋势检查（与 risk_rule trend_block 重复）— 统一走表驱动
        通过 → confirm（标"买入·无利空"）；有利空 → reject。
        用于候选池 rating=buy（age<2h）的 BUY 信号。
        """
        mkt = signal.market.value if hasattr(signal.market, "value") else str(signal.market)
        cutoff_ms = int(time.time() * 1000) - 24 * 3600 * 1000
        bad_news_title = None
        # 利空检测策略（覆盖 sentiment 字段缺失的情况）：
        #   1) 显式 sentiment='negative'
        #   2) sentiment 为 NULL 但标题含强利空关键词（暴跌/亏损/暴雷/退市/做空/调查/欺诈等）
        BAD_KEYWORDS = (
            '%暴跌%', '%亏损%', '%暴雷%', '%退市%', '%做空%', '%欺诈%', '%调查%', '%停牌%',
            '%诉讼%', '%处罚%', '%下调%', '%downgrade%', '%lawsuit%', '%investigation%',
            '%fraud%', '%plunge%', '%crash%', '%miss%', '%cut%',
        )
        try:
            async with self.db.acquire() as conn:
                # 先查显式 negative
                cur = await conn.execute(
                    """SELECT title FROM flash_news
                       WHERE published_at > ? AND categories LIKE ?
                         AND importance >= 3 AND sentiment='negative'
                       ORDER BY importance DESC, published_at DESC LIMIT 1""",
                    (cutoff_ms, f'%"{signal.symbol}"%'),
                )
                row = await cur.fetchone()
                if row:
                    bad_news_title = (row["title"] or "")[:60]
                else:
                    # 兜底：sentiment NULL/空但标题含利空关键词
                    where_kw = " OR ".join(["LOWER(title) LIKE ?"] * len(BAD_KEYWORDS))
                    cur = await conn.execute(
                        f"""SELECT title FROM flash_news
                           WHERE published_at > ? AND categories LIKE ?
                             AND importance >= 3
                             AND (sentiment IS NULL OR sentiment='' OR sentiment='neutral')
                             AND ({where_kw})
                           ORDER BY importance DESC, published_at DESC LIMIT 1""",
                        (cutoff_ms, f'%"{signal.symbol}"%', *BAD_KEYWORDS),
                    )
                    row2 = await cur.fetchone()
                    if row2:
                        bad_news_title = (row2["title"] or "")[:60]
        except Exception as e:
            logger.debug(f"[simplified-verify] {signal.symbol} 新闻查询异常: {e}")

        if bad_news_title:
            # 有利空 → reject
            verdict_text = f"[买入·遇利空] {bad_news_title}"
            try:
                async with self.db.acquire() as conn:
                    await conn.execute(
                        """UPDATE signals SET ai_verdict='reject', ai_confidence=30, ai_reason=?,
                           ai_verified_at=? WHERE id=?""",
                        (verdict_text[:300], int(time.time() * 1000), signal.id),
                    )
                    await conn.commit()
            except Exception:
                return
            try:
                broadcast = getattr(self.ws_hub, "broadcast_signal", None)
                if broadcast:
                    await broadcast({
                        "id": signal.id, "symbol": signal.symbol,
                        "_ai_update": True,
                        "ai_verdict": "reject", "ai_confidence": 30,
                        "ai_reason": verdict_text,
                        "verdict_source": "买入·遇利空",
                    })
            except Exception:
                pass
            logger.info(f"⚡ [simplified-verify] {signal.symbol} 利空否决: {bad_news_title[:30]}")
        else:
            # 无利空 → confirm（趋势已通过门槛）
            await self._fast_confirm(
                signal, "买入·无利空", 75,
                f"基于诊断 buy（{diag_age_h:.2f}h 前），4h 趋势未下跌 + 24h 无 ★3+ 利空新闻"
            )

    async def _mark_verify_skipped(self, signal_id: str, reason: str):
        """v12.13 显式把"无需 LLM 验证"的信号标成 skipped。
        场景：股票 SELL 无持仓 / 信号置信度低于阈值。
        前端用 ai_verdict='skipped' 区分"无需验证"和"还在排队验证中"两种状态。"""
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """UPDATE signals SET ai_verdict='skipped', ai_confidence=0,
                           ai_reason=?, ai_verified_at=? WHERE id=?""",
                    (reason[:300], int(time.time() * 1000), signal_id),
                )
                await conn.commit()
            try:
                broadcast = getattr(self.ws_hub, "broadcast_signal", None)
                if broadcast:
                    await broadcast({
                        "id": signal_id, "_ai_update": True,
                        "ai_verdict": "skipped", "ai_confidence": 0,
                        "ai_reason": reason,
                    })
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"[verify-skip] 写 skipped 失败 {signal_id}: {e}")

    async def _ai_verify_signal(self, signal):
        """异步对信号做 AI 二次验证 → 回填 DB + WS 推送 ai_verdict 事件。"""
        try:
            # 拉取该品种最近 24h 新闻（含 id 用于前端溯源）
            recent_news = []
            news_ids = []
            try:
                async with self.db.acquire() as conn:
                    cur = await conn.execute(
                        """SELECT id, title, source, importance, sentiment, published_at,
                                  is_macro_data, macro_impact_strength, ai_analysis
                           FROM flash_news
                           WHERE published_at > ? AND categories LIKE ?
                           ORDER BY published_at DESC LIMIT 10""",
                        (int(time.time() * 1000) - 24 * 3600 * 1000, f'%"{signal.symbol}"%'),
                    )
                    rows = await cur.fetchall()
                    for r in rows:
                        rd = dict(r)
                        news_ids.append(rd["id"])
                        # ai_analysis 反序列化（供 strategies.py 的 macro modifier 判断 tone）
                        if rd.get("ai_analysis"):
                            try:
                                rd["ai_analysis"] = json.loads(rd["ai_analysis"])
                            except Exception:
                                pass
                        recent_news.append(rd)
            except Exception:
                pass

            # 构建技术快照（RSI/MACD/MA/ATR/支撑阻力/量能/形态）供 LLM 参考
            tech_snapshot = await self._build_tech_snapshot(signal)

            # v12.13: 市场天气（北向资金 / CNN F&G）— 缓存共享，几乎无成本
            mkt_str = signal.market.value if hasattr(signal.market, "value") else str(signal.market)
            market_weather = "（暂无市场天气数据）"
            try:
                from backend.market_context import build_market_context
                market_weather = await build_market_context(mkt_str)
            except Exception as e:
                logger.debug(f"[verify] {signal.symbol} 市场天气拉取失败: {e}")

            # v12.13: 多策略共振查询 — 近 30min 同 symbol+action 其他策略的信号
            # （减少"孤狼信号"开仓，提升单笔胜率；不增加 LLM 调用数，仅给 verify 加上下文）
            consensus_context = "无（本信号是当前 30min 内首个）"
            try:
                cutoff_ms = int(time.time() * 1000) - 30 * 60 * 1000
                async with self.db.acquire() as conn:
                    cur = await conn.execute(
                        """SELECT strategy_name, interval, ai_verdict, ai_confidence, generated_at
                           FROM signals
                           WHERE symbol=? AND market=? AND action=?
                             AND id != ? AND generated_at >= ?
                           ORDER BY generated_at DESC LIMIT 8""",
                        (signal.symbol, mkt_str, signal.action, signal.id, cutoff_ms),
                    )
                    sib_rows = [dict(r) for r in await cur.fetchall()]
                if sib_rows:
                    confirm_n = sum(1 for r in sib_rows if r.get("ai_verdict") == "confirm")
                    warn_n = sum(1 for r in sib_rows if r.get("ai_verdict") == "warn")
                    reject_n = sum(1 for r in sib_rows if r.get("ai_verdict") == "reject")
                    pending_n = sum(1 for r in sib_rows if not r.get("ai_verdict"))
                    lines = [
                        f"共 {len(sib_rows)} 条同向信号；confirm={confirm_n} warn={warn_n} reject={reject_n} 排队={pending_n}",
                    ]
                    for r in sib_rows[:5]:
                        v = r.get("ai_verdict") or "排队中"
                        c = r.get("ai_confidence") or 0
                        lines.append(f"  · {r['strategy_name']}@{r.get('interval','-')}: {v}(AI{c})")
                    consensus_context = "\n".join(lines)
            except Exception as e:
                logger.debug(f"[verify] {signal.symbol} 共振查询失败: {e}")

            # 通过全局 ai_analyzer 调 LLM（复用 NewsAIAnalyzer 实例）
            from backend.news.scheduler import _ai_analyzer
            if _ai_analyzer is None:
                return
            verdict = await _ai_analyzer.verify_signal({
                "id": signal.id,
                "symbol": signal.symbol,
                "market": signal.market.value,
                "action": signal.action,
                "strategy_name": signal.strategy_name,
                "interval": getattr(signal, "interval", "1H"),
                "confidence": signal.confidence,
                "price": signal.price,
                "reason": signal.reason,
                "tech_snapshot": tech_snapshot,
                "consensus_context": consensus_context,
                "market_weather": market_weather,
            }, recent_news)
            if not verdict:
                # LLM 调用/解析失败 → 新 verdict=llm_error，与 warn（AI 评 40-59 分）严格区分
                # 这样前端能清楚看到"LLM 没跑起来"（通常是 API Key 余额不足/网络/限流）
                # 而不是误以为 AI 评估认为信号质量不佳
                verdict = {
                    "verdict": "llm_error",
                    "ai_confidence": 0,
                    "reason": "⛔ LLM 调用失败（常见原因：API Key 余额不足 / 网络 / 限流）。请检查 DeepSeek 账户余额或后端日志。",
                    "ai_stop_loss": None,
                    "ai_take_profit": None,
                }

            # 回填 DB（含 AI 给的 SL/TP 调整建议）
            try:
                async with self.db.acquire() as conn:
                    await conn.execute(
                        """UPDATE signals SET ai_verdict=?, ai_confidence=?, ai_reason=?,
                           ai_verified_at=?, ai_stop_loss=?, ai_take_profit=?, ai_news_ids=?
                           WHERE id=?""",
                        (verdict["verdict"], verdict["ai_confidence"], verdict["reason"],
                         int(time.time() * 1000),
                         verdict.get("ai_stop_loss"), verdict.get("ai_take_profit"),
                         json.dumps(news_ids, ensure_ascii=False),
                         signal.id),
                    )
                    await conn.commit()
            except Exception as e:
                logger.debug(f"AI verdict 回填失败: {e}")

            # WS 推送验证结果（带 AI SL/TP）
            try:
                broadcast = getattr(self.ws_hub, "broadcast_signal", None)
                if broadcast:
                    await broadcast({
                        "id": signal.id,
                        "symbol": signal.symbol,
                        "_ai_update": True,
                        "ai_verdict": verdict["verdict"],
                        "ai_confidence": verdict["ai_confidence"],
                        "ai_reason": verdict["reason"],
                        "ai_stop_loss": verdict.get("ai_stop_loss"),
                        "ai_take_profit": verdict.get("ai_take_profit"),
                    })
            except Exception:
                pass
            logger.info(
                f"🤖 AI 验证 {signal.symbol}/{signal.action}: "
                f"{verdict['verdict']} (AI{verdict['ai_confidence']}) - {verdict['reason'][:50]}"
            )
            # 自动交易 hook：若 verify 为 confirm 且开关打开，评估是否开仓/加仓
            try:
                from backend.main import auto_trader
                if auto_trader and auto_trader.enabled and verdict["verdict"] == "confirm":
                    self._spawn(auto_trader.on_signal_verified(signal.id))
            except Exception as e:
                logger.debug(f"[auto-trader hook] 异常: {e}")
        except Exception as e:
            logger.debug(f"AI 验证异常 {signal.symbol}: {e}")
