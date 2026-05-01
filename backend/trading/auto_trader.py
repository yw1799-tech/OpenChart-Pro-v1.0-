"""
AutoTrader — 模拟自动交易引擎。

设计原则：
  - 不连真实交易所，所有下单都是虚拟的
  - 多市场统一以 USD 计价（港股/A股 按当时汇率换算）
  - 触发事件驱动：信号 AI 验证完成 / 诊断更新 / 价格监测
  - 4 种操作：open / add / reduce / close
  - 所有决策写入 auto_trade_log 审计

默认配置（可前端改）：
  - 初始资金 $10,000
  - 市场分配 加密 30% / 美股 40% / 港股 15% / A股 15%
  - 单股上限 5%
  - 冷却期 15 分钟 / 单日同股最多 3 次
  - 池级冷静期（v12.13）：crypto -5% / us_hk -4% / cn -3% 触发 4h 冻结新开仓
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from backend.trading.fx import get_rate, market_to_currency, to_usd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 默认配置
# ═══════════════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "enabled": False,                       # 默认关闭，用户前端打开
    "initial_capital_usd": 10000.0,         # v12.13: 100K→10K 小资金高利用率账户
    # ─ 仓位上限（动态分配）─
    # 旧版固定 allocation (crypto 30/us 40/hk 15/cn 15) 改成"全局总仓位上限 + 单股上限 + 单市场过集中软警告"
    # 优势：哪个市场活跃就往哪儿配，不会出现 us 闲置 40% 而 cn 用 79% 被卡
    "total_position_cap_pct": 0.80,         # 总持仓占初始资金 ≤ 80%（剩 20% 现金缓冲）
    "market_concentration_warn_pct": 0.55,  # 单市场占总持仓 > 55% 给软警告（不拒）
    "allocation": {                          # 保留作"参考分布"展示用，不再硬限制
        "crypto": 0.30, "us": 0.30, "hk": 0.20, "cn": 0.20,
    },
    "cash_buffer_pct": 0.20,                # 20% 全局现金缓冲（fallback）
    "pool_cash_buffer_pct": 0.15,           # v12.9 每池 cash ≥ 15% × 池 initial 才允许新开/加仓
    "max_single_position_pct": 0.08,        # 单股最大 8%（fallback；股票市场用 sizing dict 的 12%）
    # v12.13: 双层 cap — soft (12%) 是 sizing 目标，hard (30%) 是 1 手升配上限
    # 高价 A 股/港股（如 1 手 ¥150×100=¥15K=$2K，超 12% 但在 30% 内）→ 允许升配 1 手开仓
    "hard_single_cap_pct": 0.30,            # 单股绝对硬上限：1 手价超此值才拒绝
    "open_position_pct_buy": 0.04,          # 首仓 4% (fallback)
    "open_position_pct_strong_buy": 0.05,   # strong_buy 首仓 5% (fallback)
    "add_position_pct": 0.01,               # 加仓 +1% (fallback)
    # v11.3: 按市场分档仓位（股票上调，加密保守）— 优先用此 dict；缺失字段 fallback 到上面的 global
    # v12.9: A股 add 提到 3%（¥9K），让中等价 A股能凑齐 1 手加仓；股票其它市场维持 1.5%
    "market_sizing": {
        "us":     {"buy": 0.06, "strong_buy": 0.08, "max_single": 0.12, "add": 0.015},
        "hk":     {"buy": 0.06, "strong_buy": 0.08, "max_single": 0.12, "add": 0.015},
        "cn":     {"buy": 0.06, "strong_buy": 0.08, "max_single": 0.12, "add": 0.030},
        "crypto": {"buy": 0.04, "strong_buy": 0.05, "max_single": 0.08, "add": 0.010},
    },
    "cooldown_sec": 900,                    # 同股冷却 15 分钟（fallback for crypto）
    # v12.14: 按市场区分冷却 — 美股/港股 1h（QCOM 33min 二开亏 -3.58% 教训）
    "cooldown_sec_per_market": {"us": 3600, "hk": 3600, "cn": 1800, "crypto": 900},
    "max_daily_ops_per_symbol": 3,
    "max_concurrent_positions": 15,
    # v12.13: 全局日亏熔断 → 池级冷静期（按市场独立，crypto -5% / us_hk -4% / cn -3% 触发 4h 冻结新开仓）
    # 旧 daily_loss_circuit_pct 已废弃，保留 key 但无逻辑使用
    "market_cooldown_loss_pct_crypto": 0.05,   # 加密池亏损 ≥5% 触发冷静期
    "market_cooldown_loss_pct_us_hk":  0.04,   # 港美股池亏损 ≥4% 触发冷静期
    "market_cooldown_loss_pct_cn":     0.03,   # A股池亏损 ≥3% 触发冷静期
    "market_cooldown_duration_sec":    4 * 3600,  # 冷静期 4 小时（出场不受限）
    "profit_take_pct_for_reduce": 0.15,     # 触达 TP 且盈利 15% 减半（已被 v11 TP/SL 监控替代）
    "reduce_ratios": {                      # （兼容老逻辑保留）
        "rating_hold": 0.50,
        "rating_reduce": 0.70,
        "profit_take": 0.50,
    },
    # v11: 智能减仓 / 分批止盈 / 跟踪止损
    "tp_sl_monitor_interval_sec": 60,       # 巡检循环间隔
    # v12.14 (A7 修复): T1 阈值从 0.33 → 0.50 — 之前候选池 buy 强势股 10min 内即触发 T1 锁微利
    # 原 GOOG#1 22:52 开仓 → 23:02 +1.2% 即平，错过后续 +2% 行情
    "tp_partial_t1_pct": 0.50,              # T1 = avg + (TP-avg) × 0.50 → 减 30%
    "tp_partial_t1_reduce": 0.30,
    "tp_partial_t2_pct": 0.80,              # T2 = avg + (TP-avg) × 0.80 → 再减 30%
    "tp_partial_t2_reduce": 0.30,
    "trailing_arm_pnl_pct": 15.0,           # 浮盈达到 15% 后激活跟踪止损
    "trailing_keep_ratio": 0.60,            # 跟踪线 = avg + 0.60 × (peak - avg)，吐出 40% 峰值利润触发
    # v11.1: 无 AI SL/TP 持仓的机械兜底（手动持仓/旧持仓的下行保护）
    "default_stop_loss_pct": 5.0,           # v12.13: 8%→5% 股票（COIN -8.20% 一刀切教训）
    "default_take_profit_pct": 25.0,        # 股票默认 25%（无 AI TP 时）
    # v12.11: 加密单独覆盖（BTC 一夜 ±8% 是常态，8% 一刀切会乱平）
    "default_stop_loss_pct_crypto": 10.0,    # v12.13: 15%→10% 加密（兜底应该比 AI SL 更紧，否则失去保护意义）
    "default_take_profit_pct_crypto": 40.0,
    "default_targets_grace_sec": 600,       # v11.4 兜底缓冲期
    # v11.3: 僵尸持仓自动减半（资金周转）
    "zombie_age_days": 14,                  # 持仓 ≥ N 天才考虑
    "zombie_pnl_band_pct": 5.0,             # 浮盈 |pct| < band 视为停滞
    "zombie_no_advice_days": 7,             # N 天内无新 AI 诊断
    "zombie_reduce_ratio": 0.30,            # 触发时减仓比例
    "zombie_scan_interval_sec": 3600,       # 每 1h 扫一次
    "min_order_usd": 10.0,                  # 最小下单金额（避免碎单）
    # === v2 修订：诊断驱动开仓 + 按 rating 分流 verify ===
    "trial_position_pct": 0.02,             # 诊断试单仓位（2%，能覆盖 A 股 1 手）
    "trial_cooldown_hours": 12,             # 同股诊断试单冷却 12 小时
    "buy_age_max_hours": 24,                # buy rating 超过 24h 视为过期，走完整 verify
    "strong_buy_age_max_hours": 12,         # strong_buy 超过 12h 视为过期
    "trial_rsi_max": 75,                    # 试单时 RSI 上限（避免追高）
    "trial_dist_to_hi_pct_min": 2.0,        # 试单时距 20 日高最小 %（避免冲到顶）
}


# ═══════════════════════════════════════════════════════════════════
# AutoTrader 类
# ═══════════════════════════════════════════════════════════════════


class AutoTrader:
    """
    自动交易决策 + 执行。
    通过 db + portfolio_manager 两个依赖工作；没有网络 IO 给真实交易所。
    """

    def __init__(self, db, portfolio_manager, ws_hub=None):
        self.db = db
        self.portfolio_manager = portfolio_manager
        self.ws_hub = ws_hub
        self._config: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self._lock = asyncio.Lock()             # 防并发决策
        self._bg_tasks: set = set()

    def _spawn_bg(self, coro):
        """Fire-and-forget 后台任务（用于按需诊断等），保留引用防 GC。"""
        t = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)
        return t

    def _market_size(self, market: str, key: str) -> float:
        """
        v11.3: 按市场取仓位参数。
        key ∈ {'buy', 'strong_buy', 'max_single', 'add'}
        缺失时 fallback 到旧的全局值（兼容老配置）。
        """
        sizing = (self._config.get("market_sizing") or {}).get(market) or {}
        if key in sizing:
            return float(sizing[key])
        # fallback to global flat config
        legacy_map = {
            "buy": "open_position_pct_buy",
            "strong_buy": "open_position_pct_strong_buy",
            "max_single": "max_single_position_pct",
            "add": "add_position_pct",
        }
        return float(self._config.get(legacy_map[key], 0.05))

    @staticmethod
    def _min_lot(market: str, symbol: str) -> float:
        """该市场该 symbol 的"1 手"实际数量，用于减仓时判断"是否凑得齐 1 手"。"""
        m = (market or "").lower()
        if m in ("cn", "hk"): return 100.0
        if m == "us": return 1.0
        if m == "crypto":
            CRYPTO_MIN = {"BTC-USDT": 0.0001, "ETH-USDT": 0.001, "SOL-USDT": 0.01,
                          "BNB-USDT": 0.01, "DOGE-USDT": 1.0, "XRP-USDT": 1.0}
            return CRYPTO_MIN.get(symbol, 0.0001)
        return 1.0

    @staticmethod
    def _normalize_qty(market: str, symbol: str, qty: float) -> float:
        """
        各市场最小下单单位规整。返回向下取整后的合规数量；不足最小手数返回 0。
        - A 股：100 股一手
        - 港股：默认 100 股一手（港股实际不同股不同手数，简化为 100；后续可按 symbol 查表）
        - 美股：1 股起
        - 加密：按 symbol 设最小精度
        """
        if qty <= 0:
            return 0.0
        m = (market or "").lower()
        if m == "cn":
            return float(int(qty // 100) * 100)
        if m == "hk":
            return float(int(qty // 100) * 100)
        if m == "us":
            return float(int(qty))  # 整数股
        if m == "crypto":
            # 各币种最小精度（保守取相对粗的小数位，避免下单失败）
            CRYPTO_PRECISION = {
                "BTC-USDT": 4, "ETH-USDT": 3, "SOL-USDT": 2,
                "BNB-USDT": 2, "DOGE-USDT": 0, "XRP-USDT": 0,
            }
            digits = CRYPTO_PRECISION.get(symbol, 4)
            factor = 10 ** digits
            return float(int(qty * factor) / factor)
        return float(qty)

    # ───────── v12.0 资金池工具（3 池：港美股 USD / A股 CNY / 加密 USD）─────────

    # v12.13: 资金缩到 1/10（用户要求小资金高利用率账户）
    POOLS = [
        {"pool_id": "us_hk",  "name": "港美股",   "currency": "USD", "initial": 10000.0,  "markets": ("us", "hk")},
        {"pool_id": "cn",     "name": "A股",      "currency": "CNY", "initial": 100000.0, "markets": ("cn",)},
        {"pool_id": "crypto", "name": "加密货币", "currency": "USD", "initial": 10000.0,  "markets": ("crypto",)},
    ]
    MARKET_TO_POOL = {"us": "us_hk", "hk": "us_hk", "cn": "cn", "crypto": "crypto"}

    async def _seed_pools(self):
        """v12.2 语义修正：initial 是池子**总规模**（含旧持仓成本），
        cash = initial - 现有持仓成本（按池币换算）。
        - 首次创建：扣掉旧持仓成本
        - 已存在 + initial 调整：按净增量加到 cash
        旧持仓成本超过 initial 时 cash 会变负 → 记 warning 但不阻塞（让用户决定是否清持仓或调高 initial）
        """
        from backend.trading.fx import get_rate, market_to_currency
        # 缓存每市场→池币换算因子
        POOL_CCY = {p["pool_id"]: p["currency"] for p in self.POOLS}
        async def _market_to_pool_factor(market: str, pool_id: str) -> float:
            local_ccy = market_to_currency(market)
            pool_ccy = POOL_CCY[pool_id]
            if local_ccy == pool_ccy: return 1.0
            try:
                local_to_usd = await get_rate(self.db, local_ccy)
                if pool_ccy == "USD": return local_to_usd
                pool_to_usd = await get_rate(self.db, pool_ccy)
                return local_to_usd / pool_to_usd if pool_to_usd > 0 else 1.0
            except Exception:
                return 1.0

        # 计算每池现有持仓成本（按池币）
        pool_held_cost = {p["pool_id"]: 0.0 for p in self.POOLS}
        # v12.2: 同时计算每池**历史已实现盈亏**（按池币）—— 加回 cash 才能让 equity = initial + total_pnl 自洽
        pool_realized = {p["pool_id"]: 0.0 for p in self.POOLS}
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT market, quantity, avg_cost FROM positions WHERE quantity > 0"
            )
            pos_rows = await cur.fetchall()
            cur2 = await conn.execute(
                "SELECT market, action, amount_usd, fx_rate, position_id FROM auto_trade_log "
                "WHERE status='executed'"
            )
            log_rows = await cur2.fetchall()

        for r in pos_rows:
            mkt = r["market"]
            pool_id = self._pool_for(mkt)
            if pool_id not in pool_held_cost: continue
            qty = float(r["quantity"] or 0)
            avg = float(r["avg_cost"] or 0)
            if qty <= 0 or avg <= 0: continue
            factor = await _market_to_pool_factor(mkt, pool_id)
            pool_held_cost[pool_id] += qty * avg * factor

        # v12.11 修：之前仅算 has_close=True 闭环单 → 部分减仓未平时利润被丢
        #          → 池 cash 系统性偏少（持有 5/10 + 减 5 利润 +$100 不入账）
        # 新版：直接累加所有 leg 的净现金流（open/add 减；reduce/close 加），不区分 closed
        # 数学正确性：cash_change_local = Σ(-open) + Σ(reduce) + Σ(close)
        # 配合 pool_held_cost = qty × avg（仅未平仓部分）→ equity = cash + held = initial + 总盈亏 ✓
        # 注意：amount_usd 的符号约定是"绝对值"，需要根据 action 加正负
        from collections import defaultdict
        pool_cashflow = defaultdict(float)  # pool_id -> net cash flow in pool currency
        for r in log_rows:
            pid = r["position_id"]
            if not pid: continue
            pool_id = self._pool_for(r["market"])
            fx_r = r["fx_rate"] or 1.0
            local_orig = (r["amount_usd"] or 0) / fx_r if fx_r > 0 else (r["amount_usd"] or 0)
            factor = await _market_to_pool_factor(r["market"], pool_id)
            local_pool = local_orig * factor
            if r["action"] in ("open", "add"):
                pool_cashflow[pool_id] -= local_pool   # 出钱
            elif r["action"] in ("reduce", "close"):
                pool_cashflow[pool_id] += local_pool   # 回钱（含已实现盈亏）
        # pool_realized = 净现金流 + 当前持仓成本（这两项加起来 = 总盈亏（含未实现），但
        # 我们要的是"已实现"，所以下面 cash 公式直接用 cashflow 替代 realized）
        # 重写 cash 公式：cash = initial + cashflow（持仓成本已通过 pool_cashflow 中的 open/add 扣过了）
        for pid_id, _ in pool_cashflow.items():
            pool_realized[pid_id] = pool_cashflow[pid_id]  # 名字保留兼容下面代码，语义改为"净现金流"

        async with self.db.acquire() as conn:
            now = int(time.time())
            for p in self.POOLS:
                cur = await conn.execute(
                    "SELECT initial_capital, cash FROM auto_trade_pool WHERE pool_id=?",
                    (p["pool_id"],)
                )
                existing = await cur.fetchone()
                if existing is None:
                    # v12.11: cash = initial + 净现金流（已隐含 -open + reduce + close）
                    # 等价于 initial - holding_cost + (out - in) 但兼顾"部分减仓未平仓"场景
                    # equity = cash + MV = initial + cashflow + holding_cost + unrealized = initial + 总盈亏 ✓
                    cashflow = pool_cashflow.get(p["pool_id"], 0.0)
                    cash = p["initial"] + cashflow
                    if cash < 0:
                        logger.warning(f"[pool] {p['name']} initial={p['initial']} + 净现金流={cashflow:.2f} = {cash:.2f} < 0（持仓成本超出本金，建议调高 initial 或清持仓）")
                    await conn.execute(
                        "INSERT INTO auto_trade_pool "
                        "(pool_id, name, currency, initial_capital, cash, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (p["pool_id"], p["name"], p["currency"], p["initial"], cash, now)
                    )
                    logger.info(f"[pool] {p['name']} 创建：initial={p['initial']} + 净现金流 {cashflow:+.2f} = cash {cash:.2f}")
                else:
                    old_init = float(existing["initial_capital"] or 0)
                    new_init = float(p["initial"])
                    if abs(new_init - old_init) > 0.01:
                        delta = new_init - old_init
                        new_cash = float(existing["cash"] or 0) + delta
                        await conn.execute(
                            "UPDATE auto_trade_pool SET initial_capital=?, cash=?, updated_at=? WHERE pool_id=?",
                            (new_init, new_cash, now, p["pool_id"])
                        )
                        logger.info(f"[pool] {p['name']} initial 从 {old_init} → {new_init}，cash 同步增量 {delta:+.2f}")
            await conn.commit()

    def _pool_for(self, market: str) -> str:
        return self.MARKET_TO_POOL.get((market or "").lower(), "us_hk")

    async def get_pool(self, pool_id: str) -> Optional[Dict[str, Any]]:
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT * FROM auto_trade_pool WHERE pool_id=?", (pool_id,))
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_all_pools(self) -> List[Dict[str, Any]]:
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT * FROM auto_trade_pool ORDER BY pool_id")
            return [dict(r) for r in await cur.fetchall()]

    async def _update_pool_cash(self, pool_id: str, delta_local: float):
        """改变指定池的现金（delta_local 为本币：正数=入账，负数=扣款）。"""
        async with self.db.acquire() as conn:
            await conn.execute(
                "UPDATE auto_trade_pool SET cash = cash + ?, updated_at = ? WHERE pool_id = ?",
                (delta_local, int(time.time()), pool_id)
            )
            await conn.commit()

    async def _check_pool_cash_buffer(self, market: str, order_usd: float) -> tuple:
        """v12.9 池级现金缓冲检查：扣掉本笔后，池内 cash 必须 ≥ initial × pool_cash_buffer_pct。
        返回 (ok: bool, reason: str)。
        v12.11 修：
          - FX 失败不再退化 1:1（CRITICAL：会让 CNY 池误算 USD 金额），改拒单
          - 池不存在不再放行（防 _seed_pools 竞态），拒单要求重试
          - cash<0（池本金透支）单独提示，不混在普通缓冲拒绝里
        buffer 配为 0 时禁用此检查。
        """
        from backend.trading.fx import get_rate, market_to_currency
        buffer_pct = float(self._config.get("pool_cash_buffer_pct", 0.0) or 0.0)
        if buffer_pct <= 0:
            return True, ""
        pool_id = self._pool_for(market)
        pool = await self.get_pool(pool_id)
        if not pool:
            return False, f"资金池[{pool_id}] 未就绪（_seed_pools 可能未完成），稍后重试"
        pool_ccy = (pool.get("currency") or "USD").upper()
        # USD → 池币换算（FX 失败必拒单，不退化 1:1）
        if pool_ccy == "USD":
            order_pool = order_usd
        else:
            try:
                pool_to_usd = await get_rate(self.db, pool_ccy)
            except Exception as e:
                return False, f"汇率获取失败 ({pool_ccy}→USD): {e}；为避免金额误算拒单，可重试"
            if pool_to_usd <= 0:
                return False, f"汇率异常 ({pool_ccy}→USD = {pool_to_usd})，拒单"
            order_pool = order_usd / pool_to_usd
        cash = float(pool.get("cash") or 0)
        initial = float(pool.get("initial_capital") or 0)
        floor = initial * buffer_pct
        sym = {"USD":"$", "CNY":"¥", "HKD":"HK$"}.get(pool_ccy, pool_ccy + " ")
        # cash<0：本金透支，单独提示根因
        if cash < 0:
            return False, (
                f"池[{pool.get('name', pool_id)}] 本金已透支（cash={sym}{cash:,.0f} < 0）— "
                f"持仓成本超过 initial {sym}{initial:,.0f}。请补充本金或清理低优先级持仓后重试"
            )
        projected = cash - order_pool
        if projected < floor:
            return False, (
                f"池[{pool.get('name', pool_id)}] 现金 {sym}{cash:,.0f} - 本单 {sym}{order_pool:,.0f} "
                f"= {sym}{projected:,.0f} < 池缓冲下限 {sym}{floor:,.0f}（{buffer_pct*100:.0f}% × {sym}{initial:,.0f}）"
            )
        return True, ""

    # ───────── 配置读写 ─────────

    async def init(self):
        """启动时读配置 + 初始化账户。"""
        # 从 config 表读 auto_trade_config（覆盖默认）
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute("SELECT value FROM config WHERE key='auto_trade_config'")
                row = await cur.fetchone()
            if row and row["value"]:
                saved = json.loads(row["value"])
                self._config.update(saved)
        except Exception as e:
            logger.debug(f"[auto-trader] 配置读取失败: {e}")

        # 初始化账户（INSERT OR IGNORE 避免读后写竞态）
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "INSERT OR IGNORE INTO auto_trade_account (id, initial_capital_usd, cash_usd, updated_at) "
                    "VALUES (1, ?, ?, ?)",
                    (self._config["initial_capital_usd"], self._config["initial_capital_usd"], int(time.time())),
                )
                await conn.commit()
                if cur.rowcount > 0:
                    logger.info(f"[auto-trader] 初始化账户：${self._config['initial_capital_usd']}")
        except Exception as e:
            logger.warning(f"[auto-trader] 账户初始化失败: {e}")

        # v12.0 资金池初始化（首次会按 trade log 重算 cash）
        try:
            await self._seed_pools()
        except Exception as e:
            logger.warning(f"[auto-trader] 资金池种子失败: {e}")

        # 日志保留策略：删除 90 天前 + 保留最近 10000 条
        try:
            cutoff = int(time.time()) - 90 * 86400
            async with self.db.acquire() as conn:
                await conn.execute("DELETE FROM auto_trade_log WHERE traded_at < ?", (cutoff,))
                await conn.execute(
                    "DELETE FROM auto_trade_log WHERE id NOT IN "
                    "(SELECT id FROM auto_trade_log ORDER BY traded_at DESC LIMIT 10000)"
                )
                await conn.commit()
        except Exception as e:
            logger.debug(f"[auto-trader] 日志清理失败: {e}")

        # v12.15: 启动时 seed 默认 risk_rules（idempotent，已存在不重写）
        try:
            from backend.trading.risk_engine import seed_default_rules, review_hits_loop
            await seed_default_rules(self.db)
            # 启动 false_reject 回查循环（30min 间隔）
            self._spawn_bg(review_hits_loop(self.db, interval_sec=1800))
            logger.info("[auto-trader] risk_rules 回查循环已启动 (30min 间隔)")
        except Exception as e:
            logger.warning(f"[auto-trader] risk_rules 启动失败: {e}")
        # v11: 启动 TP/SL 巡检循环（每 60s 跑一次，仅交易时段；fire-and-forget）
        self._spawn_bg(self._tp_sl_monitor_loop())
        logger.info(f"[auto-trader] TP/SL 巡检循环已启动 (间隔 {self._config.get('tp_sl_monitor_interval_sec', 60)}s)")
        # v11.3: 僵尸持仓巡检（每 1h，仅交易时段才动作）
        self._spawn_bg(self._zombie_position_loop())
        logger.info(f"[auto-trader] 僵尸持仓巡检已启动 (间隔 {self._config.get('zombie_scan_interval_sec', 3600)}s)")

    def get_config(self) -> Dict[str, Any]:
        return dict(self._config)

    async def set_config(self, updates: Dict[str, Any]):
        """部分更新配置并持久化到 config 表。"""
        self._config.update(updates)
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES ('auto_trade_config', ?)",
                    (json.dumps(self._config, ensure_ascii=False),),
                )
                await conn.commit()
        except Exception as e:
            logger.warning(f"[auto-trader] 配置保存失败: {e}")

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled"))

    # ───────── 账户操作 ─────────

    async def get_account(self) -> Dict[str, Any]:
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT * FROM auto_trade_account WHERE id=1")
            row = await cur.fetchone()
            return dict(row) if row else {}

    async def _update_cash(self, delta_usd: float, market: Optional[str] = None, fx: Optional[float] = None):
        """v12.11 修：之前把 market_ccy 数字直接写进池表，导致 HKD 持仓变 USD 池 cash 时数值放大 7.8×。
        新版：delta_pool_local = delta_usd × (USD→池币 因子)。
        - delta_usd: 正=入账 / 负=扣款（USD）
        - market: 用于查所属池
        - fx 参数已废弃（保留签名兼容旧调用），不再使用
        """
        from backend.trading.fx import get_rate
        now = int(time.time())
        async with self.db.acquire() as conn:
            # 兼容旧 account（汇总用，前端可能仍读它）
            await conn.execute(
                "UPDATE auto_trade_account SET cash_usd = cash_usd + ?, updated_at = ? WHERE id=1",
                (delta_usd, now),
            )
            # 新池：USD → 池币 换算
            if market:
                pool_id = self._pool_for(market)
                cur = await conn.execute(
                    "SELECT currency FROM auto_trade_pool WHERE pool_id=?", (pool_id,)
                )
                row = await cur.fetchone()
                if row:
                    pool_ccy = (row["currency"] or "USD").upper()
                    if pool_ccy == "USD":
                        delta_pool = delta_usd
                    else:
                        try:
                            pool_to_usd = await get_rate(self.db, pool_ccy)
                            delta_pool = delta_usd / pool_to_usd if pool_to_usd > 0 else delta_usd
                        except Exception as e:
                            logger.warning(f"[_update_cash] 池币换算失败 {pool_ccy}: {e}，按 USD 加（cash 可能小幅偏差）")
                            delta_pool = delta_usd
                    await conn.execute(
                        "UPDATE auto_trade_pool SET cash = cash + ?, updated_at = ? WHERE pool_id = ?",
                        (delta_pool, now, pool_id)
                    )
            await conn.commit()

    @staticmethod
    def _format_pnl_tag(pnl: Dict[str, float]) -> str:
        """生成供 reason 字段使用的整单累计盈亏标签（中文）。"""
        p_usd = pnl.get("pnl_usd") or 0.0
        p_pct = (pnl.get("pnl_pct") or 0.0) * 100
        legs = pnl.get("leg_count") or 1
        sign = "+" if p_usd >= 0 else ""
        emoji = "📈" if p_usd >= 0 else "📉"
        return (
            f"{emoji} 整单累计盈亏 {sign}${p_usd:.2f} ({sign}{p_pct:.2f}%), "
            f"共 {legs} 笔操作"
        )

    async def _calc_position_pnl(
        self, position_id: str, side: str,
        incoming_close_usd: float = 0.0, incoming_close_qty: float = 0.0,
    ) -> Dict[str, float]:
        """
        计算一个 position_id 从开仓到本次平/清仓的累计盈亏（含中间 add / reduce）。

        规则（long）：
          总买入 = Σ open/add.amount_usd   （出 USD）
          总卖出 = Σ reduce.amount_usd + 本次 close.amount_usd   （回 USD）
          pnl_usd = 总卖出 − 总买入
          pnl_pct = pnl_usd / 总买入

        规则（short）：
          open / add 是"建空"：不出真金，只是冻结 avg_cost × qty 的名义本金
          reduce / close 的 amount_usd 里已经包含了 (保证金 + 盈亏)
          所以 short 的真实盈亏 = Σ(reduce+close.amount_usd) − Σ(open/add.amount_usd_名义)
          但我们只存了 amount_usd，这里用一致口径：总"回笼" − 总"占用名义"

        incoming_* 是"本次调用时还没写进 log 的那笔 close"的金额，
        调用方在 log_trade 前先算好再传进来，避免少算最后一笔。
        """
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                """SELECT action, amount_usd, quantity, traded_at
                   FROM auto_trade_log
                   WHERE position_id=? AND status='executed'
                   ORDER BY traded_at ASC""",
                (position_id,),
            )
            rows = [dict(r) for r in await cur.fetchall()]
        open_usd = sum(r["amount_usd"] or 0 for r in rows if r["action"] in ("open", "add"))
        exit_usd = sum(r["amount_usd"] or 0 for r in rows if r["action"] in ("reduce", "close"))
        open_qty = sum(r["quantity"] or 0 for r in rows if r["action"] in ("open", "add"))
        exit_qty = sum(r["quantity"] or 0 for r in rows if r["action"] in ("reduce", "close"))
        exit_usd += incoming_close_usd
        exit_qty += incoming_close_qty
        if side == "long":
            pnl_usd = exit_usd - open_usd
            basis = open_usd
        else:
            pnl_usd = exit_usd - open_usd
            basis = open_usd
        pnl_pct = (pnl_usd / basis) if basis > 0 else 0.0
        opened_at = rows[0]["traded_at"] if rows else None
        return {
            "open_usd": open_usd,
            "exit_usd": exit_usd,
            "open_qty": open_qty,
            "exit_qty": exit_qty,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "opened_at": opened_at,
            "leg_count": len(rows) + (1 if incoming_close_usd else 0),
        }

    # ───────── 风控检查 ─────────

    async def _check_cooldown(self, symbol: str, market: str) -> bool:
        """同股冷却检查。True = 可下单；False = 冷却中。只计 executed 记录。
        v12.14: 按市场分档，美/港股 1h（避免 30min 内反复进出同一标的）"""
        per_market = self._config.get("cooldown_sec_per_market") or {}
        sec = int(per_market.get(market, self._config.get("cooldown_sec", 900)))
        cutoff = int(time.time()) - sec
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT MAX(traded_at) AS last FROM auto_trade_log "
                "WHERE symbol=? AND market=? AND status='executed'",
                (symbol, market),
            )
            row = await cur.fetchone()
        return not row or not row["last"] or row["last"] < cutoff

    async def _check_daily_limit(self, symbol: str, market: str) -> bool:
        """单日单股操作上限。只计 executed 记录，rejected 不占额度。
        v12.14 (A4 修复): 按市场时区切日，避免美股一晚跨 BJ 自然日导致计数被误重置。
          - 美股：美东时区 (EDT UTC-4 / EST UTC-5)
          - A 股/港股：北京时区 (UTC+8)
          - 加密：UTC（24/7 用 UTC 自然日已足够）
        """
        from datetime import datetime, timezone, timedelta
        now_ts = time.time()
        if market == "us":
            # 简化处理 — 美股 EDT (4-11月) UTC-4 / EST (11-3月) UTC-5；用 -4 当兜底（夏令时多）
            tz_offset = timedelta(hours=-4)
        elif market in ("cn", "hk"):
            tz_offset = timedelta(hours=8)
        else:
            tz_offset = timedelta(hours=0)
        local_now = datetime.fromtimestamp(now_ts, timezone(tz_offset))
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start = int(local_midnight.timestamp())
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM auto_trade_log "
                "WHERE symbol=? AND market=? AND traded_at>=? AND status='executed'",
                (symbol, market, day_start),
            )
            n = (await cur.fetchone())["n"]
        return n < self._config["max_daily_ops_per_symbol"]

    async def _get_pool_current_equity(self, pool_id: str) -> Tuple[float, str]:
        """v12.13: 池子当前总权益（cash + 持仓 mark-to-market），返回 (equity_local, currency)。
        pool_used_usd 来自 _market_used_usd（含浮盈），cash 来自 auto_trade_pool 表。"""
        cfg = next((p for p in self.POOLS if p["pool_id"] == pool_id), None)
        if not cfg:
            return 0.0, "USD"
        pool_ccy = cfg["currency"]
        # cash（池本币）
        pool = await self.get_pool(pool_id)
        cash_local = float((pool or {}).get("cash") or 0)
        # 该池辖下所有市场的持仓 mark-to-market（USD）
        positions_usd = 0.0
        for m in cfg["markets"]:
            try:
                positions_usd += await self._market_used_usd(m)
            except Exception:
                pass
        if pool_ccy == "USD":
            return cash_local + positions_usd, pool_ccy
        # 折算 USD → 池币
        try:
            pool_to_usd = await get_rate(self.db, pool_ccy)
            if pool_to_usd > 0:
                return cash_local + (positions_usd / pool_to_usd), pool_ccy
        except Exception:
            pass
        from backend.trading.fx import FALLBACK_RATES
        rate = FALLBACK_RATES.get(pool_ccy, 1.0)
        return cash_local + (positions_usd / rate if rate > 0 else positions_usd), pool_ccy

    async def _get_or_init_pool_day_start(self, pool_id: str) -> float:
        """v12.13: 每池独立日初快照（替代旧的全局 circuit_day_start）。
        config key='pool_day_start_<pool_id>'，UTC+8 自然日刷新。"""
        from datetime import datetime, timezone, timedelta
        cn_tz = timezone(timedelta(hours=8))
        today_str = datetime.now(cn_tz).strftime("%Y-%m-%d")
        cfg_key = f"pool_day_start_{pool_id}"
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute("SELECT value FROM config WHERE key=?", (cfg_key,))
                row = await cur.fetchone()
                if row and row[0]:
                    snap = json.loads(row[0])
                    if snap.get("date") == today_str:
                        return float(snap.get("equity", 0))
                equity, _ = await self._get_pool_current_equity(pool_id)
                await conn.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                    (cfg_key, json.dumps({"date": today_str, "equity": equity})),
                )
                await conn.commit()
                logger.info(f"[cooldown] {pool_id} 建立 {today_str} 日初快照: equity={equity:.2f}")
                return equity
        except Exception as e:
            logger.debug(f"[cooldown] {pool_id} 日初快照查询失败: {e}")
            return 0.0

    async def _check_pool_cooldown(self, market: str) -> Tuple[bool, str]:
        """v12.13 池级冷静期（替代旧全局熔断）。

        机制：
          - 每池独立日初快照 + 独立阈值（crypto -5% / us_hk -4% / cn -3%）
          - 池权益较日初跌超阈值 → 该池进入冷静期 4h
          - 冷静期内：拒绝该池所有市场的 open / add（reduce / close 不受限）
          - 4h 自动到期 + UTC+8 自然日 00:00 也会刷新基准

        返回：(allow_open, reject_reason)。allow_open=False 时 reject_reason 是给 _log_rejected 的人类可读理由。
        """
        pool_id = self._pool_for(market)
        cd_key = f"pool_cooldown_{pool_id}"
        now_ts = int(time.time())

        # 1) 是否在冷静期内
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute("SELECT value FROM config WHERE key=?", (cd_key,))
                row = await cur.fetchone()
            if row and row[0]:
                cd = json.loads(row[0])
                expires_at = int(cd.get("expires_at") or 0)
                if expires_at and now_ts < expires_at:
                    remain_min = (expires_at - now_ts) // 60
                    return False, (
                        f"{pool_id} 池冷静期中，剩余 {remain_min} 分钟"
                        f"（触发：日内损失 {float(cd.get('loss_pct', 0))*100:.2f}%）"
                    )
        except Exception as e:
            logger.debug(f"[cooldown] {pool_id} 读冷静状态失败: {e}")

        # 2) 是否需要触发新冷静期
        threshold_map = {
            "crypto": self._config.get("market_cooldown_loss_pct_crypto", 0.05),
            "us_hk":  self._config.get("market_cooldown_loss_pct_us_hk",  0.04),
            "cn":     self._config.get("market_cooldown_loss_pct_cn",     0.03),
        }
        threshold = float(threshold_map.get(pool_id, 0.05))
        try:
            day_start = await self._get_or_init_pool_day_start(pool_id)
            if day_start <= 0:
                return True, ""
            current_equity, ccy = await self._get_pool_current_equity(pool_id)
            loss_pct = (day_start - current_equity) / day_start
            if loss_pct >= threshold:
                duration = int(self._config.get("market_cooldown_duration_sec", 4 * 3600))
                expires_at = now_ts + duration
                try:
                    async with self.db.acquire() as conn:
                        await conn.execute(
                            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                            (cd_key, json.dumps({
                                "triggered_at": now_ts,
                                "expires_at": expires_at,
                                "loss_pct": loss_pct,
                                "day_start": day_start,
                                "current_equity": current_equity,
                                "currency": ccy,
                            })),
                        )
                        await conn.commit()
                except Exception as e:
                    logger.debug(f"[cooldown] {pool_id} 写冷静状态失败: {e}")
                logger.warning(
                    f"[cooldown] {pool_id} 池触发 {duration//3600}h 冷静期 "
                    f"loss={loss_pct*100:.2f}% >= {threshold*100:.0f}% "
                    f"(日初 {day_start:.2f}{ccy} → 当前 {current_equity:.2f}{ccy})"
                )
                return False, (
                    f"{pool_id} 池亏损 {loss_pct*100:.2f}% ≥ {threshold*100:.0f}% "
                    f"触发冷静期 {duration//3600}h（出场不受限）"
                )
        except Exception as e:
            logger.debug(f"[cooldown] {pool_id} 检查异常: {e}")
        return True, ""

    async def _get_current_price(self, symbol: str, market: str) -> Optional[float]:
        """取当前价：从最近日线收盘。"""
        if market not in ("cn", "hk", "us", "crypto"):
            return None  # 白名单防 SQL 注入
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    f"SELECT close FROM [klines_{market}_1d] WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                    (symbol,),
                )
                row = await cur.fetchone()
            return float(row["close"]) if row else None
        except Exception:
            return None

    async def _get_fresh_price(self, symbol: str, market: str, max_age_min: int = 30) -> Optional[float]:
        """v12.14 (A2 修复): 开仓时取真正的"近实时"价格。
        优先级 15m → 1H → 1D，且要求最新一根 K 线在 max_age_min 分钟内。
        防止用 1H K 线 close 当成交价（GOOG 22:52 开仓时实际价 ~372 但记账成 366.20，44s 后触发 T1 假赢）。
        失败时返回 None；调用方应 fallback 到 sig['price']（开仓被驳回比错价开仓更稳）。
        """
        if market not in ("cn", "hk", "us", "crypto"):
            return None
        now_ms = int(time.time() * 1000)
        max_age_ms = max_age_min * 60 * 1000
        # 加密 24/7，可放宽到 5 min；股票市场最小粒度 15m，放宽到 30 min
        for interval in ("15m", "1H", "1D"):
            try:
                async with self.db.acquire() as conn:
                    cur = await conn.execute(
                        f"SELECT close, timestamp FROM [klines_{market}_{interval.lower()}] "
                        f"WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                        (symbol,),
                    )
                    row = await cur.fetchone()
                if row and row["timestamp"] and (now_ms - row["timestamp"]) <= max_age_ms:
                    return float(row["close"])
            except Exception:
                continue
        # 全部超期 → 返回 None（开仓应推迟而非用陈旧价）
        logger.info(f"[fresh-price] {symbol}({market}) 无 {max_age_min} 分钟内 K 线，跳过本次开仓")
        return None


    async def _position_usd_value(self, symbol: str, market: str) -> float:
        """某持仓当前市值（USD）。"""
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT quantity, cost_currency FROM positions WHERE symbol=? AND market=?",
                (symbol, market),
            )
            row = await cur.fetchone()
        if not row or not row["quantity"]:
            return 0.0
        price = await self._get_current_price(symbol, market)
        if not price:
            return 0.0
        currency = row["cost_currency"] or market_to_currency(market)
        return await to_usd(self.db, row["quantity"] * price, currency)

    async def _market_used_usd(self, market: str) -> float:
        """某市场当前已占用的 USD（所有 long 按市值；short 按保证金）。
        v12.11 修：fx 拉取失败时不再退化 1.0（HKD/CNY 直接当 USD 累加 → daily_loss_circuit 基准被严重高估，熔断永不触发）；
        改为 fallback 到 fx 模块的硬编码兜底（HKD=0.128 / CNY=0.14），永远不退化为 1.0。
        """
        from backend.trading.fx import FALLBACK_RATES
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT symbol, quantity, avg_cost, cost_currency, side FROM positions WHERE market=?",
                (market,),
            )
            rows = await cur.fetchall()
        total = 0.0
        for r in rows:
            if not r["quantity"]:
                continue
            ccy = (r["cost_currency"] or market_to_currency(market) or "USD").upper()
            try:
                fx = await get_rate(self.db, ccy)
                if fx <= 0:
                    raise ValueError(f"fx={fx}")
            except Exception as e:
                fx = FALLBACK_RATES.get(ccy, 1.0)
                if fx == 1.0 and ccy != "USD":
                    logger.error(f"[_market_used_usd] {ccy} 汇率不可用且无兜底，金额会被低估！symbol={r['symbol']}: {e}")
                else:
                    logger.warning(f"[_market_used_usd] {ccy} fx 实时拉取失败，用兜底 {fx}: {e}")
            side = r["side"] or "long"
            if side == "long":
                price = await self._get_current_price(r["symbol"], market) or (r["avg_cost"] or 0)
                total += r["quantity"] * price * fx
            else:
                total += r["quantity"] * (r["avg_cost"] or 0) * fx
        return total

    async def _pool_cap_state(self, pool_id: str) -> tuple[float, float, str]:
        """v12.13: 池级 cap 检查的真相源。
        返回 (该池所有持仓入场成本累加 [本币], 池初始资金 [本币], 池币代码)。
        与 _market_used_usd 区别：
          - 用 avg_cost 不用 current_price（cap 限制资金占用，浮盈不挤占新开额度）
          - 只算该池辖下市场（us_hk: us+hk / cn: cn / crypto: crypto），不跨池
          - 全部本币累加，无 USD 折算环节（cn 池就用 CNY 比，避免汇率噪声）
        """
        from backend.trading.fx import FALLBACK_RATES
        cfg = next((p for p in self.POOLS if p["pool_id"] == pool_id), None)
        if not cfg:
            return 0.0, 0.0, "USD"
        pool_ccy = cfg["currency"]
        markets = cfg["markets"]
        placeholders = ",".join("?" * len(markets))
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                f"SELECT symbol, market, quantity, avg_cost, cost_currency "
                f"FROM positions WHERE market IN ({placeholders})",
                markets,
            )
            rows = await cur.fetchall()
        used_pool_ccy = 0.0
        for r in rows:
            if not r["quantity"]:
                continue
            local_cost = float(r["quantity"]) * float(r["avg_cost"] or 0)
            local_ccy = (r["cost_currency"] or market_to_currency(r["market"]) or "USD").upper()
            if local_ccy == pool_ccy:
                used_pool_ccy += local_cost
                continue
            # 跨币种折算（如 HK 持仓 HKD → us_hk USD 池）
            try:
                local_to_usd = await get_rate(self.db, local_ccy)
                pool_to_usd = await get_rate(self.db, pool_ccy) if pool_ccy != "USD" else 1.0
                if local_to_usd > 0 and pool_to_usd > 0:
                    used_pool_ccy += local_cost * local_to_usd / pool_to_usd
                    continue
            except Exception as e:
                logger.warning(f"[_pool_cap_state] {local_ccy}->{pool_ccy} fx 失败用兜底: {e}")
            l = FALLBACK_RATES.get(local_ccy, 1.0)
            p = FALLBACK_RATES.get(pool_ccy, 1.0) if pool_ccy != "USD" else 1.0
            used_pool_ccy += local_cost * l / p
        return used_pool_ccy, float(cfg["initial"]), pool_ccy

    # ───────── 决策入口 ─────────

    async def on_signal_verified(self, signal_id: str):
        """信号 AI 验证完成 → 评估是否开仓/加仓。"""
        if not self.enabled:
            return
        try:
            async with self._lock:
                await self._handle_signal(signal_id)
        except Exception as e:
            # v12.13: 顶层兜底 — _handle_signal 内 DB locked / 其他异常不再静默
            # 让用户在 ERROR 日志里看到为什么信号丢失（之前只能从 [auto-reject] 缺失推断）
            logger.error(
                f"[on_signal_verified] {signal_id[:8]} 异常处理 confirm 信号: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )

    async def on_diagnosis_updated(self, symbol: str, market: str, rating: str):
        """AI 诊断更新 → 评估减仓/清仓。"""
        if not self.enabled:
            return
        try:
            async with self._lock:
                await self._handle_diagnosis_change(symbol, market, rating)
        except Exception as e:
            logger.error(
                f"[on_diagnosis_updated] {symbol}({market}) rating={rating} 异常: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )

    # ───────── 信号驱动的开仓/加仓 ─────────

    async def _handle_signal(self, signal_id: str):
        """
        信号 + AI confirm 驱动的开/加仓逻辑：
          股票 (us/hk/cn)：
            - BUY 信号 + rating=buy/strong_buy → 开多/加多
            - SELL 信号 → 忽略（股票单向，只做多；平仓由诊断驱动）
          加密 (crypto) - 双向：
            - BUY 信号 + rating=buy/strong_buy → 开多/加多
            - SELL 信号 + rating=sell → 开空/加空
            - 若已有反向仓位 → 忽略（避免同时持多空）；等诊断驱动清仓
        """
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,))
            row = await cur.fetchone()
        if not row:
            return
        sig = dict(row)
        action = sig["action"]  # buy / sell
        symbol, market = sig["symbol"], sig["market"]

        # v12.13: 通用前置 — confirm + AI 置信度 ≥ 60
        if sig["ai_verdict"] != "confirm":
            return
        if (sig.get("ai_confidence") or 0) < 60:
            return

        # v12.14 (A8 修复): 信号风暴去重 — 30s 内同 symbol+market+action 只处理第一个
        # 之前 AVGO 开仓后同瞬间 4 个 add 信号涌来，靠 cooldown 兜底
        # 现在直接在入口去重，节省下游处理开销
        if not hasattr(self, "_signal_dedup_cache"):
            self._signal_dedup_cache: Dict[str, float] = {}
        dedup_key = f"{symbol}|{market}|{action}"
        last_handled = self._signal_dedup_cache.get(dedup_key, 0)
        now = time.time()
        if now - last_handled < 30:
            logger.debug(f"[signal-storm-dedup] {symbol}/{action} 30s 内已处理过，跳过 {signal_id[:8]}")
            return
        self._signal_dedup_cache[dedup_key] = now
        # 简单清理：cache > 200 项时清掉 1h 前的
        if len(self._signal_dedup_cache) > 200:
            cutoff = now - 3600
            self._signal_dedup_cache = {k: v for k, v in self._signal_dedup_cache.items() if v > cutoff}

        # v12.13: 股票 SELL 信号特殊处理（之前是 silent return）
        # 持仓中收到 SELL confirm → 触发减仓/平仓判断（不再等 AI 诊断变 reduce/sell，避免错过出场点）
        # 强 confirm（≥75）→ 平仓；中等 confirm（60-74）→ 减仓 50%
        if market != "crypto" and action == "sell":
            await self._handle_stock_sell_confirm(sig)
            return
        # 风控：同股冷却 / 单日同股操作上限
        if not await self._check_cooldown(symbol, market):
            await self._log_rejected(
                sig, "open",
                f"同股冷却期内（{self._config['cooldown_sec']}s），跳过本次信号"
            )
            return
        if not await self._check_daily_limit(symbol, market):
            await self._log_rejected(
                sig, "open",
                f"本品种当日操作已达上限 {self._config['max_daily_ops_per_symbol']} 次"
            )
            return

        # 决定目标 side：股票都是 long；加密 buy→long, sell→short
        target_side = "short" if (market == "crypto" and action == "sell") else "long"
        # 匹配的 rating 要求
        required_ratings = {"sell"} if target_side == "short" else {"buy", "strong_buy"}

        # 拉诊断
        rating = None
        pool_exists = True
        if market != "crypto":
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT ai_diagnosis FROM watch_pool WHERE symbol=? AND market=? AND status!='archived'",
                    (symbol, market),
                )
                row = await cur.fetchone()
            if not row:
                pool_exists = False
            elif row["ai_diagnosis"]:
                try:
                    rating = json.loads(row["ai_diagnosis"]).get("rating")
                except Exception:
                    rating = None
        else:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT rating FROM crypto_diagnosis WHERE symbol=?", (symbol,)
                )
                row = await cur.fetchone()
            rating = row["rating"] if row else None

        # 关键：信号已经过 verify（fast/simplified/full）确认 ai_verdict='confirm'，
        # 此处不再以 rating 二次否决，避免 verify 后 rating 微变导致决策不一致。
        # rating 仅用于决定 sizing（strong_buy 用 3%，buy/None 用 2%）和方向校验。
        ai_reason = sig.get("ai_reason") or ""
        is_fast_path = ai_reason.startswith("[强买直通]") or ai_reason.startswith("[买入·无利空]")

        # 诊断缺失且非 fast-path 信号：触发后台诊断 + 拒绝（保留谨慎）
        # fast-path 已经隐含 rating 验证，不需要再做这道门
        if rating is None and not is_fast_path:
            try:
                from backend.news.scheduler import _ai_analyzer
                if _ai_analyzer is not None:
                    if market == "crypto":
                        self._spawn_bg(_ai_analyzer.diagnose_crypto(symbol, force=True))
                    elif pool_exists:
                        self._spawn_bg(_ai_analyzer.diagnose_stock(symbol, market, force=True))
            except Exception:
                pass
            await self._log_rejected(
                sig, "open",
                f"诊断缺失{'(未入池)' if not pool_exists else '(诊断未完成)'}，已触发按需诊断，等下一个信号"
            )
            return

        # rating 与 target_side 强冲突（多空相反）才拒绝；hold/None 等中性档位通过
        if target_side == "long" and rating in ("sell", "reduce"):
            await self._log_rejected(
                sig, "open",
                f"AI诊断评级={rating}（看空），与买入信号方向冲突，拒绝"
            )
            return
        if target_side == "short" and rating in ("buy", "strong_buy"):
            await self._log_rejected(
                sig, "open",
                f"AI诊断评级={rating}（看多），与卖出信号方向冲突，拒绝"
            )
            return

        # rating None 时用 "buy" 作为 sizing 的兜底（仅 fast-path 路径才会到这）
        if rating is None:
            rating = "buy"

        # 查当前持仓
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT id, quantity, avg_cost, side FROM positions WHERE symbol=? AND market=?",
                (symbol, market),
            )
            pos = await cur.fetchone()

        # 残留持仓识别：持仓量 < 1 手（浮点残渣或数据遗留）→ 清掉，走开新仓路径
        # 否则会陷入"持仓几乎为 0 但加仓门槛永远不满足"的死锁
        if pos:
            min_lot_here = self._min_lot(market, symbol)
            if (pos["quantity"] or 0) < min_lot_here:
                try:
                    async with self.db.acquire() as conn:
                        await conn.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
                        # v11.5: 残留清理也要 cascade 删 state 行
                        await conn.execute("DELETE FROM position_state WHERE position_id=?", (pos["id"],))
                        await conn.commit()
                    logger.info(f"[cleanup] {symbol}({market}) 残留 {pos['quantity']} < 1 手({min_lot_here})，清掉准备重新开仓")
                except Exception:
                    pass
                pos = None  # 视同无持仓

        # v12.18.1 修复: 必须先检查市场是否可下单（盘前/集合竞价/闭市 → pending 等开市重试）
        # 否则美股盘前 K 线稀疏 → fresh_price 拿不到 → 错以"实时价不可用"拒单
        # 而 retry loop 只认 'pending' 关键词的拒因，导致这些盘前信号永久丢失
        from backend.signals.monitor import is_market_executable as _ime
        if not _ime(market):
            await self._log_rejected(
                sig, "open" if not pos else "add",
                "未到连续竞价时段，等待市场开盘后重试（pending）"
            )
            return

        # v12.14 (A2 修复): 开仓/加仓必须用近实时价（15m K 线），不能用 sig.price（来自上一根 1H 收盘价）
        # 让 fresh price 决定真实成交价；信号价用作"必须 ≤ sig.price + 1.5%"的滑点保护
        sig_price = float(sig["price"])
        fresh_price = await self._get_fresh_price(symbol, market, max_age_min=30)
        if fresh_price is None:
            await self._log_rejected(
                sig, "open" if not pos else "add",
                f"实时价不可用（30 min 内无 K 线），跳过本次开仓避免错价"
            )
            return
        # 滑点保护：实时价较 sig 价偏离 > 1.5% 拒绝（信号已经不新鲜，市场已跑动）
        slip_pct = abs(fresh_price - sig_price) / sig_price * 100 if sig_price > 0 else 0
        if slip_pct > 1.5:
            await self._log_rejected(
                sig, "open" if not pos else "add",
                f"信号价 {sig_price:.4f} vs 实时价 {fresh_price:.4f} 滑点 {slip_pct:.2f}%>1.5%，已不新鲜"
            )
            return
        price = fresh_price
        if pos:
            pos_dict = dict(pos)
            existing_side = pos_dict.get("side") or "long"
            if existing_side != target_side:
                # 反向信号处理（缠论特别场景）：
                #   - 强等级反向信号 (ai_confidence ≥ 75) → 平仓（不反手，让 auto_trader 下轮重评）
                #   - 弱等级反向信号 → 减仓 50%
                #   - 非缠论策略来的反向信号 → 保持旧行为（等诊断清仓）
                strategy = sig.get("strategy_name") or ""
                ai_conf = int(sig.get("ai_confidence") or 0)
                if strategy == "chanlun":
                    if ai_conf >= 75:
                        # 强反向 → 平仓
                        await self._execute_close(
                            pos_dict, symbol, market,
                            "signal_reverse",
                            f"缠论反向强信号 (conf={ai_conf})，平 {existing_side}"
                        )
                    else:
                        # 弱反向 → 减 50%
                        await self._execute_reduce(
                            pos_dict, symbol, market,
                            self._config["reduce_ratios"]["rating_hold"],
                            "signal_reverse",
                            f"缠论反向弱信号 (conf={ai_conf})，减 {existing_side} 50%"
                        )
                    return
                # 非缠论反向：高置信度直接清反向（不再僵尸态等诊断驱动）
                # 阈值 80 比缠论 75 严一档，避免噪音策略反复清仓
                if ai_conf >= 80:
                    logger.info(f"[reverse-close] {symbol} 非缠论高置信反向信号 (strategy={strategy}, conf={ai_conf})，平 {existing_side}")
                    await self._execute_close(
                        pos_dict, symbol, market,
                        "signal_reverse",
                        f"{strategy} 反向高置信信号 (conf={ai_conf})，平 {existing_side} 仓位 sell"
                    )
                    return
                await self._log_rejected(
                    sig, "open",
                    f"已持 {existing_side} 仓位，反向 {strategy} 信号置信度 {ai_conf}<80，等 AI 诊断驱动清反向"
                )
                return
            # 同向加仓
            await self._try_add_position(sig, pos_dict, rating, price, target_side)
        else:
            await self._try_open_position(sig, rating, price, target_side)

    async def _handle_stock_sell_confirm(self, sig: Dict):
        """v12.13: 持仓股票收到 SELL confirm → 触发减仓/平仓。
        之前 _handle_signal 直接 silent return，导致 SELL 信号被丢弃，
        减/平仓只能等 AI 诊断变 reduce/sell（慢，可能错过出场）。
        本路径：
          - 无持仓：silent return（信号 monitor 已 mark skipped）
          - 持仓 + ai_conf ≥ 75：平仓
          - 持仓 + ai_conf 60-74：减 50%
          - 已持空仓：忽略（股票只做多，不应该出现）
        """
        symbol, market = sig["symbol"], sig["market"]
        ai_conf = int(sig.get("ai_confidence") or 0)
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT * FROM positions WHERE symbol=? AND market=? AND quantity > 0",
                (symbol, market),
            )
            pos = await cur.fetchone()
        if not pos:
            return  # 无持仓 SELL — 信号 monitor 已 mark skipped 不会到这；防御性 return
        pos_dict = dict(pos)
        side = pos_dict.get("side") or "long"
        if side != "long":
            logger.warning(f"[stock-sell] {symbol}({market}) 检测到非 long 持仓 (side={side})，跳过")
            return
        # 同股冷却 / 单日操作上限（与 BUY 路径一致的风控）
        if not await self._check_cooldown(symbol, market):
            await self._log_rejected(sig, "reduce" if ai_conf < 75 else "close",
                f"同股冷却期内（{self._config['cooldown_sec']}s），跳过 SELL 信号")
            return
        if not await self._check_daily_limit(symbol, market):
            await self._log_rejected(sig, "reduce" if ai_conf < 75 else "close",
                f"本品种当日操作已达上限 {self._config['max_daily_ops_per_symbol']} 次")
            return
        if ai_conf >= 75:
            await self._execute_close(
                pos_dict, symbol, market,
                "signal_sell",
                f"SELL 强 confirm (conf={ai_conf}) 触发平仓"
            )
        else:
            ratio = self._config["reduce_ratios"]["rating_hold"]
            await self._execute_reduce(
                pos_dict, symbol, market, ratio,
                "signal_sell",
                f"SELL confirm (conf={ai_conf}) 触发减仓 {int(ratio*100)}%"
            )

    async def _compute_size_modifier(self, sig: Dict) -> Tuple[float, str]:
        """v12.13 智能 sizing 调整因子；v12.16 加共振 sizing_boost 直读。

        基于：
          - ai_confidence 分档：65-69 →×0.8 / 70-79 →×1.0 / 80-89 →×1.2 / 90+ →×1.4
          - v12.16 共振 super signal 优先：triggered_by.sizing_boost (Level 1=1.0/2=1.2/3=1.5)
          - 旧版多策略共振查询：30min 内同 symbol+action confirm 数 ≥2 →×1.15 / 0 (孤狼) →×0.9
        返回 (multiplier, reason_text)。multiplier 限制 [0.5, 1.5]。
        """
        ai_conf = int(sig.get("ai_confidence") or 0)
        symbol, market, action = sig["symbol"], sig["market"], sig["action"]

        # 1) ai_confidence 分档
        if ai_conf >= 90:
            conf_mult, conf_label = 1.4, f"AI={ai_conf}顶级×1.4"
        elif ai_conf >= 80:
            conf_mult, conf_label = 1.2, f"AI={ai_conf}强×1.2"
        elif ai_conf >= 70:
            conf_mult, conf_label = 1.0, f"AI={ai_conf}基线×1.0"
        else:  # 65-69 勉强 confirm
            conf_mult, conf_label = 0.8, f"AI={ai_conf}勉强×0.8"

        # v12.16 (Step 1) 共振 super signal 优先：直接读 triggered_by.sizing_boost
        # 比"30min 内同 symbol+action confirm 数"更精确（共振合并发生在同一 K 线内）
        cons_mult, cons_label = 1.0, ""
        boost_from_resonance = None
        try:
            tb = sig.get("triggered_by")
            if isinstance(tb, str):
                tb = json.loads(tb)
            if isinstance(tb, dict):
                boost_from_resonance = tb.get("sizing_boost")
                if boost_from_resonance:
                    rl = tb.get("resonance_level", "?")
                    n_strats = len(tb.get("strategies", []))
                    cons_mult = float(boost_from_resonance)
                    cons_label = f"共振 L{rl}({n_strats}策略)×{cons_mult:.2f}"
        except Exception as e:
            logger.debug(f"[sizing] {symbol} 共振 boost 解析失败: {e}")

        # 2) 旧版查询作 fallback — 仅在没有 resonance boost 时用
        if boost_from_resonance is None:
            try:
                cutoff_ms = int(time.time() * 1000) - 30 * 60 * 1000
                async with self.db.acquire() as conn:
                    cur = await conn.execute(
                        """SELECT COUNT(*) FROM signals
                           WHERE symbol=? AND market=? AND action=?
                             AND ai_verdict='confirm'
                             AND id != ? AND generated_at >= ?""",
                        (symbol, market, action, sig.get("id", ""), cutoff_ms),
                    )
                    row = await cur.fetchone()
                n_confirm = int(row[0] or 0) if row else 0
                if n_confirm >= 2:
                    cons_mult, cons_label = 1.15, f"共振×{n_confirm+1}信号→×1.15"
                elif n_confirm == 0:
                    cons_mult, cons_label = 0.9, "孤狼信号→×0.9"
            except Exception as e:
                logger.debug(f"[sizing] {symbol} 共振查询失败: {e}")

        mult = conf_mult * cons_mult
        mult = max(0.5, min(1.5, mult))
        labels = [conf_label]
        if cons_label:
            labels.append(cons_label)
        return mult, " + ".join(labels)

    async def _try_open_position(self, sig: Dict, rating: str, price: float, side: str = "long"):
        symbol, market = sig["symbol"], sig["market"]
        cfg = self._config

        # 市场连续竞价时段检查（集合竞价 / 盘前盘后 / 闭市 → 不下单，标 pending 等开市重试）
        from backend.signals.monitor import is_market_executable
        if not is_market_executable(market):
            await self._log_rejected(sig, "open", "未到连续竞价时段，等待市场开盘后重试（pending）")
            return

        # v12.13: 池级冷静期（旧的全局日亏熔断已废弃）— 各池独立判断+独立阈值
        ok, why = await self._check_pool_cooldown(market)
        if not ok:
            await self._log_rejected(sig, "open", why)
            return

        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT COUNT(*) AS n FROM positions")
            cur_positions = (await cur.fetchone())["n"]
        if cur_positions >= cfg["max_concurrent_positions"]:
            await self._log_rejected(sig, "open", "并发持仓超上限")
            return

        # 仓位比例：v11.3 按市场分档取值（股票更激进，加密保守）
        if side == "short":
            pct = self._market_size(market, "buy")  # 空仓用普通 buy（保守）
        else:
            pct = self._market_size(market, "strong_buy") if rating == "strong_buy" else self._market_size(market, "buy")

        # v12.13: AI 没给 ai_stop_loss → 降级仓位 50%（控单笔风险敞口）
        # 复盘 2026-04-29 COIN 事故：AI verify 没给 SL → 用 -8% 兜底 → 一刀切 -$477
        # 没 SL 意味着 LLM 没识别出技术性止损位 → 信号质量打折，降仓
        ai_sl_provided = sig.get("ai_stop_loss")
        try:
            ai_sl_provided = float(ai_sl_provided) if ai_sl_provided is not None else None
        except (TypeError, ValueError):
            ai_sl_provided = None
        if ai_sl_provided is None or ai_sl_provided <= 0:
            pct = pct * 0.5
            logger.info(f"[auto-open] {symbol}({market}) AI 未给 SL → 降级仓位 50% (pct={pct:.3f})")

        # v12.13: 智能 sizing 调整（ai_confidence 分档 + 共振叠加，总系数 [0.5, 1.5]）
        size_mult, size_reason = await self._compute_size_modifier(sig)
        pct = pct * size_mult
        if abs(size_mult - 1.0) > 0.01:
            logger.info(f"[auto-open] {symbol}({market}) sizing 调整 ×{size_mult:.2f} ({size_reason}) → pct={pct:.3f}")

        account = await self.get_account()
        initial = account.get("initial_capital_usd", 10000)
        order_usd = initial * pct
        if order_usd < cfg["min_order_usd"]:
            return

        # 单股上限检查（v12.13 双层）
        # soft_cap = 12% 是默认 sizing 目标，超过 soft_cap 必须降到 soft_cap
        # hard_cap = 30% 是 1 手升配的天花板（高价 A 股/港股 1 手价超 soft_cap 但 ≤ hard_cap → 升配 1 手）
        soft_cap_usd = initial * self._market_size(market, "max_single")
        hard_cap_usd = initial * cfg.get("hard_single_cap_pct", 0.30)
        if order_usd > soft_cap_usd:
            order_usd = soft_cap_usd
            logger.info(f"[auto-open] {symbol}({market}) order ${order_usd:.0f} 触 soft cap，缩到上限")
        # 注：hard_cap 仅在下面 1 手升配路径检查

        # ─ 池级仓位上限检查（v12.13：三池独立核算，不再全局加和）─
        # us/hk → us_hk 池 ($100K USD) / cn → cn 池 (¥300K CNY) / crypto → crypto 池 ($100K USD)
        # 已用 = 该池所有持仓的入场成本（本币累加），不包括浮盈
        pool_id = self._pool_for(market)
        pool_used_local, pool_initial_local, pool_ccy = await self._pool_cap_state(pool_id)
        # 把本单 USD 换成池币
        if pool_ccy == "USD":
            order_pool_local = order_usd
        else:
            try:
                pool_to_usd = await get_rate(self.db, pool_ccy)
                order_pool_local = order_usd / pool_to_usd if pool_to_usd > 0 else order_usd
            except Exception:
                from backend.trading.fx import FALLBACK_RATES
                rate = FALLBACK_RATES.get(pool_ccy, 1.0)
                order_pool_local = order_usd / rate if rate > 0 else order_usd
        cap_pct = cfg.get("total_position_cap_pct", 0.80)
        pool_cap_local = pool_initial_local * cap_pct
        if pool_used_local + order_pool_local > pool_cap_local:
            await self._log_rejected(
                sig, "open",
                f"{pool_id} 池仓位超上限（已用 {pool_used_local:.0f} {pool_ccy} + 本单 {order_pool_local:.0f} > 上限 {pool_cap_local:.0f} {pool_ccy}, 占比 {cap_pct*100:.0f}%）"
            )
            return

        # v12.11: 移除全局 cash_buffer 检查（破坏 3 池隔离）— 完全交给池级缓冲
        # 旧逻辑把 CN 池现金折成 USD 计入 account.cash_usd，让 us_hk 看似有更多现金
        ok, why = await self._check_pool_cash_buffer(market, order_usd)
        if not ok:
            await self._log_rejected(sig, "open", why)
            return

        currency = market_to_currency(market)
        try:
            fx = await get_rate(self.db, currency)
        except Exception as e:
            await self._log_rejected(sig, "open", f"汇率获取失败: {e}")
            return
        if fx <= 0:
            return
        local_amount = order_usd / fx
        qty = local_amount / price
        if qty <= 0:
            return
        # 按市场最小手数规整
        qty = self._normalize_qty(market, symbol, qty)
        if qty <= 0:
            # 首仓不够 1 手 → 检查"按 1 手价开仓"是否在所有风控阈值内
            # 例：0700.HK 1 手 $6643，order_usd $4000 不够，但 single_cap $8000 能买 → 升到 1 手
            LOT = {"cn": 100, "hk": 100, "us": 1}
            lot_size = LOT.get(market, 1)
            one_lot_usd = lot_size * price * fx

            # v12.13 双层 cap：1 手价超 hard_cap (30%) 才拒；soft_cap (12%) 与 hard_cap 之间允许升配
            if one_lot_usd > hard_cap_usd:
                await self._log_rejected(sig, "open",
                    f"1 手 ${one_lot_usd:.2f} 超硬上限 ${hard_cap_usd:.2f}（{cfg.get('hard_single_cap_pct',0.30)*100:.0f}%）, 无法建仓")
                return
            if one_lot_usd > soft_cap_usd:
                logger.info(f"[auto-open] {symbol}({market}) 高价股升配：1 手 ${one_lot_usd:.0f} > 软 cap ${soft_cap_usd:.0f}, 但 ≤ 硬 cap ${hard_cap_usd:.0f}, 允许")
            # v12.11: 升配后只查池级缓冲（已移除全局 cash 缓冲）
            ok, why = await self._check_pool_cash_buffer(market, one_lot_usd)
            if not ok:
                await self._log_rejected(sig, "open", f"1 手升配后 {why}")
                return
            # v12.13: 1 手升配后再查池级 cap（与上面同一池）
            if pool_ccy == "USD":
                one_lot_pool_local = one_lot_usd
            else:
                try:
                    pool_to_usd2 = await get_rate(self.db, pool_ccy)
                    one_lot_pool_local = one_lot_usd / pool_to_usd2 if pool_to_usd2 > 0 else one_lot_usd
                except Exception:
                    from backend.trading.fx import FALLBACK_RATES
                    rate2 = FALLBACK_RATES.get(pool_ccy, 1.0)
                    one_lot_pool_local = one_lot_usd / rate2 if rate2 > 0 else one_lot_usd
            pool_used2, pool_init2, _ = await self._pool_cap_state(pool_id)
            pool_cap2 = pool_init2 * cap_pct
            if pool_used2 + one_lot_pool_local > pool_cap2:
                await self._log_rejected(sig, "open",
                    f"1 手 {one_lot_pool_local:.0f} {pool_ccy} 超 {pool_id} 池上限（已用 {pool_used2:.0f} / 上限 {pool_cap2:.0f} {pool_ccy}）")
                return
            # 升配
            qty = float(lot_size)
            order_usd = one_lot_usd
            logger.info(f"[open-upsize] {symbol}({market}) 默认 {cfg['open_position_pct_buy']*100:.0f}% 不足 1 手，升配到 1 手 ${one_lot_usd:.2f}")
        else:
            # 重算实际 USD 成本（按规整后的 qty）
            order_usd = qty * price * fx
        await self._execute_open(symbol, market, qty, price, fx, currency, order_usd, sig, rating, side)

    async def _try_add_position(self, sig: Dict, pos: Dict, rating: str, price: float, side: str = "long"):
        symbol, market = sig["symbol"], sig["market"]
        cfg = self._config

        # 加仓也必须在连续竞价时段
        from backend.signals.monitor import is_market_executable
        if not is_market_executable(market):
            await self._log_rejected(sig, "add", "未到连续竞价时段，加仓延后（pending）")
            return

        avg_cost = pos.get("avg_cost") or 0
        if avg_cost <= 0:
            await self._log_rejected(sig, "add", "加仓失败：持仓均价缺失")
            return
        # PnL 按方向算: 多仓 = (price-avg)/avg, 空仓 = (avg-price)/avg
        pnl_pct = (price - avg_cost) / avg_cost if side == "long" else (avg_cost - price) / avg_cost
        # ─ 动态加仓门槛：根据该 position 的减仓次数降低 PnL 要求 ─
        # 设计：被减仓越多，说明系统已经"撤过部分仓"，此时新 buy 信号属于"二次入场"，门槛应该降低
        # 0 次减仓: 5%（默认，保护盈利仓位别乱加）
        # 1-2 次:   3%（已开始撤仓，但还在主力仓位上）
        # ≥3 次:    1%（仓位已被切薄，新信号视为"重新建仓"，门槛极低）
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT COUNT(*) FROM auto_trade_log "
                    "WHERE position_id=? AND action='reduce' AND status='executed'",
                    (pos.get("id"),),
                )
                reduce_count = (await cur.fetchone())[0] or 0
        except Exception:
            reduce_count = 0
        if reduce_count >= 3:
            min_pnl_pct = 0.01    # 1%
        elif reduce_count >= 1:
            min_pnl_pct = 0.03    # 3%
        else:
            min_pnl_pct = 0.05    # 5%
        if pnl_pct < min_pnl_pct:
            await self._log_rejected(
                sig, "add",
                f"加仓需持仓浮盈≥{min_pnl_pct*100:.0f}%（已减仓 {reduce_count} 次），"
                f"当前 PnL={pnl_pct*100:.2f}%（{side} 均价{avg_cost:.4f} vs 现价{price:.4f}）"
            )
            return
        current_value_usd = await self._position_usd_value(symbol, market)
        account = await self.get_account()
        initial = account.get("initial_capital_usd", 10000)
        current_pct = current_value_usd / initial
        # v11.3: 单股上限按市场取（股票 12% / 加密 8%）
        market_max = self._market_size(market, "max_single")
        if current_pct >= market_max:
            await self._log_rejected(
                sig, "add",
                f"加仓拒绝：单只仓位已达上限 {current_pct*100:.2f}% ≥ {market_max*100:.2f}%（{market}）"
            )
            return
        add_pct = self._market_size(market, "add")
        if current_pct + add_pct > market_max:
            add_pct = market_max - current_pct
        order_usd = initial * add_pct
        if order_usd < cfg["min_order_usd"]:
            await self._log_rejected(
                sig, "add",
                f"加仓金额 ${order_usd:.2f} < 最小下单额 ${cfg['min_order_usd']}"
            )
            return
        # v12.11: 移除全局 cash_usd 检查（破坏 3 池隔离）
        # 池级现金缓冲在规整后再查（防止"理论金额过、规整后实际更小却已通过"边界 bug）
        currency = market_to_currency(market)
        try:
            fx = await get_rate(self.db, currency)
        except Exception as e:
            await self._log_rejected(sig, "add", f"加仓汇率获取失败: {e}")
            return
        if fx <= 0:
            return
        qty = (order_usd / fx) / price
        # 按市场最小手数规整加仓数量
        qty = self._normalize_qty(market, symbol, qty)
        if qty <= 0:
            await self._log_rejected(sig, "add", f"加仓数量不足最小手数（{market}）")
            return
        # 重算实际下单金额（按规整后的 qty）
        actual_order_usd = qty * price * fx
        # 池级现金缓冲（按实际金额）
        ok, why = await self._check_pool_cash_buffer(market, actual_order_usd)
        if not ok:
            await self._log_rejected(sig, "add", why)
            return
        order_usd = actual_order_usd
        order_usd = qty * price * fx  # 重算实际成本
        await self._execute_add(symbol, market, qty, price, fx, currency, order_usd, sig, rating, side)

    # ───────── 诊断驱动的减仓/清仓 ─────────

    # ───────── v11.3: 僵尸持仓巡检（资金周转优化）─────────

    async def _zombie_position_loop(self):
        """
        每 1h 跑一次。识别"持仓 ≥ 14 天 + 浮盈 |%| < 5 + 7 天无新 advice"的死钱仓位 → 减 30% 释放预算。
        """
        await asyncio.sleep(120)  # 启动后 2 分钟开始（避开冷启动竞态）
        interval = self._config.get("zombie_scan_interval_sec", 3600)
        while True:
            try:
                if self.enabled:
                    n = await self._zombie_scan_once()
                    if n > 0:
                        logger.info(f"[zombie] 本轮触发 {n} 笔减仓，释放预算给新信号")
            except Exception as e:
                logger.warning(f"[zombie] 扫描异常: {e}")
            await asyncio.sleep(interval)

    async def _zombie_scan_once(self) -> int:
        cfg = self._config
        age_days = cfg.get("zombie_age_days", 14)
        pnl_band = cfg.get("zombie_pnl_band_pct", 5.0)
        no_adv_days = cfg.get("zombie_no_advice_days", 7)
        ratio = cfg.get("zombie_reduce_ratio", 0.30)
        now_ts = int(time.time())
        cutoff_open = now_ts - age_days * 86400
        cutoff_advice = now_ts - no_adv_days * 86400

        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT id, symbol, market, quantity, avg_cost, side, opened_at, cost_currency "
                    "FROM positions WHERE quantity > 0 AND opened_at < ?",
                    (cutoff_open,),
                )
                olds = [dict(r) for r in await cur.fetchall()]
        except Exception:
            return 0
        if not olds:
            return 0

        from backend.signals.monitor import is_market_executable
        triggered = 0
        for pos in olds:
            symbol = pos["symbol"]; market = pos["market"]
            if not is_market_executable(market):
                continue
            avg = float(pos.get("avg_cost") or 0)
            if avg <= 0:
                continue
            cur_price = await self._get_current_price(symbol, market)
            if not cur_price:
                continue
            side = pos.get("side") or "long"
            pnl_pct = (cur_price - avg) / avg * 100 if side == "long" else (avg - cur_price) / avg * 100
            if abs(pnl_pct) >= pnl_band:
                continue   # 涨/跌都超过 band，说明在动，不是僵尸
            try:
                async with self.db.acquire() as conn:
                    cur = await conn.execute(
                        "SELECT MAX(advised_at) AS last FROM position_advices WHERE position_id=?",
                        (pos["id"],),
                    )
                    r = await cur.fetchone()
                last_adv = (r["last"] if r else 0) or 0
            except Exception:
                last_adv = 0
            if last_adv > cutoff_advice:
                continue   # 7 天内有新 advice → 仍在 AI 视野内，不动

            # v11.6 修复：zombie 减仓必须在 self._lock 内执行，与 _handle_signal/on_diagnosis_updated 互斥
            # 否则可能与正在持锁的信号处理同时操作 positions，造成 stale dict + cash 重复入账
            async with self._lock:
                await self._execute_reduce(
                    pos, symbol, market, ratio, "stale_zombie",
                    f"💤 僵尸持仓自动减仓 ({age_days}天+ / 浮盈 {pnl_pct:+.2f}% 在 ±{pnl_band}% 内 / "
                    f"{no_adv_days}天无新 AI 诊断) → 减 {ratio*100:.0f}% 释放预算"
                )
            triggered += 1
        return triggered

    # ───────── v11.2: 主动调用 AI 补全持仓 SL/TP ─────────

    async def backfill_position_targets(self, position_id: str = None, force: bool = False) -> dict:
        """
        为没有 ai_stop_loss / ai_take_profit 的持仓主动调用 AI 给出 SL/TP 并写入。
        position_id=None  → 扫所有缺失的持仓
        position_id=xxx   → 仅处理该持仓（force=True 即使已有 SL/TP 也重新生成）
        返回 {"processed": N, "filled": M, "failed": K, "items": [...]}
        """
        try:
            from backend.news.scheduler import _ai_analyzer
        except Exception:
            return {"error": "AI analyzer 未就绪"}
        if _ai_analyzer is None:
            return {"error": "AI analyzer 未就绪"}

        # 拉目标持仓
        async with self.db.acquire() as conn:
            if position_id:
                cur = await conn.execute("SELECT * FROM positions WHERE id=?", (position_id,))
            elif force:
                cur = await conn.execute("SELECT * FROM positions WHERE quantity > 0")
            else:
                cur = await conn.execute(
                    "SELECT * FROM positions WHERE quantity > 0 "
                    "AND (ai_stop_loss IS NULL OR ai_stop_loss <= 0 "
                    "  OR ai_take_profit IS NULL OR ai_take_profit <= 0)"
                )
            rows = [dict(r) for r in await cur.fetchall()]

        result = {"processed": 0, "filled": 0, "failed": 0, "skipped_budget": 0, "items": []}
        # v11.4: 加 sleep 限速（避免 50 持仓串发 50 LLM 触发限流）+ 预算软门
        for i, pos in enumerate(rows):
            result["processed"] += 1
            # 预算软门：每条调用前问一次预算，软超就停（不强切，让 ai_analyzer 自身判断）
            try:
                if hasattr(_ai_analyzer, "_can_call"):
                    if not await _ai_analyzer._can_call(hard_stop=False):
                        result["skipped_budget"] += 1
                        result["items"].append({
                            "symbol": pos["symbol"], "market": pos["market"],
                            "ok": False, "msg": "LLM 日预算用尽，跳过；明天会重试"
                        })
                        continue
            except Exception:
                pass
            try:
                suggested = await _ai_analyzer.suggest_position_targets(pos)
                if not suggested or (suggested.get("ai_stop_loss") is None and suggested.get("ai_take_profit") is None):
                    result["failed"] += 1
                    result["items"].append({
                        "symbol": pos["symbol"], "market": pos["market"],
                        "ok": False, "msg": "AI 返回空或 SL/TP 都不合理"
                    })
                    continue
                sl = suggested.get("ai_stop_loss")
                tp = suggested.get("ai_take_profit")
                # 仅覆盖原本为空的字段（除非 force）
                if not force:
                    if pos.get("ai_stop_loss") and pos["ai_stop_loss"] > 0:
                        sl = pos["ai_stop_loss"]
                    if pos.get("ai_take_profit") and pos["ai_take_profit"] > 0:
                        tp = pos["ai_take_profit"]
                async with self.db.acquire() as conn:
                    await conn.execute(
                        "UPDATE positions SET ai_stop_loss=?, ai_take_profit=? WHERE id=?",
                        (sl, tp, pos["id"]),
                    )
                    await conn.commit()
                result["filled"] += 1
                result["items"].append({
                    "symbol": pos["symbol"], "market": pos["market"],
                    "ok": True, "ai_stop_loss": sl, "ai_take_profit": tp,
                    "reason": suggested.get("reason", ""),
                })
                logger.info(f"[backfill-targets] {pos['symbol']} → SL={sl} TP={tp} ({suggested.get('reason','')[:60]})")
            except Exception as e:
                result["failed"] += 1
                result["items"].append({
                    "symbol": pos["symbol"], "market": pos["market"],
                    "ok": False, "msg": f"{type(e).__name__}: {e}"
                })
                logger.warning(f"[backfill-targets] {pos['symbol']} 失败: {e}")
            # 限速：批量调用之间 1.2s（让 LLM provider 不要 429，且给其他请求让路）
            if i + 1 < len(rows):
                await asyncio.sleep(1.2)
        return result

    # ───────── v11: TP/SL 巡检 + 分批止盈 + 跟踪止损 ─────────

    async def _get_or_init_position_state(self, position_id: str, init_price: float = None) -> dict:
        """读取持仓状态行；不存在则创建。"""
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT * FROM position_state WHERE position_id=?", (position_id,))
            row = await cur.fetchone()
            if row:
                return dict(row)
            now = int(time.time())
            await conn.execute(
                "INSERT OR IGNORE INTO position_state (position_id, peak_price, peak_pnl_pct, created_at) "
                "VALUES (?, ?, 0, ?)",
                (position_id, init_price, now),
            )
            await conn.commit()
            cur = await conn.execute("SELECT * FROM position_state WHERE position_id=?", (position_id,))
            row = await cur.fetchone()
            return dict(row) if row else {"position_id": position_id, "peak_price": init_price,
                                          "peak_pnl_pct": 0, "tp1_hit": 0, "tp2_hit": 0,
                                          "trailing_armed": 0, "last_check_at": 0}

    async def _save_position_state(self, position_id: str, **fields):
        """部分更新持仓状态行。"""
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields.keys())
        vals = list(fields.values()) + [position_id]
        try:
            async with self.db.acquire() as conn:
                await conn.execute(f"UPDATE position_state SET {cols} WHERE position_id=?", vals)
                await conn.commit()
        except Exception as e:
            logger.debug(f"[ps] save 失败 pid={position_id}: {e}")

    async def _tp_sl_monitor_loop(self):
        """
        TP/SL 巡检循环（每 60s 跑一次）：
          1. 读所有持仓
          2. 仅在该 market 处于连续竞价时段才动作
          3. 对每个持仓计算 SL/TP/分批/跟踪 → 触发对应 reduce/close
        失败不退出循环（持续运行）。
        """
        await asyncio.sleep(15)  # 启动后稍等再开始，避免冷启动竞态
        interval = self._config.get("tp_sl_monitor_interval_sec", 60)
        while True:
            try:
                if self.enabled:
                    await self._tp_sl_scan_once()
            except Exception as e:
                logger.warning(f"[tp-sl] 巡检异常: {e}")
            await asyncio.sleep(interval)

    async def _tp_sl_scan_once(self):
        """单次扫描所有持仓。"""
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT id, symbol, market, quantity, avg_cost, side, "
                    "ai_stop_loss, ai_take_profit, cost_currency FROM positions"
                )
                positions = [dict(r) for r in await cur.fetchall()]
        except Exception:
            return
        if not positions:
            return
        from backend.signals.monitor import is_market_executable
        for pos in positions:
            if not is_market_executable(pos["market"]):
                continue
            try:
                # v12.15 教训规则巡检：drawdown_force_close 类规则优先于普通 TP/SL
                # 命中即强平，跳过本根 K 线的常规检查
                handled_by_risk_rule = await self._check_risk_rules_on_position(pos)
                if handled_by_risk_rule:
                    continue
                await self._check_position_targets(pos)
            except Exception as e:
                logger.debug(f"[tp-sl] {pos['symbol']} 检查失败: {e}")
        # v12.14 (A3 修复): 同时扫 AI advice 表，把 reduce/close 建议落到实际动作
        try:
            await self._check_pending_advices()
        except Exception as e:
            logger.debug(f"[advice-exec] 扫描异常: {e}")

    async def _check_risk_rules_on_position(self, pos: Dict) -> bool:
        """v12.15 持仓巡检走教训规则评估（drawdown_force_close 等）。
        返回 True 表示已触发规则动作（调用方应跳过常规检查）；False 表示无规则命中。
        """
        try:
            from backend.trading.risk_engine import evaluate_position_check
            symbol = pos["symbol"]
            market = pos["market"]
            # 计算当前浮亏
            pnl_pct = await self._calc_pos_pnl_pct(pos, market)
            pos_with_price = dict(pos)
            pos_with_price["current_price"] = await self._get_current_price(symbol, market)
            hit = await evaluate_position_check(self.db, pos_with_price, market, pnl_pct)
            if not hit:
                return False
            rule, action_kind, reason = hit
            if action_kind == "force_close":
                logger.info(f"[risk-rules-close] {symbol} 触发教训规则强平: {reason}")
                await self._execute_close(pos, symbol, market, "risk_rule_close", reason)
                return True
            elif action_kind == "force_reduce_50":
                logger.info(f"[risk-rules-reduce] {symbol} 触发教训规则减半: {reason}")
                await self._execute_reduce(pos, symbol, market, 0.50, "risk_rule_reduce", reason)
                return True
        except Exception as e:
            logger.debug(f"[risk-rules-on-pos] {pos.get('symbol')} 评估异常: {e}")
        return False

    async def _check_pending_advices(self):
        """v12.14 (A3 修复): AI advice 桥接执行
        扫描每个持仓最近 10 分钟的 LLM 建议：
          - 最新 advice='close' + age<10min → close 100%
          - 最新 advice='reduce' + age<10min + 30min 内 ≥2 条 reduce → reduce 30%
        防重复：in-memory dict 记录每个 position 上次 executed 的 advice id
        """
        if not self.enabled:
            return
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - 10 * 60 * 1000
        consensus_cutoff_ms = now_ms - 30 * 60 * 1000
        try:
            async with self.db.acquire() as conn:
                # 拉每个持仓"最新一条" advice — 只取 reduce/close
                cur = await conn.execute("""
                    SELECT a.id, a.position_id, a.symbol, a.advice, a.reason, a.advised_at,
                           p.market, p.quantity, p.side, p.avg_cost, p.cost_currency
                    FROM position_advices a
                    INNER JOIN positions p ON p.id = a.position_id
                    WHERE a.advised_at > ?
                      AND a.advice IN ('reduce', 'close')
                      AND a.id = (SELECT MAX(id) FROM position_advices
                                  WHERE position_id = a.position_id)
                """, (cutoff_ms,))
                rows = [dict(r) for r in await cur.fetchall()]
        except Exception as e:
            logger.debug(f"[advice-exec] 查询失败: {e}")
            return
        if not rows:
            return
        if not hasattr(self, "_advice_last_executed"):
            self._advice_last_executed: Dict[str, int] = {}

        from backend.signals.monitor import is_market_executable
        for r in rows:
            pid = r["position_id"]
            advice_id = r["id"]
            # 已执行过这条 advice → skip
            if self._advice_last_executed.get(pid) == advice_id:
                continue
            if not is_market_executable(r["market"]):
                continue
            # 同股冷却也作用于 advice 执行（避免反复减）
            if not await self._check_cooldown(r["symbol"], r["market"]):
                continue
            pos_dict = {
                "id": pid, "symbol": r["symbol"], "market": r["market"],
                "quantity": r["quantity"], "side": r["side"], "avg_cost": r["avg_cost"],
                "cost_currency": r["cost_currency"],
            }
            reason_short = (r["reason"] or "")[:120]
            try:
                if r["advice"] == "close":
                    await self._execute_close(
                        pos_dict, r["symbol"], r["market"],
                        "advice_close",
                        f"🤖 AI 建议平仓: {reason_short}",
                    )
                    self._advice_last_executed[pid] = advice_id
                    logger.info(f"[advice-exec] {r['symbol']} close (advice_id={advice_id})")
                elif r["advice"] == "reduce":
                    # consensus 检查：30 min 内必须 ≥2 条 reduce 才动手（防单条噪音）
                    try:
                        async with self.db.acquire() as conn:
                            cur = await conn.execute(
                                """SELECT COUNT(*) AS n FROM position_advices
                                   WHERE position_id=? AND advice='reduce' AND advised_at > ?""",
                                (pid, consensus_cutoff_ms),
                            )
                            n = (await cur.fetchone())["n"] or 0
                    except Exception:
                        n = 0
                    if n < 2:
                        # 单条 reduce 不执行 — 标记为已看，避免每分钟重复扫
                        self._advice_last_executed[pid] = advice_id
                        continue
                    await self._execute_reduce(
                        pos_dict, r["symbol"], r["market"],
                        0.30, "advice_reduce",
                        f"🤖 AI 共识减仓 (30min 内 {n} 次 reduce): {reason_short}",
                    )
                    self._advice_last_executed[pid] = advice_id
                    logger.info(f"[advice-exec] {r['symbol']} reduce (advice_id={advice_id}, consensus n={n})")
            except Exception as e:
                logger.warning(f"[advice-exec] {r['symbol']} 执行失败: {e}")

    async def _check_position_targets(self, pos: dict):
        """
        单个持仓的 SL/TP/分批/跟踪检查。lock 防与其他动作冲突。
        触发顺序：SL（最高优先）→ TP3 全平 → T2 → T1 → trailing。
        """
        async with self._lock:
            # 重新拉一遍 position 防止已被其他 handler 改动
            async with self.db.acquire() as conn:
                cur = await conn.execute("SELECT * FROM positions WHERE id=?", (pos["id"],))
                row = await cur.fetchone()
            if not row:
                return  # 已被平掉
            p = dict(row)
            qty = float(p.get("quantity") or 0)
            if qty <= 0:
                return
            symbol = p["symbol"]; market = p["market"]
            avg = float(p.get("avg_cost") or 0)
            side = p.get("side") or "long"
            tp = p.get("ai_take_profit"); sl = p.get("ai_stop_loss")
            tp = float(tp) if tp else None
            sl = float(sl) if sl else None
            if avg <= 0:
                return

            # ─ 先取现价 + 算 pnl_pct（默认兜底和 peak 都需要）
            price = await self._get_current_price(symbol, market)
            if not price or price <= 0:
                return
            if side == "long":
                pnl_pct = (price - avg) / avg * 100.0
            else:
                pnl_pct = (avg - price) / avg * 100.0

            # ─ 状态机：peak / 标记（必须在默认兜底前初始化，grace 检查依赖 state.created_at）
            st = await self._get_or_init_position_state(p["id"], init_price=price)
            cur_peak = st.get("peak_price") or price
            new_peak = cur_peak
            if side == "long" and price > cur_peak: new_peak = price
            if side == "short" and price < cur_peak: new_peak = price
            new_peak_pnl = max(float(st.get("peak_pnl_pct") or 0), pnl_pct)
            updates = {"last_check_at": int(time.time())}
            if new_peak != cur_peak: updates["peak_price"] = new_peak
            if new_peak_pnl != float(st.get("peak_pnl_pct") or 0): updates["peak_pnl_pct"] = new_peak_pnl

            # ─ v11.1+v11.4 默认 SL/TP 兜底：仅当 state 行年龄 ≥ grace 才启用（防启动瞬间 mass close）
            sl_is_default = False
            tp_is_default = False
            state_age_sec = int(time.time()) - int(st.get("created_at") or int(time.time()))
            grace_sec = self._config.get("default_targets_grace_sec", 600)
            grace_ok = state_age_sec >= grace_sec
            # v12.11: 加密用更宽的默认 SL/TP（BTC 一夜 ±8% 是常态，避免一刀切平）
            d_sl_key = "default_stop_loss_pct_crypto" if market == "crypto" else "default_stop_loss_pct"
            d_tp_key = "default_take_profit_pct_crypto" if market == "crypto" else "default_take_profit_pct"
            if (sl is None or sl <= 0) and grace_ok:
                d_sl_pct = self._config.get(d_sl_key, 0) or 0
                if d_sl_pct > 0:
                    sl = avg * (1 - d_sl_pct / 100.0) if side == "long" else avg * (1 + d_sl_pct / 100.0)
                    sl_is_default = True
            if (tp is None or tp <= 0) and grace_ok:
                d_tp_pct = self._config.get(d_tp_key, 0) or 0
                if d_tp_pct > 0:
                    tp = avg * (1 + d_tp_pct / 100.0) if side == "long" else avg * (1 - d_tp_pct / 100.0)
                    tp_is_default = True

            # ─ 1. SL 命中（最高优先级）
            if sl and sl > 0:
                hit_sl = (side == "long" and price <= sl) or (side == "short" and price >= sl)
                if hit_sl:
                    await self._save_position_state(p["id"], **updates)
                    src = "默认兜底" if sl_is_default else "AI"
                    await self._execute_close(p, symbol, market, "sl_hit",
                        f"🛑 触发{src}止损 SL={sl:.4f}（现价 {price:.4f}, 浮盈 {pnl_pct:+.1f}%）")
                    return

            # ─ 2. TP 全程命中（直接全平）
            if tp and tp > 0:
                hit_tp = (side == "long" and price >= tp) or (side == "short" and price <= tp)
                if hit_tp:
                    await self._save_position_state(p["id"], **updates, tp1_hit=1, tp2_hit=1)
                    src = "默认兜底" if tp_is_default else "AI"
                    await self._execute_close(p, symbol, market, "tp_hit",
                        f"🎯 触达{src}止盈 TP={tp:.4f}（现价 {price:.4f}, 浮盈 {pnl_pct:+.1f}%）")
                    return

            # ─ 3. 分批止盈：T1 / T2（仅 AI 给的 TP 才分批，机械兜底直接全平不分档）
            if tp and tp > 0 and avg > 0 and not tp_is_default:
                t1_pct = self._config.get("tp_partial_t1_pct", 0.33)
                t2_pct = self._config.get("tp_partial_t2_pct", 0.66)
                if side == "long":
                    t1_price = avg + (tp - avg) * t1_pct
                    t2_price = avg + (tp - avg) * t2_pct
                    hit_t1 = price >= t1_price
                    hit_t2 = price >= t2_price
                else:
                    t1_price = avg - (avg - tp) * t1_pct
                    t2_price = avg - (avg - tp) * t2_pct
                    hit_t1 = price <= t1_price
                    hit_t2 = price <= t2_price

                # T2 优先（已到 T2 也意味着早过 T1）
                if hit_t2 and not int(st.get("tp2_hit") or 0):
                    ratio = self._config.get("tp_partial_t2_reduce", 0.30)
                    await self._save_position_state(p["id"], **updates, tp1_hit=1, tp2_hit=1, trailing_armed=1)
                    await self._execute_reduce(p, symbol, market, ratio, "tp_partial",
                        f"📊 T2 止盈 减 {ratio*100:.0f}% (T2={t2_price:.4f}/现价 {price:.4f}, 浮盈 {pnl_pct:+.1f}%)")
                    return
                if hit_t1 and not int(st.get("tp1_hit") or 0):
                    ratio = self._config.get("tp_partial_t1_reduce", 0.30)
                    await self._save_position_state(p["id"], **updates, tp1_hit=1)
                    await self._execute_reduce(p, symbol, market, ratio, "tp_partial",
                        f"📊 T1 止盈 减 {ratio*100:.0f}% (T1={t1_price:.4f}/现价 {price:.4f}, 浮盈 {pnl_pct:+.1f}%)")
                    return

            # ─ 4. 跟踪止损：浮盈 ≥ trailing_arm_pnl_pct 后激活
            arm_pct = self._config.get("trailing_arm_pnl_pct", 15.0)
            keep = self._config.get("trailing_keep_ratio", 0.60)
            armed = int(st.get("trailing_armed") or 0) == 1 or new_peak_pnl >= arm_pct
            if armed and avg > 0:
                if side == "long":
                    trail_price = avg + keep * (new_peak - avg)
                    hit_trail = price <= trail_price and trail_price > avg  # 只在已锁定盈利时触发
                else:
                    trail_price = avg - keep * (avg - new_peak)
                    hit_trail = price >= trail_price and trail_price < avg
                if hit_trail:
                    if not int(st.get("trailing_armed") or 0):
                        updates["trailing_armed"] = 1
                    await self._save_position_state(p["id"], **updates)
                    await self._execute_close(p, symbol, market, "trailing_stop",
                        f"📉 跟踪止损 触发线={trail_price:.4f} (峰值 {new_peak:.4f}, 现价 {price:.4f}, 峰值浮盈 {new_peak_pnl:+.1f}% → 当前 {pnl_pct:+.1f}%)")
                    return
                if not int(st.get("trailing_armed") or 0):
                    updates["trailing_armed"] = 1

            # 没触发动作，仅更新峰值/last_check_at
            if updates:
                await self._save_position_state(p["id"], **updates)

    @staticmethod
    def _smart_reduce_ratio(rating: str, pnl_pct: float, side: str = "long") -> float:
        """
        v11 Plan B + v11.5 短仓修复：根据当前浮盈和 rating 动态决定减仓比例。
        pnl_pct 含义已是"正=赚 / 负=亏"（caller 已按 side 计算），所以多/空逻辑可统一。
        但是有一个语义差异：
          - 多仓遇 hold 浮亏 → 不动等趋势（市场已跌过，反弹概率高）
          - 空仓遇 hold 浮亏 → **必须动**（已破策略前提，亏损只会扩大），保守减 30% 锁部分
        返回 0~1.0；返回 0 表示"不减仓"。
        """
        if rating == "sell":
            return 1.0
        if rating == "reduce":
            if pnl_pct < 0:    return 0.30
            if pnl_pct < 5:    return 0.40
            if pnl_pct < 15:   return 0.55
            if pnl_pct < 30:   return 0.70
            return 0.80
        if rating == "hold":
            if pnl_pct < 0:
                # v11.5 短仓亏损中不能等；v12.11 改为按浮亏深度阶梯（避免反复 0.30 减半到 <1 手强平）
                # -0~5% 减 30%；-5~10% 减 50%；-10%+ 直接全平止损
                if side != "short":
                    return 0.0
                if pnl_pct < -10:  return 1.0     # 浮亏 > 10% 全平止损
                if pnl_pct < -5:   return 0.50    # 浮亏 5-10% 减半
                return 0.30                        # 浮亏 0-5% 减 30%
            if pnl_pct < 5:    return 0.30
            if pnl_pct < 15:   return 0.40
            if pnl_pct < 30:   return 0.50
            return 0.50
        return 0.0

    async def _calc_pos_pnl_pct(self, pos: dict, market: str) -> float:
        """
        计算持仓当前浮盈 %（pos.side 感知；无现价返回 0）。
        long:  (price - avg) / avg × 100
        short: (avg - price) / avg × 100
        """
        try:
            price = await self._get_current_price(pos["symbol"] if "symbol" in pos else None, market) \
                if "symbol" in pos else None
        except Exception:
            price = None
        if price is None or not pos.get("avg_cost"):
            return 0.0
        avg = float(pos["avg_cost"])
        if avg <= 0:
            return 0.0
        side = pos.get("side") or "long"
        if side == "long":
            return (price - avg) / avg * 100.0
        return (avg - price) / avg * 100.0

    async def _handle_diagnosis_change(self, symbol: str, market: str, rating: str):
        """
        诊断变化驱动 → 减仓/清仓 + strong_buy 试单。
        v11：减仓比例改为按当前浮盈动态决定（_smart_reduce_ratio）。
          多仓：sell→全平；reduce/hold→按浮盈分级；浮亏遇 hold 不动
          空仓（仅加密）：buy/strong_buy→全平；hold→按浮盈分级
        """
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT id, symbol, quantity, avg_cost, cost_currency, side FROM positions WHERE symbol=? AND market=?",
                (symbol, market),
            )
            row = await cur.fetchone()

        # 无持仓：strong_buy 触发诊断试单
        if not row or not row["quantity"]:
            if rating == "strong_buy":
                await self._try_trial_position(symbol, market)
            return

        pos = dict(row)
        side = pos.get("side") or "long"
        # 计算当前浮盈
        try:
            cur_price = await self._get_current_price(symbol, market)
            if cur_price and pos.get("avg_cost"):
                avg = float(pos["avg_cost"])
                pnl_pct = ((cur_price - avg) / avg * 100.0) if side == "long" else ((avg - cur_price) / avg * 100.0)
            else:
                pnl_pct = 0.0
        except Exception:
            pnl_pct = 0.0

        if side == "long":
            if rating == "sell":
                await self._execute_close(pos, symbol, market, "rating_change",
                                          f"AI 诊断变 sell，平多仓 (浮盈 {pnl_pct:+.1f}%)")
            elif rating in ("reduce", "hold"):
                ratio = self._smart_reduce_ratio(rating, pnl_pct, side="long")
                if ratio <= 0:
                    logger.info(f"[smart-reduce] {symbol} 多仓 rating={rating} 但浮盈 {pnl_pct:+.1f}% → 不减（保留趋势）")
                    return
                await self._execute_reduce(pos, symbol, market, ratio, "rating_change",
                                           f"AI 诊断变 {rating}，减多仓 {ratio*100:.0f}% (浮盈 {pnl_pct:+.1f}%)")
        else:
            # 空仓（仅加密）
            if rating in ("buy", "strong_buy"):
                await self._execute_close(pos, symbol, market, "rating_change",
                                          f"AI 诊断变 {rating}，平空仓 (浮盈 {pnl_pct:+.1f}%)")
            elif rating in ("hold", "reduce"):
                # v11.5: 空仓 reduce 也走 smart_reduce_ratio（之前只处理 hold）
                ratio = self._smart_reduce_ratio(rating, pnl_pct, side="short")
                if ratio <= 0:
                    logger.info(f"[smart-reduce] {symbol} 空仓 rating={rating} 浮盈 {pnl_pct:+.1f}% → 不减")
                    return
                await self._execute_reduce(pos, symbol, market, ratio, "rating_change",
                                           f"AI 诊断升至 hold，减空仓 {ratio*100:.0f}% (浮盈 {pnl_pct:+.1f}%)")

    async def _try_trial_position(self, symbol: str, market: str):
        """
        诊断驱动的强买试单：rating=strong_buy 且无持仓时直接小仓位入场。
        三道门：试单冷却 12h / RSI<75 / 距 20 日高>2% / **必须连续竞价时段**
        股票市场加密都支持，加密走 LONG。
        """
        cfg = self._config

        # 必须在连续竞价时段（集合竞价 / 闭市直接拒）
        from backend.signals.monitor import is_market_executable
        if not is_market_executable(market):
            return  # 静默跳过（试单不需要 log_rejected 噪音）

        # 加密单独处理（按需）；股票常规
        # 试单冷却（用 auto_trade_log 里 trigger_type='diagnosis_strong_buy' 的最后时间）
        # 防双触发：试单冷却查所有触发类型的 executed 记录（不只是 diagnosis_strong_buy）
        # 因为如果信号路径刚开过仓，诊断试单不应再追加；反之亦然
        cooldown_sec = cfg["trial_cooldown_hours"] * 3600
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT MAX(traded_at) AS last FROM auto_trade_log "
                    "WHERE symbol=? AND market=? AND status='executed'",
                    (symbol, market),
                )
                row = await cur.fetchone()
            if row and row["last"] and (int(time.time()) - row["last"]) < cooldown_sec:
                return  # 冷却中（任何触发源都算），静默跳过
        except Exception:
            pass

        # 防并发：同股 60 秒内已有任何 executed 记录直接拒（覆盖 race 场景）
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT 1 FROM auto_trade_log WHERE symbol=? AND market=? "
                    "AND status='executed' AND traded_at > ? LIMIT 1",
                    (symbol, market, int(time.time()) - 60),
                )
                if await cur.fetchone():
                    return
        except Exception:
            pass

        # 拉当前价 + 简单技术快照（RSI / 距 20 日高）
        price = await self._get_current_price(symbol, market)
        if not price:
            return

        try:
            from backend.data.cache import cached_get_klines
            from backend.data.models import Interval, Market
            from backend.indicators.builtin import calc_rsi
            import numpy as np

            mkt_enum = Market(market)
            candles = await cached_get_klines(
                db=self.db, market=mkt_enum, symbol=symbol, interval=Interval.D1, limit=30,
            )
            if not candles or len(candles) < 20:
                logger.debug(f"[trial] {symbol} K 线不足 ({len(candles) if candles else 0})，跳过质量门")
                return
            closes = np.array([c.close for c in candles], dtype=np.float64)
            highs = np.array([c.high for c in candles], dtype=np.float64)
            rsi = float(calc_rsi(closes, 14)[-1])
            hi20 = float(highs[-20:].max())
            dist_to_hi_pct = (hi20 - price) / price * 100

            # 质量门
            if rsi >= cfg["trial_rsi_max"]:
                await self._log_trial_rejected(symbol, market, price, f"RSI={rsi:.1f} ≥ {cfg['trial_rsi_max']} 已超买")
                return
            if dist_to_hi_pct < cfg["trial_dist_to_hi_pct_min"]:
                await self._log_trial_rejected(symbol, market, price, f"距 20 日高仅 {dist_to_hi_pct:.2f}% < {cfg['trial_dist_to_hi_pct_min']}% 追高风险")
                return
        except Exception as e:
            logger.debug(f"[trial] {symbol} 质量门检查异常: {e}")
            return

        # 风控：v12.13 池级冷静期 + 并发持仓上限（试单路径，silent return）
        cd_ok, _ = await self._check_pool_cooldown(market)
        if not cd_ok:
            return
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT COUNT(*) AS n FROM positions")
            cur_positions = (await cur.fetchone())["n"]
        if cur_positions >= cfg["max_concurrent_positions"]:
            return

        # 计算仓位
        account = await self.get_account()
        initial = account.get("initial_capital_usd", 10000)
        order_usd = initial * cfg["trial_position_pct"]
        if order_usd < cfg["min_order_usd"]:
            return

        # 单股上限 + 池级仓位上限（v12.13: 三池独立）
        single_cap_usd = initial * cfg["max_single_position_pct"]
        if order_usd > single_cap_usd:
            return
        pool_id = self._pool_for(market)
        pool_used_local, pool_initial_local, pool_ccy = await self._pool_cap_state(pool_id)
        if pool_ccy == "USD":
            order_pool_local = order_usd
        else:
            try:
                pool_to_usd = await get_rate(self.db, pool_ccy)
                order_pool_local = order_usd / pool_to_usd if pool_to_usd > 0 else order_usd
            except Exception:
                from backend.trading.fx import FALLBACK_RATES
                rate = FALLBACK_RATES.get(pool_ccy, 1.0)
                order_pool_local = order_usd / rate if rate > 0 else order_usd
        if pool_used_local + order_pool_local > pool_initial_local * cfg.get("total_position_cap_pct", 0.80):
            return

        # v12.11: 试单只查池级缓冲（已移除全局 cash 缓冲，3 池隔离）
        ok, _ = await self._check_pool_cash_buffer(market, order_usd)
        if not ok:
            return

        currency = market_to_currency(market)
        try:
            fx = await get_rate(self.db, currency)
        except Exception:
            return
        if fx <= 0:
            return
        local_amount = order_usd / fx
        qty = local_amount / price
        if qty <= 0:
            return
        # 试单也按市场最小手数规整
        qty = self._normalize_qty(market, symbol, qty)
        if qty <= 0:
            await self._log_trial_rejected(symbol, market, price, f"试单数量不足最小手数（{market}：${order_usd:.2f}/{price:.4f}不够 1 手）")
            return
        order_usd = qty * price * fx  # 重算实际成本

        # 用 _execute_open 执行，构造伪 sig
        pseudo_sig = {
            "id": f"trial-{symbol}-{int(time.time())}",
            "symbol": symbol, "market": market,
            "ai_confidence": 85,  # 诊断给的信任度
        }
        # 标记 trigger_type 为诊断试单
        side_icon = "🎯 诊断试单·强买"
        try:
            pid = await self.portfolio_manager.add_position(
                symbol=symbol, market=market, quantity=qty, avg_cost=price,
                notes=f"自动{side_icon} (rating=strong_buy, 2% 试单)",
                side="long",  # 试单固定多头
            )
        except Exception as e:
            logger.warning(f"[trial-open] {symbol} 失败: {e}")
            return
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    "UPDATE positions SET cost_currency=?, entry_fx_rate=?, total_cost_usd=?, auto_traded=1, side='long' WHERE id=?",
                    (currency, fx, order_usd, pid),
                )
                await conn.commit()
        except Exception:
            pass
        await self._update_cash(-order_usd, market=market, fx=fx)
        await self._log_trade(
            action="open", symbol=symbol, market=market, qty=qty, price=price,
            amount_usd=order_usd, fx=fx, side="long",
            trigger_type="diagnosis_strong_buy",
            trigger_detail={"rating": "strong_buy", "trial": True},
            reason=f"{side_icon} {qty:.4f}{'张' if market=='crypto' else '股'} @ {price:.4f}（rating 升级为强买，无信号直通）",
            position_id=pid, remaining_qty=qty,
        )
        logger.info(f"🎯 [trial-open] {symbol}({market}) 强买试单 qty={qty:.4f} @ {price:.4f} usd=${order_usd:.2f}")

    async def _check_a_stock_t_plus_1(self, symbol: str) -> Tuple[bool, str]:
        """v12.13 A 股 T+1 检查：当日有 open/add executed 记录 → 当日不能 sell。
        返回 (can_sell, reject_reason)。
        实现：查 auto_trade_log 当天（北京时间今日 00:00 起）该 symbol 是否有 executed open/add。
        """
        from datetime import datetime, timezone, timedelta
        bj_tz = timezone(timedelta(hours=8))
        today_start = int(datetime.now(bj_tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT MAX(traded_at) FROM auto_trade_log
                       WHERE symbol=? AND market='cn' AND status='executed'
                         AND action IN ('open','add') AND traded_at >= ?""",
                    (symbol, today_start),
                )
                row = await cur.fetchone()
            if row and row[0]:
                buy_ts = int(row[0])
                buy_time_str = datetime.fromtimestamp(buy_ts, tz=bj_tz).strftime("%H:%M")
                return False, f"T+1 限制：A 股今日 {buy_time_str} 已买入，当日不能卖（次日 09:30 后可卖）"
        except Exception as e:
            logger.debug(f"[t+1] {symbol} 检查异常: {e}")
        return True, ""

    async def _log_trial_rejected(self, symbol: str, market: str, price: float, reason: str):
        """诊断试单被质量门拒绝时记录到 auto_trade_log。"""
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO auto_trade_log
                       (symbol, market, action, quantity, price, amount_usd, fx_rate,
                        trigger_type, reason, status, rejected_reason, traded_at)
                       VALUES (?, ?, 'open', 0, ?, 0, 1, 'diagnosis_strong_buy', '诊断试单质量门', 'rejected', ?, ?)""",
                    (symbol, market, price, reason, int(time.time())),
                )
                await conn.commit()
        except Exception:
            pass

    # ───────── 执行核心（虚拟下单）─────────

    async def _execute_open(self, symbol, market, qty, price, fx, currency, usd_cost, sig, rating, side="long"):
        """开仓：多仓扣现金锁定成本；空仓同样锁现金（作为保证金，平仓时结算盈亏）。"""
        side_icon = "📥 开多" if side == "long" else "🔽 开空"
        try:
            pid = await self.portfolio_manager.add_position(
                symbol=symbol, market=market,
                quantity=qty, avg_cost=price,
                notes=f"自动{side_icon} (rating={rating}, signal={sig['id'][:8]})",
                side=side,  # 显式传 side，冲突时覆盖（避免 auto 开反向仓时 side 错乱）
            )
        except Exception as e:
            logger.warning(f"[auto-open] {symbol} 失败: {e}")
            return
        try:
            # 同时写入 AI 给的 SL/TP（之前完全丢失，开仓后看不到）
            # v11.6: 加方向合理性校验，防 AI hallucination 给出反向 SL/TP（如 long 的 sl > avg）→ 监控立刻清仓
            ai_sl = sig.get("ai_stop_loss")
            ai_tp = sig.get("ai_take_profit")
            avg_for_check = float(price)
            try: ai_sl = float(ai_sl) if ai_sl is not None else None
            except (TypeError, ValueError): ai_sl = None
            try: ai_tp = float(ai_tp) if ai_tp is not None else None
            except (TypeError, ValueError): ai_tp = None
            if ai_sl is not None:
                if side == "long" and (ai_sl <= 0 or ai_sl >= avg_for_check or ai_sl < avg_for_check * 0.50):
                    logger.warning(f"[auto-open] {symbol} long ai_sl={ai_sl} 不合理 (应在 0.50×avg ~ avg 之间)，丢弃")
                    ai_sl = None
                elif side == "short" and (ai_sl <= avg_for_check or ai_sl > avg_for_check * 1.50):
                    logger.warning(f"[auto-open] {symbol} short ai_sl={ai_sl} 不合理 (应在 avg ~ 1.50×avg 之间)，丢弃")
                    ai_sl = None
            if ai_tp is not None:
                if side == "long" and (ai_tp <= avg_for_check or ai_tp > avg_for_check * 5.0):
                    logger.warning(f"[auto-open] {symbol} long ai_tp={ai_tp} 不合理 (应在 avg ~ 5×avg)，丢弃")
                    ai_tp = None
                elif side == "short" and (ai_tp >= avg_for_check or ai_tp <= 0):
                    logger.warning(f"[auto-open] {symbol} short ai_tp={ai_tp} 不合理 (应在 0 ~ avg)，丢弃")
                    ai_tp = None
            async with self.db.acquire() as conn:
                await conn.execute(
                    "UPDATE positions SET cost_currency=?, entry_fx_rate=?, total_cost_usd=?, "
                    "auto_traded=1, side=?, ai_stop_loss=?, ai_take_profit=? WHERE id=?",
                    (currency, fx, usd_cost, side, ai_sl, ai_tp, pid),
                )
                await conn.commit()
        except Exception as e:
            # cost_currency/SL/TP 写失败：log error 便于排查，不中断后续（开仓成功是主路径）
            logger.error(f"[auto-open] {symbol} UPDATE cost_currency/sl/tp 失败 pid={pid}: {e}")
        await self._update_cash(-usd_cost, market=market, fx=fx)
        await self._log_trade(
            action="open", symbol=symbol, market=market, qty=qty, price=price,
            amount_usd=usd_cost, fx=fx, side=side,
            trigger_type="signal_confirm",
            trigger_detail={"signal_id": sig["id"], "rating": rating, "ai_confidence": sig.get("ai_confidence"), "side": side},
            reason=f"{side_icon} {qty:.4f}{'张' if market=='crypto' else '股'} @ {price:.4f} (rating={rating})",
            position_id=pid, remaining_qty=qty,
        )
        # v11.5: 预建 position_state 行（peak_price=avg_cost），让监控立刻可用
        # 防 60s 内被反向信号平掉时 state 永不写入，导致 trailing/peak 追踪丢失
        try:
            await self._get_or_init_position_state(pid, init_price=float(price))
        except Exception as e:
            logger.debug(f"[auto-open] 预建 state 失败: {e}")
        # 新开仓 → 立即触发一次 AI 建议（不等 6h 巡检）
        try:
            from backend.news.scheduler import _ai_analyzer
            if _ai_analyzer is not None:
                self._spawn_bg(_ai_analyzer.generate_advice_for_position(pid, force=True))
        except Exception as e:
            logger.debug(f"[auto-open] 触发 AI 建议失败: {e}")

    async def _execute_add(self, symbol, market, qty, price, fx, currency, usd_cost, sig, rating, side="long"):
        side_icon = "➕ 加多" if side == "long" else "➕ 加空"
        async with self.db.acquire() as conn:
            cur = await conn.execute(
                "SELECT id, quantity, avg_cost, total_cost_usd FROM positions WHERE symbol=? AND market=?",
                (symbol, market),
            )
            row = await cur.fetchone()
            if not row:
                return
            old_qty, old_cost = row["quantity"], row["avg_cost"]
            new_qty = old_qty + qty
            new_avg = (old_qty * old_cost + qty * price) / new_qty
            new_total_usd = (row["total_cost_usd"] or 0) + usd_cost
            await conn.execute(
                "UPDATE positions SET quantity=?, avg_cost=?, total_cost_usd=? WHERE id=?",
                (new_qty, new_avg, new_total_usd, row["id"]),
            )
            await conn.commit()
        await self._update_cash(-usd_cost, market=market, fx=fx)
        await self._log_trade(
            action="add", symbol=symbol, market=market, qty=qty, price=price,
            amount_usd=usd_cost, fx=fx, side=side,
            trigger_type="signal_confirm",
            trigger_detail={"signal_id": sig["id"], "rating": rating, "side": side},
            reason=f"{side_icon} {qty:.4f} @ {price:.4f}",
            position_id=row["id"], remaining_qty=new_qty,
        )

    async def _execute_reduce(self, pos, symbol, market, ratio, trigger_type, reason):
        """
        减仓结算：
          多仓：卖出部分 → 现金增加 = qty × 当前价 × fx
          空仓：平部分 → 现金变动 = (avg_cost - 当前价) × qty × fx（盈利为正）
                        + 归还该部分的保证金（原占用 = qty × avg_cost × fx）
        非交易时段：跳过（持仓继续，等开市再评估；不抢挂单出场）
        """
        # ─ 减仓冷却 / 日操作上限（防 AI 反复 hold 时无限刷次）
        # v12.11: tp_partial 也需要免冷却（风控操作；之前 T1 后 15min 内 SL/trailing 被静默吞掉）
        URGENT_REDUCE_TRIGGERS = ("tp_partial",)
        urgent_reduce = (
            (trigger_type == "rating_change" and "sell" in (reason or "").lower())
            or trigger_type in URGENT_REDUCE_TRIGGERS
        )
        bypass_daily = urgent_reduce
        if not urgent_reduce:
            if not await self._check_cooldown(symbol, market):
                logger.info(f"[reduce-cooldown] {symbol}({market}) 同股冷却期内（{self._config['cooldown_sec']}s），跳过减仓 ({trigger_type})")
                return
            if not bypass_daily and not await self._check_daily_limit(symbol, market):
                logger.info(f"[reduce-daily] {symbol}({market}) 当日操作已达上限 {self._config['max_daily_ops_per_symbol']} 次，跳过减仓 ({trigger_type})")
                return
        # 必须在连续竞价时段才能执行卖单
        from backend.signals.monitor import is_market_executable
        if not is_market_executable(market):
            logger.info(f"[reduce-defer] {symbol}({market}) 未到连续竞价时段，减仓延后")
            # 写 auto_trade_log 便于用户追踪"该减没减"
            try:
                async with self.db.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO auto_trade_log
                           (symbol, market, action, quantity, price, amount_usd, fx_rate,
                            trigger_type, reason, status, rejected_reason, traded_at)
                           VALUES (?, ?, 'reduce', 0, 0, 0, 1, ?, ?, 'rejected', ?, ?)""",
                        (symbol, market, trigger_type, reason,
                         "未到连续竞价时段，减仓延后（pending，等下个开市重评估）",
                         int(time.time())),
                    )
                    await conn.commit()
            except Exception:
                pass
            return

        # v12.13 A 股 T+1：当日买入不能当日卖
        if market == "cn":
            can_sell, why = await self._check_a_stock_t_plus_1(symbol)
            if not can_sell:
                logger.info(f"[reduce-t+1] {symbol} {why}")
                try:
                    async with self.db.acquire() as conn:
                        await conn.execute(
                            """INSERT INTO auto_trade_log
                               (symbol, market, action, quantity, price, amount_usd, fx_rate,
                                trigger_type, reason, status, rejected_reason, traded_at)
                               VALUES (?, 'cn', 'reduce', 0, 0, 0, 1, ?, ?, 'rejected', ?, ?)""",
                            (symbol, trigger_type, reason, why, int(time.time())),
                        )
                        await conn.commit()
                except Exception:
                    pass
                return

        # v11.4 修复 cash-leak race：执行前重新校验 row 仍存在，防止 zombie loop snapshot 后被用户手动平仓导致重复 update_cash
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT id, quantity FROM positions WHERE id=?", (pos["id"],))
            still_exists = await cur.fetchone()
        if not still_exists or not still_exists["quantity"]:
            logger.info(f"[reduce-stale] {symbol}({market}) position 已不存在/已清，取消减仓")
            return
        # 用 DB 真实 quantity 校正 pos（防 snapshot 过时）
        if abs(float(still_exists["quantity"]) - float(pos.get("quantity") or 0)) > 1e-9:
            pos = dict(pos)
            pos["quantity"] = float(still_exists["quantity"])
        side = pos.get("side") or "long"
        raw_qty_to_close = pos["quantity"] * ratio
        if raw_qty_to_close <= 0:
            return
        # ─ 关键修复 1：减仓规整到市场最小手数
        # 否则会出现 100 → 50 → 25 → 12.5 这种荒唐结果（违反 A 股/港股 100 股最小手数）
        qty_to_close = self._normalize_qty(market, symbol, raw_qty_to_close)
        # ─ v12.14 修复 (A1+A6)：分清"减不出 1 手"和"剩不到 1 手"两种情况
        # 之前 OR 逻辑把"想减太少"误当成"剩太少"，导致 3 股仓位 T1 减 30% 实际全清
        # 正确逻辑：
        #   1) 想减的量 < 1 手 → 改用 1 手（保留剩余继续走 T2/T3）；若剩也 < 1 手则跳过本次减仓
        #   2) 减完后剩余 < 1 手 → 残料全平（cleanup）
        min_lot = self._min_lot(market, symbol)
        if qty_to_close < min_lot:
            # 名义减仓量不足 1 手
            if pos["quantity"] >= 2 * min_lot:
                # 仓位有 ≥2 手 → 减 1 手
                qty_to_close = min_lot
                logger.info(f"[reduce-min1lot] {symbol} 名义减 {raw_qty_to_close:.4f}<1 手，改减 1 手({min_lot})")
            else:
                # 仓位 <2 手 → 减 1 手 = 全平，但本来想"部分"减 → 跳过，留给 T2/T3/SL 处理
                logger.info(f"[reduce-skip] {symbol} 仓位 {pos['quantity']:.4f} <2 手({min_lot})，部分减仓无意义，跳过")
                return
        remaining_after = pos["quantity"] - qty_to_close
        if remaining_after < min_lot:
            # 减完后只剩残料 → 全平
            qty_to_close = pos["quantity"]
            logger.info(f"[reduce→close] {symbol} 剩余 {remaining_after:.6f} <1 手({min_lot})，全平 cleanup")
        if qty_to_close <= 0:
            return
        price = await self._get_current_price(symbol, market)
        if not price:
            return
        currency = pos.get("cost_currency") or market_to_currency(market)
        try:
            fx = await get_rate(self.db, currency)
        except Exception as e:
            logger.warning(f"[reduce] {symbol} 汇率获取失败: {e}，跳过本次减仓")
            return
        if fx <= 0:
            return
        avg_cost = pos.get("avg_cost") or price
        if side == "long":
            proceed_usd = qty_to_close * price * fx
        else:
            # 空仓平部分 = 退回保证金 + 盈亏
            margin_release = qty_to_close * avg_cost * fx
            pnl = (avg_cost - price) * qty_to_close * fx
            raw = margin_release + pnl
            # 爆仓保护：亏损超过保证金时（价格翻倍以上），最多亏光保证金
            if raw < 0:
                logger.warning(f"[short-liq] {symbol} 减仓爆仓 price={price:.4f} avg={avg_cost:.4f} margin={margin_release:.2f} pnl={pnl:.2f} → 钳制为 0")
                proceed_usd = 0.0
            else:
                proceed_usd = raw
        new_qty = pos["quantity"] - qty_to_close
        # 精度问题：接近 0 视为清零，并按"平仓"口径输出整单盈亏
        is_fully_closed = new_qty <= max(pos["quantity"] * 1e-6, 1e-9)
        async with self.db.acquire() as conn:
            if is_fully_closed:
                await conn.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
                # v11: 减仓全平时也清状态机
                await conn.execute("DELETE FROM position_state WHERE position_id=?", (pos["id"],))
            else:
                # v12.13: 减仓时同步按比例缩减 total_cost_usd（avg_cost 不变是会计标准；
                # total_cost_usd 是"剩余仓位的入场总成本"，必须随 quantity 一起缩减，
                # 否则池级 cap / P&L 报表会高估占用）
                old_qty = pos["quantity"] or 0
                old_total = pos.get("total_cost_usd") or 0
                new_total_cost_usd = old_total * (new_qty / old_qty) if old_qty > 0 else 0
                await conn.execute(
                    "UPDATE positions SET quantity=?, total_cost_usd=? WHERE id=?",
                    (new_qty, new_total_cost_usd, pos["id"]),
                )
            await conn.commit()
        await self._update_cash(proceed_usd, market=market, fx=fx)
        side_icon = "🏁 平多" if (is_fully_closed and side == "long") else \
                    "🏁 平空" if (is_fully_closed and side != "long") else \
                    "➖ 减多" if side == "long" else "➖ 减空"
        detail = {"ratio": ratio, "side": side}
        reason_txt = f"{side_icon} {qty_to_close:.4f} @ {price:.4f} ({reason})"
        if is_fully_closed:
            pnl = await self._calc_position_pnl(
                pos["id"], side,
                incoming_close_usd=proceed_usd, incoming_close_qty=qty_to_close,
            )
            detail.update({
                "realized_pnl_usd": round(pnl["pnl_usd"], 2),
                "realized_pnl_pct": round(pnl["pnl_pct"] * 100, 2),
                "total_in_usd": round(pnl["open_usd"], 2),
                "total_out_usd": round(pnl["exit_usd"], 2),
                "leg_count": pnl["leg_count"],
            })
            reason_txt += f" | {self._format_pnl_tag(pnl)}"
        await self._log_trade(
            action="close" if is_fully_closed else "reduce",
            symbol=symbol, market=market, qty=qty_to_close, price=price,
            amount_usd=proceed_usd, fx=fx, side=side,
            trigger_type=trigger_type, trigger_detail=detail,
            reason=reason_txt,
            position_id=pos["id"], remaining_qty=0 if is_fully_closed else new_qty,
        )

    async def _execute_close(self, pos, symbol, market, trigger_type, reason):
        # 关键风控：close 路径也加冷却检查（防 AI 反复触发同向清仓尝试）
        # ⚠️ v11.4：监控触发的 sl_hit / tp_hit / trailing_stop 必须不受冷却拦截（否则止损被静默吞掉）
        # rating=sell / signal_reverse + 含 'sell' 的 reason 也属紧急
        URGENT_TRIGGERS = ("sl_hit", "tp_hit", "trailing_stop")
        urgent = (trigger_type in URGENT_TRIGGERS) or (
            trigger_type in ("rating_change", "signal_reverse") and "sell" in (reason or "").lower()
        )
        if not urgent and not await self._check_cooldown(symbol, market):
            logger.info(f"[close-cooldown] {symbol}({market}) 冷却期内，跳过平仓")
            return
        # 必须在连续竞价时段才能平仓
        from backend.signals.monitor import is_market_executable
        if not is_market_executable(market):
            logger.info(f"[close-defer] {symbol}({market}) 未到连续竞价时段，平仓延后")
            try:
                async with self.db.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO auto_trade_log
                           (symbol, market, action, quantity, price, amount_usd, fx_rate,
                            trigger_type, reason, status, rejected_reason, traded_at)
                           VALUES (?, ?, 'close', 0, 0, 0, 1, ?, ?, 'rejected', ?, ?)""",
                        (symbol, market, trigger_type, reason,
                         "未到连续竞价时段，平仓延后（pending，等下个开市重评估）",
                         int(time.time())),
                    )
                    await conn.commit()
            except Exception:
                pass
            return

        # v12.13 A 股 T+1：当日买入不能当日平仓（含 SL/TP/诊断变 sell 等所有 close 触发）
        if market == "cn":
            can_sell, why = await self._check_a_stock_t_plus_1(symbol)
            if not can_sell:
                logger.info(f"[close-t+1] {symbol} {why} (trigger={trigger_type})")
                try:
                    async with self.db.acquire() as conn:
                        await conn.execute(
                            """INSERT INTO auto_trade_log
                               (symbol, market, action, quantity, price, amount_usd, fx_rate,
                                trigger_type, reason, status, rejected_reason, traded_at)
                               VALUES (?, 'cn', 'close', 0, 0, 0, 1, ?, ?, 'rejected', ?, ?)""",
                            (symbol, trigger_type, reason, why, int(time.time())),
                        )
                        await conn.commit()
                except Exception:
                    pass
                return

        # 执行前重新校验 position 仍存在（防止手动平仓后 auto_close 重复入账）
        async with self.db.acquire() as conn:
            cur = await conn.execute("SELECT id, quantity FROM positions WHERE id=?", (pos["id"],))
            still_exists = await cur.fetchone()
        if not still_exists or not still_exists["quantity"]:
            logger.info(f"[close-stale] {symbol}({market}) position 已不存在/已清，取消平仓")
            return

        side = pos.get("side") or "long"
        price = await self._get_current_price(symbol, market)
        if not price:
            return
        qty = float(still_exists["quantity"])  # 用 DB 最新数量，防 snapshot 过时
        currency = pos.get("cost_currency") or market_to_currency(market)
        try:
            fx = await get_rate(self.db, currency)
        except Exception as e:
            logger.warning(f"[close] {symbol} 汇率获取失败: {e}，跳过本次平仓")
            return
        if fx <= 0:
            return
        avg_cost = pos.get("avg_cost") or price
        if side == "long":
            proceed_usd = qty * price * fx
        else:
            # 空仓平仓 = 退回保证金 + 盈亏
            margin_release = qty * avg_cost * fx
            pnl = (avg_cost - price) * qty * fx
            raw = margin_release + pnl
            # 爆仓保护：亏损超过保证金 → 钳制为 0（最多亏保证金）
            if raw < 0:
                logger.warning(f"[short-liq] {symbol} 平仓爆仓 price={price:.4f} avg={avg_cost:.4f} margin={margin_release:.2f} pnl={pnl:.2f} → 钳制为 0")
                proceed_usd = 0.0
            else:
                proceed_usd = raw
        # 在 DELETE 之前计算整单盈亏（包含本次 close 的 proceed_usd）
        pnl = await self._calc_position_pnl(
            pos["id"], side,
            incoming_close_usd=proceed_usd, incoming_close_qty=qty,
        )
        async with self.db.acquire() as conn:
            await conn.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
            # v11: 同步清理 position_state（防僵尸状态行影响下次同 id 复用）
            await conn.execute("DELETE FROM position_state WHERE position_id=?", (pos["id"],))
            await conn.commit()
        await self._update_cash(proceed_usd, market=market, fx=fx)
        side_icon = "🏁 平多" if side == "long" else "🏁 平空"
        # 构造带整单累计盈亏的 reason
        pnl_tag = self._format_pnl_tag(pnl)
        await self._log_trade(
            action="close", symbol=symbol, market=market, qty=qty, price=price,
            amount_usd=proceed_usd, fx=fx, side=side,
            trigger_type=trigger_type,
            trigger_detail={
                "side": side,
                "realized_pnl_usd": round(pnl["pnl_usd"], 2),
                "realized_pnl_pct": round(pnl["pnl_pct"] * 100, 2),
                "total_in_usd": round(pnl["open_usd"], 2),
                "total_out_usd": round(pnl["exit_usd"], 2),
                "leg_count": pnl["leg_count"],
            },
            reason=f"{side_icon} {qty:.4f} @ {price:.4f} ({reason}) | {pnl_tag}",
            position_id=pos["id"], remaining_qty=0,
        )
        # v12.11: 平仓后立即触发深度复盘（之前依赖 4h batch loop，存在 4-10h 教训空窗）
        try:
            from backend.main import trade_reviewer
            if trade_reviewer is not None:
                self._spawn_bg(trade_reviewer.review_position(pos["id"], force=False))
        except Exception as e:
            logger.debug(f"[auto-close] 触发复盘失败: {e}")

    # ───────── 日志 ─────────

    async def _log_trade(self, action, symbol, market, qty, price, amount_usd, fx,
                         trigger_type, trigger_detail, reason, position_id, remaining_qty, side="long"):
        # 把 side 放进 trigger_detail（DB 表没有 side 字段，走 JSON）
        if isinstance(trigger_detail, dict):
            trigger_detail.setdefault("side", side)
        account = await self.get_account()
        remaining_cash = account.get("cash_usd") or 0
        # 写日志必须成功 —— 之前 BTC 04-22 04:27 那笔 open 就是这里静默失败导致 log 丢失
        # 最多重试 3 次，每次等 50ms，所有尝试都失败才放弃（并 ERROR 级别告警）
        last_exc = None
        written = False
        for attempt in range(3):
            try:
                async with self.db.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO auto_trade_log
                           (symbol, market, action, quantity, price, amount_usd, fx_rate,
                            trigger_type, trigger_detail, reason, position_id,
                            remaining_qty, remaining_cash_usd, status, traded_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'executed', ?)""",
                        (symbol, market, action, qty, price, amount_usd, fx,
                         trigger_type, json.dumps(trigger_detail, ensure_ascii=False),
                         reason, position_id, remaining_qty, remaining_cash, int(time.time())),
                    )
                    await conn.commit()
                written = True
                break
            except Exception as e:
                last_exc = e
                if attempt < 2:
                    await asyncio.sleep(0.05)
        if not written:
            logger.error(
                f"[auto-trade-log] 🚨 严重：3 次重试后仍写入失败！"
                f" action={action} symbol={symbol}/{market} qty={qty} price={price} "
                f"position_id={position_id} err={last_exc}"
            )
            # 即便 DB 写失败，也把事件推送到前端 + 控制台，避免用户看不到"到底发生了什么"
            if self.ws_hub:
                try:
                    b = getattr(self.ws_hub, "broadcast_auto_trade", None)
                    if b:
                        await b({
                            "action": action, "symbol": symbol, "market": market,
                            "qty": qty, "price": price, "amount_usd": round(amount_usd, 2),
                            "reason": f"⚠️ DB 写失败: {reason}", "traded_at": int(time.time()),
                            "log_write_failed": True,
                        })
                except Exception:
                    pass
        # WS 推送（专用通道，与新闻分离）
        if self.ws_hub:
            try:
                broadcast = getattr(self.ws_hub, "broadcast_auto_trade", None)
                if broadcast:
                    await broadcast({
                        "action": action, "symbol": symbol, "market": market,
                        "qty": qty, "price": price, "amount_usd": round(amount_usd, 2),
                        "reason": reason, "traded_at": int(time.time()),
                    })
            except Exception:
                pass
        logger.info(f"🤖 [auto-{action}] {symbol}({market}) qty={qty:.4f} @ {price:.4f} usd=${amount_usd:.2f} · {reason}")
        # Telegram 推送（fire-and-forget）
        try:
            from backend.notify.telegram import send_trade_event as _tg_send
            self._spawn_bg(_tg_send(
                action=action, symbol=symbol, market=market,
                qty=qty, price=price, amount_usd=amount_usd,
                reason=reason or "",
                extra=trigger_detail if isinstance(trigger_detail, dict) else None,
            ))
        except Exception as e:
            logger.debug(f"[telegram] spawn 失败: {e}")

    # 拒单去重：10 分钟内同 (symbol, market, action, reason_category) 只记 1 次
    # 避免"单日操作上限"/"预算超上限"同信号反复 log 刷屏
    _reject_dedupe: Dict[tuple, float] = {}
    _REJECT_DEDUP_WINDOW = 600  # 10 分钟

    @staticmethod
    def _reject_category(reason: str) -> str:
        """提取拒因主干（去掉变化的数字参数），用于同类去重。"""
        if not reason: return ""
        # 几个常见模式按前 15-20 字符匹配就够
        for kw in ("加仓需持仓浮盈", "单日操作", "当日操作", "冷却期", "冷静期",
                   "诊断缺失", "连续竞价", "未到交易时段", "预算超上限", "现金缓冲",
                   "并发持仓", "AI诊断评级", "最小手数", "汇率获取失败", "池亏损"):
            if kw in reason:
                return kw
        return reason[:20]

    async def _log_rejected(self, sig, action, reason):
        # v12.13: dedup 只控制 stdout 日志频率（避免刷屏），但 SQL 入库每次都写。
        # 旧行为：dedup 也阻断 SQL 入库 → 用户在前端看不到反复被拒的拒因，无法定位问题。
        now = time.time()
        key = (sig["symbol"], sig["market"], action, self._reject_category(reason))
        last = self._reject_dedupe.get(key, 0)
        if now - last >= self._REJECT_DEDUP_WINDOW:
            logger.info(f"[auto-reject] {sig['symbol']}({sig['market']}) {action}: {reason}")
            self._reject_dedupe[key] = now
            # 定期清理过期 key 避免无限增长
            if len(self._reject_dedupe) > 500:
                cutoff = now - self._REJECT_DEDUP_WINDOW
                self._reject_dedupe = {k: v for k, v in self._reject_dedupe.items() if v > cutoff}
        # 追溯字段
        td = {
            "signal_id": sig.get("id"),
            "strategy": sig.get("strategy_name", ""),
            "interval": sig.get("interval", ""),
            "ai_confidence": sig.get("ai_confidence"),
        }
        # SQL 入库总是执行（前端能看到完整拒因历史）
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO auto_trade_log (symbol, market, action, quantity, price, amount_usd, fx_rate,
                       trigger_type, trigger_detail, reason, status, rejected_reason, traded_at)
                       VALUES (?, ?, ?, 0, 0, 0, 1, ?, ?, ?, 'rejected', ?, ?)""",
                    (sig["symbol"], sig["market"], action,
                     "signal_confirm", json.dumps(td, ensure_ascii=False),
                     sig.get("reason", ""), reason, int(time.time())),
                )
                await conn.commit()
        except Exception as e:
            # v12.13: 不再静默吞，至少 debug 级别记录（database locked 等）
            logger.debug(f"[auto-reject] {sig['symbol']} 写库失败: {type(e).__name__}: {e}")
