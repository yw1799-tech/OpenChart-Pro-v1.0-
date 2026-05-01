"""
v12.3 深度交易复盘模块。

每笔已闭环单送给 LLM 完整快照：
  - 入场上下文：价位 / 信号 / 当时 RSI / 距 20日高低 / 入场前后新闻
  - 持仓中期：每日价格 + 中途新闻 + 关键转折点（缺口/急涨急跌）
  - 出场上下文：触发原因 / 出场时市场状态 / 错过的最佳价
  - 同股历史：之前几笔同 symbol 交易的成败模式

LLM 输出多段深度分析（不是 100 字一句话）：
  - score (0-100) + grade (A-D)
  - entry_analysis / mid_analysis / exit_analysis
  - turning_points: 期间关键时刻 + 解读
  - improvements: 具体改进
  - lessons: 可复用教训
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime as _dt
from typing import Dict, List, Optional, Any, Tuple
from zoneinfo import ZoneInfo

# v12.18.2 修复时区：服务器东京 (UTC+9)，但用户在北京 (UTC+8)
# 复盘报告里所有时间显示都按北京时间，否则用户看到的时间会比实际晚 1 小时
_BJ_TZ = ZoneInfo("Asia/Shanghai")

import backend.config as config

logger = logging.getLogger(__name__)


REVIEW_PROMPT = """你是顶级交易复盘师。下面是一笔已闭环交易的**完整链路快照**（信号→AI验证→开仓→持仓→减仓/加仓→平仓）。
你的任务是**对每个环节单独打分 + 找出失误根因**。避免事后聪明 — 没数据预知的不算决策错。

# 交易概况
品种: {symbol} ({market})  方向: {side_cn}
开仓: {open_at_str} @ {open_price}    平仓: {close_at_str} @ {close_price}
持仓: {hold_hours:.1f}h     本币 PnL: {pnl_local:+.2f} {currency} ({pnl_pct:+.2f}%)
期间高: {period_high} | 低: {period_low} | 最佳{best_label}: {best_exit_price} → 错过 {missed_profit_pct:+.2f}%

# ═══ 链路环节 1: 信号生成 ═══
{signal_link}

# ═══ 链路环节 2: AI 验证 ═══
{verify_link}

# ═══ 链路环节 3: 入场上下文（市场状态/新闻情绪）═══
入场时市场状态: {entry_context}
入场前后相关新闻: {entry_news}

# ═══ 链路环节 4: 开仓 SL/TP 设置 ═══
AI 给的止损: {ai_sl}    AI 给的止盈: {ai_tp}
  (默认兜底: SL=avg×0.92, TP=avg×1.25)

# ═══ 链路环节 5: 持仓中事件时间轴 ═══
{events_timeline}

# ═══ 链路环节 6: AI 诊断变化 ═══
{advice_history}

# ═══ 链路环节 7: 持仓中价格走势采样 ═══
{price_path}

# ═══ 链路环节 8: 平仓决策 ═══
平仓触发: {close_trigger}
平仓原因: {close_reason}
出场时市场状态: {exit_context}

# 该品种历史战绩
{history_summary}

# ═══ 你的任务 ═══

## 任务 A：链路逐环节评估（核心！）
针对**每个环节**给出 score (0-100) + verdict + 该环节具体的失误/优点 + 教训。**不要笼统**。

## 任务 B：根因定位
找出**这笔 PnL 的最关键单一环节**：到底是信号有问题？AI 验证有问题？SL/TP 设置错？还是出场太早？

## 任务 C："如果做对"沙盘
假设某个关键环节做对了，结果会改善多少 %（具体数字）。例：
- "如果出场点提前到 70 (period_high)，本笔收益可达 +15% 而非现在的 -2%"
- "如果信号 ai_confidence < 70 时拒绝入场，本笔可避免 -5% 亏损"

# 输出严格 JSON
{{
  "score": 0-100 综合评分,
  "grade": "A|B|C|D",
  "decision_score": 0-100 (仅看决策质量,不看结果),
  "outcome_score": 0-100 (仅看结果),
  "pros": ["决策合理处 2-3 条"],
  "cons": ["决策不合理处 2-3 条"],
  "primary_lesson": "本笔最重要的一条教训（一句话，<= 50 字）",
  "what_if_better": "如果某环节做对，结果会改善多少 %（具体数字 + 哪个环节）",

  "link_evaluations": {{
    "signal":      {{"score": 0-100, "verdict": "good|bad|neutral", "summary": "策略信号本身可信吗？多周期一致吗？置信度合理吗？(80 字)", "lessons": [{{"type":"signal","content":"..."}}]}},
    "ai_verify":   {{"score": 0-100, "verdict": "...", "summary": "AI 验证给的判断对吗？事后看 ai_confidence 是否过高/过低？(80 字)", "lessons": [...]}},
    "news_judge":  {{"score": 0-100, "verdict": "...", "summary": "入场时新闻判断准吗？有没有错过/误读重要新闻？(80 字)", "lessons": [...]}},
    "entry_timing":{{"score": 0-100, "verdict": "...", "summary": "入场时机：是否追高/低吸？技术面充分吗？(80 字)", "lessons": [...]}},
    "sl_tp_setup": {{"score": 0-100, "verdict": "...", "summary": "AI 给的 SL/TP 合理吗？或用了默认兜底？触发位置如何？(80 字)", "lessons": [...]}},
    "mid_management":{{"score": 0-100, "verdict": "...", "summary": "持仓中加仓/减仓决策对吗？错过的转折点？(80 字)", "lessons": [...]}},
    "diagnose_response":{{"score": 0-100, "verdict": "...", "summary": "AI 诊断变化是否及时？rating 切换抓住了吗？(80 字)", "lessons": [...]}},
    "exit_quality":{{"score": 0-100, "verdict": "...", "summary": "出场触发是否合理？错过 X% 利润根因？太早还是太晚？(80 字)", "lessons": [...]}},
    "money_management":{{"score": 0-100, "verdict": "...", "summary": "仓位大小/盈亏比合理吗？(80 字)", "lessons": [...]}}
  }},

  "improvements": "200 字：3 条**针对薄弱环节**的具体可执行改进（如：'下次同 strategy 信号 ai_confidence < 75 时降低仓位至 50%'）",
  "lessons": [{{"type": "signal|ai_verify|news_judge|entry_timing|sl_tp_setup|mid_management|diagnose_response|exit_quality|money_management", "content": "10-30 字"}}]
}}

每个 link_evaluations 的 lessons 字段必须 ≥ 1 条，**type 必须用上面 9 个环节名之一**（不要再用 entry/exit/risk 等旧标签）。
只输出 JSON，不要 markdown 包围。
"""


WEEKLY_PROMPT = """你是交易导师。下面是用户本周所有交易的复盘汇总，请综合评估并给出**改进建议**（输出严格 JSON）：

# 本周交易统计
- 总笔数: {total_count}
- 盈利: {wins} 笔  亏损: {losses} 笔  胜率: {win_rate:.1%}
- 总盈亏: ${total_pnl_usd:+.2f}
- 平均评级: {avg_grade}
- 各市场分布: {market_dist}

# 单笔评估摘要 (按 grade 排序)
{trade_summaries}

# Top 3 赚最多
{top_wins_text}

# Top 3 亏最多
{top_losses_text}

# 你的任务
输出 JSON：

{{
  "summary": "300-500 字本周综合评估：表现如何 / 哪些做得好 / 哪些拖后腿 / 整体趋势",
  "recurring_mistakes": [
    "具体错误模式 1（如：连续 3 次在 RSI > 80 时入场全部亏损）",
    "..."
  ],
  "actionable_changes": [
    "下周可执行的具体动作 1（不要空话）",
    "..."
  ]
}}

只输出 JSON，不要 markdown 包围。
"""


class TradeReviewer:
    def __init__(self, db, ai_analyzer=None):
        self.db = db
        # v12.3.1: 每次调用前实时拉取最新 _ai_analyzer（init 时 scheduler 可能还没 attach）
        self._injected_analyzer = ai_analyzer
        # v12.11: 防并发双跑同 pid LLM
        self._inflight_pids: set = set()
        self._inflight_lock = asyncio.Lock()

    @property
    def ai_analyzer(self):
        """实时取 ai_analyzer：优先用注入值，否则从 scheduler module 拉最新。"""
        if self._injected_analyzer is not None:
            return self._injected_analyzer
        try:
            from backend.news import scheduler as _sched
            return _sched._ai_analyzer
        except Exception:
            return None

    # ──────────────────── 单笔深度复盘 ────────────────────

    async def review_position(self, position_id: str, force: bool = False) -> Optional[Dict]:
        if not force:
            async with self.db.acquire() as conn:
                cur = await conn.execute("SELECT id FROM trade_review WHERE position_id=?", (position_id,))
                if await cur.fetchone():
                    return None

        # v12.11: in-flight lock 防并发同 pid 双跑（batch + manual + auto loop 同时触发会烧双倍 LLM）
        async with self._inflight_lock:
            if position_id in self._inflight_pids:
                logger.info(f"[review] {position_id[:8]} 正在复盘中（in-flight），跳过本次")
                return None
            self._inflight_pids.add(position_id)
        try:
            return await self._do_review_position(position_id, force)
        finally:
            async with self._inflight_lock:
                self._inflight_pids.discard(position_id)

    async def _do_review_position(self, position_id: str, force: bool) -> Optional[Dict]:
        """实际复盘逻辑（被 in-flight lock 包裹的内层）。"""
        # 1. 拉所有 leg
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT * FROM auto_trade_log WHERE position_id=? AND status='executed' ORDER BY traded_at ASC",
                (position_id,)
            )
            legs = [dict(r) for r in await cur.fetchall()]
        if not legs: return None
        if not any(l["action"] == "close" for l in legs): return None

        symbol = legs[0]["symbol"]; market = legs[0]["market"]
        side = "long"
        try:
            td = json.loads(legs[0].get("trigger_detail") or "{}")
            side = td.get("side") or "long"
        except Exception: pass

        open_legs = [l for l in legs if l["action"] in ("open", "add")]
        close_legs = [l for l in legs if l["action"] in ("reduce", "close")]
        if not open_legs or not close_legs: return None

        open_at = open_legs[0]["traded_at"]
        close_at = close_legs[-1]["traded_at"]
        hold_hours = max(0.0, (close_at - open_at) / 3600.0)
        total_qty = sum(l["quantity"] for l in open_legs) or 1
        open_price = sum(l["price"] * l["quantity"] for l in open_legs) / total_qty
        close_price = close_legs[-1]["price"]
        fx = legs[0].get("fx_rate") or 1.0
        in_local = sum((l["amount_usd"] or 0) / fx for l in open_legs)
        out_local = sum((l["amount_usd"] or 0) / fx for l in close_legs)
        pnl_local = out_local - in_local
        pnl_pct = (pnl_local / in_local * 100) if in_local > 0 else 0.0
        currency = self._market_currency(market)
        pool_id = {"us":"us_hk","hk":"us_hk","cn":"cn","crypto":"crypto"}.get(market, "us_hk")

        # 2. 拉持仓期间 K 线
        klines = await self._fetch_klines_in_range(symbol, market, open_at, close_at)
        period_high, period_low, ph_at, pl_at = self._extract_extremes(klines, open_price, close_price)
        best_exit = period_high if side == "long" else period_low
        best_label = "高点" if side == "long" else "低点"
        missed_pct = ((best_exit - close_price)/close_price*100) if side == "long" and close_price > 0 \
                     else ((close_price - best_exit)/close_price*100) if close_price > 0 else 0
        # 价格路径采样（5 个点）
        price_path = self._sample_price_path(klines, n=5)

        # 3. 入场/出场上下文
        entry_ctx = await self._build_market_context(symbol, market, open_at)
        exit_ctx = await self._build_market_context(symbol, market, close_at)

        # 4. 期间新闻
        news_count, news_summary = await self._fetch_news_during(symbol, open_at, close_at)

        # 5. 同股历史战绩
        history_summary = await self._fetch_history_summary(symbol, market, open_at)

        # 6. 平仓触发 + 原因
        close_reason = close_legs[-1].get("reason") or ""
        close_trigger = close_legs[-1].get("trigger_type") or ""

        # 7. v12.8 拉链路全量数据
        signal_link, verify_link, ai_sl, ai_tp = await self._fetch_signal_chain(legs[0])
        events_timeline = self._build_events_timeline(legs, news_count)
        advice_history = await self._fetch_advice_history(position_id)
        entry_news = await self._fetch_news_around(symbol, open_at, hours_window=24)

        # 8. 调 LLM 深度复盘
        snapshot_text = json.dumps({
            "symbol": symbol, "market": market, "side": side,
            "open_price": open_price, "close_price": close_price,
            "klines_count": len(klines), "news_count": news_count,
            "leg_count": len(legs), "advice_count": advice_history.count("\n"),
        }, ensure_ascii=False)

        result = await self._llm_deep_review(
            symbol=symbol, market=market, side=side,
            side_cn="做多" if side=="long" else "做空",
            open_price=open_price, close_price=close_price,
            open_at_str=self._fmt_ts(open_at), close_at_str=self._fmt_ts(close_at),
            hold_hours=hold_hours, pnl_local=pnl_local, pnl_pct=pnl_pct, currency=currency,
            close_trigger=close_trigger, close_reason=close_reason[:300],
            entry_context=entry_ctx, exit_context=exit_ctx,
            price_path=price_path,
            period_high=period_high, period_low=period_low,
            period_high_at=self._fmt_ts(ph_at) if ph_at else "?",
            period_low_at=self._fmt_ts(pl_at) if pl_at else "?",
            best_exit_price=best_exit, best_label=best_label,
            missed_profit_pct=missed_pct,
            news_count=news_count, news_summary=news_summary,
            entry_news=entry_news,
            history_summary=history_summary,
            signal_link=signal_link, verify_link=verify_link,
            ai_sl=ai_sl, ai_tp=ai_tp,
            events_timeline=events_timeline,
            advice_history=advice_history,
        )
        # v12.11: LLM 失败 / 解析失败 / 半成品（缺 score+grade）→ 不落库（避免 force=False 永久跳过 + 污染周报）
        if not result or not isinstance(result, dict):
            logger.warning(f"[reviewer] {symbol}({position_id[:8]}) LLM 调用失败，不写半成品（下次会重试）")
            return None
        if not result.get("grade") or result.get("score") is None:
            logger.warning(f"[reviewer] {symbol}({position_id[:8]}) LLM 返回缺 score/grade，不写半成品")
            return None

        # v12.16.2 (#1): 策略参数分析 — LLM 评估开仓策略 params 合理性 + 改进建议
        # v12.18.4: 加防护 try/except — 此辅助步骤异常不应阻断主复盘落库
        try:
            strategy_param_analysis = await self._llm_strategy_param_analysis(
                position_id, symbol, market, side, open_at, close_at,
                open_price, close_price, pnl_pct, period_high, period_low, snapshot_text,
            )
        except Exception as e:
            logger.warning(f"[strat-analysis] {symbol}({position_id[:8]}) 策略参数分析异常 (主复盘继续): {e}")
            strategy_param_analysis = {}

        # 8. 落库（v12.8: 含 link_evaluations / primary_lesson / what_if_better）
        try:
            async with self.db.acquire() as conn:
                await conn.execute("""
                    INSERT OR REPLACE INTO trade_review
                    (position_id, symbol, market, pool_id, side,
                     open_price, close_price, open_at, close_at, hold_hours,
                     realized_pnl_local, realized_pnl_pct,
                     period_high, period_low, best_exit_price, missed_profit_pct,
                     score, grade, decision_score, outcome_score, pros, cons,
                     entry_analysis, mid_analysis, exit_analysis,
                     turning_points, improvements, lessons,
                     link_evaluations, primary_lesson, what_if_better,
                     strategy_param_analysis,
                     snapshot_json, llm_model, llm_tokens, reviewed_at)
                    VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?, ?,?,?,?, ?,?,?,?,?,?, ?,?,?, ?,?,?, ?,?,?, ?, ?,?,?,?)
                """, (
                    position_id, symbol, market, pool_id, side,
                    open_price, close_price, open_at, close_at, hold_hours,
                    pnl_local, pnl_pct,
                    period_high, period_low, best_exit, missed_pct,
                    result["score"], result["grade"],
                    result.get("decision_score"), result.get("outcome_score"),
                    json.dumps(result.get("pros", []), ensure_ascii=False),
                    json.dumps(result.get("cons", []), ensure_ascii=False),
                    result.get("entry_analysis", ""), result.get("mid_analysis", ""), result.get("exit_analysis", ""),
                    json.dumps(result.get("turning_points", []), ensure_ascii=False),
                    result.get("improvements", ""),
                    json.dumps(result.get("lessons", []), ensure_ascii=False),
                    json.dumps(result.get("link_evaluations", {}), ensure_ascii=False),
                    result.get("primary_lesson", ""), result.get("what_if_better", ""),
                    json.dumps(strategy_param_analysis, ensure_ascii=False) if strategy_param_analysis else "{}",
                    snapshot_text, result.get("llm_model", ""), result.get("llm_tokens", 0),
                    int(time.time())
                ))
                await conn.commit()
        except Exception as e:
            logger.warning(f"[reviewer] {symbol}({position_id[:8]}) 落库失败: {e}")
            return None

        logger.info(f"[reviewer] ✓ {symbol}({market}) {pnl_pct:+.2f}% grade={result['grade']} d={result.get('decision_score','-')}/o={result.get('outcome_score','-')} 主因: {result.get('primary_lesson','-')[:40]}")
        return {"position_id": position_id, **result}

    # ──────────────────── 周报聚合 ────────────────────

    async def generate_weekly_report(self, week_start_ts: int) -> Optional[Dict]:
        """对一周内的 reviews 出综合报告。week_start_ts: 周一 00:00 UTC ts"""
        week_end_ts = week_start_ts + 7 * 86400
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT * FROM trade_review WHERE close_at >= ? AND close_at < ? "
                "ORDER BY realized_pnl_pct DESC",
                (week_start_ts, week_end_ts)
            )
            reviews = [dict(r) for r in await cur.fetchall()]
        if not reviews:
            return None

        wins = sum(1 for r in reviews if (r["realized_pnl_pct"] or 0) > 0)
        losses = len(reviews) - wins
        win_rate = wins / len(reviews) if reviews else 0
        # 折算 USD（简单：本币 × fx 估算）
        from backend.trading.fx import get_rate
        total_pnl_usd = 0
        for r in reviews:
            mkt = r["market"]
            local = r["realized_pnl_local"] or 0
            try:
                ccy = self._market_currency(mkt)
                fx_to_usd = await get_rate(self.db, ccy)
            except Exception:
                fx_to_usd = 1.0
            total_pnl_usd += local * fx_to_usd
        # 平均评级
        grade_score = {"A": 95, "B": 80, "C": 60, "D": 40}
        avg_score = sum(grade_score.get(r["grade"], 60) for r in reviews) / len(reviews)
        avg_grade = "A" if avg_score >= 90 else "B" if avg_score >= 70 else "C" if avg_score >= 50 else "D"
        # 市场分布
        from collections import Counter
        mc = Counter(r["market"] for r in reviews)
        market_dist = ", ".join(f"{m}={c}" for m,c in mc.most_common())

        top_wins = sorted(reviews, key=lambda r: r["realized_pnl_pct"] or 0, reverse=True)[:3]
        top_losses = sorted(reviews, key=lambda r: r["realized_pnl_pct"] or 0)[:3]

        # 摘要文本（按 grade 倒序）
        sorted_reviews = sorted(reviews, key=lambda r: grade_score.get(r["grade"], 0), reverse=True)
        summaries = "\n".join(
            f"- [{r['grade']}/{r['score']}] {r['symbol']}({r['market']}) {r['side']} "
            f"{(r['realized_pnl_pct'] or 0):+.2f}% · {(r['entry_analysis'] or '')[:60]}…"
            for r in sorted_reviews[:30]
        )
        top_wins_text = "\n".join(f"- {r['symbol']} +{(r['realized_pnl_pct'] or 0):.2f}% / score={r['score']}" for r in top_wins)
        top_losses_text = "\n".join(f"- {r['symbol']} {(r['realized_pnl_pct'] or 0):.2f}% / score={r['score']}" for r in top_losses)

        if not self.ai_analyzer:
            # 兜底：简单统计
            llm_out = {
                "summary": f"本周 {len(reviews)} 笔交易，胜率 {win_rate:.1%}，总盈亏 ${total_pnl_usd:+.2f}。",
                "recurring_mistakes": [],
                "actionable_changes": []
            }
        else:
            try:
                prompt = WEEKLY_PROMPT.format(
                    total_count=len(reviews), wins=wins, losses=losses, win_rate=win_rate,
                    total_pnl_usd=total_pnl_usd, avg_grade=avg_grade, market_dist=market_dist,
                    trade_summaries=summaries, top_wins_text=top_wins_text, top_losses_text=top_losses_text,
                )
                r = await self.ai_analyzer._call_llm(prompt, max_tokens=1500, path="trade_review_weekly")
                llm_out = r or {"summary": "LLM 无响应", "recurring_mistakes": [], "actionable_changes": []}
            except Exception as e:
                logger.warning(f"[reviewer-weekly] LLM 失败: {e}")
                llm_out = {"summary": "LLM 调用异常", "recurring_mistakes": [], "actionable_changes": []}

        try:
            async with self.db.acquire() as conn:
                await conn.execute("""
                    INSERT OR REPLACE INTO trade_review_weekly
                    (week_start, week_end, trades_count, wins, losses, win_rate,
                     total_pnl_usd, avg_grade, summary, top_wins, top_losses,
                     recurring_mistakes, actionable_changes, generated_at)
                    VALUES (?,?,?,?,?,?, ?,?,?,?,?, ?,?,?)
                """, (week_start_ts, week_end_ts, len(reviews), wins, losses, win_rate,
                      total_pnl_usd, avg_grade, llm_out.get("summary", ""),
                      json.dumps([r["position_id"] for r in top_wins]),
                      json.dumps([r["position_id"] for r in top_losses]),
                      json.dumps(llm_out.get("recurring_mistakes", []), ensure_ascii=False),
                      json.dumps(llm_out.get("actionable_changes", []), ensure_ascii=False),
                      int(time.time())))
                await conn.commit()
        except Exception as e:
            logger.warning(f"[reviewer-weekly] 落库失败: {e}")

        return {"trades_count": len(reviews), "wins": wins, "losses": losses, "win_rate": win_rate,
                "total_pnl_usd": total_pnl_usd, "summary": llm_out.get("summary", "")}

    # ──────────────────── 批量调度 ────────────────────

    async def batch_review_unreviewed(self, limit: int = 20, sleep_sec: float = 2.0) -> Dict:
        async with self.db.acquire() as conn:
            cur = await conn.execute("""
                SELECT DISTINCT l.position_id
                FROM auto_trade_log l
                LEFT JOIN trade_review r ON r.position_id = l.position_id
                WHERE l.status='executed' AND l.action='close' AND l.position_id IS NOT NULL
                  AND r.id IS NULL
                ORDER BY l.traded_at DESC
                LIMIT ?
            """, (limit,))
            pids = [r["position_id"] for r in await cur.fetchall()]
        if not pids:
            return {"processed": 0, "ok": 0, "fail": 0, "skipped_budget": 0}
        ok = fail = skipped = 0
        for pid in pids:
            try:
                if self.ai_analyzer and hasattr(self.ai_analyzer, "_can_call"):
                    if not await self.ai_analyzer._can_call(hard_stop=False):
                        skipped = len(pids) - ok - fail
                        logger.info(f"[reviewer] LLM 预算用尽，跳过剩余 {skipped} 笔")
                        break
            except Exception:
                pass
            try:
                r = await self.review_position(pid, force=False)
                if r: ok += 1
                else: fail += 1
            except Exception as e:
                fail += 1
                logger.debug(f"[reviewer] {pid[:8]} 失败: {e}")
            await asyncio.sleep(sleep_sec)
        return {"processed": len(pids), "ok": ok, "fail": fail, "skipped_budget": skipped}

    # ──────────────────── 工具 ────────────────────

    @staticmethod
    def _market_currency(market: str) -> str:
        return {"us": "USD", "hk": "HKD", "cn": "CNY", "crypto": "USDT"}.get(market, "USD")

    @staticmethod
    def _fmt_ts(ts: int) -> str:
        if not ts: return "?"
        # v12.18.2: 显式按北京时区格式化（之前 time.localtime 会拿东京时区）
        return _dt.fromtimestamp(ts, _BJ_TZ).strftime("%m-%d %H:%M")

    async def _fetch_klines_in_range(self, symbol: str, market: str, open_ts: int, close_ts: int) -> List[Dict]:
        if market not in ("us","hk","cn","crypto"): return []
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    f"SELECT timestamp, open, high, low, close, volume FROM [klines_{market}_1d] "
                    f"WHERE symbol=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp ASC",
                    (symbol, (open_ts or 0)*1000, (close_ts or 0)*1000)
                )
                return [dict(r) for r in await cur.fetchall()]
        except Exception:
            return []

    @staticmethod
    def _extract_extremes(klines: List[Dict], op: float, cp: float):
        if not klines: return (max(op, cp), min(op, cp), None, None)
        ph = max(k["high"] for k in klines)
        pl = min(k["low"] for k in klines)
        ph_at = next((k["timestamp"]/1000 for k in klines if k["high"] == ph), None)
        pl_at = next((k["timestamp"]/1000 for k in klines if k["low"] == pl), None)
        return (ph, pl, ph_at, pl_at)

    @staticmethod
    def _sample_price_path(klines: List[Dict], n: int = 5) -> str:
        if not klines: return "(无 K 线数据)"
        if len(klines) <= n:
            picks = klines
        else:
            step = len(klines) / n
            picks = [klines[int(i * step)] for i in range(n)]
        return "\n".join(
            f"  {_dt.fromtimestamp(k['timestamp']/1000, _BJ_TZ).strftime('%m-%d')}: "
            f"O {k['open']:.4f} H {k['high']:.4f} L {k['low']:.4f} C {k['close']:.4f} V {k['volume']}"
            for k in picks
        )

    async def _build_market_context(self, symbol: str, market: str, ts: int) -> str:
        """简短描述某时刻的市场状态：近 20 日高低 / RSI / MA 位置"""
        if market not in ("us","hk","cn","crypto"): return "(数据不足)"
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    f"SELECT close, high, low FROM [klines_{market}_1d] "
                    f"WHERE symbol=? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 25",
                    (symbol, (ts or 0)*1000)
                )
                rows = [dict(r) for r in await cur.fetchall()]
            if len(rows) < 5: return "(K 线不足)"
            cur_close = rows[0]["close"]
            hi20 = max(r["high"] for r in rows[:20]) if len(rows) >= 20 else max(r["high"] for r in rows)
            lo20 = min(r["low"] for r in rows[:20]) if len(rows) >= 20 else min(r["low"] for r in rows)
            ma5 = sum(r["close"] for r in rows[:5]) / min(5, len(rows))
            ma20 = sum(r["close"] for r in rows[:20]) / min(20, len(rows))
            # 简化 RSI 14
            gains = losses = 0.0
            for i in range(min(14, len(rows)-1)):
                diff = rows[i]["close"] - rows[i+1]["close"]
                if diff > 0: gains += diff
                else: losses -= diff
            rs = gains / max(losses, 1e-9)
            rsi = 100 - 100 / (1 + rs) if losses > 0 else 100
            dist_hi = (cur_close - hi20) / hi20 * 100
            dist_lo = (cur_close - lo20) / lo20 * 100
            return (f"现价 {cur_close:.4f}; 20日高 {hi20:.4f} (距 {dist_hi:+.1f}%); "
                    f"20日低 {lo20:.4f} (距 {dist_lo:+.1f}%); MA5 {ma5:.4f}; MA20 {ma20:.4f}; "
                    f"价格 {'在 MA5 上方' if cur_close>ma5 else '在 MA5 下方'}; RSI≈{rsi:.0f}")
        except Exception:
            return "(查询失败)"

    async def _fetch_news_during(self, symbol: str, open_ts: int, close_ts: int) -> Tuple[int, str]:
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT title, importance, sentiment, published_at FROM flash_news "
                    "WHERE published_at BETWEEN ? AND ? AND categories LIKE ? "
                    "ORDER BY importance DESC, published_at DESC LIMIT 30",
                    ((open_ts or 0)*1000, (close_ts or 0)*1000, f'%"{symbol}"%')  # v12.11: 精确匹配
                )
                rows = [dict(r) for r in await cur.fetchall()]
            if not rows: return (0, "(无相关新闻)")
            high_imp = [r for r in rows if (r["importance"] or 0) >= 3]
            picks = high_imp[:6] if high_imp else rows[:3]
            text = "\n".join(
                f"  ★{r['importance']} [{r['sentiment']}] {_dt.fromtimestamp(r['published_at']/1000, _BJ_TZ).strftime('%m-%d')}: {r['title'][:80]}"
                for r in picks
            )
            return (len(rows), text)
        except Exception:
            return (0, "(查询失败)")

    async def _fetch_signal_chain(self, open_leg: Dict) -> Tuple[str, str, str, str]:
        """v12.8 拉该笔开仓对应的信号 + AI 验证记录。"""
        signal_id = None
        try:
            td = json.loads(open_leg.get("trigger_detail") or "{}")
            signal_id = td.get("signal_id")
        except Exception:
            pass
        if not signal_id:
            return ("(无关联 signal — 可能是 diagnose_strong_buy 试单或手动开仓)",
                    "(同上)", "—", "—")
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,))
                s = await cur.fetchone()
            if not s:
                return ("(信号已删除)", "(同上)", "—", "—")
            s = dict(s)
            sig_link = (
                f"策略: {s.get('strategy_name')}  周期: {s.get('interval')}  动作: {s.get('action')}\n"
                f"系统置信度: {s.get('confidence')}  价格: {s.get('price')}\n"
                f"系统理由: {(s.get('reason') or '')[:200]}"
            )
            # v12.11: 把 ai_news_ids 也拉出来，让复盘看到"开仓时 LLM 看了哪些新闻"
            news_ids_text = ""
            try:
                ids_raw = s.get("ai_news_ids") or "[]"
                ids = json.loads(ids_raw) if isinstance(ids_raw, str) else (ids_raw or [])
                if ids:
                    async with self.db.acquire() as conn:
                        # 用 IN (...) 拉前 5 条新闻标题
                        placeholders = ",".join(["?"] * min(len(ids), 5))
                        cur2 = await conn.execute(
                            f"SELECT title, importance, sentiment FROM flash_news "
                            f"WHERE id IN ({placeholders}) ORDER BY importance DESC LIMIT 5",
                            tuple(ids[:5])
                        )
                        nrows = [dict(r) for r in await cur2.fetchall()]
                    if nrows:
                        news_ids_text = "\n开仓时 AI 参考新闻:\n" + "\n".join(
                            f"  ★{r['importance']} [{r['sentiment']}] {r['title'][:80]}"
                            for r in nrows
                        )
            except Exception as e:
                logger.debug(f"[reviewer] ai_news_ids 解析失败: {e}")
            verify_link = (
                f"AI verdict: {s.get('ai_verdict') or '未验证'}  AI 置信度: {s.get('ai_confidence') or '-'}\n"
                f"AI 理由: {(s.get('ai_reason') or '(无)')[:300]}"
                f"{news_ids_text}"
            )
            ai_sl = f"{s.get('ai_stop_loss'):.4f}" if s.get('ai_stop_loss') else "未给"
            ai_tp = f"{s.get('ai_take_profit'):.4f}" if s.get('ai_take_profit') else "未给"
            return (sig_link, verify_link, ai_sl, ai_tp)
        except Exception:
            return ("(查询失败)", "(查询失败)", "—", "—")

    def _build_events_timeline(self, legs: List[Dict], news_count: int) -> str:
        """v12.8 串起所有 leg 形成"开/加/减/平"事件时间轴。"""
        lines = []
        for l in legs:
            t = self._fmt_ts(l.get("traded_at") or 0)
            act = l.get("action", "?")
            qty = l.get("quantity") or 0
            price = l.get("price") or 0
            trig = l.get("trigger_type") or "?"
            reason = (l.get("reason") or "")[:120]
            lines.append(f"  {t} | {act:6s} qty={qty:.4f} @ {price:.4f} [{trig}] {reason}")
        return "\n".join(lines) if lines else "(无)"

    async def _fetch_advice_history(self, position_id: str) -> str:
        """v12.8 拉该单的所有 AI 持仓建议历史。"""
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT advice, reason, advised_at FROM position_advices "
                    "WHERE position_id=? ORDER BY advised_at ASC LIMIT 30",
                    (position_id,)
                )
                rows = [dict(r) for r in await cur.fetchall()]
            if not rows:
                return "(该单期间无 AI 持仓建议)"
            return "\n".join(
                f"  {self._fmt_ts(r['advised_at'])} | advice={r['advice']} : {(r['reason'] or '')[:100]}"
                for r in rows
            )
        except Exception:
            return "(查询失败)"

    async def _fetch_news_around(self, symbol: str, ts: int, hours_window: int = 24) -> str:
        """v12.8 取入场前后 N 小时窗口内该股相关的 ★≥3 新闻。"""
        try:
            t_from = (ts - hours_window * 3600) * 1000
            t_to = (ts + hours_window * 3600) * 1000
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT title, importance, sentiment, published_at FROM flash_news "
                    "WHERE published_at BETWEEN ? AND ? AND categories LIKE ? AND importance >= 3 "
                    "ORDER BY importance DESC, published_at DESC LIMIT 8",
                    (t_from, t_to, f'%"{symbol}"%')  # v12.11: 精确匹配
                )
                rows = [dict(r) for r in await cur.fetchall()]
            if not rows:
                return "(入场前后 24h 无 ★3+ 相关新闻)"
            return "\n".join(
                f"  ★{r['importance']} [{r['sentiment']}] {self._fmt_ts(r['published_at']/1000)}: {r['title'][:80]}"
                for r in rows
            )
        except Exception:
            return "(查询失败)"

    async def _fetch_history_summary(self, symbol: str, market: str, before_ts: int) -> str:
        """近 30 天该 symbol 的历史复盘成败"""
        try:
            cutoff = before_ts - 30 * 86400
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT COUNT(*) AS n, "
                    "SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END) AS wins, "
                    "AVG(realized_pnl_pct) AS avg_pct, "
                    "AVG(missed_profit_pct) AS avg_missed "
                    "FROM trade_review WHERE symbol=? AND market=? AND close_at BETWEEN ? AND ?",
                    (symbol, market, cutoff, before_ts)
                )
                r = await cur.fetchone()
            if not r or not r["n"]:
                return "(近 30 天无该品种历史复盘)"
            return (f"近 30 天 {r['n']} 笔，胜 {r['wins']} 败 {r['n']-r['wins']}，"
                    f"平均收益 {r['avg_pct']:+.2f}%，平均错过 {r['avg_missed']:+.2f}%")
        except Exception:
            return "(查询失败)"

    async def _llm_deep_review(self, **ctx) -> Dict:
        """v12.8: 链路逐环节评估 + pros/cons + primary_lesson + what_if_better。"""
        pnl = ctx["pnl_pct"]; missed = ctx["missed_profit_pct"]
        if pnl > 5 and missed < 3: outcome_default = 90
        elif pnl > 0: outcome_default = 75
        elif pnl > -5: outcome_default = 55
        else: outcome_default = 35
        decision_default = 60
        score_default = int(decision_default * 0.6 + outcome_default * 0.4)
        grade_default = "A" if score_default >= 85 else "B" if score_default >= 70 else "C" if score_default >= 55 else "D"
        # 链路环节默认（每个 50 分中性）
        LINKS = ["signal","ai_verify","news_judge","entry_timing","sl_tp_setup",
                 "mid_management","diagnose_response","exit_quality","money_management"]
        empty_link_eval = {k: {"score": 50, "verdict": "neutral", "summary": "(LLM 不可用)", "lessons": []} for k in LINKS}
        fallback = {
            "decision_score": decision_default, "outcome_score": outcome_default,
            "score": score_default, "grade": grade_default,
            "pros": [], "cons": [],
            "primary_lesson": "(LLM 不可用)", "what_if_better": "(LLM 不可用)",
            "entry_analysis": "(LLM 不可用)", "mid_analysis": "(LLM 不可用)", "exit_analysis": "(LLM 不可用)",
            "turning_points": [], "improvements": "(LLM 不可用)", "lessons": [],
            "link_evaluations": empty_link_eval,
            "llm_model": "fallback", "llm_tokens": 0,
        }
        if not self.ai_analyzer:
            return fallback
        try:
            prompt = REVIEW_PROMPT.format(**ctx)
            # 深度链路复盘需要更多 token（9 个环节 + 综合）
            result = await self.ai_analyzer._call_llm(prompt, max_tokens=4000, path="trade_review")
            if not result: return fallback
            ds = result.get("decision_score")
            os_ = result.get("outcome_score")
            try: ds = int(ds) if ds is not None else decision_default
            except: ds = decision_default
            try: os_ = int(os_) if os_ is not None else outcome_default
            except: os_ = outcome_default
            ds = max(0, min(100, ds)); os_ = max(0, min(100, os_))
            score = int(ds * 0.6 + os_ * 0.4)
            grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D"
            # 解析 link_evaluations，缺失的环节用默认中性
            link_raw = result.get("link_evaluations") or {}
            link_eval = {}
            for k in LINKS:
                v = link_raw.get(k)
                if isinstance(v, dict):
                    le_lessons = []
                    for l in (v.get("lessons", []) or []):
                        if isinstance(l, dict):
                            le_lessons.append({
                                "type": str(l.get("type", k))[:30],
                                "content": str(l.get("content", ""))[:200]
                            })
                    link_eval[k] = {
                        "score": max(0, min(100, int(v.get("score", 50)) if str(v.get("score","")).lstrip("-").isdigit() else 50)),
                        "verdict": str(v.get("verdict", "neutral"))[:20],
                        "summary": str(v.get("summary", ""))[:500],
                        "lessons": le_lessons[:3],
                    }
                else:
                    link_eval[k] = empty_link_eval[k]
            # 顶层 lessons：合并所有环节的 lessons
            top_lessons = list(result.get("lessons", []) or [])
            return {
                "decision_score": ds, "outcome_score": os_,
                "score": score, "grade": grade,
                "pros": [str(p)[:200] for p in (result.get("pros", []) or []) if isinstance(p, str)][:5],
                "cons": [str(p)[:200] for p in (result.get("cons", []) or []) if isinstance(p, str)][:5],
                "primary_lesson": str(result.get("primary_lesson", ""))[:200],
                "what_if_better": str(result.get("what_if_better", ""))[:500],
                "entry_analysis": str(result.get("entry_analysis", ""))[:1000],
                "mid_analysis": str(result.get("mid_analysis", ""))[:1000],
                "exit_analysis": str(result.get("exit_analysis", ""))[:1000],
                "turning_points": result.get("turning_points", []) if isinstance(result.get("turning_points"), list) else [],
                "improvements": str(result.get("improvements", ""))[:1500],
                "lessons": [
                    {"type": str(l.get("type", "general"))[:30], "content": str(l.get("content", ""))[:200]}
                    for l in top_lessons if isinstance(l, dict)
                ][:10],
                "link_evaluations": link_eval,
                "llm_model": getattr(self.ai_analyzer, "_model", "unknown"),
                "llm_tokens": 0,
            }
        except Exception as e:
            logger.debug(f"[reviewer] LLM 失败 → 兜底: {e}")
            return fallback

    # ──────────────────── v12.16.2 (#1): 策略参数分析 ────────────────────

    async def _llm_strategy_param_analysis(
        self, position_id, symbol, market, side, open_at, close_at,
        open_price, close_price, pnl_pct, period_high, period_low, snapshot_text,
    ):
        """v12.16.2 评估开仓使用的策略参数合理性 + 给出改进建议。
        返回 {strategies: [{name, current_params, evaluation, reason, suggested_params, expected_improvement}]}
        失败返回 {} (不阻断主复盘流程)
        """
        if not self.ai_analyzer:
            return {}
        # 1) 找入场策略 + params
        try:
            async with self.db.acquire() as conn:
                # 拉首笔 open 的 signal_id
                cur = await conn.execute(
                    """SELECT trigger_detail FROM auto_trade_log
                       WHERE position_id=? AND action='open' AND status='executed'
                       ORDER BY id ASC LIMIT 1""",
                    (position_id,),
                )
                row = await cur.fetchone()
            if not row or not row["trigger_detail"]:
                return {}
            try: td = json.loads(row["trigger_detail"]) if isinstance(row["trigger_detail"], str) else row["trigger_detail"]
            except Exception: td = {}
            sid = td.get("signal_id") if isinstance(td, dict) else None
            if not sid:
                return {}
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT strategy_name, triggered_by, interval, confidence, ai_confidence FROM signals WHERE id=?",
                    (sid,),
                )
                sig = await cur.fetchone()
            if not sig:
                return {}
            strategy_name = sig["strategy_name"] or ""
            try: trig_by = json.loads(sig["triggered_by"]) if sig["triggered_by"] else {}
            except Exception: trig_by = {}
            # 共振 — 拉所有原始策略
            if strategy_name == "resonance":
                strategy_list = trig_by.get("strategies", [])
            else:
                strategy_list = [strategy_name]
        except Exception as e:
            logger.debug(f"[strat-analysis] {symbol} 拉入场策略失败: {e}")
            return {}

        if not strategy_list:
            return {}
        # 2) 拉每个策略的 binding params (从 strategy_bindings 表)
        strat_params = {}
        try:
            async with self.db.acquire() as conn:
                for s in strategy_list:
                    cur = await conn.execute(
                        "SELECT params FROM strategy_bindings WHERE strategy_name=? AND market=? AND interval=? LIMIT 1",
                        (s, market, sig["interval"] or "1H"),
                    )
                    pr = await cur.fetchone()
                    if pr and pr["params"]:
                        try: strat_params[s] = json.loads(pr["params"])
                        except Exception: strat_params[s] = {}
                    else:
                        strat_params[s] = {}
        except Exception:
            pass

        # 3) 构造 prompt
        from datetime import datetime, timezone, timedelta
        def fmt(ts):
            if not ts: return "-"
            return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
        hold_hours = (close_at - open_at) / 3600 if open_at and close_at else 0
        max_run_pct = (period_high - open_price) / open_price * 100 if (open_price and period_high) else 0
        max_drawdown_pct = (period_low - open_price) / open_price * 100 if (open_price and period_low) else 0
        prompt = f"""你是量化策略调参专家。下面是一笔已闭环的交易，使用了下列策略入场。请评估每个策略当前参数是否合理，给出改进建议。

【交易概况】
品种: {symbol} ({market})  方向: {side}
开仓: {fmt(open_at)} @ {open_price:.4f}
平仓: {fmt(close_at)} @ {close_price:.4f}
持仓: {hold_hours:.1f} 小时
实现盈亏: {pnl_pct:+.2f}%
持仓期间最高: {period_high or 0:.4f} (最大浮盈 {max_run_pct:+.2f}%)
持仓期间最低: {period_low or 0:.4f} (最大浮亏 {max_drawdown_pct:+.2f}%)
信号系统置信度: {sig['confidence'] or 0}; AI 验证置信度: {sig['ai_confidence'] or 0}

【入场策略 + 当前参数】
"""
        for s in strategy_list:
            prompt += f"- {s}: {json.dumps(strat_params.get(s, {}), ensure_ascii=False)}\n"
        prompt += f"""
【入场时市场快照】
{(snapshot_text or '')[:1500]}

【任务】
对每个策略评估：
1. 当前参数是否适合当前市场（{market}）和品种？
2. 给出 ★1-5 评级（5=完美 / 1=应放弃）
3. 如果可改进，建议新参数（具体数值）+ 预期改善（一句话）
4. 如果是某个共振组合命中，是否过早或过晚？

输出严格 JSON：
{{
  "strategies": [
    {{
      "name": "策略名",
      "current_params": {{...}},
      "evaluation": 1-5,
      "reason": "<= 80 字理由（中文）",
      "suggested_params": {{...}} 或 null,
      "expected_improvement": "<= 50 字（中文，null 也可）"
    }}
  ],
  "overall_conclusion": "<= 60 字综合结论（中文）"
}}

只输出 JSON，不输出别的。
"""
        try:
            result = await self.ai_analyzer._call_llm(prompt, max_tokens=800, path="strategy_param_review")
            if not isinstance(result, dict) or not result.get("strategies"):
                return {}
            return result
        except Exception as e:
            logger.debug(f"[strat-analysis] LLM 调用失败 {symbol}: {e}")
            return {}

    # ──────────────────── Phase A: 教训聚合 + 反馈 ────────────────────

    async def aggregate_lessons(self):
        """v12.7 UPSERT + 生命周期；v12.15 加 adoption_score + 自动采纳：
        - 新模式 INSERT (status=active, last_seen_at=now)
        - 已存在 UPDATE 计数 + last_seen_at（保留 status；不会覆盖 'adopted' / 'disabled'）
        - 60 天没新出现的非 adopted 模式 → 标 'expired'
        - prompt 注入时只用 active + adopted（disabled / expired 跳过）
        - v12.15 新增：每条 lesson 计算 adoption_score；满足 auto_adopt 条件 → 自动写入 risk_rules
        """
        cutoff = int(time.time()) - 60 * 86400
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT pool_id, lessons, link_evaluations, realized_pnl_pct, position_id, close_at "
                    "FROM trade_review WHERE close_at > ? AND lessons IS NOT NULL AND lessons != '[]'",
                    (cutoff,)
                )
                rows = [dict(r) for r in await cur.fetchall()]
        except Exception:
            return 0
        if not rows: return 0
        from collections import defaultdict
        groups = defaultdict(lambda: {"count": 0, "pnls": [], "samples": [], "full_text": ""})
        for r in rows:
            # v12.8: 同时聚合顶层 lessons + link_evaluations 内的 lessons（按环节分类）
            collected_lessons = []
            try:
                top_lessons = json.loads(r["lessons"] or "[]")
                if isinstance(top_lessons, list):
                    collected_lessons.extend(top_lessons)
            except: pass
            try:
                links = json.loads(r["link_evaluations"] or "{}")
                if isinstance(links, dict):
                    for link_key, link_data in links.items():
                        if isinstance(link_data, dict):
                            for l in (link_data.get("lessons", []) or []):
                                if isinstance(l, dict):
                                    # type 用 link 名（更精准）
                                    collected_lessons.append({"type": l.get("type", link_key), "content": l.get("content", "")})
            except: pass

            for l in collected_lessons:
                if not isinstance(l, dict): continue
                t = (l.get("type", "general") or "general")[:30]
                content = (l.get("content", "") or "")[:200]
                if not content: continue
                pool = r["pool_id"] or "all"
                pattern = content[:60]
                key = (pool, t, pattern)
                g = groups[key]
                g["count"] += 1
                g["pnls"].append(r["realized_pnl_pct"] or 0)
                g["full_text"] = content
                if len(g["samples"]) < 5:
                    g["samples"].append(r["position_id"])

        now = int(time.time())
        auto_adopted_count = 0
        try:
            async with self.db.acquire() as conn:
                # v12.11: 改为原子 UPSERT (INSERT ... ON CONFLICT) 防并发竞态
                # 旧 SELECT-then-UPDATE/INSERT 在 batch + 6h loop 同时跑时会撞 UNIQUE 约束
                # ON CONFLICT 子句保留 status（adopted/disabled 不会被覆盖），expired 自动复活成 active
                for (pool, t, pat), g in groups.items():
                    if g["count"] < 1:
                        continue
                    avg = sum(g["pnls"]) / len(g["pnls"]) if g["pnls"] else 0
                    worst = min(g["pnls"]) if g["pnls"] else 0
                    has_params = _detect_lesson_has_params(g["full_text"])
                    score = _compute_adoption_score(
                        occurrences=g["count"], avg_pnl_pct=avg,
                        worst_pnl_pct=worst, has_params=has_params,
                    )
                    samples_json = json.dumps(g["samples"])
                    await conn.execute(
                        "INSERT INTO lesson_pattern "
                        "(pool_id, type, pattern, full_text, occurrences, avg_pnl_pct, "
                        " sample_position_ids, status, last_seen_at, last_updated, "
                        " adoption_score, worst_pnl_pct, has_specific_params) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?) "
                        "ON CONFLICT(pool_id, type, pattern) DO UPDATE SET "
                        "  full_text = excluded.full_text, "
                        "  occurrences = excluded.occurrences, "
                        "  avg_pnl_pct = excluded.avg_pnl_pct, "
                        "  sample_position_ids = excluded.sample_position_ids, "
                        "  last_seen_at = excluded.last_seen_at, "
                        "  last_updated = excluded.last_updated, "
                        "  adoption_score = excluded.adoption_score, "
                        "  worst_pnl_pct = excluded.worst_pnl_pct, "
                        "  has_specific_params = excluded.has_specific_params, "
                        "  status = CASE WHEN status = 'expired' THEN 'active' ELSE status END",
                        (pool, t, pat, g["full_text"], g["count"], avg, samples_json, now, now,
                         score, worst, 1 if has_params else 0)
                    )

                # 自动过期：60 天没在新 review 中出现的非 adopted 模式 → expired
                expire_cutoff = now - 60 * 86400
                cur = await conn.execute(
                    "UPDATE lesson_pattern SET status='expired' "
                    "WHERE status='active' AND (last_seen_at IS NULL OR last_seen_at < ?)",
                    (expire_cutoff,)
                )
                expired_n = cur.rowcount or 0

                await conn.commit()
            n_patterns = sum(1 for g in groups.values() if g["count"] >= 1)
            # v12.15 自动采纳：扫所有 active + 满足 auto_adopt 条件的 lessons
            try:
                auto_adopted_count = await self._auto_adopt_eligible_lessons()
            except Exception as e:
                logger.warning(f"[lessons] 自动采纳异常: {e}")
            msg = f"[lessons] 聚合 — {len(rows)} 笔复盘 → {n_patterns} 个高频模式"
            if expired_n > 0:
                msg += f"，自动过期 {expired_n} 个"
            if auto_adopted_count > 0:
                msg += f"，自动采纳为风控规则 {auto_adopted_count} 条"
            logger.info(msg)
            return n_patterns
        except Exception as e:
            logger.warning(f"[lessons] 聚合落库失败: {e}")
            return 0

    async def merge_similar_lessons(self, dry_run: bool = False) -> Dict[str, Any]:
        """v12.15 用 LLM 把语义重复的教训合并 — 解决"131 条但只 ~10 个独立观点"问题。

        流程：
          1. 拉每个 (pool_id, type) 分组下的所有 active 教训
          2. 单组超过 5 条时调 LLM 聚类（小组直接跳过）
          3. 每个 cluster 保留 occurrences 最高的那条作为 canonical
          4. canonical 累加被合并条目的 occurrences；其它条目 status='disabled' (留痕不删)
          5. 合并后重新评分 + 触发 auto_adopt

        返回 {merged_clusters: N, before: M, after: K, lessons_disabled: D}
        """
        # 1. 拉所有 active + 分组
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT id, pool_id, type, pattern, full_text, occurrences, "
                    "       avg_pnl_pct, worst_pnl_pct, has_specific_params "
                    "FROM lesson_pattern WHERE status='active' "
                    "ORDER BY pool_id, type, occurrences DESC"
                )
                rows = [dict(r) for r in await cur.fetchall()]
        except Exception as e:
            logger.warning(f"[merge-lessons] 查询失败: {e}")
            return {"error": str(e)}
        if not rows:
            return {"merged_clusters": 0, "before": 0, "after": 0, "lessons_disabled": 0}

        from collections import defaultdict
        groups: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
        for r in rows:
            groups[(r["pool_id"], r["type"])].append(r)

        before_count = len(rows)
        clusters_merged = 0
        lessons_disabled = 0
        now = int(time.time())

        for (pool_id, lesson_type), group in groups.items():
            if len(group) <= 4:
                continue  # 4 条以内不值得聚类（LLM 调用 cost 不划算）

            # 2. 调 LLM 聚类
            clusters = await self._cluster_lessons_via_llm(group, pool_id, lesson_type)
            if not clusters:
                logger.debug(f"[merge-lessons] {pool_id}/{lesson_type} 聚类失败/无变化，跳过")
                continue

            # 3. 应用合并（dry_run 时只报告不写库）
            for cluster in clusters:
                lesson_ids = cluster.get("lesson_ids", [])
                if len(lesson_ids) < 2:
                    continue
                # 找 cluster 内 occurrences 最高的作为 canonical
                cluster_lessons = [l for l in group if l["id"] in lesson_ids]
                if not cluster_lessons:
                    continue
                cluster_lessons.sort(key=lambda x: x["occurrences"] or 0, reverse=True)
                canonical = cluster_lessons[0]
                others = cluster_lessons[1:]
                # 加权平均 PnL：canonical_occ × canonical_pnl + Σ(other_occ × other_pnl) / total
                total_occ = canonical["occurrences"] or 0
                total_pnl_sum = (canonical["avg_pnl_pct"] or 0) * total_occ
                worst_pnl = canonical["worst_pnl_pct"] or 0
                has_params = canonical["has_specific_params"] or 0
                for o in others:
                    occ = o["occurrences"] or 0
                    total_occ += occ
                    total_pnl_sum += (o["avg_pnl_pct"] or 0) * occ
                    worst_pnl = min(worst_pnl, o["worst_pnl_pct"] or 0)
                    if (o["has_specific_params"] or 0) > has_params:
                        has_params = o["has_specific_params"]
                merged_avg_pnl = total_pnl_sum / total_occ if total_occ else 0
                # 重新计算 score
                new_score = _compute_adoption_score(
                    occurrences=total_occ, avg_pnl_pct=merged_avg_pnl,
                    worst_pnl_pct=worst_pnl, has_params=bool(has_params),
                )
                if dry_run:
                    logger.info(
                        f"[merge-lessons-dry] cluster {pool_id}/{lesson_type}: "
                        f"keep #{canonical['id']} occ {canonical['occurrences']}→{total_occ} "
                        f"score {new_score:.1f}; disable {[o['id'] for o in others]}"
                    )
                    clusters_merged += 1
                    lessons_disabled += len(others)
                    continue
                # 写库：UPDATE canonical + DISABLE 其它
                try:
                    async with self.db.acquire() as conn:
                        await conn.execute(
                            "UPDATE lesson_pattern SET occurrences=?, avg_pnl_pct=?, "
                            "worst_pnl_pct=?, has_specific_params=?, adoption_score=?, "
                            "last_updated=? WHERE id=?",
                            (total_occ, merged_avg_pnl, worst_pnl, has_params,
                             new_score, now, canonical["id"])
                        )
                        for o in others:
                            await conn.execute(
                                "UPDATE lesson_pattern SET status='disabled', last_updated=? WHERE id=?",
                                (now, o["id"])
                            )
                        await conn.commit()
                    clusters_merged += 1
                    lessons_disabled += len(others)
                except Exception as e:
                    logger.warning(f"[merge-lessons] 合并 cluster 失败: {e}")

        # 4. 触发自动采纳（合并后高 score 教训会满足条件）
        auto_adopted = 0
        if not dry_run and clusters_merged > 0:
            try:
                auto_adopted = await self._auto_adopt_eligible_lessons()
            except Exception as e:
                logger.warning(f"[merge-lessons] 后续 auto_adopt 失败: {e}")

        # 5. 统计 after count
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT COUNT(*) AS n FROM lesson_pattern WHERE status='active'"
                )
                after = (await cur.fetchone())["n"]
        except Exception:
            after = before_count - lessons_disabled

        result = {
            "before": before_count,
            "after": after,
            "merged_clusters": clusters_merged,
            "lessons_disabled": lessons_disabled,
            "auto_adopted_after_merge": auto_adopted,
            "dry_run": dry_run,
        }
        logger.info(f"[merge-lessons] 完成: {result}")
        return result

    async def _llm_translate_lesson(self, lesson_text: str, pool_id: str,
                                    lesson_type: str) -> Optional[Dict]:
        """v12.15.2 LLM 兜底翻译教训为 rule_type + params（启发式失败时调用）。
        约束 LLM 只能从 5 种 rule_type 选；返回不合规的丢弃。
        """
        if not self.ai_analyzer or not lesson_text:
            return None
        prompt = f"""你是交易系统风控规则编译器。给定一条复盘教训，把它翻译成可执行的硬规则。

教训文本：
{lesson_text}

可选 rule_type（只能从这 5 类选，不能创造新类型）：
1. rsi_block            — 拒绝开仓（参数：max_rsi 数字, min_dist_to_high_pct 数字）
2. drawdown_force_close — 强平/强减持仓（参数：loss_threshold_pct 数字, action 'close'|'reduce_50'）
3. trend_block          — 趋势下跌拒绝开仓（参数：lookback_h 整数, max_drop_pct 负数）
4. cooldown_override    — 冷却时长覆盖（参数：market 'us'|'hk'|'cn'|'crypto', cooldown_sec 整数）
5. NULL                 — 该教训不适合编译为硬规则（不可参数化）

输出严格 JSON：
{{"rule_type": "rsi_block" 或其它，"params": {{...}}}}
- 不可参数化的教训：{{"rule_type": null, "params": {{}}}}
- 教训含 RSI 但无具体阈值：用合理默认 max_rsi=80, min_dist_to_high_pct=3
- 教训含"亏损/减仓"但无具体阈值：用 loss_threshold_pct=8, action='reduce_50'

只输出 JSON，不输出其它任何内容。
"""
        try:
            result = await self.ai_analyzer._call_llm(
                prompt, max_tokens=200, path="lesson_translate"
            )
            if not isinstance(result, dict):
                return None
            rt = result.get("rule_type")
            params = result.get("params") or {}
            if not rt or rt == "null" or not isinstance(params, dict):
                return None
            valid_types = {"rsi_block", "drawdown_force_close", "trend_block", "cooldown_override"}
            if rt not in valid_types:
                return None
            logger.info(f"[llm-translate] 教训→{rt} 成功: {lesson_text[:50]}")
            return {"rule_type": rt, "params": params}
        except Exception as e:
            logger.debug(f"[llm-translate] 失败: {e}")
            return None

    async def _cluster_lessons_via_llm(self, lessons: List[Dict],
                                       pool_id: str, lesson_type: str) -> Optional[List[Dict]]:
        """LLM 聚类同一组教训 — 返回 cluster 列表 [{cluster_id, lesson_ids, canonical_text}]
        失败返回 None；不调用方应跳过本组。
        """
        if not self.ai_analyzer:
            return None
        # 构造 prompt
        lessons_text = "\n".join(
            f'{i+1}. (id={l["id"]}) {(l.get("full_text") or l.get("pattern") or "")[:150]}'
            for i, l in enumerate(lessons)
        )
        prompt = f"""你是一个交易复盘分析专家。下面是 {len(lessons)} 条同一池/同一环节（{pool_id} / {lesson_type}）的复盘教训。

任务：判断哪些教训语义相同/几乎相同（表达同一个 actionable 改进），把它们合并为一个 cluster。

合并标准：
- 表达同一个核心 actionable 改进（如"AI 验证应加 RSI 检查"）= 合并，即使措辞不同
- 数值阈值略有差异（RSI>80 vs RSI>85）= 合并，取严格的那个
- 不同的具体规则建议（"加 RSI" vs "加 ATR 止损"）= 不合并
- 单条独立成一类也可以

教训列表：
{lessons_text}

请输出严格 JSON 数组，每个 cluster 一个对象：
[
  {{"cluster_id": 1, "lesson_ids": [1707, 1715, 1718], "canonical_text": "AI 验证应加 RSI 超买检查（最具体的措辞）"}},
  {{"cluster_id": 2, "lesson_ids": [1709], "canonical_text": "..."}}
]

注意：
- lesson_ids 必须是上面教训前的实际数字 id（不是行号）
- 每个 lesson_id 只能出现在一个 cluster 中
- canonical_text 应选最具体可执行的版本（含数值阈值优先）
"""
        try:
            result = await self.ai_analyzer._call_llm(
                prompt, max_tokens=2000, path="lesson_merge"
            )
            if not result:
                return None
            # _call_llm 返回 dict（已 JSON 解析）— 但本任务返回的是数组
            # NewsAIAnalyzer._call_llm 用 response_format=json，所以返回值取决于 LLM 输出形式
            # 兼容两种：dict {"clusters": [...]} 或直接 list [...]
            if isinstance(result, dict):
                clusters = result.get("clusters") or result.get("data") or []
                if not clusters and isinstance(result, dict):
                    # LLM 可能直接 dump 数组到 dict 形式 — 检查所有 value
                    for v in result.values():
                        if isinstance(v, list) and v and isinstance(v[0], dict) and "lesson_ids" in v[0]:
                            clusters = v
                            break
            elif isinstance(result, list):
                clusters = result
            else:
                return None
            if not isinstance(clusters, list):
                return None
            # 校验：每个 cluster 必须有 lesson_ids
            valid = [c for c in clusters if isinstance(c, dict) and isinstance(c.get("lesson_ids"), list)]
            return valid
        except Exception as e:
            logger.warning(f"[merge-lessons] LLM 聚类异常 {pool_id}/{lesson_type}: {e}")
            return None

    async def _auto_adopt_eligible_lessons(self) -> int:
        """v12.15 扫描所有 active 的 lesson，对满足 auto_adopt 条件的自动写入 risk_rules。
        v12.15.1 多条件 OR — 单一阈值过严（昨晚 worst 仅 -3.58% 永远不到 -5）：
          1) worst < -5% AND score >= 10 AND has_params  — 血亏单笔
          2) score >= 15 AND avg_pnl < -2% AND has_params — 合并后高频证据
          3) occurrences >= 8 AND avg_pnl < -1% AND has_params — 反复踩雷
        三条件任一命中即自动采纳；翻译失败的（_heuristic_lesson_to_rule 返回 None）仍跳过。
        """
        now = int(time.time())
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT id, pool_id, type, pattern, full_text, adoption_score,
                              worst_pnl_pct, occurrences, avg_pnl_pct
                       FROM lesson_pattern
                       WHERE status='active' AND has_specific_params=1
                         AND id NOT IN (SELECT source_lesson_id FROM risk_rules WHERE source_lesson_id IS NOT NULL)
                         AND (
                              (worst_pnl_pct <= -5.0 AND adoption_score >= 10)
                           OR (adoption_score >= 15 AND avg_pnl_pct <= -2.0)
                           OR (occurrences >= 8 AND avg_pnl_pct <= -1.0)
                         )
                       ORDER BY adoption_score DESC LIMIT 10"""
                )
                candidates = [dict(r) for r in await cur.fetchall()]
        except Exception as e:
            logger.debug(f"[auto-adopt] 候选查询失败: {e}")
            return 0
        if not candidates:
            return 0
        adopted = 0
        for lesson in candidates:
            try:
                rule = _heuristic_lesson_to_rule(lesson["full_text"], lesson["type"])
                # v12.15.2: 启发式失败 → LLM fallback（高 score 教训值得花一次 LLM 翻译）
                if not rule and self.ai_analyzer:
                    rule = await self._llm_translate_lesson(
                        lesson["full_text"], lesson["pool_id"], lesson["type"],
                    )
                if not rule:
                    continue  # LLM 也翻译失败 → 留给手动采纳
                async with self.db.acquire() as conn:
                    # 写 risk_rules
                    await conn.execute(
                        """INSERT INTO risk_rules
                           (rule_type, pool_id, params, source_lesson_id, source_kind,
                            description, enabled, created_at, updated_at)
                           VALUES (?, ?, ?, ?, 'auto_adopted', ?, 1, ?, ?)""",
                        (rule["rule_type"], lesson["pool_id"], json.dumps(rule["params"]),
                         lesson["id"], lesson["full_text"][:200], now, now)
                    )
                    # 标 lesson 为 adopted
                    await conn.execute(
                        "UPDATE lesson_pattern SET status='adopted', adopted_at=?, last_updated=? WHERE id=?",
                        (now, now, lesson["id"])
                    )
                    await conn.commit()
                logger.info(
                    f"[auto-adopt] 教训 #{lesson['id']} (score={lesson['adoption_score']:.1f}) "
                    f"→ {rule['rule_type']} 规则: {lesson['full_text'][:60]}"
                )
                # v12.15.2 WS 推送（让 mobile 收到"系统刚自动采纳了规则"通知）
                try:
                    if self.ws_hub is not None:
                        broadcast = getattr(self.ws_hub, "broadcast_alert", None)
                        if broadcast:
                            await broadcast({
                                "type": "rule_auto_adopted",
                                "data": {
                                    "lesson_id": lesson["id"],
                                    "lesson_text": lesson["full_text"][:120],
                                    "rule_type": rule["rule_type"],
                                    "params": rule["params"],
                                    "adoption_score": lesson["adoption_score"],
                                    "occurrences": lesson["occurrences"],
                                },
                            })
                except Exception:
                    pass
                adopted += 1
            except Exception as e:
                logger.warning(f"[auto-adopt] 教训 #{lesson['id']} 写入失败: {e}")
        return adopted


# ═════════════════════════════════════════════════════════════════
# v12.15 教训采纳闭环 — 评分 / 启发式翻译 / 自动采纳判定
# ═════════════════════════════════════════════════════════════════

def _compute_adoption_score(occurrences: int, avg_pnl_pct: float,
                            worst_pnl_pct: float, has_params: bool) -> float:
    """教训采纳分数 — 越高越值得变成硬规则。
    - 频次每次 +1（频次本身有价值）
    - 平均亏损 × 2（亏越多越紧迫；赢的"教训"=0 分加权）
    - 单次最差亏损 × 0.5（单笔血亏额外权重）
    - 可参数化加 5 分（关键加分项 — 不能编译成代码的教训永远不该 auto_adopt）
    """
    score = float(occurrences or 1) * 1.0
    score += abs(min(avg_pnl_pct or 0, 0)) * 2.0
    score += abs(min(worst_pnl_pct or 0, 0)) * 0.5
    if has_params:
        score += 5.0
    return round(score, 2)


# 可参数化教训特征关键词 — 出现这些表示教训含有"具体阈值/状态"，可以编译成硬规则
_PARAMETERIZABLE_KEYWORDS = (
    "RSI", "rsi",
    "ATR", "atr",
    "MA5", "MA10", "MA20", "MA50",
    "%", "百分比", "几个点",
    "亏损", "浮亏", "止损", "下跌",
    "上涨", "突破", "前高", "新高",
    "超买", "超卖",
    "趋势", "下跌趋势", "下行通道",
    "诊断", "评级",
    "冷却", "等待",
)

def _detect_lesson_has_params(content: str) -> bool:
    """启发式判断教训文本是否"可参数化" — 含具体技术指标/数值/方向词 → 大概率可编译成硬规则。"""
    if not content:
        return False
    hit_count = sum(1 for kw in _PARAMETERIZABLE_KEYWORDS if kw in content)
    return hit_count >= 2


def _heuristic_lesson_to_rule(content: str, lesson_type: str = ""):
    """启发式把 lesson 文本翻译成 rule_type + params（不调 LLM，纯 keyword 匹配）。
    返回 {rule_type, params} 或 None（无法翻译则交给手动采纳）。
    覆盖 5 种典型教训模式，对昨晚 50 条教训中约 70% 适用。
    """
    if not content:
        return None
    c = content
    cl = c.lower()

    # 1) RSI 超买阻拦：含 "RSI" + 数字（≥80）+ "追"/"严禁"/"避免"
    import re
    m = re.search(r'RSI\s*[>≥&gt;]\s*(\d{2})', c)
    if m and any(kw in c for kw in ("严禁", "避免", "追", "高位", "超买")):
        max_rsi = int(m.group(1))
        # 距前高 % 通常默认 3
        m2 = re.search(r'距前高\s*[<≤&lt;]\s*(\d+(?:\.\d+)?)\s*%', c)
        min_dist = float(m2.group(1)) if m2 else 3.0
        return {
            "rule_type": "rsi_block",
            "params": {"max_rsi": max_rsi, "min_dist_to_high_pct": min_dist},
        }

    # 2) 浮亏强平：含 "亏损" / "浮亏" + 数字 + "%" + "强平"/"减仓"/"止损"
    m = re.search(r'(?:浮亏|亏损)\s*[>≥&gt;]\s*(\d+(?:\.\d+)?)\s*%', c)
    if m and any(kw in c for kw in ("强平", "止损", "减仓", "close", "reduce")):
        loss = float(m.group(1))
        return {
            "rule_type": "drawdown_force_close",
            "params": {"loss_threshold_pct": loss, "action": "close"},
        }

    # 3) 下跌趋势阻拦：含 "下跌" / "下行" / "趋势" + "拒绝"/"过滤"/"不"
    if any(kw in c for kw in ("下跌趋势", "下行通道", "逆势")) and any(
        kw in c for kw in ("拒绝", "过滤", "不入", "禁止", "避免")
    ):
        return {
            "rule_type": "trend_block",
            "params": {"lookback_h": 4, "max_drop_pct": -3.0},
        }

    # 4) 冷却时长：含 "冷却" / "等待" / "N 小时" / "N 分钟"
    m = re.search(r'冷却\s*(\d+)\s*(小时|分钟|h|min)', c, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        sec = n * 3600 if unit in ("小时", "h") else n * 60
        return {
            "rule_type": "cooldown_override",
            "params": {"market": "us", "cooldown_sec": sec},
        }

    # 5) 兜底 — 不可参数化 → 返回 None
    return None


async def get_top_lessons_for_prompt(db, market: str, top_n: int = 5) -> str:
    """v12.7 给 LLM prompt 注入的高频教训段落 — 只用 active + adopted，跳过 disabled/expired。
    adopted 排在最前面（用户已认可），并标注 [已采纳] 让 LLM 给更高权重。"""
    pool_id = {"us": "us_hk", "hk": "us_hk", "cn": "cn", "crypto": "crypto"}.get(market, "us_hk")
    try:
        async with db.acquire() as conn:
            cur = await conn.execute(
                "SELECT type, full_text, occurrences, avg_pnl_pct, status FROM lesson_pattern "
                "WHERE pool_id IN (?, 'all') AND status IN ('active', 'adopted') "
                # adopted 优先，然后 occurrences 降序
                "ORDER BY CASE status WHEN 'adopted' THEN 0 ELSE 1 END, occurrences DESC LIMIT ?",
                (pool_id, top_n)
            )
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows: return ""
        TYPE_CN = {"entry": "入场", "exit": "出场", "risk": "风控", "psychology": "心态", "general": "综合"}
        lines = ["【近期复盘提炼的教训，请在你的判断中考虑（[已采纳] 是用户认可的硬约束）】"]
        for r in rows:
            t_cn = TYPE_CN.get(r["type"], r["type"])
            tag = "[已采纳] " if r["status"] == "adopted" else ""
            lines.append(f"  · {tag}[{t_cn}] {r['full_text']} (出现 {r['occurrences']} 次, 平均收益 {r['avg_pnl_pct']:+.1f}%)")
        return "\n".join(lines) + "\n"
    except Exception:
        return ""


async def set_lesson_status(db, lesson_id: int, status: str) -> bool:
    """v12.7 切换教训状态：active / adopted / disabled。"""
    if status not in ("active", "adopted", "disabled"):
        return False
    try:
        async with db.acquire() as conn:
            now = int(time.time())
            if status == "adopted":
                await conn.execute(
                    "UPDATE lesson_pattern SET status=?, adopted_at=?, last_updated=? WHERE id=?",
                    (status, now, now, lesson_id)
                )
            else:
                await conn.execute(
                    "UPDATE lesson_pattern SET status=?, last_updated=? WHERE id=?",
                    (status, now, lesson_id)
                )
            await conn.commit()
        return True
    except Exception as e:
        logger.warning(f"[lessons] 切换状态失败 id={lesson_id}: {e}")
        return False
