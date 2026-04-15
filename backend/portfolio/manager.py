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
    ) -> str:
        position_id = str(uuid.uuid4())
        async with self.db.acquire() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO positions (id, symbol, market, quantity, avg_cost, opened_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, market) DO UPDATE SET
                    quantity = excluded.quantity,
                    avg_cost = excluded.avg_cost,
                    notes = excluded.notes
                RETURNING id
                """,
                (position_id, symbol, market, quantity, avg_cost, int(time.time()), notes),
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
