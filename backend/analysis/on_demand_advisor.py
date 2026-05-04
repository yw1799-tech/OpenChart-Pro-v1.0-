"""按需分析 — LLM 分析师层 (v12.22.0)

单轮 LLM 分析,扮演资深量化分析师角色:
  - 用数据说话,客观判断,不强求双面平衡
  - 输出: 操作建议 + 主导逻辑 + 支撑/反向信号 + 风险 + 操作参数
  - 信心度仅作把握度参考,不机械触发"建议放弃"
  - 必须列 watch_signals (执行后需关注)

LLM 调用统一走 NewsAIAnalyzer._call_llm (memory 硬要求),
path='on_demand' (新建独立 path,与 signal_verify 等不混)。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── path 配置 (写到 ai_analyzer.PATH_LLM_CONFIG 不需要,会走 PATH_LLM_DEFAULT)
# 默认 deepseek-v4-flash + thinking=high,适合按需分析的中频 + 结构化输出场景
ON_DEMAND_PATH = "on_demand"
ON_DEMAND_MAX_TOKENS = 2400  # 输出含多个数组,留足额度


# ─── prompt 模板 (核心) ────────────────────────────────────────
PROMPT_TEMPLATE = """你是一位资深量化分析师,有 15 年股票/加密交易经验,精通技术分析、基本面分析、衍生品数据解读。

你的任务: 基于用户提供的完整数据,给出专业、客观、可执行的操作建议。

【分析原则】
1. 用数据说话,不预设立场。数据看多就看多,看空就看空,中性就承认中性。
2. 优先识别"主导信号"而非强求两面平衡。如果 80% 信号一致,就明确给出方向。
3. 区分"短期波动"和"趋势性变化",根据持仓周期匹配建议。
4. 风险评估要具体: 不写"市场有风险",要写"如果跌破 X,下方支撑位在 Y,可能再跌 Z%"。
5. 已知缺失数据 ({missing_data_str}) 时,如实告知"该项数据缺失,建议仅供参考",不凭空推测。
6. 持仓周期建议: 日内(<1天) / 短线(1-5天) / 中线(1-4周) / 长线(>1月)。
7. 仓位建议要专业: 把握度高可建议 10-20%,普通 5-10%,试探 2-5%。绝不建议 all-in。
8. 必须给止损和目标位,基于支撑位/阻力位/ATR,不能拍脑袋。
9. **重要 — 检查 t0_snapshot.today_high / today_low**:
   - 给目标价 (take_profit_1/2) 时,必须确保该价格 > today_high(对多头),否则该目标已被今日实时高点超越,需重选更高一档目标 OR 在 main_thesis 注明"今日已达 X,目标已实现,建议止盈/移动止损保本"
   - 同理给止损 (stop_loss) 时,确保 stop_loss < today_low(对多头),否则止损已被击穿
   - 数据延迟提示: t0_snapshot.data_age_min 显示数据延迟分钟数, > 15 分钟时在 main_thesis 注明"数据延迟 X 分钟,实时价可能已偏离"

【场景判断】
- 用户已有持仓: action ∈ {{hold, add, reduce, close}}
- 用户暂无持仓: action ∈ {{open_long, open_short, wait}}
- wait 表示"暂不建议开仓",必须列出"重新评估的触发条件"

【输出 JSON 格式 — 必须严格遵守】
{{
    "action": "hold|add|reduce|close|open_long|open_short|wait",
    "confidence": 0-100,
    "main_thesis": "核心逻辑,1-2 句话",
    "supporting_signals": [
        {{"signal": "信号描述", "data": "具体数据", "weight": "强|中|弱"}}
    ],
    "counter_signals": [
        {{"signal": "反向信号", "data": "具体数据", "impact": "影响描述"}}
    ],
    "key_risks": [
        {{"risk": "风险描述", "trigger": "触发条件", "magnitude": "影响幅度"}}
    ],
    "watch_signals": [
        "执行后需关注的具体信号 1",
        "执行后需关注的具体信号 2"
    ],
    "position_sizing": {{
        "suggested_pct": 0-30,
        "reasoning": "为什么是这个比例"
    }},
    "entry_strategy": {{
        "ideal_price": 数字 或 null (仅 open/add 时填),
        "acceptable_range": [低, 高] 或 null,
        "approach": "市价|限价|分批"
    }},
    "exit_strategy": {{
        "stop_loss": 数字,
        "take_profit_1": 数字,
        "take_profit_2": 数字 或 null,
        "trail_logic": "移动止损规则描述"
    }},
    "time_horizon": "日内|短线|中线|长线",
    "professional_summary": "200-300 字综合判断,像写给客户的分析报告",
    "abort_conditions": [
        "执行前若满足任一条件应放弃: 触发条件 1",
        "触发条件 2"
    ]
}}

【特殊情况】
- 若 action=wait,entry_strategy/exit_strategy 可填 null,但必须在 main_thesis 解释为什么等待,
  并在 watch_signals 列出"满足什么条件可重新分析"。
- 若 action=hold,entry_strategy 填 null,exit_strategy 必须给(更新止损/止盈)。
- 若 action=close,entry_strategy/exit_strategy 都可填 null。

【数据】
{collected_data_json}

只输出 JSON,不要任何其他文字。
"""


# ─── 主入口 ──────────────────────────────────────────────────────
async def analyze(
    db,
    collected_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    单轮 LLM 分析,返回结构化建议。

    参数:
      db: DatabaseManager
      collected_data: data_collector.collect_all() 的输出

    返回:
      建议 dict (含 advice_id, advice 内容, raw 原文等),失败返回 None
    """
    try:
        analyzer = _get_ai_analyzer()
    except RuntimeError as e:
        logger.warning(f"[on_demand_advisor] LLM 入口未就绪: {e}")
        return None

    # 1) 准备 prompt
    missing = collected_data.get("missing_data", [])
    missing_str = ", ".join(missing) if missing else "无"

    # 精简 collected_data → 只给 LLM 需要的字段 (不发 raw K 线 200×5,只发指标 + 摘要)
    llm_data = _trim_for_llm(collected_data)
    data_json = json.dumps(llm_data, ensure_ascii=False, indent=2)

    prompt = PROMPT_TEMPLATE.format(
        missing_data_str=missing_str,
        collected_data_json=data_json,
    )

    # 2) 调 LLM (走统一入口)
    t_start = time.time()
    try:
        result = await analyzer._call_llm(
            prompt=prompt,
            news_id=None,
            max_tokens=ON_DEMAND_MAX_TOKENS,
            path=ON_DEMAND_PATH,
            force=False,  # 走预算检查
        )
    except Exception as e:
        logger.warning(f"[on_demand_advisor] _call_llm 抛异常: {e}")
        return None

    elapsed = time.time() - t_start
    if not result:
        logger.warning(f"[on_demand_advisor] LLM 返回 None ({collected_data.get('symbol')}, {elapsed:.1f}s)")
        return None

    # 3) 校验 + 规整
    advice = _validate_and_normalize(result, collected_data)
    if not advice:
        logger.warning(f"[on_demand_advisor] 校验失败: {result}")
        return None

    advice_id = str(uuid.uuid4())
    advice["advice_id"] = advice_id
    advice["llm_elapsed_sec"] = round(elapsed, 1)

    # 4) 写库
    try:
        await _persist_advice(
            db=db,
            advice_id=advice_id,
            symbol=collected_data["symbol"],
            market=collected_data["market"],
            t0_snapshot=collected_data.get("t0_snapshot") or {},
            advice=advice,
            position=collected_data.get("position"),
        )
    except Exception as e:
        logger.warning(f"[on_demand_advisor] 入库失败: {e}")
        # 入库失败不阻塞返回 (用户已等了 20s)

    logger.info(
        f"[on_demand] {collected_data['symbol']}({collected_data['market']}) "
        f"action={advice['action']} conf={advice['confidence']} ({elapsed:.1f}s)"
    )
    return advice


# ─── 精简数据给 LLM ────────────────────────────────────────────
def _trim_for_llm(data: Dict[str, Any]) -> Dict[str, Any]:
    """裁剪 data → LLM prompt (避免 K 线 raw 灌爆 input tokens)"""
    klines = data.get("klines") or {}
    klines_summary: Dict[str, Any] = {}
    for interval, candles in klines.items():
        if not candles:
            continue
        # 只给最近 30 根 + 数量统计 (最旧→最新)
        recent = candles[-30:]
        klines_summary[interval] = {
            "bar_count": len(candles),
            "recent_30": recent,
            "oldest_ts": candles[0]["ts"] if candles else None,
            "latest_ts": candles[-1]["ts"] if candles else None,
        }

    # 新闻只给标题 + 重要性 + 时间 (内容 LLM 不需要那么多)
    news_summary: List[Dict[str, Any]] = []
    for n in (data.get("news") or [])[:20]:  # 最多 20 条
        news_summary.append({
            "title": n.get("title", ""),
            "source": n.get("source", ""),
            "importance": n.get("importance"),
            "sentiment": n.get("sentiment"),
            "ts": n.get("ts"),
            # content 摘要前 150 字
            "content_excerpt": (n.get("content") or "")[:150],
        })

    return {
        "symbol": data.get("symbol"),
        "market": data.get("market"),
        "t0_snapshot": data.get("t0_snapshot"),
        "klines_summary": klines_summary,
        "indicators": data.get("indicators"),
        "news": news_summary,
        "fundamentals": data.get("fundamentals"),
        "derivatives": data.get("derivatives"),
        "position": data.get("position"),
        "peers": data.get("peers"),
        "missing_data": data.get("missing_data"),
    }


# ─── 校验 + 规整 LLM 输出 ──────────────────────────────────────
VALID_ACTIONS = {"hold", "add", "reduce", "close", "open_long", "open_short", "wait"}


def _validate_and_normalize(
    raw: Dict[str, Any],
    collected_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """LLM 输出校验:
    - action 必须在 VALID_ACTIONS 内
    - 持仓场景 vs 无持仓场景 action 类型必须匹配
    - confidence 必须是 0-100 整数
    - 数值字段必须能转 float
    """
    if not isinstance(raw, dict):
        return None

    action = (raw.get("action") or "").strip().lower()
    if action not in VALID_ACTIONS:
        return None

    has_position = collected_data.get("position") is not None
    if has_position:
        if action not in ("hold", "add", "reduce", "close"):
            logger.warning(f"[validate] 已持仓场景但 action={action},校正为 hold")
            action = "hold"
    else:
        if action not in ("open_long", "open_short", "wait"):
            logger.warning(f"[validate] 无持仓场景但 action={action},校正为 wait")
            action = "wait"

    try:
        confidence = int(raw.get("confidence") or 0)
        confidence = max(0, min(100, confidence))
    except (TypeError, ValueError):
        confidence = 50

    main_thesis = (raw.get("main_thesis") or "").strip()
    if not main_thesis:
        return None  # 必填

    out: Dict[str, Any] = {
        "action": action,
        "confidence": confidence,
        "main_thesis": main_thesis,
        "supporting_signals": _normalize_list(raw.get("supporting_signals")),
        "counter_signals": _normalize_list(raw.get("counter_signals")),
        "key_risks": _normalize_list(raw.get("key_risks")),
        "watch_signals": _normalize_str_list(raw.get("watch_signals")),
        "position_sizing": _normalize_position_sizing(raw.get("position_sizing")),
        "entry_strategy": _normalize_entry_strategy(raw.get("entry_strategy")),
        "exit_strategy": _normalize_exit_strategy(raw.get("exit_strategy")),
        "time_horizon": (raw.get("time_horizon") or "短线").strip(),
        "professional_summary": (raw.get("professional_summary") or "").strip(),
        "abort_conditions": _normalize_str_list(raw.get("abort_conditions")),
    }
    return out


def _normalize_list(v: Any) -> List[Dict[str, Any]]:
    if not isinstance(v, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in v:
        if isinstance(item, dict):
            out.append({k: str(val) if val is not None else "" for k, val in item.items()})
        elif isinstance(item, str):
            out.append({"signal": item})
    return out


def _normalize_str_list(v: Any) -> List[str]:
    if not isinstance(v, list):
        return []
    return [str(x) for x in v if x]


def _normalize_position_sizing(v: Any) -> Dict[str, Any]:
    if not isinstance(v, dict):
        return {"suggested_pct": 0, "reasoning": ""}
    try:
        pct = float(v.get("suggested_pct") or 0)
        pct = max(0.0, min(100.0, pct))
    except (TypeError, ValueError):
        pct = 0.0
    return {
        "suggested_pct": round(pct, 2),
        "reasoning": str(v.get("reasoning") or ""),
    }


def _safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _normalize_entry_strategy(v: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(v, dict):
        return None
    rng = v.get("acceptable_range")
    rng_out = None
    if isinstance(rng, list) and len(rng) == 2:
        lo = _safe_float(rng[0])
        hi = _safe_float(rng[1])
        if lo is not None and hi is not None:
            rng_out = [lo, hi] if lo <= hi else [hi, lo]
    return {
        "ideal_price": _safe_float(v.get("ideal_price")),
        "acceptable_range": rng_out,
        "approach": str(v.get("approach") or "市价"),
    }


def _normalize_exit_strategy(v: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(v, dict):
        return None
    return {
        "stop_loss": _safe_float(v.get("stop_loss")),
        "take_profit_1": _safe_float(v.get("take_profit_1")),
        "take_profit_2": _safe_float(v.get("take_profit_2")),
        "trail_logic": str(v.get("trail_logic") or ""),
    }


# ─── 入库 ────────────────────────────────────────────────────────
async def _persist_advice(
    db,
    advice_id: str,
    symbol: str,
    market: str,
    t0_snapshot: Dict[str, Any],
    advice: Dict[str, Any],
    position: Optional[Dict[str, Any]],
):
    """写 on_demand_advices 表"""
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO on_demand_advices
               (id, symbol, market, t0_price, t0_ts_ms,
                action, confidence, advice_json, position_json,
                executed, execution_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)""",
            (
                advice_id,
                symbol,
                market,
                t0_snapshot.get("price"),
                t0_snapshot.get("ts_ms"),
                advice["action"],
                advice["confidence"],
                json.dumps(advice, ensure_ascii=False),
                json.dumps(position, ensure_ascii=False) if position else None,
                int(time.time()),
            ),
        )
        await conn.commit()


async def get_advice_by_id(db, advice_id: str) -> Optional[Dict[str, Any]]:
    """读建议 (含原始 advice_json)"""
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT * FROM on_demand_advices WHERE id=?",
            (advice_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["advice"] = json.loads(d.get("advice_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["advice"] = {}
    try:
        d["position"] = json.loads(d.get("position_json") or "null")
    except (json.JSONDecodeError, TypeError):
        d["position"] = None
    return d


async def mark_executed(db, advice_id: str, execution_id: Optional[str] = None):
    """标记建议已执行"""
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE on_demand_advices SET executed=1, execution_id=? WHERE id=?",
            (execution_id, advice_id),
        )
        await conn.commit()


async def get_history(db, limit: int = 20) -> List[Dict[str, Any]]:
    """最近 N 条按需分析历史 (轻量,不返回 advice_json 全文)"""
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT id, symbol, market, t0_price, t0_ts_ms, action, confidence, "
            "executed, execution_id, created_at "
            "FROM on_demand_advices ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ─── 工具 ────────────────────────────────────────────────────────
def _get_ai_analyzer():
    """从 main 拿全局 NewsAIAnalyzer 单例 (memory 硬要求所有 LLM 必须走这里)"""
    try:
        from backend.main import ai_analyzer
    except Exception as e:
        raise RuntimeError(f"无法导入 ai_analyzer: {e}")
    if ai_analyzer is None:
        raise RuntimeError("ai_analyzer 未初始化 (lifespan 未跑完?)")
    return ai_analyzer
