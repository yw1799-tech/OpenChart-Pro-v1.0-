"""
新闻采集调度器（PRD F4.1 / TDD §1.3 后台任务）。

随 FastAPI 启动，按每个源 config['interval'] 定时拉取，
拉到的新闻经规则引擎评分后入库 + WebSocket 推送给前端。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from backend.news.collector import NewsCollector, create_collector
from backend.news.dedup import url_hash, simhash_text, simhash_distance, SIMHASH_DUP_THRESHOLD
from backend.news.rule_engine import score_news
from backend.news.sources import get_enabled_sources

logger = logging.getLogger(__name__)

# 全局 AI Analyzer 引用（在 main.py 启动时通过 attach_ai_analyzer 注入）
_ai_analyzer = None


def attach_ai_analyzer(analyzer):
    """从外部注入 NewsAIAnalyzer 实例（避免循环 import）。"""
    global _ai_analyzer
    _ai_analyzer = analyzer


def _normalize_hk_symbol(s: str) -> str:
    """
    v11.5 港股 symbol 标准化：HKEX 收集器有时给 5 位 (00700.HK)，watch_pool 存的是 4 位 (0700.HK)。
    剥前导 0，保留至少 4 位（不到补齐）。
    """
    if not s.upper().endswith(".HK"):
        return s
    code, suffix = s.upper().rsplit(".", 1)
    code = code.lstrip("0")
    if len(code) < 4:
        code = code.zfill(4)
    return f"{code}.{suffix}"


def _infer_stock_market(symbol: str) -> str:
    """
    根据 symbol 推断市场。
    返回 'us' / 'hk' / 'cn' / 'crypto' / 'unknown'
    候选池只接受 us/hk/cn（加密 6 币种走专属监控通道）。
    """
    if not symbol:
        return "unknown"
    s = symbol.upper()
    # 加密 USDT 对
    if s.endswith("-USDT") or s.endswith("-USD") or s.endswith("-USDC"):
        return "crypto"
    # 港股: 如 0700.HK / 9988.HK / 00700.HK 都识别为 hk
    if s.endswith(".HK"):
        return "hk"
    # A 股: 6 位纯数字 (主板/科创板/创业板)
    if s.isdigit() and len(s) == 6:
        return "cn"
    # 美股: 1-5 位纯字母
    if s.isalpha() and 1 <= len(s) <= 5:
        return "us"
    return "unknown"


class NewsScheduler:
    """
    后台采集调度器。

    每个启用的源开一个独立 asyncio 任务，按其 interval 循环拉取。
    新拉到的新闻：
      1. URL hash 去重
      2. content_hash 去重（DB 查询）
      3. 规则引擎评分
      4. importance >= 1 才入库
      5. importance >= 3 通过 WebSocket 推送前端
    """

    def __init__(self, db, ws_hub, holding_provider=None, pool_provider=None):
        """
        db:          DatabaseManager 实例
        ws_hub:      WebSocketHub 实例
        holding_provider: 可选 callable() -> Set[str] 持仓品种
        pool_provider:    可选 callable() -> Set[str] 候选池品种
        """
        self.db = db
        self.ws_hub = ws_hub
        self.holding_provider = holding_provider or (lambda: set())
        self.pool_provider = pool_provider or (lambda: set())
        self._tasks: List[asyncio.Task] = []
        self._collectors: Dict[str, NewsCollector] = {}
        self._running = False
        self._url_seen: Dict[str, List[str]] = {}
        # 保存 fire-and-forget 任务引用（LLM 分析、入池、持仓缓冲等），避免 GC 丢任务
        self._bg_tasks: set = set()
        # 限制 diagnose_stock(force=True) 并发数 — 防止爆炸性 LLM 调用
        self._diagnose_sem = asyncio.Semaphore(2)

    async def _diagnose_with_limit(self, symbol: str, market: str):
        """带信号量的 diagnose_stock，保证同时最多 2 个 LLM 调用。"""
        if _ai_analyzer is None:
            return
        async with self._diagnose_sem:
            try:
                await _ai_analyzer.diagnose_stock(symbol, market, force=True)
            except Exception as e:
                logger.debug(f"[diagnose-limited] {symbol} 失败: {e}")

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        def _done(task):
            self._bg_tasks.discard(task)
            if not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.warning(f"[scheduler] 后台任务异常: {type(exc).__name__}: {exc}")
        t.add_done_callback(_done)
        return t

    def start(self):
        """启动所有启用源的采集循环。"""
        if self._running:
            logger.warning("NewsScheduler 已在运行")
            return

        sources = get_enabled_sources()
        logger.info(f"NewsScheduler 启动，启用源: {len(sources)} 个")
        for src in sources:
            collector = create_collector(src)
            self._collectors[src["name"]] = collector
            task = asyncio.create_task(self._run_loop(collector))
            self._tasks.append(task)
        self._running = True

    async def stop(self):
        """停止所有采集任务。"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for collector in self._collectors.values():
            await collector.close()
        self._tasks.clear()
        self._collectors.clear()
        logger.info("NewsScheduler 已停止")

    def get_health(self) -> List[Dict[str, Any]]:
        """各源健康度快照（供 /api/news/sources 端点）。"""
        return [c.get_stats() for c in self._collectors.values()]

    async def _run_loop(self, collector: NewsCollector):
        """单个 collector 的采集循环。"""
        while self._running:
            try:
                await self._fetch_once(collector)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[{collector.name}] 采集循环异常: {e}")
            # 等到下次拉取
            try:
                await asyncio.sleep(collector.interval)
            except asyncio.CancelledError:
                break

    async def _fetch_once(self, collector: NewsCollector):
        """单次采集：拉取 → 去重 → 规则评分 → 入库 → 推送。"""
        # 动态降级：连续失败时按指数退避或永久禁用
        if collector.should_skip_fetch():
            return
        items = await collector.fetch()
        if not items:
            return

        # URL hash 去重（内存级）
        seen = self._url_seen.setdefault(collector.name, [])
        seen_set = set(seen)
        new_items = []
        for it in items:
            uh = url_hash(it.get("url", "") or it["id"])
            if uh in seen_set:
                continue
            new_items.append((it, uh))
            seen.append(uh)
        # 简单 LRU：限制大小
        if len(seen) > 500:
            self._url_seen[collector.name] = seen[-500:]

        if not new_items:
            return

        # 规则引擎评分 + 入库
        try:
            holdings = self.holding_provider()
            pool_syms = self.pool_provider()
        except Exception:
            holdings = set()
            pool_syms = set()

        saved_count = 0
        pool_added_count = 0
        # 性能优化：本批次入口预取一次近 24h simhash 列表，所有新闻复用
        # limit=8000：活跃日单源可产生数千条，2000 太小会导致早期 simhash 被截、重复入库
        try:
            _batch_simhashes = await self.db.find_similar_simhashes(window_hours=24, limit=8000)
        except Exception:
            _batch_simhashes = []
        for raw_news, _ in new_items:
            try:
                # 内容 hash 去重（DB 查）
                if raw_news.get("content_hash"):
                    if await self.db.is_news_duplicate(raw_news["content_hash"]):
                        continue

                # SimHash 语义去重（同事件不同媒体表述）
                try:
                    sim = simhash_text(
                        (raw_news.get("title") or "") + " " + (raw_news.get("content") or "")[:500]
                    )
                    if sim:
                        raw_news["simhash"] = sim
                        if any(simhash_distance(sim, h) <= SIMHASH_DUP_THRESHOLD for h in _batch_simhashes):
                            logger.debug(f"[dedup-sim] 语义重复丢弃: {raw_news.get('title','')[:40]}")
                            continue
                        # 本批次后续复用更新后的列表（避免批内重复保存相似新闻）
                        _batch_simhashes.append(sim)
                except Exception as e:
                    logger.debug(f"[dedup-sim] 失败: {e}")

                # 规则引擎评分
                score_result = score_news(raw_news, holdings, pool_syms)
                # importance=0 直接丢弃
                if score_result["importance"] == 0:
                    continue

                # 合并字段
                news_record = {**raw_news, **score_result}

                inserted = await self.db.save_flash_news(news_record)
                if inserted:
                    saved_count += 1
                    # 同步刷新候选池中相关股票的 last_news_mention_at
                    # 不分重要度：哪怕 ★1 新闻也证明这只股票还活在话题里，避免被"30 天无新闻"误淘汰
                    cats_all = score_result.get("categories") or []
                    manual_hits = []  # 手动添加的股票被提及 → 特殊优先推送
                    if cats_all:
                        ts = int(time.time())
                        # 收集本条新闻涉及的所有 (symbol, market) 对
                        # 传 importance：≥3 时会自动唤醒已归档股票（archived → candidate）
                        importance = score_result.get("importance", 1)
                        pairs: list = []
                        woken = []
                        for sym in cats_all:
                            mkt = _infer_stock_market(sym)
                            if mkt in ("us", "hk", "cn"):
                                # v11.5: 港股标准化（00700.HK → 0700.HK），与 watch_pool 存储一致
                                if mkt == "hk":
                                    sym = _normalize_hk_symbol(sym)
                                pairs.append((sym, mkt))
                                try:
                                    woke = await self.db.update_pool_news_mention(sym, mkt, ts, importance=importance)
                                    if woke:
                                        woken.append((sym, mkt))
                                except Exception:
                                    pass
                        if woken:
                            logger.info(f"[pool-wake] {len(woken)} 只归档股被 ★{importance} 新闻唤醒: {woken[:3]}")
                        # 批量查 watch_pool —— 一次 IN 查询，消除 N+1
                        if pairs:
                            try:
                                async with self.db.acquire() as conn:
                                    # 先用 symbol IN 粗筛，Python 里再按 (symbol, market) 双键精确匹配
                                    syms_set = list({p[0] for p in pairs})
                                    placeholders = ",".join("?" for _ in syms_set)
                                    cur = await conn.execute(
                                        f"SELECT id, symbol, market, source FROM watch_pool "
                                        f"WHERE symbol IN ({placeholders}) AND status != 'archived'",
                                        syms_set,
                                    )
                                    pool_rows = {(r["symbol"], r["market"]): r for r in await cur.fetchall()}
                                for sym, mkt in pairs:
                                    row = pool_rows.get((sym, mkt))
                                    if row and row["source"] == "manual":
                                        manual_hits.append({"symbol": sym, "market": mkt, "pool_id": row["id"]})
                            except Exception:
                                pass
                    # manual 股票被新闻提及 → 即时 WS 推送（不管重要度）+ 触发 AI 重诊断
                    if manual_hits:
                        for hit in manual_hits:
                            try:
                                await self.ws_hub.broadcast_flash_news({
                                    "type": "manual_stock_news",
                                    "data": {
                                        "symbol": hit["symbol"], "market": hit["market"],
                                        "pool_id": hit["pool_id"],
                                        "news_title": news_record.get("title", "")[:100],
                                        "news_source": news_record.get("source", ""),
                                        "importance": score_result.get("importance", 1),
                                        "sentiment": score_result.get("sentiment", "neutral"),
                                        "published_at": news_record.get("published_at"),
                                    },
                                })
                            except Exception:
                                pass
                        # 新闻有实质内容（★2+）才触发重诊断，避免频繁 LLM 调用
                        if score_result.get("importance", 1) >= 2 and _ai_analyzer is not None:
                            for hit in manual_hits:
                                self._spawn(self._diagnose_with_limit(hit["symbol"], hit["market"]))
                            logger.info(f"[manual-news] 排队 {len(manual_hits)} 只 manual 股票重诊断 (新闻: {news_record.get('title','')[:40]})")
                    # 高分（★★★+）实时推送给前端
                    if score_result["importance"] >= 3:
                        await self._broadcast_news(news_record)
                    # F5B 宏观影响分析（CPI/FOMC/NFP/PPI/GDP 规则引擎）
                    try:
                        from backend.news.macro_impact import analyze_macro_news, save_and_broadcast
                        impact = await analyze_macro_news(news_record)
                        if impact:
                            await save_and_broadcast(self.db, self.ws_hub, news_record, impact)
                    except Exception as e:
                        logger.debug(f"[macro] 分析异常 {news_record.get('id')}: {e}")
                    # PRD F6.1: ★★★+ 新闻涉及的股票自动推入候选池（加密 6 币种跳过）
                    if score_result["importance"] >= 3:
                        added = await self._auto_add_to_pool(news_record, score_result)
                        pool_added_count += added

                    # PRD F5.7: ★★★★+ 新闻自动 LLM 深度解读（异步，不阻塞主流程）
                    # 优化：宏观类新闻（CPI/FOMC/NFP 等）impact 几乎永远空数组，规则引擎已有 macro_impact.py，
                    # LLM 信息增量极低，跳过 deep_analyze（省 $2-3/天 LLM 成本）
                    if score_result["importance"] >= 4 and _ai_analyzer is not None:
                        is_macro = (
                            news_record.get("is_macro_data")
                            or any(kw in (news_record.get("title", "") + news_record.get("content", "")[:500]).upper()
                                   for kw in ("CPI", "FOMC", "NFP", "PPI", "GDP", "美联储", "央行", "加息", "降息"))
                        )
                        if not is_macro:
                            self._spawn(_ai_analyzer.deep_analyze_news(news_record))
                        else:
                            logger.debug(f"[news-skip-llm] 宏观新闻跳过 LLM 解读: {news_record.get('title','')[:40]}")

                    # 持仓相关新闻 → 进入 30 分钟批量缓冲，到期统一调 LLM 出持仓建议
                    if (
                        score_result["importance"] >= 3
                        and _ai_analyzer is not None
                        and holdings
                    ):
                        cats = set(score_result.get("categories") or [])
                        hit_symbols = cats & set(holdings)
                        for sym in hit_symbols:
                            mkt = _infer_stock_market(sym)
                            if mkt == "unknown":
                                continue
                            self._spawn(_ai_analyzer.enqueue_position_news(sym, mkt, news_record))
            except Exception as e:
                logger.exception(f"[{collector.name}] 处理单条新闻异常: {e}")

        if saved_count > 0 or pool_added_count > 0:
            logger.info(
                f"[{collector.name}] 入库 {saved_count} 条新闻 (共 {len(new_items)} 条新)"
                + (f", 新闻驱动入池 {pool_added_count} 只" if pool_added_count else "")
            )

    async def _auto_add_to_pool(
        self, news: Dict[str, Any], score_result: Dict[str, Any]
    ) -> int:
        """
        PRD F6.1 新闻事件驱动入池：
        ★★★+ 新闻涉及的股票自动推入候选池。加密 6 币种跳过。
        返回新增入池数量。
        """
        cats = score_result.get("categories") or []
        if not cats:
            return 0
        importance = score_result.get("importance", 0)
        # 事件分（0-50 压缩刻度）：importance × 4
        from backend.watchpool.scorer import event_score_news
        score = event_score_news(importance)
        added = 0
        for sym in cats:
            market = _infer_stock_market(sym)
            if market not in ("us", "hk", "cn"):
                continue  # 加密币种跳过 (DB CHECK 也会拒绝)
            # 质量硬筛选（严格）
            # P2 修复 (审计 #17): is_eligible 模块崩溃时之前默认放行 → 改为异常时拒入
            try:
                from backend.watchpool.quality_filter import is_eligible
                ok, reason = await is_eligible(self.db, sym, market)
                if not ok:
                    logger.info(f"[news-filter] 拒绝 {sym}/{market}: {reason}")
                    if "数据源" in reason:
                        await self._enqueue_pending(sym, market, "news", score, news.get("title", "")[:80])
                    continue
            except Exception as e:
                logger.warning(f"[news-filter] is_eligible 异常 {sym}: {e} → 拒入(保守策略)")
                continue  # 异常时拒入,不放行
            try:
                pool_id = await self.db.add_to_pool(
                    symbol=sym,
                    market=market,
                    source="news",
                    score=score,
                    reason=f"新闻 ★{importance}: {news.get('title', '')[:80]}",
                )
                await self.ws_hub.broadcast_pool_update(
                    "added",
                    {
                        "id": pool_id, "symbol": sym, "market": market,
                        "source": "news", "score": score,
                        "reason": f"新闻 ★{importance}",
                    },
                )
                added += 1
                # P1 修复 (审计 #5): 之前 fire-and-forget 在池满时会被挤掉永不监控 → 改 await
                # 同步等 bind 完成后再处理下一条新闻,30 分钟黑洞消除
                try:
                    await self._bind_strategies_now(sym, market)
                except Exception as e:
                    logger.warning(f"[news-bind] {sym}/{market} 绑定失败 (5min auto_bind 兜底): {e}")
            except ValueError:
                pass
            except Exception as e:
                logger.debug(f"自动入池 {sym}/{market} 失败: {e}")
        return added

    async def _bind_strategies_now(self, symbol: str, market: str):
        """新闻/AI/宏观驱动入池后立即绑定策略，不等 auto_bind 循环。"""
        try:
            from backend.signals.binding import StrategyBindingManager
            binder = StrategyBindingManager(self.db)
            # 与 MonitorEngine 保持一致的默认策略集
            for strat in ("ma_cross", "bollinger_reversion", "volume_breakout"):
                try:
                    await binder.bind(symbol=symbol, market=market,
                                     strategy_name=strat, interval="1D",
                                     enabled=True)
                except Exception as e:
                    logger.debug(f"[news-bind] {symbol}/{strat} 异常: {e}")
            logger.info(f"[news-bind] {symbol}/{market} 入池即绑 3 策略")
        except Exception as e:
            logger.debug(f"[news-bind] {symbol} 绑定异常: {e}")

    async def _enqueue_pending(self, symbol, market, source, score, reason):
        """暂存到 pool_pending_review，后台 30 分钟重试。"""
        import time as _t
        now = int(_t.time())
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO pool_pending_review
                       (symbol, market, source, score, reason, first_attempt_at, last_attempt_at, attempts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                       ON CONFLICT(symbol, market) DO UPDATE SET
                           last_attempt_at=excluded.last_attempt_at,
                           attempts=pool_pending_review.attempts+1""",
                    (symbol, market, source, score, reason, now, now),
                )
                await conn.commit()
        except Exception as e:
            logger.debug(f"暂存待审失败 {symbol}: {e}")

    async def _broadcast_news(self, news: Dict[str, Any]):
        """通过 WebSocket Hub 推送高价值新闻到前端。"""
        try:
            payload = {
                "type": "flash_news",
                "data": {
                    "id": news["id"],
                    "title": news["title"],
                    "source": news["source"],
                    "importance": news["importance"],
                    "sentiment": news.get("sentiment", "neutral"),
                    "categories": news.get("categories", []),
                    "url": news.get("url", ""),
                    "published_at": news["published_at"],
                    "impact_on_crypto": news.get("impact_on_crypto"),
                },
            }
            # WebSocketHub 没有 broadcast_news 方法时降级到 broadcast_alert
            broadcast = getattr(self.ws_hub, "broadcast_news", None)
            if broadcast:
                await broadcast(payload)
            else:
                # 最简单的兜底：用现有 broadcast_alert API（前端要做适配）
                logger.debug(f"WebSocketHub 缺少 broadcast_news，跳过实时推送: {news['id']}")
        except Exception as e:
            logger.warning(f"推送新闻失败: {e}")
