"""
v12.20 加密永续合约模拟交易引擎

设计：真 OKX 数据 + 模拟资金下单 + 真实手续费 + 真实滑点 + 杠杆 + 强平 + funding
完全独立于现货 mock (auto_trader.py / positions 表)，通过 config.CRYPTO_TRADING_MODE 切换

数据流:
  信号 → place_order (limit/market) → swap_orders 表
       ↓
  limit_order_loop (30s) 扫 pending → 现价穿过限价 → fill → swap_positions
       ↓
  liquidation_loop (30s) 扫强平价 → 触发强平 / 距强平<3% 减仓 50%
       ↓
  funding_loop (8h) 结算 funding fee

关键设计:
- 双向持仓 (long + short 同币种可同时持有, UNIQUE(symbol, pos_side))
- 限价单价格 = 当前价 ± 0.15×ATR (BUY 等回踩 / SELL 等反弹)
- 动态杠杆 = base 5x ± AI conf bonus ± ATR 波动调整, clamp [1, 20]
- 手续费 = maker 0.02% (挂单成交) / taker 0.05% (市价 + 限价吃单)
- 强平公式 (OKX isolated): liq = avg × (1 ∓ 1/lev ± 0.005)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from backend import config

logger = logging.getLogger(__name__)


def _to_swap_inst(symbol: str) -> str:
    """BTC-USDT → BTC-USDT-SWAP"""
    if not symbol:
        return symbol
    if symbol.endswith("-SWAP"):
        return symbol
    return f"{symbol}-SWAP"


class MockSwapEngine:
    """加密永续合约模拟交易引擎 (单例，由 main.py 启动)。"""

    def __init__(self, db, ws_hub=None):
        self.db = db
        self.ws_hub = ws_hub
        self._lock = asyncio.Lock()
        self._loops_task: Optional[asyncio.Task] = None
        self._spec_cache: Dict[str, Dict] = {}      # symbol → {ctVal, lotSz, ...}
        self._spec_cache_ts: Dict[str, float] = {}
        self._spec_ttl = 3600
        self._running = False

    # ─────────────────────── 启动 / 停止 ───────────────────────

    async def start(self):
        if self._running:
            return
        # 初始化 swap_account (若不存在)
        await self._ensure_account()
        self._running = True
        # 启动 3 个后台循环：limit 扫单 / 强平监控 / funding 结算
        self._loops_task = asyncio.gather(
            self._limit_order_loop(),
            self._liquidation_loop(),
            self._funding_loop(),
            return_exceptions=True,
        )
        logger.info("[swap-engine] 启动 — 3 个后台 loop 已运行")

    async def stop(self):
        # v12.20.5 Bug 5 修复: cancel 后 await 等待 loop 真停 (避免重启时野任务残留)
        self._running = False
        if self._loops_task:
            self._loops_task.cancel()
            try:
                await self._loops_task
            except (asyncio.CancelledError, Exception):
                pass

    # ─────────────────────── 账户 ───────────────────────

    async def _ensure_account(self):
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT id FROM swap_account WHERE id=1")
            row = await cur.fetchone()
            if not row:
                init = float(getattr(config, "SWAP_INITIAL_BALANCE_USD", 10000))
                await conn.execute(
                    """INSERT INTO swap_account (id, balance_usd, initial_balance_usd, updated_at)
                       VALUES (1, ?, ?, ?)""",
                    (init, init, int(time.time())),
                )
                await conn.commit()
                logger.info(f"[swap-engine] 初始化模拟账户: ${init:.2f}")

    async def get_account(self) -> Dict[str, Any]:
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT * FROM swap_account WHERE id=1")
            row = await cur.fetchone()
            return dict(row) if row else {}

    # ─────────────────────── 合约规格缓存 ───────────────────────

    async def _get_specs(self, swap_inst: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        if swap_inst in self._spec_cache and now - self._spec_cache_ts.get(swap_inst, 0) < self._spec_ttl:
            return self._spec_cache[swap_inst]
        try:
            from backend.data.fetcher import get_fetcher
            from backend.data.models import Market
            okx = get_fetcher(Market.CRYPTO)
            specs = await okx.get_contract_specs(swap_inst)
            if specs:
                self._spec_cache[swap_inst] = specs
                self._spec_cache_ts[swap_inst] = now
            return specs
        except Exception as e:
            logger.debug(f"[swap-engine] specs {swap_inst} failed: {e}")
            return None

    # ─────────────────────── 杠杆 / 限价计算 ───────────────────────

    @staticmethod
    def calc_dynamic_leverage(ai_conf: int, atr_pct: float) -> int:
        """v12.20 动态杠杆: base 5x + AI conf bonus + 波动调整, clamp [1, MAX]"""
        base = config.SWAP_DEFAULT_LEVERAGE
        bonus = 0
        if ai_conf >= 80:
            bonus += 3
        elif ai_conf >= 70:
            bonus += 1
        elif ai_conf < 60:
            bonus -= 2
        # 波动调整：高 ATR 降杠杆，低 ATR 加杠杆
        if atr_pct > 3.0:
            bonus -= 2
        elif atr_pct < 1.5:
            bonus += 2
        lev = max(1, min(config.SWAP_MAX_LEVERAGE, base + bonus))
        return lev

    @staticmethod
    def calc_limit_price(side: str, current_price: float, atr_value: float) -> float:
        """v12.20 限价单挂单价 = 当前价 ± offset × ATR
        BUY: 略低于当前（等回踩） / SELL: 略高于当前（等反弹）
        """
        offset = config.SWAP_LIMIT_ATR_OFFSET * (atr_value or current_price * 0.01)
        if side == "buy":
            return current_price - offset
        return current_price + offset

    @staticmethod
    def calc_slippage_pct(order_usd: float) -> float:
        """市价单滑点 = 基础 + 单笔规模冲击"""
        base = config.SWAP_SLIPPAGE_BASE_PCT
        impact = config.SWAP_SLIPPAGE_PER_1K_PCT * (order_usd / 1000.0)
        return base + impact

    async def calc_initial_sltp(self, swap_inst: str, pos_side: str,
                                 fill_price: float) -> Tuple[Optional[float], Optional[float]]:
        """v12.20.6 算开仓初始 SL/TP (基于 ATR + floor 阈值)
        - SL 距入场: max(2×ATR, 1.5% floor) — 防低波动股噪音
        - TP 距入场: max(4×ATR, 2.5% floor) — 1:2 风险回报
        """
        try:
            from backend.data.cache import cached_get_klines
            from backend.data.models import Market, Interval
            candles = await cached_get_klines(
                db=self.db, market=Market.CRYPTO, symbol=swap_inst,
                interval=Interval("1H"), limit=20,
            )
            atr_value = None
            if candles and len(candles) >= 14:
                trs = []
                for i in range(len(candles) - 14, len(candles)):
                    if i == 0:
                        trs.append(candles[i].high - candles[i].low)
                        continue
                    c, p = candles[i], candles[i - 1]
                    trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
                atr_value = sum(trs) / len(trs)
        except Exception as e:
            logger.debug(f"[swap-sltp] {swap_inst} ATR 计算失败: {e}")
            atr_value = None

        sl_floor = fill_price * (config.SWAP_INITIAL_SL_FLOOR_PCT / 100.0)
        tp_floor = fill_price * (config.SWAP_INITIAL_TP_FLOOR_PCT / 100.0)
        if atr_value:
            sl_dist = max(config.SWAP_INITIAL_SL_ATR_MULT * atr_value, sl_floor)
            tp_dist = max(config.SWAP_INITIAL_TP_ATR_MULT * atr_value, tp_floor)
        else:
            sl_dist = sl_floor
            tp_dist = tp_floor
        if pos_side == "long":
            return (fill_price - sl_dist, fill_price + tp_dist)
        return (fill_price + sl_dist, fill_price - tp_dist)

    @staticmethod
    def calc_liq_price(pos_side: str, avg_price: float, leverage: int) -> float:
        """OKX isolated 强平价公式
        long:  avg × (1 - 1/lev + MMR)
        short: avg × (1 + 1/lev - MMR)
        """
        mmr = config.SWAP_MAINTENANCE_MARGIN_RATE
        if pos_side == "long":
            return avg_price * (1 - 1.0 / leverage + mmr)
        return avg_price * (1 + 1.0 / leverage - mmr)

    # ─────────────────────── 下单主入口 ───────────────────────

    async def place_order(
        self,
        symbol: str,
        side: str,                # buy / sell
        pos_side: str,            # long / short
        order_type: str = "limit",  # limit / market
        qty: Optional[float] = None,        # 张数 (None → 按 margin_usd 算)
        margin_usd: Optional[float] = None, # 想用多少保证金 (qty=None 时必填)
        leverage: Optional[int] = None,
        signal: Optional[Dict] = None,
        intent: str = "open",     # open / close / reduce / add
    ) -> Dict[str, Any]:
        """主入口。返回 {ok, order_id, reason}。"""
        async with self._lock:
            return await self._place_order_locked(
                symbol, side, pos_side, order_type, qty, margin_usd, leverage, signal, intent
            )

    async def _place_order_locked(
        self, symbol, side, pos_side, order_type, qty, margin_usd, leverage, signal, intent,
    ) -> Dict[str, Any]:
        swap_inst = _to_swap_inst(symbol)
        specs = await self._get_specs(swap_inst)
        if not specs:
            return {"ok": False, "reason": f"无法获取 {swap_inst} 合约规格"}
        ct_val = specs["ctVal"]
        min_sz = specs["minSz"]
        lot_sz = specs["lotSz"]

        # 当前价
        from backend.data.fetcher import get_fetcher
        from backend.data.models import Market
        okx = get_fetcher(Market.CRYPTO)
        ticker = await okx.get_ticker(swap_inst)
        if not ticker or not ticker.get("last"):
            return {"ok": False, "reason": "无法获取当前价"}
        current_price = float(ticker["last"])

        # 杠杆
        if leverage is None:
            ai_conf = (signal or {}).get("ai_confidence", 60) or 60
            atr_pct = (signal or {}).get("atr_pct", 2.0) or 2.0
            leverage = self.calc_dynamic_leverage(ai_conf, atr_pct)
        leverage = max(1, min(config.SWAP_MAX_LEVERAGE, int(leverage)))

        # 限价 / 市价 价格
        if order_type == "limit":
            atr_val = (signal or {}).get("atr_value") or current_price * 0.01
            limit_price = self.calc_limit_price(side, current_price, atr_val)
        else:
            limit_price = None

        # 计算 qty (若未提供)
        if qty is None:
            if margin_usd is None:
                return {"ok": False, "reason": "qty 和 margin_usd 必须提供其一"}
            # qty (张) = margin × leverage / (price × ctVal)
            entry_price = limit_price or current_price
            qty = (margin_usd * leverage) / (entry_price * ct_val)
            # 圆整到 lotSz 步长
            qty = max(min_sz, round(qty / lot_sz) * lot_sz)
        # 算实际 margin
        entry_price = limit_price or current_price
        nominal_usd = qty * ct_val * entry_price
        margin = nominal_usd / leverage

        # 检查保证金充足
        acct = await self.get_account()
        if intent == "open" and margin > acct["balance_usd"]:
            return {"ok": False, "reason": f"保证金不足 (需 ${margin:.2f}, 余 ${acct['balance_usd']:.2f})"}

        # 写订单
        order_id = str(uuid.uuid4())
        now = int(time.time())
        expire_at = now + config.SWAP_LIMIT_ORDER_TIMEOUT_SEC
        async with self.db.acquire() as conn:
            await conn.execute(
                """INSERT INTO swap_orders
                   (id, symbol, side, pos_side, order_type, price, qty, leverage, margin_usd,
                    status, signal_id, intent, created_at, expire_at)
                   VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?, ?, ?, ?)""",
                (order_id, swap_inst, side, pos_side, order_type, limit_price, qty, leverage, margin,
                 (signal or {}).get("id"), intent, now, expire_at),
            )
            await conn.commit()

        # 市价单立即 fill
        if order_type == "market":
            return await self._fill_market_order(order_id)
        # v12.20.5 Bug 8 修复: 限价单"下单时已可立即成交" → 立即 taker fill
        # BUY limit ≥ 当前 ask / SELL limit ≤ 当前 bid → 主动吃单 = taker
        bid = float(ticker.get("bidPx") or current_price)
        ask = float(ticker.get("askPx") or current_price)
        immediate_fill = (
            (side == "buy" and limit_price is not None and limit_price >= ask) or
            (side == "sell" and limit_price is not None and limit_price <= bid)
        )
        if immediate_fill:
            return await self._fill_limit_immediate(order_id, ask if side == "buy" else bid)
        # 否则限价单 → 等 limit_order_loop 扫 (passive maker fill)
        return {"ok": True, "order_id": order_id, "status": "pending",
                "limit_price": limit_price, "qty": qty, "leverage": leverage}

    async def _fill_limit_immediate(self, order_id: str, market_px: float) -> Dict[str, Any]:
        """v12.20.5 Bug 8: 限价单下单时若已可立即成交 → taker fill (类似 market)"""
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT * FROM swap_orders WHERE id=?", (order_id,))
            row = await cur.fetchone()
        if not row:
            return {"ok": False, "reason": "订单不存在"}
        order = dict(row)
        # 立即成交的限价单 = 吃单方, 用当前对手价 (BUY 用 ask, SELL 用 bid)
        # 不需要再加滑点 (限价已经定了价格)
        fill_price = market_px
        nominal = order["qty"] * fill_price * (await self._get_specs(order["symbol"]) or {}).get("ctVal", 0.01)
        fee = nominal * config.SWAP_TAKER_FEE_RATE
        return await self._mark_filled(order, fill_price, 0.0, fee, is_maker=False)

    # ─────────────────────── 市价单立即成交 ───────────────────────

    async def _fill_market_order(self, order_id: str) -> Dict[str, Any]:
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT * FROM swap_orders WHERE id=?", (order_id,))
            row = await cur.fetchone()
        if not row:
            return {"ok": False, "reason": "订单不存在"}
        order = dict(row)
        # 当前价
        from backend.data.fetcher import get_fetcher
        from backend.data.models import Market
        okx = get_fetcher(Market.CRYPTO)
        ticker = await okx.get_ticker(order["symbol"])
        if not ticker or not ticker.get("last"):
            await self._reject_order(order_id, "无法获取成交价")
            return {"ok": False, "reason": "无法获取成交价"}
        cur_price = float(ticker["last"])
        # 滑点
        nominal = order["qty"] * cur_price * (await self._get_specs(order["symbol"]) or {}).get("ctVal", 0.01)
        slip_pct = self.calc_slippage_pct(nominal)
        if order["side"] == "buy":
            fill_price = cur_price * (1 + slip_pct / 100.0)
        else:
            fill_price = cur_price * (1 - slip_pct / 100.0)
        # taker 手续费
        fee = nominal * config.SWAP_TAKER_FEE_RATE
        return await self._mark_filled(order, fill_price, slip_pct, fee, is_maker=False)

    # ─────────────────────── 限价单后台扫单 ───────────────────────

    async def _limit_order_loop(self, interval_sec: int = 30):
        """每 30s 扫 pending limit 订单, 检查现价是否穿过限价 + 60min 超时撤单"""
        await asyncio.sleep(60)  # 启动 60s 后再扫
        while self._running:
            try:
                async with self.db.acquire() as conn:
                    cur = await conn.execute(
                        "SELECT * FROM swap_orders WHERE status='pending' AND order_type='limit'"
                    )
                    pending = [dict(r) for r in await cur.fetchall()]
                now = int(time.time())
                from backend.data.fetcher import get_fetcher
                from backend.data.models import Market
                okx = get_fetcher(Market.CRYPTO)
                # 按 symbol 聚合一次拉 ticker
                symbols = {o["symbol"] for o in pending}
                ticker_map = {}
                for s in symbols:
                    try:
                        t = await okx.get_ticker(s)
                        if t and t.get("last"):
                            ticker_map[s] = t
                    except Exception:
                        pass
                for o in pending:
                    # 超时撤单
                    if now >= o["expire_at"]:
                        await self._cancel_order(o["id"], "60min 超时未成交")
                        continue
                    t = ticker_map.get(o["symbol"])
                    if not t:
                        continue
                    last = float(t["last"])
                    bid = float(t.get("bidPx") or last)
                    ask = float(t.get("askPx") or last)
                    # 判断是否穿过限价
                    side = o["side"]
                    limit = o["price"]
                    # v12.20.5 Bug 1 修复:
                    # 这里能扫到的 pending 限价单 = 下单 60s+ 之前已存在 = 一直在订单簿等待
                    # 现在 ask <= limit (BUY) / bid >= limit (SELL) → 价格主动来吃挂单 = MAKER
                    # 注: place_order 已加路径 (Bug 8) 处理"下单时已可立即成交"为 taker
                    if side == "buy" and ask <= limit:
                        is_maker = True   # passive fill, 价格回到限价被动 fill
                        fill_price = limit
                    elif side == "sell" and bid >= limit:
                        is_maker = True
                        fill_price = limit
                    else:
                        continue  # 价格未穿过, 继续等
                    fee_rate = config.SWAP_MAKER_FEE_RATE
                    # fill
                    nominal = o["qty"] * fill_price * (await self._get_specs(o["symbol"]) or {}).get("ctVal", 0.01)
                    fee = nominal * fee_rate
                    # v12.20.5 Bug 13 修复: _mark_filled 必须在 _lock 内
                    # 避免与 place_order(已在 lock)/force_liquidate(已在 lock) 竞态写同一持仓
                    async with self._lock:
                        await self._mark_filled(o, fill_price, 0.0, fee, is_maker)
            except Exception as e:
                logger.warning(f"[swap-engine] limit loop err: {e}")
            await asyncio.sleep(interval_sec)

    # ─────────────────────── 撤单 / 拒单 ───────────────────────

    async def _cancel_order(self, order_id: str, reason: str):
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    "UPDATE swap_orders SET status='cancelled', reject_reason=? WHERE id=?",
                    (reason, order_id),
                )
                await conn.commit()
            logger.info(f"[swap-engine] cancel {order_id[:8]}: {reason}")
        except Exception as e:
            logger.debug(f"[swap-engine] cancel failed: {e}")

    async def _reject_order(self, order_id: str, reason: str):
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    "UPDATE swap_orders SET status='rejected', reject_reason=? WHERE id=?",
                    (reason, order_id),
                )
                await conn.commit()
        except Exception:
            pass

    # ─────────────────────── 成交后更新持仓 + 账户 ───────────────────────

    async def _mark_filled(self, order: Dict, fill_price: float, slip_pct: float,
                            fee: float, is_maker: bool) -> Dict[str, Any]:
        """订单 fill → 更新 swap_orders + swap_positions + swap_account"""
        now = int(time.time())
        try:
            async with self.db.acquire() as conn:
                # 1. 更新订单
                await conn.execute(
                    """UPDATE swap_orders SET status='filled', fill_price=?, fill_qty=?,
                       fee_usd=?, is_maker=?, slippage_pct=?, filled_at=? WHERE id=?""",
                    (fill_price, order["qty"], fee, 1 if is_maker else 0, slip_pct, now, order["id"]),
                )
                # 2. 找/建持仓 (UNIQUE symbol+pos_side)
                cur = await conn.execute(
                    "SELECT * FROM swap_positions WHERE symbol=? AND pos_side=? AND status='open'",
                    (order["symbol"], order["pos_side"]),
                )
                pos_row = await cur.fetchone()
                ct_val = (await self._get_specs(order["symbol"]) or {}).get("ctVal", 0.01)
                # 判断: 加仓 / 减仓 / 开新 / 平仓
                intent = order["intent"]
                if pos_row is None:
                    # 开新仓
                    pos_id = str(uuid.uuid4())
                    margin = order["margin_usd"]
                    liq_price = self.calc_liq_price(order["pos_side"], fill_price, order["leverage"])
                    # v12.20.6: 算初始 SL/TP + 初始化 peak_price = fill_price
                    sl, tp = await self.calc_initial_sltp(order["symbol"], order["pos_side"], fill_price)
                    await conn.execute(
                        """INSERT INTO swap_positions
                           (id, symbol, pos_side, qty, avg_open_price, leverage, margin_usd,
                            liq_price, contract_size, total_fee_usd, opened_at, last_funding_at,
                            stop_loss, take_profit, peak_price, peak_pnl_pct)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?)""",
                        (pos_id, order["symbol"], order["pos_side"], order["qty"], fill_price,
                         order["leverage"], margin, liq_price, ct_val, fee, now, now,
                         sl, tp, fill_price, 0.0),
                    )
                    # 扣保证金 + 手续费
                    await conn.execute(
                        "UPDATE swap_account SET balance_usd=balance_usd-?-?, total_margin_usd=total_margin_usd+?, updated_at=? WHERE id=1",
                        (margin, fee, margin, now),
                    )
                    pos_id_use = pos_id
                    logger.info(
                        f"[swap-fill] OPEN {order['pos_side']} {order['symbol']} {order['qty']:.4f}@{fill_price:.4f} "
                        f"lev={order['leverage']}x margin=${margin:.2f} liq={liq_price:.4f} fee=${fee:.2f}"
                    )
                else:
                    pos = dict(pos_row)
                    # 同方向 → 加仓
                    if intent in ("open", "add"):
                        new_qty = pos["qty"] + order["qty"]
                        new_avg = (pos["avg_open_price"] * pos["qty"] + fill_price * order["qty"]) / new_qty
                        new_margin = pos["margin_usd"] + order["margin_usd"]
                        # v12.20.5 Bug 3 修复: 加仓后实际杠杆 = 总 nominal / 总 margin
                        # 不再用旧 pos.leverage (会导致 liq_price 与 margin 实际杠杆不匹配)
                        new_nominal = new_qty * ct_val * new_avg
                        effective_leverage = new_nominal / new_margin if new_margin > 0 else pos["leverage"]
                        new_lev_int = max(1, min(config.SWAP_MAX_LEVERAGE, int(round(effective_leverage))))
                        new_liq = self.calc_liq_price(pos["pos_side"], new_avg, effective_leverage)
                        await conn.execute(
                            """UPDATE swap_positions SET qty=?, avg_open_price=?, margin_usd=?,
                               leverage=?, liq_price=?, total_fee_usd=total_fee_usd+? WHERE id=?""",
                            (new_qty, new_avg, new_margin, new_lev_int, new_liq, fee, pos["id"]),
                        )
                        await conn.execute(
                            "UPDATE swap_account SET balance_usd=balance_usd-?-?, total_margin_usd=total_margin_usd+?, updated_at=? WHERE id=1",
                            (order["margin_usd"], fee, order["margin_usd"], now),
                        )
                        pos_id_use = pos["id"]
                        logger.info(
                            f"[swap-fill] ADD {pos['pos_side']} {order['symbol']} +{order['qty']:.4f}@{fill_price:.4f} "
                            f"new_avg={new_avg:.4f} new_qty={new_qty:.4f} eff_lev={effective_leverage:.2f}x liq={new_liq:.4f} fee=${fee:.2f}"
                        )
                    # 反方向 → 减仓 / 平仓
                    else:
                        # close_qty = min(order_qty, pos.qty)
                        close_qty = min(order["qty"], pos["qty"])
                        # 实现 PnL: long: (fill - avg) × qty × ctVal / SHORT 反向
                        if pos["pos_side"] == "long":
                            realized = (fill_price - pos["avg_open_price"]) * close_qty * ct_val
                        else:
                            realized = (pos["avg_open_price"] - fill_price) * close_qty * ct_val
                        # 释放保证金按 close 比例
                        margin_released = pos["margin_usd"] * (close_qty / pos["qty"])
                        new_qty = pos["qty"] - close_qty
                        new_margin = pos["margin_usd"] - margin_released
                        if new_qty <= 0.000001:
                            # 全平
                            await conn.execute(
                                """UPDATE swap_positions SET qty=0, status='closed',
                                   realized_pnl_usd=realized_pnl_usd+?, total_fee_usd=total_fee_usd+?,
                                   margin_usd=0, closed_at=? WHERE id=?""",
                                (realized, fee, now, pos["id"]),
                            )
                            # v12.20.9: 全平后 spawn 复盘 (与现货 _execute_close 行为一致)
                            self._spawn_review(pos["id"])
                        else:
                            await conn.execute(
                                """UPDATE swap_positions SET qty=?, margin_usd=?,
                                   realized_pnl_usd=realized_pnl_usd+?, total_fee_usd=total_fee_usd+? WHERE id=?""",
                                (new_qty, new_margin, realized, fee, pos["id"]),
                            )
                        # 账户: 加 (释放保证金 + 实现 PnL - 手续费)
                        await conn.execute(
                            "UPDATE swap_account SET balance_usd=balance_usd+?+?-?, total_margin_usd=total_margin_usd-?, total_pnl_usd=total_pnl_usd+?, updated_at=? WHERE id=1",
                            (margin_released, realized, fee, margin_released, realized, now),
                        )
                        pos_id_use = pos["id"]
                        logger.info(
                            f"[swap-fill] {'CLOSE' if new_qty<=0 else 'REDUCE'} {pos['pos_side']} "
                            f"{order['symbol']} {close_qty:.4f}@{fill_price:.4f} "
                            f"realized=${realized:+.2f} fee=${fee:.2f}"
                        )
                # 关联订单 → position
                await conn.execute(
                    "UPDATE swap_orders SET position_id=? WHERE id=?",
                    (pos_id_use, order["id"]),
                )
                # v12.20.9: 同步写 auto_trade_log (让 _check_cooldown/_check_daily_limit 自动生效)
                # trigger_type='swap_fill' 区分现货
                try:
                    sym_short = order["symbol"].replace("-SWAP", "")
                    intent_to_action = {"open": "open", "add": "add", "reduce": "reduce", "close": "close"}
                    log_action = intent_to_action.get(order.get("intent", "open"), "open")
                    await conn.execute(
                        """INSERT INTO auto_trade_log (symbol, market, action, quantity, price,
                           amount_usd, fx_rate, trigger_type, trigger_detail, reason,
                           status, position_id, traded_at)
                           VALUES (?, 'crypto', ?, ?, ?, ?, 1.0, 'swap_fill', ?, ?,
                                   'executed', ?, ?)""",
                        (sym_short, log_action, order["qty"], fill_price,
                         order["qty"] * fill_price * ct_val,
                         json.dumps({
                             "pos_side": order["pos_side"], "leverage": order["leverage"],
                             "is_maker": is_maker, "fee_usd": fee, "swap_order_id": order["id"],
                         }, ensure_ascii=False),
                         f"swap {order['pos_side']} {log_action} {fill_price:.4f}",
                         pos_id_use, now),
                    )
                except Exception as e:
                    logger.debug(f"[swap-fill] auto_trade_log 写入失败: {e}")
                await conn.commit()
            return {"ok": True, "order_id": order["id"], "status": "filled",
                    "fill_price": fill_price, "fee_usd": fee, "is_maker": is_maker}
        except Exception as e:
            logger.warning(f"[swap-engine] mark_filled err: {e}", exc_info=True)
            return {"ok": False, "reason": f"成交处理异常: {e}"}

    # ─────────────────────── 强平监控 ───────────────────────

    async def _liquidation_loop(self, interval_sec: int = 30):
        """v12.20.6: 5 阶段动态止盈止损闭环
        每 30s 扫所有 open 持仓:
          阶段 1   SL 命中     → close
          阶段 1.5 break-even (浮盈≥1.5% → SL 上移到保本线)
          阶段 2   TP 命中     → close
          阶段 3   分批 T1/T2  (TP 路径 50%/80% 各减 30%)
          阶段 4   trailing    (浮盈≥3% → SL 跟踪上移到 avg+0.6×(peak-avg))
          阶段 5   强平兜底    + pre_liq_reduce (距强平<3% 减 50%)
        """
        await asyncio.sleep(90)
        while self._running:
            try:
                async with self.db.acquire() as conn:
                    cur = await conn.execute(
                        "SELECT * FROM swap_positions WHERE status='open' AND qty > 0"
                    )
                    positions = [dict(r) for r in await cur.fetchall()]
                from backend.data.fetcher import get_fetcher
                from backend.data.models import Market
                okx = get_fetcher(Market.CRYPTO)
                for p in positions:
                    try:
                        mark = await okx.get_mark_price(p["symbol"])
                        if not mark:
                            continue
                        await self._check_position_dynamic_targets(p, mark, okx)
                    except Exception as e:
                        logger.debug(f"[swap-engine] liq check {p.get('symbol')}: {e}")
            except Exception as e:
                logger.warning(f"[swap-engine] liq loop err: {e}")
            await asyncio.sleep(interval_sec)

    async def _check_position_dynamic_targets(self, pos: Dict, mark: float, okx):
        """v12.20.6 单笔持仓 5 阶段检查 (从 _liquidation_loop 调)"""
        side = pos["pos_side"]
        avg = pos["avg_open_price"]
        # 算 PnL + 距强平 + 更新 peak
        if side == "long":
            upnl = (mark - avg) * pos["qty"] * pos["contract_size"]
            pnl_pct = (mark - avg) / avg * 100 if avg > 0 else 0
            dist_to_liq = (mark - pos["liq_price"]) / mark * 100 if (mark > 0 and pos.get("liq_price")) else 999
            should_liq = pos.get("liq_price") and mark <= pos["liq_price"]
            new_peak = max(pos.get("peak_price") or avg, mark)
            new_peak_pnl = max(pos.get("peak_pnl_pct") or 0, pnl_pct)
        else:  # short
            upnl = (avg - mark) * pos["qty"] * pos["contract_size"]
            pnl_pct = (avg - mark) / avg * 100 if avg > 0 else 0
            dist_to_liq = (pos["liq_price"] - mark) / mark * 100 if (mark > 0 and pos.get("liq_price")) else 999
            should_liq = pos.get("liq_price") and mark >= pos["liq_price"]
            new_peak = min(pos.get("peak_price") or avg, mark)
            new_peak_pnl = max(pos.get("peak_pnl_pct") or 0, pnl_pct)

        # 持续更新 unrealized + peak (单字段, 不需 lock)
        async with self.db.acquire() as conn:
            updates = {"unrealized_pnl_usd": upnl, "peak_price": new_peak, "peak_pnl_pct": new_peak_pnl}
            cols = ", ".join(f"{k}=?" for k in updates)
            await conn.execute(
                f"UPDATE swap_positions SET {cols} WHERE id=?",
                list(updates.values()) + [pos["id"]],
            )
            await conn.commit()

        sl = pos.get("stop_loss")
        tp = pos.get("take_profit")

        # ─ 阶段 1: SL 命中 → close (反向市价)
        if sl and ((side == "long" and mark <= sl) or (side == "short" and mark >= sl)):
            await self._close_position(pos, "sl_hit", f"SL 命中 sl={sl:.4f} mark={mark:.4f} pnl={pnl_pct:+.2f}%")
            return

        # ─ 阶段 1.5: Break-even (浮盈 >= 1.5% 锁保本)
        be_arm = config.SWAP_BREAKEVEN_ARM_PNL_PCT
        be_lock = config.SWAP_BREAKEVEN_LOCK_PCT
        if (not pos.get("breakeven_armed")) and pnl_pct >= be_arm:
            if side == "long":
                new_sl = avg * (1 + be_lock / 100)
                if not sl or new_sl > sl:
                    await self._update_pos_sl(pos["id"], new_sl, breakeven_armed=1)
                    sl = new_sl
                    logger.info(f"[swap-BE] {side} {pos['symbol']} 浮盈 {pnl_pct:+.1f}% → SL 上移到 {new_sl:.4f} 锁保本")
            else:
                new_sl = avg * (1 - be_lock / 100)
                if not sl or new_sl < sl:
                    await self._update_pos_sl(pos["id"], new_sl, breakeven_armed=1)
                    sl = new_sl
                    logger.info(f"[swap-BE] short {pos['symbol']} 浮盈 {pnl_pct:+.1f}% → SL 下移到 {new_sl:.4f}")
            # v12.20.7 Bug F: BE 上移 SL 后回头检查是否立即 SL 命中 (避免 30s 延迟)
            if sl and ((side == "long" and mark <= sl) or (side == "short" and mark >= sl)):
                await self._close_position(pos, "sl_hit", f"BE 后立即 SL 命中 sl={sl:.4f} mark={mark:.4f}")
                return

        # ─ 阶段 2: TP 命中 → 全平
        if tp and ((side == "long" and mark >= tp) or (side == "short" and mark <= tp)):
            await self._close_position(pos, "tp_hit", f"TP 命中 tp={tp:.4f} mark={mark:.4f} pnl={pnl_pct:+.2f}%")
            return

        # ─ 阶段 3: 分批 T1/T2
        if tp and avg:
            t1_ratio = config.SWAP_TP_PARTIAL_T1_RATIO
            t2_ratio = config.SWAP_TP_PARTIAL_T2_RATIO
            reduce_ratio = config.SWAP_TP_PARTIAL_REDUCE_RATIO
            if side == "long":
                t1_price = avg + (tp - avg) * t1_ratio
                t2_price = avg + (tp - avg) * t2_ratio
                hit_t1 = mark >= t1_price
                hit_t2 = mark >= t2_price
            else:
                t1_price = avg - (avg - tp) * t1_ratio
                t2_price = avg - (avg - tp) * t2_ratio
                hit_t1 = mark <= t1_price
                hit_t2 = mark <= t2_price
            # T2 优先 (已过 T2 必已过 T1)
            if hit_t2 and not pos.get("tp2_hit"):
                await self._partial_reduce(pos, reduce_ratio, "tp_partial_t2",
                    f"T2 减 {reduce_ratio*100:.0f}% (T2={t2_price:.4f}, mark={mark:.4f}, pnl={pnl_pct:+.1f}%)",
                    set_flag="tp2_hit")
                return
            if hit_t1 and not pos.get("tp1_hit"):
                await self._partial_reduce(pos, reduce_ratio, "tp_partial_t1",
                    f"T1 减 {reduce_ratio*100:.0f}% (T1={t1_price:.4f}, mark={mark:.4f}, pnl={pnl_pct:+.1f}%)",
                    set_flag="tp1_hit")
                return

        # ─ 阶段 4: Trailing (peak 浮盈 >= 3% 启动 → SL 跟踪上移)
        tr_arm = config.SWAP_TRAILING_ARM_PNL_PCT
        tr_keep = config.SWAP_TRAILING_KEEP_RATIO
        if pos.get("trailing_armed") or new_peak_pnl >= tr_arm:
            if side == "long":
                new_trail = avg + tr_keep * (new_peak - avg)
                if new_trail > avg and (not sl or new_trail > sl):
                    await self._update_pos_sl(pos["id"], new_trail, trailing_armed=1)
                    sl = new_trail
                    logger.info(f"[swap-TRAIL] long {pos['symbol']} peak_pnl {new_peak_pnl:+.1f}% → SL 跟踪 {new_trail:.4f}")
            else:
                new_trail = avg - tr_keep * (avg - new_peak)
                if new_trail < avg and (not sl or new_trail < sl):
                    await self._update_pos_sl(pos["id"], new_trail, trailing_armed=1)
                    sl = new_trail
                    logger.info(f"[swap-TRAIL] short {pos['symbol']} peak_pnl {new_peak_pnl:+.1f}% → SL 跟踪 {new_trail:.4f}")
            # v12.20.7 Bug F: trailing 上移 SL 后回头立即检查 SL 命中
            if sl and ((side == "long" and mark <= sl) or (side == "short" and mark >= sl)):
                await self._close_position(pos, "sl_hit", f"trailing 后立即 SL 命中 sl={sl:.4f} mark={mark:.4f}")
                return

        # ─ 阶段 5: 强平兜底 + pre_liq_reduce
        if should_liq:
            await self._force_liquidate(pos, mark)
            return
        thr = config.SWAP_PRE_LIQ_REDUCE_THRESHOLD_PCT
        if (not pos.get("pre_liq_armed")) and 0 < dist_to_liq < thr:
            await self._pre_liq_reduce(pos, mark)

    async def _update_pos_sl(self, pos_id: str, new_sl: float, **flags):
        """v12.20.6 动态更新 SL + 状态标记 (break-even / trailing 上移用)"""
        try:
            async with self._lock:
                async with self.db.acquire() as conn:
                    set_clauses = ["stop_loss=?"]
                    vals: list = [new_sl]
                    for k, v in flags.items():
                        set_clauses.append(f"{k}=?")
                        vals.append(v)
                    vals.append(pos_id)
                    await conn.execute(
                        f"UPDATE swap_positions SET {', '.join(set_clauses)} WHERE id=?", vals,
                    )
                    await conn.commit()
        except Exception as e:
            logger.debug(f"[update-pos-sl] {pos_id} 失败: {e}")

    async def _close_position(self, pos: Dict, reason_tag: str, reason_text: str):
        """v12.20.6 主动平仓 (SL/TP 命中) — 反向市价单
        走 place_order(intent='close') → _mark_filled close 分支 (统一路径)
        """
        side = "sell" if pos["pos_side"] == "long" else "buy"
        # 取消该 symbol+pos_side 的 pending 限价单 (避免再开同向)
        await self._cancel_pending_for_position(pos["symbol"], pos["pos_side"])
        result = await self.place_order(
            symbol=pos["symbol"].replace("-SWAP", ""),
            side=side, pos_side=pos["pos_side"],
            order_type="market", qty=pos["qty"],
            leverage=pos["leverage"], intent="close",
        )
        if result.get("ok"):
            logger.warning(f"[swap-{reason_tag}] {pos['pos_side']} {pos['symbol']} → 平仓 ({reason_text})")
        else:
            logger.warning(f"[swap-{reason_tag}] {pos['pos_side']} {pos['symbol']} 平仓失败: {result.get('reason')}")

    async def _partial_reduce(self, pos: Dict, ratio: float, reason_tag: str,
                               reason_text: str, set_flag: str):
        """v12.20.6 分批减仓 (T1/T2 触发) — 减 ratio% qty + 标记 set_flag=1"""
        reduce_qty = pos["qty"] * ratio
        side = "sell" if pos["pos_side"] == "long" else "buy"
        result = await self.place_order(
            symbol=pos["symbol"].replace("-SWAP", ""),
            side=side, pos_side=pos["pos_side"],
            order_type="market", qty=reduce_qty,
            leverage=pos["leverage"], intent="reduce",
        )
        # 标 flag (避免重复)
        try:
            async with self._lock:
                async with self.db.acquire() as conn:
                    await conn.execute(
                        f"UPDATE swap_positions SET {set_flag}=1 WHERE id=?", (pos["id"],),
                    )
                    await conn.commit()
        except Exception:
            pass
        logger.info(f"[swap-{reason_tag}] {pos['pos_side']} {pos['symbol']} → 减 {ratio*100:.0f}% ({reason_text})")

    async def _cancel_pending_for_position(self, symbol: str, pos_side: str):
        """v12.20.6 平仓前取消该 symbol+pos_side 所有 pending 限价单 (避免回头又开同向)"""
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT id FROM swap_orders WHERE symbol=? AND pos_side=? AND status='pending' AND intent IN ('open','add')",
                    (symbol, pos_side),
                )
                ids = [r["id"] for r in await cur.fetchall()]
            for oid in ids:
                await self._cancel_order(oid, "持仓平仓时同步撤单")
        except Exception as e:
            logger.debug(f"[cancel-pending] {symbol} 失败: {e}")

    def _spawn_review(self, swap_pos_id: str):
        """v12.20.9 spawn 复盘任务 (避免阻塞 fill/liq 主流程)"""
        try:
            from backend.main import trade_reviewer
            if trade_reviewer is None:
                return
            asyncio.create_task(trade_reviewer.review_swap_position(swap_pos_id, force=False))
        except Exception as e:
            logger.debug(f"[swap-review-spawn] {swap_pos_id[:8]} 失败: {e}")

    async def _force_liquidate(self, pos: Dict, mark: float):
        """强平: 持仓清零, 保证金归 0, status='liquidated'
        v12.20.5 Bug 4 修复: 加 self._lock 防与 _mark_filled 写表竞态
        v12.20.6: 强平后取消该 symbol+pos_side 所有 pending 限价单
        v12.20.9: 强平也触发复盘 (要让 LLM 学习强平教训)
        """
        # v12.20.6: 平仓前先取消 pending (在 lock 外, 不会冲突)
        await self._cancel_pending_for_position(pos["symbol"], pos["pos_side"])
        now = int(time.time())
        try:
            async with self._lock:
              async with self.db.acquire() as conn:
                # v12.20.5 加 lock 后再读一次最新状态 (避免重复强平)
                cur = await conn.execute("SELECT status, qty, margin_usd FROM swap_positions WHERE id=?", (pos["id"],))
                latest = await cur.fetchone()
                if not latest or latest["status"] != "open" or (latest["qty"] or 0) <= 0:
                    return  # 已被其他路径关闭
                # 实现 PnL = -保证金 (强平亏完保证金)
                realized = -float(latest["margin_usd"] or pos["margin_usd"])
                await conn.execute(
                    """UPDATE swap_positions SET qty=0, status='liquidated',
                       realized_pnl_usd=realized_pnl_usd+?, margin_usd=0, closed_at=? WHERE id=?""",
                    (realized, now, pos["id"]),
                )
                # 账户：保证金扣完 (但 total_margin 减回, balance 不变 - 已在开仓时扣过)
                await conn.execute(
                    """UPDATE swap_account SET total_margin_usd=total_margin_usd-?,
                       total_pnl_usd=total_pnl_usd+?, updated_at=? WHERE id=1""",
                    (pos["margin_usd"], realized, now),
                )
                await conn.commit()
            logger.warning(
                f"[swap-LIQ] {pos['pos_side']} {pos['symbol']} mark={mark:.4f} <= liq={pos['liq_price']:.4f} "
                f"→ 强平 损失 ${pos['margin_usd']:.2f}"
            )
            # v12.20.9: 强平也要复盘 (学习强平教训)
            self._spawn_review(pos["id"])
        except Exception as e:
            logger.warning(f"[swap-engine] force_liquidate err: {e}")

    async def _pre_liq_reduce(self, pos: Dict, mark: float):
        """距强平 < 3% → 减仓 50% 防爆仓"""
        ratio = config.SWAP_PRE_LIQ_REDUCE_RATIO
        reduce_qty = pos["qty"] * ratio
        # 触发反向市价单减仓
        side = "sell" if pos["pos_side"] == "long" else "buy"
        result = await self.place_order(
            symbol=pos["symbol"].replace("-SWAP", ""),
            side=side,
            pos_side=pos["pos_side"],
            order_type="market",
            qty=reduce_qty,
            leverage=pos["leverage"],
            intent="reduce",
        )
        # 标记已触发, 避免重复
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    "UPDATE swap_positions SET pre_liq_armed=1 WHERE id=?", (pos["id"],),
                )
                await conn.commit()
        except Exception:
            pass
        logger.warning(
            f"[swap-PRE-LIQ] {pos['pos_side']} {pos['symbol']} mark={mark:.4f} 距强平 < {config.SWAP_PRE_LIQ_REDUCE_THRESHOLD_PCT}% "
            f"→ 自动减仓 {ratio*100:.0f}% (qty {reduce_qty:.4f})"
        )

    # ─────────────────────── Funding 8h 结算 ───────────────────────

    async def _funding_loop(self):
        """每 1 分钟检查所有持仓是否到 8h funding 结算时点 (UTC 0/8/16)"""
        await asyncio.sleep(120)
        while self._running:
            try:
                now = int(time.time())
                async with self.db.acquire() as conn:
                    cur = await conn.execute(
                        "SELECT * FROM swap_positions WHERE status='open' AND qty > 0 "
                        "AND last_funding_at < ?",
                        (now - config.SWAP_FUNDING_INTERVAL_SEC,),
                    )
                    positions = [dict(r) for r in await cur.fetchall()]
                if not positions:
                    await asyncio.sleep(60)
                    continue
                from backend.data.fetcher import get_fetcher
                from backend.data.models import Market
                okx = get_fetcher(Market.CRYPTO)
                for p in positions:
                    try:
                        f = await okx.get_funding_rate(p["symbol"])
                        if not f or f.get("fundingRate") is None:
                            continue
                        rate = float(f["fundingRate"])
                        mark = await okx.get_mark_price(p["symbol"]) or p["avg_open_price"]
                        nominal = p["qty"] * p["contract_size"] * mark
                        # long: 付 funding 给 short; rate>0 时多付空收
                        funding_fee = nominal * rate
                        if p["pos_side"] == "long":
                            net = -funding_fee  # 多头付 (rate>0 时为负)
                        else:
                            net = funding_fee   # 空头收
                        async with self.db.acquire() as conn:
                            await conn.execute(
                                """UPDATE swap_positions SET funding_fee_total_usd=funding_fee_total_usd+?,
                                   last_funding_at=? WHERE id=?""",
                                (net, now, p["id"]),
                            )
                            await conn.execute(
                                "UPDATE swap_account SET balance_usd=balance_usd+?, updated_at=? WHERE id=1",
                                (net, now),
                            )
                            await conn.commit()
                        logger.info(
                            f"[swap-funding] {p['pos_side']} {p['symbol']} rate={rate*100:.4f}% "
                            f"net={net:+.4f} USDT (mark={mark:.4f})"
                        )
                    except Exception as e:
                        logger.debug(f"[swap-funding] {p.get('symbol')}: {e}")
            except Exception as e:
                logger.warning(f"[swap-funding] loop err: {e}")
            await asyncio.sleep(60)

    # ─────────────────────── 公开查询 ───────────────────────

    async def list_positions(self, status: str = "open") -> List[Dict]:
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT * FROM swap_positions WHERE status=? ORDER BY opened_at DESC",
                (status,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def list_orders(self, status: Optional[str] = None, limit: int = 50) -> List[Dict]:
        async with self.db.acquire() as conn:
            if status:
                cur = await conn.execute(
                    "SELECT * FROM swap_orders WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                )
            else:
                cur = await conn.execute(
                    "SELECT * FROM swap_orders ORDER BY created_at DESC LIMIT ?", (limit,),
                )
            return [dict(r) for r in await cur.fetchall()]


# 全局单例 (由 main.py 启动时实例化)
swap_engine: Optional[MockSwapEngine] = None
