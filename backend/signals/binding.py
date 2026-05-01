"""
策略绑定管理（PRD F7.2/F7.3 / TDD §6.5.2）。

支持灵活多对多绑定：
- 一只品种 → 一个策略（基础）
- 一只品种 → 多个策略（任一触发即信号）
- 一个策略 → 批量多只品种（同时启用）
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional


class StrategyBindingManager:
    """策略绑定 CRUD（依赖 db: DatabaseManager 实例）。"""

    def __init__(self, db):
        self.db = db

    async def bind(
        self,
        symbol: str,
        market: str,
        strategy_name: str,
        interval: str = "1H",
        params: Optional[Dict] = None,
        enabled: bool = True,
    ) -> str:
        """绑定单个策略到单只品种 + 周期。重复绑定返回原 id。"""
        binding_id = str(uuid.uuid4())
        async with self.db.acquire() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO strategy_bindings (id, symbol, market, strategy_name, interval, params, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, market, strategy_name, interval) DO UPDATE SET
                    params = excluded.params,
                    enabled = excluded.enabled
                RETURNING id
                """,
                (
                    binding_id,
                    symbol,
                    market,
                    strategy_name,
                    interval,
                    json.dumps(params or {}),
                    1 if enabled else 0,
                    int(time.time()),
                ),
            )
            row = await cursor.fetchone()
            await conn.commit()
            return row["id"] if row else binding_id

    async def batch_bind(
        self,
        strategy_name: str,
        targets: List[Dict[str, str]],  # [{"symbol":..., "market":..., "interval":可选}]
        params: Optional[Dict] = None,
    ) -> Dict[str, int]:
        """一个策略批量绑定多只品种。"""
        ok = 0
        failed = 0
        for t in targets:
            try:
                # v11.4 修复：必须用关键字传 params，否则会被 bind() 的 interval 位置参数吃掉
                await self.bind(
                    symbol=t["symbol"],
                    market=t["market"],
                    strategy_name=strategy_name,
                    interval=t.get("interval", "1H"),
                    params=params,
                )
                ok += 1
            except Exception:
                failed += 1
        return {"bound": ok, "failed": failed}

    async def unbind(self, symbol: str, market: str, strategy_name: str) -> bool:
        async with self.db.acquire() as conn:
            cursor = await conn.execute(
                "DELETE FROM strategy_bindings WHERE symbol = ? AND market = ? AND strategy_name = ?",
                (symbol, market, strategy_name),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def get_bindings(
        self,
        symbol: Optional[str] = None,
        market: Optional[str] = None,
        strategy_name: Optional[str] = None,
        enabled_only: bool = True,
    ) -> List[Dict[str, Any]]:
        async with self.db.acquire() as conn:
            sql = "SELECT * FROM strategy_bindings WHERE 1=1"
            params: list = []
            if symbol:
                sql += " AND symbol = ?"
                params.append(symbol)
            if market:
                sql += " AND market = ?"
                params.append(market)
            if strategy_name:
                sql += " AND strategy_name = ?"
                params.append(strategy_name)
            if enabled_only:
                sql += " AND enabled = 1"
            sql += " ORDER BY created_at DESC"
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                try:
                    d["params"] = json.loads(d.get("params") or "{}")
                except json.JSONDecodeError:
                    d["params"] = {}
                results.append(d)
            return results

    async def get_strategies_for_symbol(self, symbol: str, market: str) -> List[Dict]:
        """获取某品种绑定的所有启用策略。"""
        return await self.get_bindings(symbol=symbol, market=market, enabled_only=True)
