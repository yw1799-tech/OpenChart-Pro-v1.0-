"""按需分析 API 路由 (v12.22.0)

4 个端点:
  POST /api/on-demand/analyze   — 输入 symbol+market+持仓信息,返回 advice
  GET  /api/on-demand/{advice_id}  — 读取已有 advice
  POST /api/on-demand/execute   — 按 advice 执行下单 (重读价格 + 漂移检查 + 复用 cooldown/daily_limit)
  GET  /api/on-demand/history   — 最近 N 条历史
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/on-demand", tags=["按需分析"])


# ─── Pydantic 模型 ──────────────────────────────────────────────
class PositionInput(BaseModel):
    quantity: float
    avg_cost: float
    side: str = "long"            # long / short
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


class AnalyzeRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    market: str                    # crypto / us / hk / cn
    has_position: bool = False     # 用户声明是否有持仓
    position: Optional[PositionInput] = None


class ExecuteRequest(BaseModel):
    advice_id: str
    confirm: bool = True
    # 用户可选覆盖建议的仓位/数量 (基于 sizing 推导,默认走 advice)
    override_qty: Optional[float] = None
    # 合约用: margin_usd 覆盖
    override_margin_usd: Optional[float] = None
    # 合约用: leverage 覆盖
    override_leverage: Optional[int] = None


# ─── 配置常量 ────────────────────────────────────────────────────
PRICE_DRIFT_THRESHOLD = 0.005  # 0.5% 漂移阻断
VALID_MARKETS = ("crypto", "us", "hk", "cn")


# ─── POST /analyze ───────────────────────────────────────────────
@router.post("/analyze")
async def analyze_endpoint(req: AnalyzeRequest):
    """
    收集数据 + LLM 分析,返回结构化建议。
    用户填了 position 即用用户填的;否则按 has_position 决定:
      - has_position=True 但 position=None → 拒绝,要求填持仓信息
      - has_position=False → 假设暂无持仓,LLM 输出 open/wait
    """
    from backend.main import db
    if db is None:
        raise HTTPException(status_code=503, detail="DB 未就绪")

    market = req.market.lower()
    if market not in VALID_MARKETS:
        raise HTTPException(status_code=400, detail=f"market 必须是 {VALID_MARKETS}")

    # 持仓校验
    position_override: Optional[Dict[str, Any]] = None
    if req.has_position:
        if not req.position:
            raise HTTPException(status_code=400, detail="has_position=True 时 position 必填")
        position_override = req.position.model_dump()

    # 1) 收集数据
    from backend.analysis import data_collector
    try:
        collected = await data_collector.collect_all(
            db=db,
            symbol=req.symbol,
            market=market,
            position_override=position_override,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning(f"[on_demand/analyze] 数据收集异常: {e}")
        raise HTTPException(status_code=500, detail=f"数据收集失败: {e}")

    # 数据合理性: 至少要有 t0 价格 OR K 线
    has_t0 = bool((collected.get("t0_snapshot") or {}).get("price"))
    has_klines = bool(collected.get("klines"))
    if not has_t0 and not has_klines:
        raise HTTPException(
            status_code=404,
            detail=f"未找到 {req.symbol}({market}) 的价格/K线数据。可能代码错误或数据未抓取。",
        )

    # 用户声明无持仓但 DB 实际有 → 提示用户但仍按用户意图分析 (treat as "如果暂无持仓应如何")
    if not req.has_position and collected.get("position"):
        # 强制清空 position 让 LLM 走 open/wait 路径
        collected["position"] = None
        collected["_user_ignored_db_position"] = True

    # 2) LLM 分析
    from backend.analysis import on_demand_advisor
    advice = await on_demand_advisor.analyze(db=db, collected_data=collected)
    if not advice:
        raise HTTPException(
            status_code=503,
            detail="AI 分析失败 — 可能 LLM 未配置/超预算/网络异常,请稍后重试或检查 API Key",
        )

    # 3) 返回 advice + 部分 collected_data 给前端展示
    return {
        "advice": advice,
        "t0_snapshot": collected.get("t0_snapshot"),
        "indicators": collected.get("indicators"),
        "position": collected.get("position"),
        "missing_data": collected.get("missing_data"),
        "news_count": len(collected.get("news") or []),
        "klines_intervals": list((collected.get("klines") or {}).keys()),
        "fundamentals": collected.get("fundamentals"),
        "derivatives": collected.get("derivatives"),
        "peers": collected.get("peers"),
        # 完整 news 用单独字段 (前端展开新闻列表用)
        "news": collected.get("news"),
    }


# ─── GET /{advice_id} ──────────────────────────────────────────
@router.get("/advice/{advice_id}")
async def get_advice_endpoint(advice_id: str):
    from backend.main import db
    if db is None:
        raise HTTPException(status_code=503, detail="DB 未就绪")

    from backend.analysis import on_demand_advisor
    rec = await on_demand_advisor.get_advice_by_id(db, advice_id)
    if not rec:
        raise HTTPException(status_code=404, detail="advice not found")
    return rec


# ─── GET /history ──────────────────────────────────────────────
@router.get("/history")
async def history_endpoint(limit: int = Query(20, ge=1, le=100)):
    from backend.main import db
    if db is None:
        raise HTTPException(status_code=503, detail="DB 未就绪")

    from backend.analysis import on_demand_advisor
    items = await on_demand_advisor.get_history(db, limit=limit)
    return {"items": items}


# ─── POST /execute ──────────────────────────────────────────────
@router.post("/execute")
async def execute_endpoint(req: ExecuteRequest):
    """
    执行守门:
      1. 重读实时价 → 漂移 >0.5% 阻断
      2. 同股冷却检查 (复用 auto_trader._check_cooldown)
      3. 单股每日上限检查 (复用 auto_trader._check_daily_limit)
      4. 按 advice.action + position 分发到具体执行函数
      5. 写 trades + 更新 position + 标记 advice 已执行
    """
    if not req.confirm:
        raise HTTPException(status_code=400, detail="必须 confirm=true 才能执行")

    from backend.main import db, auto_trader, portfolio_manager, swap_engine
    if db is None:
        raise HTTPException(status_code=503, detail="DB 未就绪")
    if auto_trader is None:
        raise HTTPException(status_code=503, detail="AutoTrader 未就绪")

    # 1) 取 advice
    from backend.analysis import on_demand_advisor
    rec = await on_demand_advisor.get_advice_by_id(db, req.advice_id)
    if not rec:
        raise HTTPException(status_code=404, detail="advice not found")
    if rec.get("executed"):
        raise HTTPException(status_code=409, detail="该建议已执行过,请重新分析")

    advice = rec.get("advice") or {}
    action = advice.get("action")
    if not action:
        raise HTTPException(status_code=400, detail="advice 损坏: action 缺失")

    symbol = rec["symbol"]
    market = rec["market"]
    t0_price = rec.get("t0_price")
    t0_ts_ms = rec.get("t0_ts_ms")

    # 建议生成时间太久 → 拒绝 (5 分钟过期)
    if t0_ts_ms:
        age_sec = (int(time.time() * 1000) - int(t0_ts_ms)) / 1000
        if age_sec > 300:
            raise HTTPException(
                status_code=410,
                detail=f"建议已过期 ({age_sec:.0f}s > 300s),请重新分析",
            )

    # action=wait 不能执行
    if action == "wait":
        raise HTTPException(status_code=400, detail="action=wait,无可执行操作。请等待触发条件。")

    # 早期市场限制检查 (在风控/价格检查之前快速拒绝,避免无谓副作用)
    if action == "open_short" and market in ("us", "hk", "cn"):
        raise HTTPException(
            status_code=400,
            detail=f"{market} 市场不支持现货做空 (融券未实现)。如需做空请考虑加密合约。",
        )

    # 2) 重读实时价
    current_price = await _get_realtime_price(db, symbol, market)
    if current_price is None or current_price <= 0:
        raise HTTPException(status_code=503, detail="无法获取当前实时价,稍后重试")

    drift_pct = 0.0
    if t0_price and t0_price > 0:
        drift_pct = abs(current_price - float(t0_price)) / float(t0_price)
        if drift_pct > PRICE_DRIFT_THRESHOLD:
            raise HTTPException(
                status_code=409,
                detail=f"价格已漂移 {drift_pct*100:.2f}% (T0={t0_price:.4f} → 现价={current_price:.4f}),"
                       f"超过 {PRICE_DRIFT_THRESHOLD*100:.1f}% 阈值,请重新分析",
            )

    # 3) 冷却 + 每日上限 (复用 auto_trader)
    # hold 仅更新 SL/TP 元数据,不消耗"操作次数",跳过这两个检查
    if action != "hold":
        try:
            if not await auto_trader._check_cooldown(symbol, market):
                raise HTTPException(status_code=429, detail=f"{symbol}({market}) 同股冷却期内,请稍后再试")
            if not await auto_trader._check_daily_limit(symbol, market):
                raise HTTPException(status_code=429, detail=f"{symbol}({market}) 当日操作已达上限")
        except HTTPException:
            raise
        except Exception as e:
            # 风控检查抛异常(DB hiccup 等)不能"放行",必须拒绝
            logger.warning(f"[on_demand/execute] 风控检查异常: {e}")
            raise HTTPException(status_code=503, detail=f"风控检查异常,执行被拒: {e}")

    # 4) 分发执行
    position_info = rec.get("position")  # 分析时的持仓快照
    is_swap = bool(position_info and position_info.get("type") == "swap")

    try:
        if action in ("open_long", "open_short"):
            if market == "crypto" and (is_swap or req.override_margin_usd is not None):
                # 合约开仓
                result = await _execute_swap_open(
                    swap_engine=swap_engine,
                    symbol=symbol,
                    advice=advice,
                    pos_side="long" if action == "open_long" else "short",
                    current_price=current_price,
                    override_margin_usd=req.override_margin_usd,
                    override_leverage=req.override_leverage,
                )
            else:
                # 现货开仓 (含股票 + 加密现货)
                result = await _execute_spot_open(
                    db=db,
                    auto_trader=auto_trader,
                    portfolio_manager=portfolio_manager,
                    symbol=symbol,
                    market=market,
                    advice=advice,
                    side="long" if action == "open_long" else "short",
                    current_price=current_price,
                    override_qty=req.override_qty,
                    advice_id=req.advice_id,
                )

        elif action == "add":
            # 加仓 — 用 collected.position 判断 spot/swap
            if is_swap:
                result = await _execute_swap_add(
                    swap_engine=swap_engine,
                    symbol=symbol,
                    position=position_info,
                    advice=advice,
                    current_price=current_price,
                    override_margin_usd=req.override_margin_usd,
                )
            else:
                result = await _execute_spot_add(
                    db=db,
                    auto_trader=auto_trader,
                    portfolio_manager=portfolio_manager,
                    symbol=symbol,
                    market=market,
                    position=position_info,
                    advice=advice,
                    current_price=current_price,
                    override_qty=req.override_qty,
                    advice_id=req.advice_id,
                )

        elif action in ("reduce", "close"):
            ratio = 0.5 if action == "reduce" else 1.0
            if is_swap:
                result = await _execute_swap_reduce(
                    swap_engine=swap_engine,
                    symbol=symbol,
                    position=position_info,
                    ratio=ratio,
                    current_price=current_price,
                )
            else:
                result = await _execute_spot_reduce(
                    db=db,
                    auto_trader=auto_trader,
                    portfolio_manager=portfolio_manager,
                    symbol=symbol,
                    market=market,
                    position=position_info,
                    ratio=ratio,
                    current_price=current_price,
                    advice_id=req.advice_id,
                )

        elif action == "hold":
            # 仅更新 SL/TP (基于 advice.exit_strategy)
            result = await _execute_hold_update_sltp(
                db=db,
                symbol=symbol,
                market=market,
                position=position_info,
                advice=advice,
            )
        else:
            raise HTTPException(status_code=400, detail=f"不支持的 action: {action}")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[on_demand/execute] 执行异常 advice={req.advice_id} action={action}: {e}")
        raise HTTPException(status_code=500, detail=f"执行失败: {e}")

    # 5) 标记已执行
    try:
        await on_demand_advisor.mark_executed(db, req.advice_id, result.get("execution_id"))
    except Exception as e:
        logger.warning(f"[on_demand/execute] mark_executed 失败 (执行已成功): {e}")

    return {
        "ok": True,
        "advice_id": req.advice_id,
        "action": action,
        "drift_pct": round(drift_pct * 100, 3),
        "executed_price": current_price,
        "result": result,
    }


# ─── 实时价获取 ──────────────────────────────────────────────────
async def _get_realtime_price(db, symbol: str, market: str) -> Optional[float]:
    """复用 auto_trader._get_fresh_price 的逻辑 (已有 ticker + K 线兜底)"""
    from backend.main import auto_trader
    if auto_trader:
        try:
            price = await auto_trader._get_fresh_price(symbol, market, max_age_min=10)
            if price and price > 0:
                return price
        except Exception as e:
            logger.debug(f"[on_demand/price] auto_trader.fresh 失败: {e}")

    # 退化: 直接查 K 线
    for interval in ("15m", "1H", "1D"):
        try:
            async with db.acquire() as conn:
                cur = await conn.execute(
                    f"SELECT close FROM [klines_{market}_{interval.lower()}] "
                    "WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                    (symbol,),
                )
                row = await cur.fetchone()
            if row and row["close"]:
                return float(row["close"])
        except Exception:
            continue
    return None


# ─── 现货开仓 ────────────────────────────────────────────────────
async def _execute_spot_open(
    db, auto_trader, portfolio_manager,
    symbol: str, market: str,
    advice: Dict[str, Any],
    side: str,
    current_price: float,
    override_qty: Optional[float] = None,
    advice_id: Optional[str] = None,
) -> Dict[str, Any]:
    """现货开仓:
      - qty: override_qty,否则按 advice.position_sizing.suggested_pct × pool_cash 折算
      - 写 positions + 扣现金 + 写 auto_trade_log (source='on_demand')
    """
    from backend.trading.fx import market_to_currency, get_rate

    # P1 修复: 股票市场不支持现货做空 (无融券模拟,做空建议应改走加密合约)
    if side == "short" and market in ("us", "hk", "cn"):
        raise HTTPException(
            status_code=400,
            detail=f"{market} 市场不支持现货做空 (融券未实现)。如需做空请考虑加密合约。",
        )

    # P0 修复: 用户声称"无持仓"但 DB 实际有持仓 → portfolio_manager.add_position 会 UPSERT
    # 覆盖原 quantity/avg_cost,毁掉用户真实持仓。这里强制拦截。
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT id, quantity FROM positions WHERE symbol=? AND market=?",
            (symbol, market),
        )
        existing = await cur.fetchone()
    if existing and existing["quantity"]:
        raise HTTPException(
            status_code=409,
            detail=f"DB 中已存在 {symbol}({market}) 持仓 (qty={existing['quantity']}) — "
                   f"请勾选「已有持仓」并填入信息后重新分析,或先平掉旧仓再开新仓",
        )

    # 计算 qty
    qty = override_qty
    if qty is None:
        qty = await _calc_qty_from_sizing(
            auto_trader=auto_trader, market=market, advice=advice, current_price=current_price,
        )
    if qty is None or qty <= 0:
        raise HTTPException(status_code=400, detail=f"无法确定下单数量 (override_qty 为空且 sizing 计算失败)")

    # 规整最小手数
    qty = auto_trader._normalize_qty(market, symbol, qty)
    if qty <= 0:
        raise HTTPException(status_code=400, detail=f"下单数量低于市场最小手数")

    # 汇率
    currency = market_to_currency(market)
    try:
        fx = await get_rate(db, currency)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"汇率获取失败 ({currency}): {e}")
    if fx <= 0:
        raise HTTPException(status_code=503, detail=f"汇率异常 ({currency}={fx})")

    usd_cost = qty * current_price * fx

    # 池资金检查
    ok_buf, why_buf = await auto_trader._check_pool_cash_buffer(market, usd_cost)
    if not ok_buf:
        raise HTTPException(status_code=400, detail=f"资金检查未通过: {why_buf}")

    # 写持仓
    exit_strategy = advice.get("exit_strategy") or {}
    ai_sl = _safe_float(exit_strategy.get("stop_loss"))
    ai_tp = _safe_float(exit_strategy.get("take_profit_1"))

    pid = await portfolio_manager.add_position(
        symbol=symbol, market=market,
        quantity=qty, avg_cost=current_price,
        notes=f"按需分析开{('多' if side=='long' else '空')} (advice action={advice.get('action')})",
        side=side,
    )
    # 写 SL/TP + cost_currency
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE positions SET cost_currency=?, entry_fx_rate=?, total_cost_usd=?, "
            "auto_traded=0, side=?, ai_stop_loss=?, ai_take_profit=? WHERE id=?",
            (currency, fx, usd_cost, side, ai_sl, ai_tp, pid),
        )
        await conn.commit()

    # 扣现金
    await auto_trader._update_cash(-usd_cost, market=market, fx=fx)

    # 写 auto_trade_log
    await auto_trader._log_trade(
        action="open", symbol=symbol, market=market, qty=qty, price=current_price,
        amount_usd=usd_cost, fx=fx, side=side,
        trigger_type="on_demand",
        trigger_detail={
            "advice_id": advice_id,
            "confidence": advice.get("confidence"),
            "side": side,
        },
        reason=f"按需分析开{'多' if side=='long' else '空'} {qty:.4f} @ {current_price:.4f}",
        position_id=pid, remaining_qty=qty,
    )

    return {
        "type": "spot_open",
        "execution_id": pid,
        "position_id": pid,
        "qty": qty,
        "price": current_price,
        "usd_cost": round(usd_cost, 2),
        "stop_loss": ai_sl,
        "take_profit": ai_tp,
    }


# ─── 现货加仓 ────────────────────────────────────────────────────
async def _execute_spot_add(
    db, auto_trader, portfolio_manager,
    symbol: str, market: str,
    position: Optional[Dict[str, Any]],
    advice: Dict[str, Any],
    current_price: float,
    override_qty: Optional[float] = None,
    advice_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not position or not position.get("id"):
        raise HTTPException(status_code=400, detail="加仓需已有持仓 (DB 未找到)")

    from backend.trading.fx import market_to_currency, get_rate

    qty = override_qty
    if qty is None:
        qty = await _calc_qty_from_sizing(
            auto_trader=auto_trader, market=market, advice=advice, current_price=current_price,
        )
    if qty is None or qty <= 0:
        raise HTTPException(status_code=400, detail="无法确定加仓数量")

    qty = auto_trader._normalize_qty(market, symbol, qty)
    if qty <= 0:
        raise HTTPException(status_code=400, detail="加仓数量低于最小手数")

    currency = market_to_currency(market)
    try:
        fx = await get_rate(db, currency)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"汇率获取失败: {e}")
    if fx <= 0:
        raise HTTPException(status_code=503, detail="汇率异常")

    usd_cost = qty * current_price * fx
    ok_buf, why_buf = await auto_trader._check_pool_cash_buffer(market, usd_cost)
    if not ok_buf:
        raise HTTPException(status_code=400, detail=f"资金检查未通过: {why_buf}")

    pid = position["id"]
    side = position.get("side") or "long"
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT quantity, avg_cost, total_cost_usd FROM positions WHERE id=?", (pid,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="持仓已不存在")
        old_qty = float(row["quantity"])
        old_avg = float(row["avg_cost"])
        new_qty = old_qty + qty
        new_avg = (old_qty * old_avg + qty * current_price) / new_qty
        new_total_usd = float(row["total_cost_usd"] or 0) + usd_cost
        await conn.execute(
            "UPDATE positions SET quantity=?, avg_cost=?, total_cost_usd=? WHERE id=?",
            (new_qty, new_avg, new_total_usd, pid),
        )
        await conn.commit()

    await auto_trader._update_cash(-usd_cost, market=market, fx=fx)
    await auto_trader._log_trade(
        action="add", symbol=symbol, market=market, qty=qty, price=current_price,
        amount_usd=usd_cost, fx=fx, side=side,
        trigger_type="on_demand",
        trigger_detail={
            "advice_id": advice_id,
            "confidence": advice.get("confidence"),
            "side": side,
        },
        reason=f"按需分析加仓 {qty:.4f} @ {current_price:.4f}",
        position_id=pid, remaining_qty=new_qty,
    )

    return {
        "type": "spot_add",
        "execution_id": pid,
        "position_id": pid,
        "qty": qty,
        "price": current_price,
        "new_avg": round(new_avg, 6),
        "new_qty": round(new_qty, 6),
    }


# ─── 现货减仓/平仓 ───────────────────────────────────────────────
async def _execute_spot_reduce(
    db, auto_trader, portfolio_manager,
    symbol: str, market: str,
    position: Optional[Dict[str, Any]],
    ratio: float,
    current_price: float,
    advice_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not position or not position.get("id"):
        raise HTTPException(status_code=400, detail="减仓/平仓需已有持仓 (DB 未找到)")

    from backend.trading.fx import market_to_currency, get_rate

    pid = position["id"]
    # 取最新真实持仓 (防 snapshot 过时)
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT id, quantity, avg_cost, side, total_cost_usd FROM positions WHERE id=?",
            (pid,),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="持仓已不存在")
    cur_qty = float(row["quantity"])
    if cur_qty <= 0:
        raise HTTPException(status_code=400, detail="持仓数量为 0")

    qty_to_close = cur_qty * ratio
    qty_to_close = auto_trader._normalize_qty(market, symbol, qty_to_close)
    if qty_to_close <= 0:
        raise HTTPException(status_code=400, detail="可平数量低于最小手数")

    # 减完剩余 < 最小手数 → 全平
    LOT = {"cn": 100, "hk": 100, "us": 1}
    min_lot = LOT.get(market)
    new_qty = cur_qty - qty_to_close
    is_full_close = (ratio >= 1.0) or (min_lot is not None and 0 < new_qty < min_lot)
    if is_full_close:
        qty_to_close = cur_qty
        new_qty = 0

    side = row["side"] or "long"
    avg_cost = float(row["avg_cost"])
    currency = market_to_currency(market)
    try:
        fx = await get_rate(db, currency)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"汇率获取失败: {e}")
    if fx <= 0:
        raise HTTPException(status_code=503, detail="汇率异常")

    if side == "long":
        proceed_usd = qty_to_close * current_price * fx
    else:
        margin_release = qty_to_close * avg_cost * fx
        pnl = (avg_cost - current_price) * qty_to_close * fx
        raw = margin_release + pnl
        proceed_usd = max(0.0, raw)

    async with db.acquire() as conn:
        if is_full_close:
            await conn.execute("DELETE FROM positions WHERE id=?", (pid,))
            await conn.execute("DELETE FROM position_state WHERE position_id=?", (pid,))
        else:
            old_qty = cur_qty
            old_total = float(row["total_cost_usd"] or 0)
            new_total = old_total * (new_qty / old_qty) if old_qty > 0 else 0
            await conn.execute(
                "UPDATE positions SET quantity=?, total_cost_usd=? WHERE id=?",
                (new_qty, new_total, pid),
            )
        await conn.commit()

    await auto_trader._update_cash(proceed_usd, market=market, fx=fx)
    await auto_trader._log_trade(
        action="close" if is_full_close else "reduce",
        symbol=symbol, market=market, qty=qty_to_close, price=current_price,
        amount_usd=proceed_usd, fx=fx, side=side,
        trigger_type="on_demand",
        trigger_detail={"advice_id": advice_id, "ratio": ratio, "side": side},
        reason=f"按需分析{'平仓' if is_full_close else '减仓'} {qty_to_close:.4f} @ {current_price:.4f}",
        position_id=pid, remaining_qty=0 if is_full_close else new_qty,
    )

    return {
        "type": "spot_close" if is_full_close else "spot_reduce",
        "execution_id": pid,
        "position_id": pid,
        "qty_closed": qty_to_close,
        "proceed_usd": round(proceed_usd, 2),
        "remaining_qty": 0 if is_full_close else new_qty,
        "is_full_close": is_full_close,
    }


# ─── 仅更新 SL/TP (hold) ────────────────────────────────────────
async def _execute_hold_update_sltp(
    db, symbol: str, market: str,
    position: Optional[Dict[str, Any]],
    advice: Dict[str, Any],
) -> Dict[str, Any]:
    if not position or not position.get("id"):
        raise HTTPException(status_code=400, detail="未找到对应持仓,无法更新止损/止盈")

    exit_strategy = advice.get("exit_strategy") or {}
    new_sl = _safe_float(exit_strategy.get("stop_loss"))
    new_tp = _safe_float(exit_strategy.get("take_profit_1"))

    if new_sl is None and new_tp is None:
        return {
            "type": "hold_no_change",
            "execution_id": position["id"],
            "message": "建议保持现状,exit_strategy 未给出新止损/止盈",
        }

    pid = position["id"]
    if position.get("type") == "swap":
        async with db.acquire() as conn:
            sets = []
            params: List[Any] = []
            if new_sl is not None:
                sets.append("stop_loss=?"); params.append(new_sl)
            if new_tp is not None:
                sets.append("take_profit=?"); params.append(new_tp)
            if sets:
                params.append(pid)
                await conn.execute(
                    f"UPDATE swap_positions SET {', '.join(sets)} WHERE id=?", params,
                )
                await conn.commit()
    else:
        async with db.acquire() as conn:
            sets = []
            params = []
            if new_sl is not None:
                sets.append("ai_stop_loss=?"); params.append(new_sl)
            if new_tp is not None:
                sets.append("ai_take_profit=?"); params.append(new_tp)
            if sets:
                params.append(pid)
                await conn.execute(
                    f"UPDATE positions SET {', '.join(sets)} WHERE id=?", params,
                )
                await conn.commit()

    return {
        "type": "hold_sltp_updated",
        "execution_id": pid,
        "position_id": pid,
        "new_sl": new_sl,
        "new_tp": new_tp,
    }


# ─── 合约执行 ────────────────────────────────────────────────────
async def _execute_swap_open(
    swap_engine, symbol: str, advice: Dict[str, Any],
    pos_side: str, current_price: float,
    override_margin_usd: Optional[float] = None,
    override_leverage: Optional[int] = None,
) -> Dict[str, Any]:
    if swap_engine is None:
        raise HTTPException(status_code=503, detail="SwapEngine 未启动")

    # 计算 margin_usd
    margin_usd = override_margin_usd
    if margin_usd is None:
        # 从 sizing 推导: sizing.suggested_pct × balance
        try:
            acct = await swap_engine.get_account()
            balance = float(acct.get("balance_usd") or 0)
        except Exception:
            balance = 0
        sizing = advice.get("position_sizing") or {}
        pct = float(sizing.get("suggested_pct") or 5)
        margin_usd = balance * pct / 100
    if margin_usd is None or margin_usd <= 0:
        raise HTTPException(status_code=400, detail=f"margin_usd 无法确定 (balance 不足或 sizing=0)")

    side = "buy" if pos_side == "long" else "sell"
    # P0 修复: swap_engine._to_swap_inst 仅追加 -SWAP, 必须传 "ETH-USDT" (不是 "ETH"),
    # 否则会得到 "ETH-SWAP" 导致合约规格查询失败
    swap_arg_symbol = symbol if "-" in symbol else f"{symbol}-USDT"
    result = await swap_engine.place_order(
        symbol=swap_arg_symbol,
        side=side,
        pos_side=pos_side,
        order_type="market",
        margin_usd=margin_usd,
        leverage=override_leverage,
        intent="open",
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=f"合约下单失败: {result.get('reason')}")
    return {
        "type": "swap_open",
        "execution_id": result.get("order_id"),
        **result,
    }


async def _execute_swap_add(
    swap_engine, symbol: str, position: Dict[str, Any],
    advice: Dict[str, Any], current_price: float,
    override_margin_usd: Optional[float] = None,
) -> Dict[str, Any]:
    if swap_engine is None:
        raise HTTPException(status_code=503, detail="SwapEngine 未启动")
    pos_side = position.get("side") or "long"
    leverage = position.get("leverage")

    margin_usd = override_margin_usd
    if margin_usd is None:
        try:
            acct = await swap_engine.get_account()
            balance = float(acct.get("balance_usd") or 0)
        except Exception:
            balance = 0
        sizing = advice.get("position_sizing") or {}
        pct = float(sizing.get("suggested_pct") or 5)
        margin_usd = balance * pct / 100
    if margin_usd is None or margin_usd <= 0:
        raise HTTPException(status_code=400, detail="加仓 margin_usd 无法确定")

    side = "buy" if pos_side == "long" else "sell"
    swap_arg_symbol = symbol if "-" in symbol else f"{symbol}-USDT"
    result = await swap_engine.place_order(
        symbol=swap_arg_symbol, side=side, pos_side=pos_side,
        order_type="market", margin_usd=margin_usd,
        leverage=leverage, intent="add",
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=f"合约加仓失败: {result.get('reason')}")
    return {"type": "swap_add", "execution_id": result.get("order_id"), **result}


async def _execute_swap_reduce(
    swap_engine, symbol: str, position: Dict[str, Any],
    ratio: float, current_price: float,
) -> Dict[str, Any]:
    if swap_engine is None:
        raise HTTPException(status_code=503, detail="SwapEngine 未启动")
    pos_side = position.get("side") or "long"
    qty = float(position.get("quantity") or 0) * ratio
    if qty <= 0:
        raise HTTPException(status_code=400, detail="减仓数量为 0")

    side = "sell" if pos_side == "long" else "buy"
    swap_arg_symbol = symbol if "-" in symbol else f"{symbol}-USDT"
    intent = "close" if ratio >= 1.0 else "reduce"
    result = await swap_engine.place_order(
        symbol=swap_arg_symbol, side=side, pos_side=pos_side,
        order_type="market", qty=qty,
        leverage=position.get("leverage"), intent=intent,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=f"合约{'平仓' if ratio>=1.0 else '减仓'}失败: {result.get('reason')}")
    return {"type": "swap_reduce" if ratio < 1.0 else "swap_close", "execution_id": result.get("order_id"), **result}


# ─── sizing 转 qty 的辅助 ──────────────────────────────────────
async def _calc_qty_from_sizing(
    auto_trader, market: str, advice: Dict[str, Any], current_price: float,
) -> Optional[float]:
    """按 advice.position_sizing.suggested_pct × pool_cash 算 qty"""
    sizing = advice.get("position_sizing") or {}
    pct = float(sizing.get("suggested_pct") or 0)
    if pct <= 0:
        return None
    try:
        pool_id = auto_trader._pool_for(market)
        pool = await auto_trader.get_pool(pool_id)
        if not pool:
            return None
        cash_local = float(pool.get("cash") or 0)
        if cash_local <= 0:
            return None
        # cash 是池本币;USD 池直接用,CNY 池需要折回 USD
        currency = pool.get("currency") or "USD"
        if currency == "USD":
            cash_usd = cash_local
        else:
            from backend.trading.fx import get_rate
            try:
                rate = await get_rate(auto_trader.db, currency)
                cash_usd = cash_local / rate if rate > 0 else cash_local
            except Exception:
                cash_usd = cash_local
        # 用 pct% 计算下单 USD,再除以 (price × fx) 得 qty
        order_usd = cash_usd * pct / 100
        from backend.trading.fx import get_rate, market_to_currency
        try:
            mkt_ccy = market_to_currency(market)
            fx = await get_rate(auto_trader.db, mkt_ccy)
            if fx <= 0:
                fx = 1.0
        except Exception:
            fx = 1.0
        qty = order_usd / (current_price * fx)
        return qty
    except Exception as e:
        logger.debug(f"[on_demand/sizing] 计算 qty 失败: {e}")
        return None


def _safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None
