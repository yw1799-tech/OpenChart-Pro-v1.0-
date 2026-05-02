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
        self._running = False
        if self._loops_task:
            self._loops_task.cancel()

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
        # 限价单 → 等 limit_order_loop 扫
        return {"ok": True, "order_id": order_id, "status": "pending",
                "limit_price": limit_price, "qty": qty, "leverage": leverage}

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
                    # BUY 限价单：当 best_ask <= limit 时可成交
                    # SELL 限价单：当 best_bid >= limit 时可成交
                    if side == "buy" and ask <= limit:
                        # 判断是 maker 还是 taker
                        # 如果下单时 limit >= 当前 ask 即时成交 → taker
                        # 否则等待成交 → maker (本次成交是被动)
                        # 这里粗略判定：bid_now > limit 一定是 taker
                        is_maker = limit < ask  # 实际是 limit < ask 才能挂单等
                        fill_price = limit  # 限价单成交在限价
                        fee_rate = config.SWAP_MAKER_FEE_RATE if is_maker else config.SWAP_TAKER_FEE_RATE
                    elif side == "sell" and bid >= limit:
                        is_maker = limit > bid
                        fill_price = limit
                        fee_rate = config.SWAP_MAKER_FEE_RATE if is_maker else config.SWAP_TAKER_FEE_RATE
                    else:
                        continue  # 价格未穿过, 继续等
                    # fill
                    nominal = o["qty"] * fill_price * (await self._get_specs(o["symbol"]) or {}).get("ctVal", 0.01)
                    fee = nominal * fee_rate
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
                    await conn.execute(
                        """INSERT INTO swap_positions
                           (id, symbol, pos_side, qty, avg_open_price, leverage, margin_usd,
                            liq_price, contract_size, total_fee_usd, opened_at, last_funding_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (pos_id, order["symbol"], order["pos_side"], order["qty"], fill_price,
                         order["leverage"], margin, liq_price, ct_val, fee, now, now),
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
                        new_liq = self.calc_liq_price(pos["pos_side"], new_avg, pos["leverage"])
                        await conn.execute(
                            """UPDATE swap_positions SET qty=?, avg_open_price=?, margin_usd=?,
                               liq_price=?, total_fee_usd=total_fee_usd+? WHERE id=?""",
                            (new_qty, new_avg, new_margin, new_liq, fee, pos["id"]),
                        )
                        await conn.execute(
                            "UPDATE swap_account SET balance_usd=balance_usd-?-?, total_margin_usd=total_margin_usd+?, updated_at=? WHERE id=1",
                            (order["margin_usd"], fee, order["margin_usd"], now),
                        )
                        pos_id_use = pos["id"]
                        logger.info(
                            f"[swap-fill] ADD {pos['pos_side']} {order['symbol']} +{order['qty']:.4f}@{fill_price:.4f} "
                            f"new_avg={new_avg:.4f} new_qty={new_qty:.4f} fee=${fee:.2f}"
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
                await conn.commit()
            return {"ok": True, "order_id": order["id"], "status": "filled",
                    "fill_price": fill_price, "fee_usd": fee, "is_maker": is_maker}
        except Exception as e:
            logger.warning(f"[swap-engine] mark_filled err: {e}", exc_info=True)
            return {"ok": False, "reason": f"成交处理异常: {e}"}

    # ─────────────────────── 强平监控 ───────────────────────

    async def _liquidation_loop(self, interval_sec: int = 30):
        """每 30s 检查所有 open 持仓的 mark price vs liq_price
        - mark <= liq (long) → 强平 (qty=0, status='liquidated', balance 减 margin)
        - 距强平 < SWAP_PRE_LIQ_REDUCE_THRESHOLD_PCT % → 自动减仓 50% 并标 pre_liq_armed
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
                        # 更新 unrealized PnL + mark
                        if p["pos_side"] == "long":
                            upnl = (mark - p["avg_open_price"]) * p["qty"] * p["contract_size"]
                            dist_to_liq = (mark - p["liq_price"]) / mark * 100 if mark > 0 else 0
                            should_liq = mark <= p["liq_price"]
                        else:
                            upnl = (p["avg_open_price"] - mark) * p["qty"] * p["contract_size"]
                            dist_to_liq = (p["liq_price"] - mark) / mark * 100 if mark > 0 else 0
                            should_liq = mark >= p["liq_price"]
                        async with self.db.acquire() as conn:
                            await conn.execute(
                                "UPDATE swap_positions SET unrealized_pnl_usd=? WHERE id=?",
                                (upnl, p["id"]),
                            )
                            await conn.commit()
                        # 强平
                        if should_liq:
                            await self._force_liquidate(p, mark)
                            continue
                        # 距强平 < 阈值 → 减仓 50%
                        thr = config.SWAP_PRE_LIQ_REDUCE_THRESHOLD_PCT
                        if (not p["pre_liq_armed"]) and 0 < dist_to_liq < thr:
                            await self._pre_liq_reduce(p, mark)
                    except Exception as e:
                        logger.debug(f"[swap-engine] liq check {p.get('symbol')}: {e}")
            except Exception as e:
                logger.warning(f"[swap-engine] liq loop err: {e}")
            await asyncio.sleep(interval_sec)

    async def _force_liquidate(self, pos: Dict, mark: float):
        """强平: 持仓清零, 保证金归 0, status='liquidated'"""
        now = int(time.time())
        try:
            async with self.db.acquire() as conn:
                # 实现 PnL = -保证金 (强平亏完保证金)
                realized = -pos["margin_usd"]
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
