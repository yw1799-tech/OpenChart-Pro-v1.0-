"""
持仓管理 CRUD（Phase 5）。

存储用户手动录入或券商同步（Phase 7）的持仓。
浮动盈亏由当前价计算，不持久化（每次查询时计算最新值）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional


class PortfolioManager:
    def __init__(self, db):
        self.db = db

    async def add_position(
        self,
        symbol: str,
        market: str,
        quantity: float,
        avg_cost: float,
        notes: str = "",
        side: Optional[str] = None,
    ) -> str:
        """
        新增/更新持仓。UPSERT 策略：
          - 新建：side 缺省用 'long'（schema 默认）
          - 冲突（同 symbol+market 已存在）：
              · side 显式传入 → 覆盖（用于 auto_trader 开反向仓）
              · side 未传 → 保留原值（用于用户手动 add，不破坏 auto 字段）
          - 自动维护字段（cost_currency/entry_fx_rate/total_cost_usd/auto_traded）始终保留原值
        """
        from backend.trading.fx import market_to_currency
        position_id = str(uuid.uuid4())
        new_side = side if side in ("long", "short") else "long"
        # 按市场设置本币（避免留 schema 默认 'USD' 导致 CNY/HKD 持仓被当 USD 计值）
        ccy = market_to_currency(market)
        async with self.db.acquire() as conn:
            if side in ("long", "short"):
                # 显式传 side（auto_trader 主动调用）→ 冲突时覆盖 side
                cursor = await conn.execute(
                    """
                    INSERT INTO positions (id, symbol, market, quantity, avg_cost, opened_at, notes, side, cost_currency)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, market) DO UPDATE SET
                        quantity = excluded.quantity,
                        avg_cost = excluded.avg_cost,
                        notes = excluded.notes,
                        side = excluded.side
                    RETURNING id
                    """,
                    (position_id, symbol, market, quantity, avg_cost, int(time.time()), notes, new_side, ccy),
                )
            else:
                # side 未传（用户手动 add）→ 冲突时保留原 side 和 cost_currency，避免破坏 auto 字段
                cursor = await conn.execute(
                    """
                    INSERT INTO positions (id, symbol, market, quantity, avg_cost, opened_at, notes, cost_currency)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, market) DO UPDATE SET
                        quantity = excluded.quantity,
                        avg_cost = excluded.avg_cost,
                        notes = excluded.notes
                    RETURNING id
                    """,
                    (position_id, symbol, market, quantity, avg_cost, int(time.time()), notes, ccy),
                )
            row = await cursor.fetchone()
            await conn.commit()
            return row["id"] if row else position_id

    async def update_position(
        self,
        position_id: str,
        quantity: Optional[float] = None,
        avg_cost: Optional[float] = None,
        notes: Optional[str] = None,
    ):
        sets = []
        params = []
        if quantity is not None:
            sets.append("quantity = ?"); params.append(quantity)
        if avg_cost is not None:
            sets.append("avg_cost = ?"); params.append(avg_cost)
        if notes is not None:
            sets.append("notes = ?"); params.append(notes)
        if not sets:
            return
        params.append(position_id)
        async with self.db.acquire() as conn:
            await conn.execute(
                f"UPDATE positions SET {', '.join(sets)} WHERE id = ?", params
            )
            await conn.commit()

    async def remove_position(self, position_id: str) -> bool:
        async with self.db.acquire() as conn:
            cursor = await conn.execute(
                "DELETE FROM positions WHERE id = ?", (position_id,)
            )
            # v11.4: 同步清掉 position_state（防 stale 行积累 + 影响下次同 id 复用）
            await conn.execute(
                "DELETE FROM position_state WHERE position_id = ?", (position_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def get_all(self) -> List[Dict[str, Any]]:
        async with self.db.acquire() as conn:
            cursor = await conn.execute(
                "SELECT * FROM positions ORDER BY opened_at DESC"
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_by_id(self, position_id: str) -> Optional[Dict[str, Any]]:
        async with self.db.acquire() as conn:
            cursor = await conn.execute(
                "SELECT * FROM positions WHERE id = ?", (position_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_advice_history(self, position_id: str, limit: int = 50) -> List[Dict]:
        async with self.db.acquire() as conn:
            cursor = await conn.execute(
                "SELECT * FROM position_advices WHERE position_id = ? ORDER BY advised_at DESC LIMIT ?",
                (position_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
