"""
SQLite 异步数据库管理模块。
使用 aiosqlite 实现连接池模式，支持并发访问。
"""

import asyncio
import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite


class DatabaseManager:
    """异步 SQLite 数据库管理器，使用连接池模式。"""

    def __init__(self, db_path: str, pool_size: int = 5):
        self.db_path = db_path
        self.pool_size = pool_size
        self._pool: asyncio.Queue = asyncio.Queue(maxsize=pool_size)
        self._initialized = False

    async def init_db(self):
        """初始化数据库：创建连接池并建表。"""
        for _ in range(self.pool_size):
            conn = await aiosqlite.connect(self.db_path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await self._pool.put(conn)

        async with self.acquire() as conn:
            await self._create_tables(conn)

        self._initialized = True

    async def close(self):
        """关闭连接池中的所有连接。"""
        while not self._pool.empty():
            conn = await self._pool.get()
            await conn.close()
        self._initialized = False

    class _acquire:
        """连接池上下文管理器，自动借还连接。"""

        def __init__(self, manager: "DatabaseManager"):
            self.manager = manager
            self.conn: Optional[aiosqlite.Connection] = None

        def __init_subclass__(cls, **kwargs):
            pass

        async def __aenter__(self) -> aiosqlite.Connection:
            self.conn = await self.manager._pool.get()
            return self.conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if self.conn is not None:
                await self.manager._pool.put(self.conn)
            return False

    def acquire(self):
        """获取一个连接池上下文管理器。"""
        return self._acquire(self)

    # ──────────────────────── 建表 ────────────────────────

    async def _create_tables(self, conn: aiosqlite.Connection):
        """创建所有基础表。"""
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT,
                sort_order INTEGER DEFAULT 0,
                added_at INTEGER NOT NULL,
                UNIQUE(symbol, market)
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                condition_type TEXT NOT NULL,
                condition_json TEXT NOT NULL,
                message TEXT,
                notify_methods TEXT DEFAULT '["browser","sound"]',
                label TEXT DEFAULT '',
                repeat_mode TEXT DEFAULT 'once',
                cooldown INTEGER DEFAULT 300,
                enabled INTEGER DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                triggered_at INTEGER NOT NULL,
                price REAL,
                message TEXT
            );

            CREATE TABLE IF NOT EXISTS backtest_reports (
                id TEXT PRIMARY KEY,
                strategy_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                start_date TEXT,
                end_date TEXT,
                config_json TEXT,
                result_json TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- ═══ 新闻快讯（Phase 3A 规则引擎产出 + Phase 3B LLM 异步回填）═══
            CREATE TABLE IF NOT EXISTS flash_news (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT,
                source TEXT NOT NULL,
                url TEXT,
                published_at INTEGER NOT NULL,      -- 源站发布时间
                collected_at INTEGER NOT NULL,      -- 采集入库时间
                importance INTEGER DEFAULT 1,       -- 1~5 星
                sentiment TEXT DEFAULT 'neutral',   -- bullish | bearish | neutral
                categories TEXT DEFAULT '[]',       -- JSON: 关联品种代码
                impact_tags TEXT DEFAULT '[]',      -- JSON: 事件类型标签
                keywords TEXT DEFAULT '[]',         -- JSON: 命中关键词
                is_holding_related INTEGER DEFAULT 0,
                l2_score REAL DEFAULT 0,
                -- 加密 6 币种影响（规则引擎产出）
                impact_on_crypto TEXT,              -- JSON: [{symbol, direction, strength}]
                -- 宏观数据字段（仅 is_macro_data=1 时有值）
                is_macro_data INTEGER DEFAULT 0,
                macro_type TEXT DEFAULT '',         -- CPI | FOMC | NFP
                macro_actual TEXT,
                macro_forecast TEXT,
                macro_previous TEXT,
                macro_deviation_pct REAL,
                macro_impact_strength TEXT DEFAULT '',  -- neutral | light | strong
                -- LLM 深度解读（Phase 3B 异步回填）
                ai_analysis TEXT,
                -- 去重辅助字段
                event_id TEXT,
                content_hash TEXT,
                simhash TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_flash_time ON flash_news (published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_flash_importance ON flash_news (importance);
            CREATE INDEX IF NOT EXISTS idx_flash_event ON flash_news (event_id);

            -- ═══ 候选池（仅股票市场，market CHECK 硬约束）═══
            CREATE TABLE IF NOT EXISTS watch_pool (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL CHECK(market IN ('us', 'hk', 'cn')),
                score REAL DEFAULT 0,               -- 综合评分 0-100
                status TEXT DEFAULT 'candidate',    -- candidate | monitoring | archived
                source TEXT DEFAULT 'manual',       -- news | anomaly | macro_theme | manual
                reason TEXT DEFAULT '',
                added_at INTEGER NOT NULL,
                last_scored_at INTEGER,
                last_news_mention_at INTEGER,       -- 最近一次新闻提及时间
                low_score_since INTEGER,            -- 开始连续低分时间
                archived_at INTEGER,
                UNIQUE(symbol, market)
            );
            CREATE INDEX IF NOT EXISTS idx_pool_status ON watch_pool (status);
            CREATE INDEX IF NOT EXISTS idx_pool_score ON watch_pool (score DESC);

            -- ═══ 候选池评分历史（用于复盘和趋势分析）═══
            CREATE TABLE IF NOT EXISTS pool_score_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_item_id TEXT NOT NULL,
                score REAL NOT NULL,
                factors TEXT,                        -- JSON: 评分分量详情
                scored_at INTEGER NOT NULL
            );

            -- ═══ 策略绑定（多对多）═══
            CREATE TABLE IF NOT EXISTS strategy_bindings (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                params TEXT DEFAULT '{}',            -- JSON: 策略参数覆盖
                enabled INTEGER DEFAULT 1,
                created_at INTEGER NOT NULL,
                UNIQUE(symbol, market, strategy_name)
            );
            CREATE INDEX IF NOT EXISTS idx_binding_symbol ON strategy_bindings (symbol, market);

            -- ═══ 策略信号 ═══
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                action TEXT NOT NULL,                -- buy | sell
                strategy_name TEXT NOT NULL,
                confidence INTEGER DEFAULT 0,        -- 0-100
                price REAL,
                suggested_qty REAL,
                stop_loss REAL,
                take_profit REAL,
                reason TEXT DEFAULT '',
                triggered_by TEXT DEFAULT '{}',      -- JSON: 触发来源详情
                status TEXT DEFAULT 'active',        -- active | expired | acted
                generated_at INTEGER NOT NULL,
                expires_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_signals_time ON signals (generated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals (symbol, market);

            -- ═══ 持仓 ═══
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                quantity REAL NOT NULL,
                avg_cost REAL NOT NULL,
                opened_at INTEGER NOT NULL,
                notes TEXT DEFAULT '',
                UNIQUE(symbol, market)
            );

            -- ═══ 持仓建议历史 ═══
            CREATE TABLE IF NOT EXISTS position_advices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                advice TEXT NOT NULL,                -- hold | reduce | add | close
                reason TEXT NOT NULL,
                triggered_by TEXT DEFAULT '{}',      -- JSON: 触发来源
                advised_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_advices_time ON position_advices (advised_at DESC);

            -- ═══ LLM 调用成本追踪（日预算控制用）═══
            CREATE TABLE IF NOT EXISTS llm_cost_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                called_at INTEGER NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL NOT NULL,
                news_id TEXT                         -- 关联的新闻 ID（可选）
            );
            CREATE INDEX IF NOT EXISTS idx_llm_cost_time ON llm_cost_log (called_at DESC);
        """)
        await conn.commit()

    async def _ensure_kline_table(self, conn: aiosqlite.Connection, market: str, interval: str):
        """动态创建 K线表（按市场和周期），带复合主键和索引。"""
        table = f"klines_{market}_{interval}".lower().replace("-", "_")
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS [{table}] (
                symbol TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                turnover REAL DEFAULT 0,
                PRIMARY KEY (symbol, timestamp)
            )
        """)
        await conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON [{table}] (symbol, timestamp DESC)")
        await conn.commit()
        return table

    # ──────────────────────── K线 CRUD ────────────────────────

    async def save_klines(
        self,
        market: str,
        interval: str,
        symbol: str,
        candles: List[Dict[str, Any]],
    ):
        """批量 upsert K线数据。"""
        async with self.acquire() as conn:
            table = await self._ensure_kline_table(conn, market, interval)
            await conn.executemany(
                f"""
                INSERT INTO [{table}] (symbol, timestamp, open, high, low, close, volume, turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timestamp) DO UPDATE SET
                    open=excluded.open, high=excluded.high,
                    low=excluded.low, close=excluded.close,
                    volume=excluded.volume, turnover=excluded.turnover
                """,
                [
                    (
                        symbol,
                        c["timestamp"],
                        c["open"],
                        c["high"],
                        c["low"],
                        c["close"],
                        c["volume"],
                        c.get("turnover", 0),
                    )
                    for c in candles
                ],
            )
            await conn.commit()

    async def get_klines(
        self,
        market: str,
        interval: str,
        symbol: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """查询 K线数据，支持时间范围和条数限制。"""
        async with self.acquire() as conn:
            table = await self._ensure_kline_table(conn, market, interval)
            sql = f"SELECT * FROM [{table}] WHERE symbol = ?"
            params: list = [symbol]
            if start_ts is not None:
                sql += " AND timestamp >= ?"
                params.append(start_ts)
            if end_ts is not None:
                sql += " AND timestamp <= ?"
                params.append(end_ts)
            sql += " ORDER BY timestamp ASC LIMIT ?"
            params.append(limit)

            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ──────────────────────── Watchlist ────────────────────────

    async def add_to_watchlist(self, symbol: str, market: str, name: str = "") -> int:
        """添加自选，返回 id。"""
        async with self.acquire() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO watchlist (symbol, market, name, added_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol, market) DO UPDATE SET name=excluded.name
                """,
                (symbol, market, name, int(time.time())),
            )
            await conn.commit()
            return cursor.lastrowid or 0

    async def remove_from_watchlist(self, symbol: str, market: str):
        """移除自选。"""
        async with self.acquire() as conn:
            await conn.execute(
                "DELETE FROM watchlist WHERE symbol = ? AND market = ?",
                (symbol, market),
            )
            await conn.commit()

    async def get_watchlist(self, market: Optional[str] = None) -> List[Dict]:
        """获取自选列表，可按市场过滤。"""
        async with self.acquire() as conn:
            if market:
                cursor = await conn.execute(
                    "SELECT * FROM watchlist WHERE market = ? ORDER BY sort_order, added_at DESC",
                    (market,),
                )
            else:
                cursor = await conn.execute("SELECT * FROM watchlist ORDER BY sort_order, added_at DESC")
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def update_watchlist_order(self, items: List[Dict[str, Any]]):
        """批量更新自选排序。items: [{"id": 1, "sort_order": 0}, ...]"""
        async with self.acquire() as conn:
            for item in items:
                await conn.execute(
                    "UPDATE watchlist SET sort_order = ? WHERE id = ?",
                    (item["sort_order"], item["id"]),
                )
            await conn.commit()

    # ──────────────────────── Alerts ────────────────────────

    async def create_alert(self, alert: Dict[str, Any]) -> str:
        """创建警报，返回 id。"""
        alert_id = alert.get("id") or str(uuid.uuid4())
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO alerts (id, symbol, market, condition_type, condition_json,
                    message, notify_methods, label, repeat_mode, cooldown, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    alert["symbol"],
                    alert["market"],
                    alert["condition_type"],
                    json.dumps(alert.get("condition", {})),
                    alert.get("message", ""),
                    json.dumps(alert.get("notify_methods", ["browser", "sound"])),
                    alert.get("label", ""),
                    alert.get("repeat_mode", "once"),
                    alert.get("cooldown", 300),
                    1 if alert.get("enabled", True) else 0,
                    int(time.time()),
                ),
            )
            await conn.commit()
        return alert_id

    async def update_alert(self, alert_id: str, updates: Dict[str, Any]):
        """更新警报字段。"""
        async with self.acquire() as conn:
            set_clauses = []
            params = []
            field_map = {
                "condition_type": "condition_type",
                "condition": "condition_json",
                "message": "message",
                "notify_methods": "notify_methods",
                "label": "label",
                "repeat_mode": "repeat_mode",
                "cooldown": "cooldown",
                "enabled": "enabled",
            }
            for key, col in field_map.items():
                if key in updates:
                    val = updates[key]
                    if key == "condition":
                        val = json.dumps(val)
                    elif key == "notify_methods":
                        val = json.dumps(val)
                    elif key == "enabled":
                        val = 1 if val else 0
                    set_clauses.append(f"{col} = ?")
                    params.append(val)
            if not set_clauses:
                return
            set_clauses.append("updated_at = ?")
            params.append(int(time.time()))
            params.append(alert_id)
            await conn.execute(f"UPDATE alerts SET {', '.join(set_clauses)} WHERE id = ?", params)
            await conn.commit()

    async def delete_alert(self, alert_id: str):
        """删除警报。"""
        async with self.acquire() as conn:
            await conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
            await conn.commit()

    async def get_alerts(self, symbol: Optional[str] = None, enabled_only: bool = False) -> List[Dict]:
        """查询警报列表。"""
        async with self.acquire() as conn:
            sql = "SELECT * FROM alerts WHERE 1=1"
            params: list = []
            if symbol:
                sql += " AND symbol = ?"
                params.append(symbol)
            if enabled_only:
                sql += " AND enabled = 1"
            sql += " ORDER BY created_at DESC"
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["condition"] = json.loads(d.pop("condition_json", "{}"))
                d["notify_methods"] = json.loads(d.get("notify_methods", "[]"))
                d["enabled"] = bool(d["enabled"])
                results.append(d)
            return results

    async def get_alert_by_id(self, alert_id: str) -> Optional[Dict]:
        """按 ID 获取单条警报。"""
        async with self.acquire() as conn:
            cursor = await conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            d = dict(row)
            d["condition"] = json.loads(d.pop("condition_json", "{}"))
            d["notify_methods"] = json.loads(d.get("notify_methods", "[]"))
            d["enabled"] = bool(d["enabled"])
            return d

    # ──────────────────────── Alert History ────────────────────────

    async def add_alert_history(self, alert_id: str, symbol: str, market: str, price: float, message: str):
        """记录警报触发历史。"""
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO alert_history (alert_id, symbol, market, triggered_at, price, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (alert_id, symbol, market, int(time.time()), price, message),
            )
            await conn.commit()

    async def get_alert_history(self, alert_id: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """查询警报触发历史。"""
        async with self.acquire() as conn:
            if alert_id:
                cursor = await conn.execute(
                    "SELECT * FROM alert_history WHERE alert_id = ? ORDER BY triggered_at DESC LIMIT ?",
                    (alert_id, limit),
                )
            else:
                cursor = await conn.execute(
                    "SELECT * FROM alert_history ORDER BY triggered_at DESC LIMIT ?",
                    (limit,),
                )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ──────────────────────── Config (KV) ────────────────────────

    async def get_config(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """读取配置值。"""
        async with self.acquire() as conn:
            cursor = await conn.execute("SELECT value FROM config WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row["value"] if row else default

    async def set_config(self, key: str, value: str):
        """写入配置值（upsert）。"""
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO config (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
            await conn.commit()

    async def get_all_config(self) -> Dict[str, str]:
        """获取所有配置。"""
        async with self.acquire() as conn:
            cursor = await conn.execute("SELECT key, value FROM config")
            rows = await cursor.fetchall()
            return {r["key"]: r["value"] for r in rows}

    async def delete_config(self, key: str):
        """删除配置项。"""
        async with self.acquire() as conn:
            await conn.execute("DELETE FROM config WHERE key = ?", (key,))
            await conn.commit()

    # ──────────────────────── Backtest Reports ────────────────────────

    async def save_backtest_report(self, report: Dict[str, Any]) -> str:
        """保存回测报告，返回 id。"""
        report_id = report.get("id") or str(uuid.uuid4())
        async with self.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO backtest_reports (id, strategy_name, symbol, interval,
                    start_date, end_date, config_json, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET result_json=excluded.result_json
                """,
                (
                    report_id,
                    report["strategy_name"],
                    report["symbol"],
                    report["interval"],
                    report.get("start_date", ""),
                    report.get("end_date", ""),
                    json.dumps(report.get("config", {})),
                    json.dumps(report.get("result", {})),
                    int(time.time()),
                ),
            )
            await conn.commit()
        return report_id

    async def get_backtest_reports(self, limit: int = 50) -> List[Dict]:
        """获取回测报告列表。"""
        async with self.acquire() as conn:
            cursor = await conn.execute(
                "SELECT * FROM backtest_reports ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["config"] = json.loads(d.pop("config_json", "{}"))
                d["result"] = json.loads(d.pop("result_json", "{}"))
                results.append(d)
            return results

    async def get_backtest_report_by_id(self, report_id: str) -> Optional[Dict]:
        """按 ID 获取回测报告。"""
        async with self.acquire() as conn:
            cursor = await conn.execute("SELECT * FROM backtest_reports WHERE id = ?", (report_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            d = dict(row)
            d["config"] = json.loads(d.pop("config_json", "{}"))
            d["result"] = json.loads(d.pop("result_json", "{}"))
            return d

    async def delete_backtest_report(self, report_id: str):
        """删除回测报告。"""
        async with self.acquire() as conn:
            await conn.execute("DELETE FROM backtest_reports WHERE id = ?", (report_id,))
            await conn.commit()

    # Phase 3A/3B/4/5 的 CRUD 方法将在对应模块实现时追加
    # 新表已在 _create_tables 中声明:
    #   - flash_news (Phase 3A)
    #   - watch_pool (Phase 3A)
    #   - pool_score_history (Phase 3B)
    #   - strategy_bindings (Phase 4)
    #   - signals (Phase 4)
    #   - positions (Phase 5)
    #   - position_advices (Phase 5)
    #   - llm_cost_log (Phase 3B)
