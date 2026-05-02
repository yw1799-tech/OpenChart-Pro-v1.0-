"""
SQLite 异步数据库管理模块。
使用 aiosqlite 实现连接池模式，支持并发访问。
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite


# 新闻 source -> 默认市场归属（粗粒度，最终以 categories 推断为准）
_SOURCE_MARKET_HINTS = {
    "金十": ("us", "macro"), "金十数据": ("us", "macro"),
    "财联社": ("cn",), "财联社电报": ("cn",),
    "上海证券报": ("cn",), "新浪财经": ("cn",),
    "同花顺": ("cn",), "东方财富": ("cn",),
    "Yahoo Finance HK": ("hk",), "SCMP": ("hk",), "21财经港股": ("hk",),
    "经济通": ("hk",), "etnet": ("hk",), "HKEX": ("hk",),
    "Yahoo Finance": ("us",), "MarketWatch": ("us",), "CNBC": ("us",),
    "PR Newswire": ("us",), "SEC EDGAR": ("us",), "Finnhub": ("us",),
    "Bloomberg": ("us", "macro"), "Reuters": ("us", "macro"),
    # v12.19.2: 补全加密源 hint (之前漏了 Binance/OKX/TheDefiant 导致 market=crypto 过滤失效)
    "CoinDesk": ("crypto",), "Cointelegraph": ("crypto",),
    "TheBlock": ("crypto",), "Decrypt": ("crypto",),
    "TheDefiant": ("crypto",), "Bitcoin Magazine": ("crypto",),
    "Binance公告": ("crypto",), "Binance Announcement": ("crypto",),
    "OKX公告": ("crypto",), "OKX Announcement": ("crypto",),
    "ChainCatcher": ("crypto",), "Odaily": ("crypto",), "PANews": ("crypto",),
    "BeInCrypto": ("crypto",), "CryptoSlate": ("crypto",), "AMBCrypto": ("crypto",),
    # v12.19.3: 国际加密源补充
    "U.Today": ("crypto",), "NewsBTC": ("crypto",),
    "Bitcoin.com News": ("crypto",), "CryptoPotato": ("crypto",),
    "CoinGape": ("crypto",), "Crypto Briefing": ("crypto",),
    "DLNews": ("crypto",), "Blockworks": ("crypto",),
    # 宏观补全
    "Federal Reserve": ("macro",), "BLS": ("macro",), "ECB": ("macro",),
}

# v12.19.2: 反向索引 — 给定 market 返回所有归属 source 列表（用于 SQL 过滤）
_MARKET_TO_SOURCES = {}
for _src, _mkts in _SOURCE_MARKET_HINTS.items():
    for _m in _mkts:
        _MARKET_TO_SOURCES.setdefault(_m, []).append(_src)


def _infer_news_markets(d: Dict) -> List[str]:
    """
    实时推断一条新闻涉及的市场列表（前端按此过滤）。
    优先级：1) categories 里的 symbol → 推断市场； 2) source 默认归属； 3) is_macro_data 加 macro。
    """
    markets = set()
    cats = d.get("categories") or []
    if isinstance(cats, str):
        try:
            cats = json.loads(cats)
        except Exception:
            cats = []
    # 港股 source 提示：含 "HK" / "港" / "Yahoo Finance HK" / "SCMP" 等 → 4-5 位数字推断为港股
    src_str = (d.get("source", "") or "").upper()
    src_hk = any(k in src_str for k in ("HK", "港", "SCMP"))
    for sym in cats:
        if not isinstance(sym, str):
            continue
        s = sym.upper()
        if s.endswith("-USDT") or s.endswith("-USD") or s.endswith("-USDC"):
            markets.add("crypto")
        elif s.endswith(".HK"):
            markets.add("hk")
        elif s.isdigit() and len(s) == 6:
            # 6 位纯数字优先 A 股；但港股新闻源含 6 位（实际多为 0XXXXX）→ 强 source hint 时归港股
            if src_hk and s.startswith("0"):
                markets.add("hk")
            else:
                markets.add("cn")
        elif s.isdigit() and 4 <= len(s) <= 5 and src_hk:
            # 4-5 位数字 + 港股来源 → 当港股
            markets.add("hk")
        elif s.isalpha() and 1 <= len(s) <= 5:
            markets.add("us")
    src = d.get("source", "") or ""
    for hint_src, hint_mkts in _SOURCE_MARKET_HINTS.items():
        if hint_src in src:
            for m in hint_mkts:
                markets.add(m)
            break
    if d.get("is_macro_data"):
        markets.add("macro")
    return sorted(markets) if markets else ["other"]


class DatabaseManager:
    """异步 SQLite 数据库管理器，使用连接池模式。"""

    def __init__(self, db_path: str, pool_size: int = 12):
        # v12.11: pool_size 5→12，应对 ~20 个后台 loop + REST + WS 并发
        # WAL 模式允许多 reader + 1 writer，12 是个安全的并发上限
        self.db_path = db_path
        self.pool_size = pool_size
        self._pool: asyncio.Queue = asyncio.Queue(maxsize=pool_size)
        self._initialized = False
        # 已建过的 K 线表名缓存，避免每次 save/get 都跑 CREATE IF NOT EXISTS
        self._kline_table_cache: set = set()

    async def init_db(self):
        """初始化数据库：创建连接池并建表。"""
        for _ in range(self.pool_size):
            conn = await aiosqlite.connect(self.db_path)
            conn.row_factory = aiosqlite.Row
            # WAL 必须先启动
            await conn.execute("PRAGMA journal_mode=WAL")
            # synchronous=NORMAL：写入时不 fsync 到磁盘，只 fsync checkpoint 时；崩溃最多丢末尾 WAL 数据
            # 对交易日志足够安全（我们也不是金融级强一致性），写入速度 3-5x 提升
            await conn.execute("PRAGMA synchronous=NORMAL")
            # 单连接 busy 等待 30 秒（v12.13: 5s 在 K 线批量写入高峰仍出现 21k+/天 lock 错误，提到 30s）
            # 影响：写锁竞争时 SQL 调用最多阻塞 30s 而不是立刻 raise；读密集场景 WAL 仍允许并发读
            await conn.execute("PRAGMA busy_timeout=30000")
            # WAL 自动 checkpoint 阈值：1000 页 ≈ 4MB（默认已是 1000，显式设一下）
            await conn.execute("PRAGMA wal_autocheckpoint=1000")
            # page cache：20 MB（默认 2MB 偏小）。负数是 KB 单位
            await conn.execute("PRAGMA cache_size=-20000")
            # 临时表走内存，避免磁盘 IO
            await conn.execute("PRAGMA temp_store=MEMORY")
            # mmap I/O 64 MB，大表扫描更快（避免 page cache miss 时的系统调用）
            await conn.execute("PRAGMA mmap_size=67108864")
            await conn.execute("PRAGMA foreign_keys=ON")
            await self._pool.put(conn)

        async with self.acquire() as conn:
            await self._create_tables(conn)
            # 冷启动时主动 checkpoint 一次，把历史 WAL 清到主库
            try:
                await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass

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
            -- collected_at 用于数据保留策略 / simhash 查询
            CREATE INDEX IF NOT EXISTS idx_flash_collected ON flash_news (collected_at DESC);

            -- ═══ 候选池（仅股票市场，market CHECK 硬约束）═══
            CREATE TABLE IF NOT EXISTS watch_pool (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL CHECK(market IN ('us', 'hk', 'cn')),
                score REAL DEFAULT 0,               -- 综合评分 0-100 (= event+tech+fund)
                event_score REAL DEFAULT 0,         -- 事件分 0-50
                technical_score REAL DEFAULT 0,     -- 技术分 0-30
                fundamentals_score REAL DEFAULT 0,  -- 基本面分 0-20
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
            -- 老库迁移：用 PRAGMA user_version 追踪，避免每次启动都重跑（启动卡顿源）
        """)
        # 查当前 schema 版本
        cur = await conn.execute("PRAGMA user_version")
        current_version = (await cur.fetchone())[0]
        TARGET_VERSION = 21  # v21 (v12.20.9): trade_review 加 is_swap 字段（合约复盘闭环）
        if current_version < TARGET_VERSION:
            # v12.11: 用 BEGIN/COMMIT 包整个迁移；任何步骤抛错则 ROLLBACK + 不前进 user_version
            migration_ok = True
            await conn.execute("BEGIN")
            for alter in [
                "ALTER TABLE watch_pool ADD COLUMN event_score REAL DEFAULT 0",
                "ALTER TABLE watch_pool ADD COLUMN technical_score REAL DEFAULT 0",
                "ALTER TABLE watch_pool ADD COLUMN fundamentals_score REAL DEFAULT 0",
                "ALTER TABLE watch_pool ADD COLUMN reason_anomaly TEXT DEFAULT ''",
                "ALTER TABLE watch_pool ADD COLUMN reason_news TEXT DEFAULT ''",
                "ALTER TABLE watch_pool ADD COLUMN reason_ai TEXT DEFAULT ''",
                "ALTER TABLE watch_pool ADD COLUMN ai_diagnosis TEXT DEFAULT ''",
                "ALTER TABLE watch_pool ADD COLUMN ai_diagnosed_at INTEGER DEFAULT 0",
                "ALTER TABLE watch_pool ADD COLUMN archived_reason TEXT DEFAULT ''",
                "ALTER TABLE strategy_bindings ADD COLUMN interval TEXT DEFAULT '1H'",
                "ALTER TABLE signals ADD COLUMN interval TEXT DEFAULT '1H'",
                "ALTER TABLE signals ADD COLUMN ai_verdict TEXT DEFAULT ''",
                "ALTER TABLE signals ADD COLUMN ai_confidence INTEGER DEFAULT 0",
                "ALTER TABLE signals ADD COLUMN ai_reason TEXT DEFAULT ''",
                "ALTER TABLE signals ADD COLUMN ai_verified_at INTEGER DEFAULT 0",
                "ALTER TABLE signals ADD COLUMN ai_stop_loss REAL",
                "ALTER TABLE signals ADD COLUMN ai_take_profit REAL",
                "ALTER TABLE signals ADD COLUMN ai_news_ids TEXT DEFAULT ''",  # v6: JSON 数组，用于信号→新闻溯源
                "ALTER TABLE llm_cost_log ADD COLUMN path TEXT DEFAULT ''",    # v6: news|signal_verify|diagnose|position_advice
                # v8: 持仓加汇率字段（成本价按本币，同时存当时的 USD 换算成本 + 入场汇率）
                "ALTER TABLE positions ADD COLUMN cost_currency TEXT DEFAULT 'USD'",
                "ALTER TABLE positions ADD COLUMN entry_fx_rate REAL DEFAULT 1.0",
                "ALTER TABLE positions ADD COLUMN total_cost_usd REAL DEFAULT 0",
                "ALTER TABLE positions ADD COLUMN auto_traded INTEGER DEFAULT 0",
                "ALTER TABLE positions ADD COLUMN side TEXT DEFAULT 'long'",   # v9: long | short (仅加密可 short)
                "ALTER TABLE positions ADD COLUMN ai_stop_loss REAL",          # v10: 开仓时 AI 给的止损价（本币）
                "ALTER TABLE positions ADD COLUMN ai_take_profit REAL",        # v10: 开仓时 AI 给的止盈价（本币）
                # v12.5 trade_review 双评分 + pros/cons（Phase B + C）
                "ALTER TABLE trade_review ADD COLUMN decision_score INTEGER",  # 0-100 仅看决策质量（不看结果）
                "ALTER TABLE trade_review ADD COLUMN outcome_score INTEGER",   # 0-100 仅看实际收益
                "ALTER TABLE trade_review ADD COLUMN pros TEXT",               # JSON [string] 决策合理之处
                "ALTER TABLE trade_review ADD COLUMN cons TEXT",               # JSON [string] 决策不合理之处
                # v12.7 lesson_pattern 生命周期
                "ALTER TABLE lesson_pattern ADD COLUMN status TEXT DEFAULT 'active'",
                "ALTER TABLE lesson_pattern ADD COLUMN last_seen_at INTEGER",
                "ALTER TABLE lesson_pattern ADD COLUMN adopted_at INTEGER",
                # v12.8 深度分链路评估
                "ALTER TABLE trade_review ADD COLUMN link_evaluations TEXT",   # JSON {signal:{...}, ai_verify:{...}, ...}
                "ALTER TABLE trade_review ADD COLUMN primary_lesson TEXT",     # 一句话核心教训
                "ALTER TABLE trade_review ADD COLUMN what_if_better TEXT",     # 如果某环节做对结果改善多少
                # v12.11: signals 加 side 字段，明确 long / short 语义
                # 旧 sell action 在加密里既可能是"平多"也可能是"开空"，下游不区分；新版 action+side 双字段强约束
                "ALTER TABLE signals ADD COLUMN side TEXT DEFAULT 'long'",
                # v14 (v12.15) 教训采纳闭环：lesson_pattern 加评分 + 最坏单笔
                "ALTER TABLE lesson_pattern ADD COLUMN adoption_score REAL DEFAULT 0",
                "ALTER TABLE lesson_pattern ADD COLUMN worst_pnl_pct REAL DEFAULT 0",
                "ALTER TABLE lesson_pattern ADD COLUMN has_specific_params INTEGER DEFAULT 0",
                "ALTER TABLE lesson_pattern ADD COLUMN suggested_rule_type TEXT",   # LLM 翻译建议的 rule_type
                "ALTER TABLE lesson_pattern ADD COLUMN suggested_params TEXT",      # JSON：LLM 建议的参数
                # v15 (v12.16.2) 复盘策略参数分析：LLM 评估开仓策略参数合理性 + 改进建议
                "ALTER TABLE trade_review ADD COLUMN strategy_param_analysis TEXT",  # JSON
                # v16 (v12.18.0) 4 层重验机制：开市后 pending 信号需重新验证有效性
                "ALTER TABLE signals ADD COLUMN revalidated_at INTEGER DEFAULT 0",
                "ALTER TABLE signals ADD COLUMN revalidation_tier TEXT DEFAULT ''",     # gap/strategy/news/ai/pass
                "ALTER TABLE signals ADD COLUMN revalidation_reason TEXT DEFAULT ''",
                "ALTER TABLE signals ADD COLUMN original_ai_verdict TEXT DEFAULT ''",   # 重验前备份
                "ALTER TABLE signals ADD COLUMN original_ai_confidence INTEGER DEFAULT 0",
                # v17 (v12.19.1) AI 重验失败次数 — 防 LLM 持续故障导致的无限循环
                "ALTER TABLE signals ADD COLUMN revalidation_count INTEGER DEFAULT 0",
                # v18 (v12.19.6) 动态 SL 闭环 — break-even 阶段标记
                "ALTER TABLE position_state ADD COLUMN breakeven_armed INTEGER DEFAULT 0",
                # v19 (v12.20.0) 加密合约模拟引擎 — 真 OKX 数据 + 模拟资金下单
                # 完全独立于现货 mock (positions 表), 用 config.CRYPTO_TRADING_MODE 切换
                """CREATE TABLE IF NOT EXISTS swap_orders (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,                -- BTC-USDT-SWAP
                    side TEXT NOT NULL,                  -- buy / sell (开仓/平仓方向)
                    pos_side TEXT NOT NULL,              -- long / short (双向持仓)
                    order_type TEXT NOT NULL,            -- limit / market
                    price REAL,                          -- 限价单价格 (NULL for market)
                    qty REAL NOT NULL,                   -- 张数
                    leverage INTEGER NOT NULL,
                    margin_usd REAL NOT NULL,            -- 占用的逐仓保证金
                    status TEXT DEFAULT 'pending',       -- pending/filled/partial/cancelled/rejected
                    fill_price REAL DEFAULT 0,
                    fill_qty REAL DEFAULT 0,
                    fee_usd REAL DEFAULT 0,              -- 实际手续费
                    is_maker INTEGER DEFAULT 0,          -- 1=maker(0.02%) 0=taker(0.05%)
                    slippage_pct REAL DEFAULT 0,         -- 实际滑点 (市价单)
                    reject_reason TEXT DEFAULT '',
                    signal_id TEXT,                      -- 关联触发的信号
                    position_id TEXT,                    -- 关联的 swap_positions.id
                    intent TEXT DEFAULT 'open',          -- open/close/reduce/add (语义标签)
                    created_at INTEGER NOT NULL,
                    filled_at INTEGER DEFAULT 0,
                    expire_at INTEGER NOT NULL           -- 限价单 60min 超时
                )""",
                "CREATE INDEX IF NOT EXISTS idx_swap_orders_status ON swap_orders(status, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_swap_orders_pos ON swap_orders(position_id, created_at DESC)",
                """CREATE TABLE IF NOT EXISTS swap_positions (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    pos_side TEXT NOT NULL,              -- long / short
                    qty REAL NOT NULL,                   -- 当前持仓张数 (减仓后会减小)
                    avg_open_price REAL NOT NULL,        -- 加仓后的加权均价
                    leverage INTEGER NOT NULL,
                    margin_usd REAL NOT NULL,            -- 当前占用保证金
                    liq_price REAL,                      -- 强平价 (实时计算)
                    contract_size REAL NOT NULL,         -- 合约面值 (e.g. 0.01)
                    unrealized_pnl_usd REAL DEFAULT 0,   -- 未实现 PnL (mark_to_market)
                    realized_pnl_usd REAL DEFAULT 0,     -- 已实现 PnL (减仓累积)
                    funding_fee_total_usd REAL DEFAULT 0, -- 累积资金费率扣费
                    total_fee_usd REAL DEFAULT 0,        -- 累积手续费
                    pre_liq_armed INTEGER DEFAULT 0,     -- 距强平 < 3% 减仓 50% 已触发标记
                    last_funding_at INTEGER DEFAULT 0,   -- 上次结算 funding 时间
                    opened_at INTEGER NOT NULL,
                    closed_at INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'open'           -- open/closed/liquidated
                    -- v12.20.5 Bug 14: 不能用 UNIQUE(symbol, pos_side) — 平仓后再开同向会撞
                    -- 改为 partial unique index 只约束 status='open' 的仓位 (见下方)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_swap_positions_open ON swap_positions(status, symbol)",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_swap_positions_unique_open ON swap_positions(symbol, pos_side) WHERE status='open'",
                """CREATE TABLE IF NOT EXISTS swap_account (
                    id INTEGER PRIMARY KEY,
                    balance_usd REAL NOT NULL,           -- 可用余额
                    initial_balance_usd REAL NOT NULL,   -- 初始资金
                    total_margin_usd REAL DEFAULT 0,     -- 占用保证金合计
                    total_pnl_usd REAL DEFAULT 0,        -- 累计已实现盈亏
                    updated_at INTEGER NOT NULL
                )""",
                # v20 (v12.20.6) swap_positions 加动态 SL/TP 闭环字段
                "ALTER TABLE swap_positions ADD COLUMN stop_loss REAL",
                "ALTER TABLE swap_positions ADD COLUMN take_profit REAL",
                "ALTER TABLE swap_positions ADD COLUMN breakeven_armed INTEGER DEFAULT 0",
                "ALTER TABLE swap_positions ADD COLUMN trailing_armed INTEGER DEFAULT 0",
                "ALTER TABLE swap_positions ADD COLUMN peak_price REAL",
                "ALTER TABLE swap_positions ADD COLUMN peak_pnl_pct REAL DEFAULT 0",
                "ALTER TABLE swap_positions ADD COLUMN tp1_hit INTEGER DEFAULT 0",
                "ALTER TABLE swap_positions ADD COLUMN tp2_hit INTEGER DEFAULT 0",
                # v21 (v12.20.9) trade_review 加 is_swap 标记区分现货/合约复盘
                "ALTER TABLE trade_review ADD COLUMN is_swap INTEGER DEFAULT 0",
                "ALTER TABLE trade_review ADD COLUMN swap_pos_side TEXT DEFAULT ''",   # long/short
                "ALTER TABLE trade_review ADD COLUMN swap_leverage INTEGER DEFAULT 0",
                "ALTER TABLE trade_review ADD COLUMN swap_funding_total REAL DEFAULT 0",
                "ALTER TABLE trade_review ADD COLUMN swap_total_fee REAL DEFAULT 0",
                "ALTER TABLE trade_review ADD COLUMN swap_liquidated INTEGER DEFAULT 0",
            ]:
                try:
                    await conn.execute(alter)
                except aiosqlite.OperationalError as e:
                    # "duplicate column name" 是预期（迁移已部分完成时）；其它错误是真异常
                    msg = str(e).lower()
                    if "duplicate column" not in msg and "already exists" not in msg:
                        logging.getLogger(__name__).error(f"v13 ALTER 失败 ({alter[:60]}...): {e}")
                        migration_ok = False
                        break
            # v13 (v12.11): 把旧 type 名归一到 v12.8 9 环节命名；冲突时按权重合并 occurrences/avg_pnl
            # 旧 → 新 映射（仅迁移有明确等价的；psychology / general 留原名）
            type_migration = {
                "entry":  "entry_timing",
                "exit":   "exit_quality",
                "risk":   "sl_tp_setup",
            }
            try:
                for old_t, new_t in type_migration.items():
                    # 取所有旧 type 行
                    cur_old = await conn.execute(
                        "SELECT id, pool_id, pattern, full_text, occurrences, avg_pnl_pct, "
                        "sample_position_ids, status, last_seen_at, adopted_at, last_updated "
                        "FROM lesson_pattern WHERE type=?", (old_t,)
                    )
                    olds = await cur_old.fetchall()
                    for r in olds:
                        # 查同 pool/pattern 的新 type 行
                        cur_new = await conn.execute(
                            "SELECT id, occurrences, avg_pnl_pct, last_seen_at, status, adopted_at "
                            "FROM lesson_pattern WHERE pool_id=? AND type=? AND pattern=?",
                            (r["pool_id"], new_t, r["pattern"])
                        )
                        existing = await cur_new.fetchone()
                        if existing is None:
                            # 直接改名
                            await conn.execute(
                                "UPDATE lesson_pattern SET type=? WHERE id=?",
                                (new_t, r["id"])
                            )
                        else:
                            # 合并：occurrences 求和，avg_pnl_pct 加权平均，last_seen_at 取最大；
                            # status 优先级 adopted > active > expired > disabled（保留更"活"的状态）
                            STATUS_RANK = {"adopted": 3, "active": 2, "expired": 1, "disabled": 0}
                            old_occ = int(r["occurrences"] or 0)
                            new_occ = int(existing["occurrences"] or 0)
                            sum_occ = max(old_occ + new_occ, 1)
                            merged_pnl = (
                                (float(r["avg_pnl_pct"] or 0) * old_occ +
                                 float(existing["avg_pnl_pct"] or 0) * new_occ) / sum_occ
                            )
                            merged_last = max(int(r["last_seen_at"] or 0), int(existing["last_seen_at"] or 0))
                            old_status = r["status"] or "active"
                            new_status = existing["status"] or "active"
                            merged_status = (old_status if STATUS_RANK.get(old_status, 0)
                                             > STATUS_RANK.get(new_status, 0) else new_status)
                            merged_adopted = r["adopted_at"] if old_status == "adopted" else existing["adopted_at"]
                            await conn.execute(
                                "UPDATE lesson_pattern SET occurrences=?, avg_pnl_pct=?, "
                                "last_seen_at=?, status=?, adopted_at=?, last_updated=? WHERE id=?",
                                (sum_occ, merged_pnl, merged_last, merged_status,
                                 merged_adopted, int(time.time()), existing["id"])
                            )
                            await conn.execute("DELETE FROM lesson_pattern WHERE id=?", (r["id"],))
                logging.getLogger(__name__).info(f"v13 lesson_pattern type 归一化完成（{list(type_migration.keys())} → {list(type_migration.values())}）")
            except Exception as e:
                logging.getLogger(__name__).error(f"v13 type 归一化失败: {e}")
                migration_ok = False
            # 防御性回填：v8/v9 ALTER 后的旧行若因历史原因残留 NULL，统一补默认值
            if migration_ok:
                try:
                    await conn.execute("UPDATE positions SET side='long' WHERE side IS NULL OR side=''")
                    await conn.execute("UPDATE positions SET cost_currency='USD' WHERE cost_currency IS NULL OR cost_currency=''")
                    await conn.execute("UPDATE positions SET entry_fx_rate=1.0 WHERE entry_fx_rate IS NULL OR entry_fx_rate<=0")
                    await conn.execute("UPDATE positions SET auto_traded=0 WHERE auto_traded IS NULL")
                except Exception as e:
                    logging.getLogger(__name__).warning(f"v13 防御性回填 NULL 失败: {e}")
                    # 回填非致命，不阻塞迁移
            if migration_ok:
                await conn.execute(f"PRAGMA user_version = {TARGET_VERSION}")
                await conn.execute("COMMIT")
                logging.getLogger(__name__).info(f"DB 迁移完成 v{current_version} → v{TARGET_VERSION}")
            else:
                await conn.execute("ROLLBACK")
                logging.getLogger(__name__).error(f"DB 迁移失败 v{current_version} → v{TARGET_VERSION}：已 ROLLBACK，下次启动会重试")
        await conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_pool_status ON watch_pool (status);
            CREATE INDEX IF NOT EXISTS idx_pool_score ON watch_pool (score DESC);
            -- _pool_diagnose_loop 扫 status + ai_diagnosed_at
            CREATE INDEX IF NOT EXISTS idx_pool_diag ON watch_pool (status, ai_diagnosed_at);

            -- ═══ 候选池评分历史（用于复盘和趋势分析）═══
            CREATE TABLE IF NOT EXISTS pool_score_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_item_id TEXT NOT NULL,
                score REAL NOT NULL,
                factors TEXT,                        -- JSON: 评分分量详情
                scored_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_score_hist_item ON pool_score_history (pool_item_id, scored_at DESC);

            -- ═══ AI 诊断历史（每次诊断追加一条，用于对比和复盘）═══
            CREATE TABLE IF NOT EXISTS ai_diagnosis_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                diagnosis TEXT NOT NULL,             -- JSON 全文
                rating TEXT,                         -- 抽出来便于查询
                confidence INTEGER,
                diagnosed_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_diag_hist_symbol ON ai_diagnosis_history (symbol, market, diagnosed_at DESC);

            -- ═══ 加密诊断（独立表，因为加密不走 watch_pool）═══
            CREATE TABLE IF NOT EXISTS crypto_diagnosis (
                symbol TEXT PRIMARY KEY,             -- BTC-USDT / ETH-USDT ...
                diagnosis TEXT NOT NULL,             -- JSON: 完整诊断结果
                rating TEXT,                         -- strong_buy|buy|hold|reduce|sell
                confidence INTEGER,
                price REAL,                          -- 诊断时的价格
                diagnosed_at INTEGER NOT NULL
            );

            -- ═══ 加密数据快照（缓存 1 小时内的市场数据，减少重复 API 调用）═══
            CREATE TABLE IF NOT EXISTS crypto_insights_snapshot (
                symbol TEXT PRIMARY KEY,
                payload TEXT NOT NULL,               -- JSON: 融合后的完整数据（funding/oi/ratio/ticker/...）
                updated_at INTEGER NOT NULL
            );

            -- ═══ 自动交易决策日志（所有自动开/加/减/清仓操作的审计）═══
            CREATE TABLE IF NOT EXISTS auto_trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                action TEXT NOT NULL,                -- open | add | reduce | close
                quantity REAL NOT NULL,              -- 本次操作数量（本币单位）
                price REAL NOT NULL,                 -- 本币价格
                amount_usd REAL NOT NULL,            -- 本次换算为 USD 的金额
                fx_rate REAL NOT NULL,               -- 使用的汇率
                trigger_type TEXT NOT NULL,          -- signal_confirm | rating_change | tp_hit | sl_hit | exit_trigger
                trigger_detail TEXT,                 -- JSON: 触发事件详情（signal_id / rating 变化 / 命中规则等）
                reason TEXT,                         -- 人类可读理由
                position_id TEXT,                    -- 关联的 positions.id (open 时新建)
                remaining_qty REAL,                  -- 操作后剩余仓位
                remaining_cash_usd REAL,             -- 操作后账户现金
                status TEXT DEFAULT 'executed',      -- executed | rejected | pending
                rejected_reason TEXT,
                traded_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_auto_trade_time ON auto_trade_log (traded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_auto_trade_symbol ON auto_trade_log (symbol, market, traded_at DESC);
            -- 按 position_id 查该单全部 leg / trades-by-position API 用
            CREATE INDEX IF NOT EXISTS idx_auto_trade_pos ON auto_trade_log (position_id);

            -- ═══ 自动交易账户状态（单行，记录现金/总资产）═══
            CREATE TABLE IF NOT EXISTS auto_trade_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                initial_capital_usd REAL NOT NULL,
                cash_usd REAL NOT NULL,              -- 当前可用现金（汇总用，已被 auto_trade_pool 取代但保留兼容）
                updated_at INTEGER NOT NULL
            );

            -- ═══ v12.3 深度交易复盘（LLM 多段分析）═══
            -- 含完整快照：入场/中期/出场上下文 + 关键转折点 + 改进建议
            CREATE TABLE IF NOT EXISTS trade_review (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                pool_id TEXT,
                side TEXT NOT NULL,
                open_price REAL,
                close_price REAL,
                open_at INTEGER,
                close_at INTEGER,
                hold_hours REAL,
                realized_pnl_local REAL,
                realized_pnl_pct REAL,
                period_high REAL,
                period_low REAL,
                best_exit_price REAL,
                missed_profit_pct REAL,
                -- LLM 深度输出
                score INTEGER,                           -- 0-100
                grade TEXT,                              -- A / B / C / D
                entry_analysis TEXT,                     -- 入场质量分析
                mid_analysis TEXT,                       -- 持仓管理分析
                exit_analysis TEXT,                      -- 出场质量分析
                turning_points TEXT,                     -- JSON: [{time, price, event, ai_note}]
                improvements TEXT,                       -- 改进建议（多段）
                lessons TEXT,                            -- JSON [{type, content}]
                -- 元数据
                snapshot_json TEXT,                      -- 输入给 LLM 的完整快照（debug 用）
                llm_model TEXT,
                llm_tokens INTEGER,
                reviewed_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_review_close ON trade_review (close_at DESC);
            CREATE INDEX IF NOT EXISTS idx_review_pool ON trade_review (pool_id, close_at DESC);
            CREATE INDEX IF NOT EXISTS idx_review_grade ON trade_review (grade);

            -- v12.5 教训聚合（Phase A 反馈闭环）+ v12.7 生命周期
            -- 由 reviewer.aggregate_lessons 定期 UPSERT — verify/diagnose prompt 注入时只用 active+adopted
            CREATE TABLE IF NOT EXISTS lesson_pattern (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_id TEXT NOT NULL,                   -- us_hk / cn / crypto / all
                type TEXT NOT NULL,                      -- entry / exit / risk / psychology
                pattern TEXT NOT NULL,                   -- 教训描述前 60 字符（去重 key）
                full_text TEXT,                          -- 完整教训文本
                occurrences INTEGER DEFAULT 0,
                avg_pnl_pct REAL DEFAULT 0,
                sample_position_ids TEXT,                -- JSON [pid] 最近 5 个例
                status TEXT DEFAULT 'active',            -- active / adopted / disabled / expired
                last_seen_at INTEGER,                    -- 最后一次在新 review 中出现的 ts（用于自动过期判定）
                adopted_at INTEGER,                      -- 用户采纳时间
                last_updated INTEGER NOT NULL,
                UNIQUE(pool_id, type, pattern)
            );
            CREATE INDEX IF NOT EXISTS idx_lesson_freq ON lesson_pattern (pool_id, occurrences DESC);
            CREATE INDEX IF NOT EXISTS idx_lesson_status ON lesson_pattern (status);

            -- v12.15 教训采纳闭环：风控规则表
            -- lesson_pattern.adoption_score >= 阈值 + 可参数化 → 写入 risk_rules
            -- fast_path / simplified_verify / _handle_signal 入口处统一执行
            CREATE TABLE IF NOT EXISTS risk_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_type TEXT NOT NULL,                 -- rsi_block / drawdown_force_close / trend_block / cooldown_override / prompt_principle
                pool_id TEXT NOT NULL DEFAULT 'all',     -- 作用域：us_hk / cn / crypto / all
                params TEXT NOT NULL,                    -- JSON 参数 — 各 rule_type 自定义结构
                source_lesson_id INTEGER,                -- 关联的教训 id（manual 创建时为 NULL）
                source_kind TEXT DEFAULT 'manual',       -- auto_adopted / user_adopted / manual / migrated
                description TEXT,                        -- 给用户看的"这条规则在做什么"
                enabled INTEGER DEFAULT 1,
                hits INTEGER DEFAULT 0,                  -- 命中次数（拦截了多少信号/触发了多少强平）
                false_reject_count INTEGER DEFAULT 0,    -- 拦截后事后回查 K 线"如果不拦会赢"的次数
                last_hit_at INTEGER,                     -- 最后一次命中时间
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rule_enabled ON risk_rules (enabled, pool_id);
            CREATE INDEX IF NOT EXISTS idx_rule_lesson ON risk_rules (source_lesson_id);

            -- v12.15 规则命中明细（事后回查用）
            -- 每次规则命中（reject 信号 / 强平等动作）写一行；后台 worker 回查 K 线判定 false_reject
            CREATE TABLE IF NOT EXISTS risk_rule_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                signal_id TEXT,                          -- 拦下的信号 id
                position_id TEXT,                        -- 强平的持仓 id
                action TEXT NOT NULL,                    -- reject_signal / force_close / force_reduce
                price_at_hit REAL,                       -- 命中时刻价格
                hit_at INTEGER NOT NULL,
                false_reject INTEGER,                    -- NULL=待回查; 0=拦得对; 1=假阳（事后涨了）
                reviewed_at INTEGER                      -- false_reject 字段填充时间
            );
            CREATE INDEX IF NOT EXISTS idx_rule_hits_rule ON risk_rule_hits (rule_id, hit_at DESC);
            CREATE INDEX IF NOT EXISTS idx_rule_hits_pending ON risk_rule_hits (false_reject, hit_at);

            -- ═══ v12.3 周报聚合（LLM 看本周所有 reviews 出总结）═══
            CREATE TABLE IF NOT EXISTS trade_review_weekly (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start INTEGER NOT NULL UNIQUE,      -- 周一 00:00 UTC ts
                week_end INTEGER NOT NULL,
                trades_count INTEGER,
                wins INTEGER,
                losses INTEGER,
                win_rate REAL,
                total_pnl_usd REAL,
                avg_grade TEXT,
                summary TEXT,                            -- LLM 综合评估
                top_wins TEXT,                           -- JSON [position_id]
                top_losses TEXT,                         -- JSON [position_id]
                recurring_mistakes TEXT,                 -- JSON [string]
                actionable_changes TEXT,                 -- JSON [string]
                generated_at INTEGER NOT NULL
            );

            -- ═══ v12.0 自动交易资金池 ═══
            -- 3 池独立：港美股(USD) / A股(CNY) / 加密(USD)
            -- cash 字段以本币（local currency）存储，避免汇率波动改变现金值
            CREATE TABLE IF NOT EXISTS auto_trade_pool (
                pool_id TEXT PRIMARY KEY,            -- 'us_hk' | 'cn' | 'crypto'
                name TEXT NOT NULL,                  -- '港美股' | 'A股' | '加密货币'
                currency TEXT NOT NULL,              -- 'USD' | 'CNY' | 'USD'
                initial_capital REAL NOT NULL,       -- 初始资金（本币）
                cash REAL NOT NULL,                  -- 当前可用现金（本币）
                updated_at INTEGER NOT NULL
            );

            -- ═══ 持仓状态机（v11: TP/SL 巡检 + 分批止盈 + 跟踪止损）═══
            -- 每个 positions.id 一行，open 时插入，close 时随 position 一起 DELETE
            CREATE TABLE IF NOT EXISTS position_state (
                position_id TEXT PRIMARY KEY,
                peak_price REAL,                     -- 持仓期间价格峰值（多仓:max / 空仓:min）
                peak_pnl_pct REAL DEFAULT 0,         -- 持仓期间最大浮盈 %
                tp1_hit INTEGER DEFAULT 0,           -- 1/3 路径分批止盈已触发
                tp2_hit INTEGER DEFAULT 0,           -- 2/3 路径分批止盈已触发
                trailing_armed INTEGER DEFAULT 0,    -- 跟踪止损已激活（peak_pnl_pct ≥ 15%）
                last_check_at INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            );

            -- ═══ 汇率快照（避免每次下单都请求外部 API）═══
            CREATE TABLE IF NOT EXISTS fx_rates (
                currency TEXT PRIMARY KEY,           -- HKD / CNY / USD
                rate_to_usd REAL NOT NULL,           -- 1 unit of currency = rate_to_usd USD
                updated_at INTEGER NOT NULL
            );

            -- ═══ 策略绑定（多对多）═══
            CREATE TABLE IF NOT EXISTS strategy_bindings (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                interval TEXT NOT NULL DEFAULT '1H',  -- 监控周期（1H / 4H / 1D 等）
                params TEXT DEFAULT '{}',
                enabled INTEGER DEFAULT 1,
                created_at INTEGER NOT NULL,
                UNIQUE(symbol, market, strategy_name, interval)
            );
            CREATE INDEX IF NOT EXISTS idx_binding_symbol ON strategy_bindings (symbol, market);

            -- ═══ 策略信号 ═══
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                action TEXT NOT NULL,                -- buy | sell
                strategy_name TEXT NOT NULL,
                interval TEXT DEFAULT '1H',          -- 触发周期（1H / 4H / 1D 等）
                confidence INTEGER DEFAULT 0,        -- 0-100
                price REAL,
                suggested_qty REAL,
                stop_loss REAL,
                take_profit REAL,
                reason TEXT DEFAULT '',
                triggered_by TEXT DEFAULT '{}',      -- JSON: 触发来源详情
                status TEXT DEFAULT 'active',        -- active | expired | acted
                generated_at INTEGER NOT NULL,
                expires_at INTEGER,
                ai_verdict TEXT DEFAULT '',          -- approve | reject | unknown
                ai_confidence INTEGER DEFAULT 0,
                ai_reason TEXT DEFAULT '',
                ai_verified_at INTEGER DEFAULT 0,
                ai_stop_loss REAL,
                ai_take_profit REAL,
                ai_news_ids TEXT DEFAULT '',
                side TEXT DEFAULT 'long'             -- v12.11: long / short（仅加密可 short）
            );
            CREATE INDEX IF NOT EXISTS idx_signals_time ON signals (generated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals (symbol, market);
            -- 验证补全循环专用：WHERE ai_verdict='' AND symbol=? AND market=? ORDER BY generated_at DESC
            CREATE INDEX IF NOT EXISTS idx_signals_verify ON signals (symbol, market, ai_verdict, generated_at DESC);

            -- ═══ 持仓 ═══
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                quantity REAL NOT NULL,
                avg_cost REAL NOT NULL,
                opened_at INTEGER NOT NULL,
                notes TEXT DEFAULT '',
                cost_currency TEXT DEFAULT 'USD',
                entry_fx_rate REAL DEFAULT 1.0,
                total_cost_usd REAL DEFAULT 0,
                auto_traded INTEGER DEFAULT 0,
                side TEXT DEFAULT 'long',
                ai_stop_loss REAL,
                ai_take_profit REAL,
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
            -- 按持仓查最新建议：GROUP BY position_id MAX(advised_at)
            CREATE INDEX IF NOT EXISTS idx_advices_pos ON position_advices (position_id, advised_at DESC);

            -- ═══ LLM 调用成本追踪（日预算控制用）═══
            CREATE TABLE IF NOT EXISTS llm_cost_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                called_at INTEGER NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL NOT NULL,
                news_id TEXT,                        -- 关联的新闻 ID（可选）
                path TEXT DEFAULT ''                 -- news | signal_verify | diagnose | position_advice
            );
            CREATE INDEX IF NOT EXISTS idx_llm_cost_time ON llm_cost_log (called_at DESC);

            -- ═══ 股票基本面缓存（候选池质量筛选用，24h TTL）═══
            CREATE TABLE IF NOT EXISTS symbol_fundamentals (
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT DEFAULT '',
                price REAL,
                market_cap REAL,            -- 本币市值（CN 为流通市值；HK/US 为总市值）
                avg_turnover REAL,          -- 日均成交额（本币）
                avg_volume REAL,            -- 日均成交量（股）— 美股用
                listed_days INTEGER,        -- 上市天数（仅 CN 查询）
                is_st INTEGER DEFAULT 0,    -- 是否 ST/*ST
                is_gem INTEGER DEFAULT 0,   -- 是否港股 GEM 创业板
                is_otc INTEGER DEFAULT 0,   -- 是否美股 OTC
                pe REAL DEFAULT 0,          -- 市盈率 TTM
                pb REAL DEFAULT 0,          -- 市净率
                turnover_rate REAL DEFAULT 0, -- 换手率 %
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (symbol, market)
            );
            CREATE INDEX IF NOT EXISTS idx_fund_updated ON symbol_fundamentals (updated_at DESC);

            -- ═══ 严格筛选拒绝队列（数据拉取失败暂存，后台 30min 重试）═══
            CREATE TABLE IF NOT EXISTS pool_pending_review (
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                source TEXT DEFAULT '',
                score REAL DEFAULT 0,
                reason TEXT DEFAULT '',
                first_attempt_at INTEGER NOT NULL,
                last_attempt_at INTEGER NOT NULL,
                attempts INTEGER DEFAULT 1,
                PRIMARY KEY (symbol, market)
            );
            CREATE INDEX IF NOT EXISTS idx_pending_last ON pool_pending_review (last_attempt_at);
        """)
        # 老库迁移：为 symbol_fundamentals 补列
        for alter in [
            "ALTER TABLE symbol_fundamentals ADD COLUMN pe REAL DEFAULT 0",
            "ALTER TABLE symbol_fundamentals ADD COLUMN pb REAL DEFAULT 0",
            "ALTER TABLE symbol_fundamentals ADD COLUMN turnover_rate REAL DEFAULT 0",
        ]:
            try:
                await conn.execute(alter)
            except Exception:
                pass
        await conn.commit()

    async def _ensure_kline_table(self, conn: aiosqlite.Connection, market: str, interval: str):
        """动态创建 K线表（按市场和周期），带复合主键和索引。
        进程级缓存已建表名，避免每次 save/get 都跑 CREATE IF NOT EXISTS（高频 DDL 是后端瓶颈）。
        """
        table = f"klines_{market}_{interval}".lower().replace("-", "_")
        if table in self._kline_table_cache:
            return table
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
        self._kline_table_cache.add(table)
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
                    volume=excluded.volume,
                    turnover=CASE WHEN excluded.turnover > 0 THEN excluded.turnover
                                  ELSE [{table}].turnover END
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
        order_desc: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        查询 K 线数据，支持时间范围和条数限制。
        order_desc=True 时按 timestamp 降序返回（用于"拿 end_ts 之前最近 N 根"）。
        """
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
            if order_desc:
                sql += " ORDER BY timestamp DESC LIMIT ?"
            else:
                sql += " ORDER BY timestamp ASC LIMIT ?"
            params.append(limit)

            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_kline_range(
        self, market: str, interval: str, symbol: str
    ) -> Optional[Dict[str, int]]:
        """
        获取某品种在 DB 中已有 K 线的时间范围和条数。
        返回 {"min_ts", "max_ts", "count"} 或 None（无数据）。
        缓存层用于判断是否需要向上游拉增量。
        """
        async with self.acquire() as conn:
            table = await self._ensure_kline_table(conn, market, interval)
            cursor = await conn.execute(
                f"SELECT MIN(timestamp) as min_ts, MAX(timestamp) as max_ts, COUNT(*) as cnt "
                f"FROM [{table}] WHERE symbol = ?",
                (symbol,),
            )
            row = await cursor.fetchone()
            if not row or row["cnt"] == 0:
                return None
            return {"min_ts": row["min_ts"], "max_ts": row["max_ts"], "count": row["cnt"]}

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

    # ──────────────────────── Flash News (Phase 3A) ────────────────────────

    async def save_flash_news(self, news: Dict[str, Any]) -> bool:
        """
        保存一条新闻快讯。news 必须包含: id, title, source, url, published_at。
        其他字段可选。返回 True=新插入，False=已存在（去重命中）。
        """
        async with self.acquire() as conn:
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO flash_news (
                    id, title, content, source, url,
                    published_at, collected_at,
                    importance, sentiment, categories, impact_tags, keywords,
                    is_holding_related, l2_score,
                    impact_on_crypto,
                    is_macro_data, macro_type, macro_actual, macro_forecast, macro_previous,
                    macro_deviation_pct, macro_impact_strength,
                    ai_analysis, event_id, content_hash, simhash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    news["id"],
                    news["title"],
                    news.get("content", ""),
                    news["source"],
                    news.get("url", ""),
                    news["published_at"],
                    news.get("collected_at", int(time.time() * 1000)),
                    news.get("importance", 1),
                    news.get("sentiment", "neutral"),
                    json.dumps(news.get("categories", [])),
                    json.dumps(news.get("impact_tags", [])),
                    json.dumps(news.get("keywords", [])),
                    1 if news.get("is_holding_related") else 0,
                    news.get("l2_score", 0.0),
                    json.dumps(news["impact_on_crypto"]) if news.get("impact_on_crypto") else None,
                    1 if news.get("is_macro_data") else 0,
                    news.get("macro_type", ""),
                    news.get("macro_actual"),
                    news.get("macro_forecast"),
                    news.get("macro_previous"),
                    news.get("macro_deviation_pct"),
                    news.get("macro_impact_strength", ""),
                    json.dumps(news["ai_analysis"]) if news.get("ai_analysis") else None,
                    news.get("event_id"),
                    news.get("content_hash"),
                    news.get("simhash"),
                ),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def get_flash_news(
        self,
        market: Optional[str] = None,
        importance_min: int = 1,
        limit: int = 50,
        offset: int = 0,
        keyword: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> List[Dict]:
        """
        查询新闻快讯，按发布时间倒序。
        market: 过滤新闻所属市场（crypto/us/hk/cn/macro）。
        keyword: 标题/正文模糊搜索。
        symbol: 在 categories 字段里搜（精确）。
        """
        async with self.acquire() as conn:
            sql = "SELECT * FROM flash_news WHERE importance >= ?"
            params: list = [importance_min]
            if keyword:
                sql += " AND (title LIKE ? OR content LIKE ?)"
                like = f"%{keyword}%"
                params += [like, like]
            if symbol:
                sql += " AND categories LIKE ?"
                params.append(f'%"{symbol}"%')
            # v12.19.2: market filter 推前到 SQL — 之前后置过滤 + LIMIT 200 导致大市场 (SEC EDGAR)
            # 占满前 200 条, 小市场 (crypto) 几乎过滤不到
            # 现在按 market 反查所有归属 source → SQL `source IN (...)` 直接过滤
            if market:
                src_list = _MARKET_TO_SOURCES.get(market, [])
                if src_list:
                    placeholders = ",".join("?" for _ in src_list)
                    sql += f" AND source IN ({placeholders})"
                    params.extend(src_list)
                # macro 同时也来自 is_macro_data 标记
                if market == "macro":
                    # 用 OR 重新拼: (source IN macro_srcs) OR (is_macro_data=1)
                    sql = sql.replace(
                        f"AND source IN ({placeholders})",
                        f"AND (source IN ({placeholders}) OR is_macro_data=1)"
                    )
            sql += " ORDER BY published_at DESC LIMIT ? OFFSET ?"
            params += [limit, offset]
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                for k in ("categories", "impact_tags", "keywords", "impact_on_crypto", "ai_analysis"):
                    if d.get(k):
                        try:
                            d[k] = json.loads(d[k])
                        except (json.JSONDecodeError, TypeError):
                            pass
                d["markets"] = _infer_news_markets(d)
                results.append(d)
            # v12.19.2: 后置过滤改为 fallback 校验 (SQL 已主过滤)
            # 处理 categories 推断出非 source 默认 market 的情况 (e.g. CNBC 文章 cats=["BTC-USDT"] 也算 crypto)
            # 但只有当 SQL 没过滤过 source 才做后置 (避免重复)
            if market and not _MARKET_TO_SOURCES.get(market):
                results = [d for d in results if market in (d.get("markets") or [])]
            return results

    async def get_flash_news_by_id(self, news_id: str) -> Optional[Dict]:
        async with self.acquire() as conn:
            cursor = await conn.execute("SELECT * FROM flash_news WHERE id = ?", (news_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            d = dict(row)
            for k in ("categories", "impact_tags", "keywords", "impact_on_crypto", "ai_analysis"):
                if d.get(k):
                    try:
                        d[k] = json.loads(d[k])
                    except (json.JSONDecodeError, TypeError):
                        pass
            d["markets"] = _infer_news_markets(d)
            return d

    async def is_news_duplicate(self, content_hash: str) -> bool:
        """根据 content_hash 检查新闻是否已存在（去重用）。"""
        async with self.acquire() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM flash_news WHERE content_hash = ? LIMIT 1",
                (content_hash,),
            )
            row = await cursor.fetchone()
            return row is not None

    async def find_similar_simhashes(self, window_hours: int = 24, limit: int = 500) -> list:
        """返回最近 N 小时内所有非空 simhash（供 SimHash 近重复检测用）。"""
        import time as _t
        cutoff = int(_t.time() * 1000) - window_hours * 3600 * 1000
        async with self.acquire() as conn:
            cur = await conn.execute(
                "SELECT simhash FROM flash_news WHERE simhash IS NOT NULL AND simhash != '' "
                "AND collected_at >= ? ORDER BY collected_at DESC LIMIT ?",
                (cutoff, limit),
            )
            rows = await cur.fetchall()
            return [r["simhash"] for r in rows]

    # ──────────────────────── Watch Pool (Phase 3A) ────────────────────────

    async def add_to_pool(
        self,
        symbol: str,
        market: str,
        source: str = "manual",
        score: float = 50.0,
        reason: str = "",
    ) -> str:
        """
        向候选池添加品种。market 必须为 us/hk/cn（DB CHECK 约束保证不接受 crypto）。
        返回 pool_item_id。重复添加返回已有 id。
        """
        if market not in ("us", "hk", "cn"):
            raise ValueError(f"候选池只支持股票市场（us/hk/cn），不支持: {market}")
        # 防御 NaN/inf/超出范围的 score
        try:
            score = float(score)
            if score != score or score == float('inf') or score == float('-inf'):
                score = 50.0
            score = max(0.0, min(100.0, score))
        except (TypeError, ValueError):
            score = 50.0

        async with self.acquire() as conn:
            now = int(time.time())
            item_id = str(uuid.uuid4())
            news_src = source in ("news", "news_ai")
            mention_ts = now if news_src else None
            # UPSERT 语义：
            #   score: 取 max（不降级）
            #   source: 保留首次来源（不覆盖，除非原为 manual 且新为 news/news_ai 更精确）
            #   reason: 最新理由前置追加并截断（保留足迹）
            #   last_scored_at: 总是刷新
            #   last_news_mention_at: 仅 news/news_ai 触发才刷新
            # 按 source 选择独立的 reason 字段（避免不同来源理由混拼）
            reason_anomaly = reason if source == "anomaly" else ""
            reason_news = reason if source == "news" else ""
            reason_ai = reason if source == "news_ai" else ""
            # v12.15.4: 手动添加直接设 monitoring（用户明确认可，不走"待观察"通道）
            initial_status = "monitoring" if source == "manual" else "candidate"
            cursor = await conn.execute(
                f"""
                INSERT INTO watch_pool (
                    id, symbol, market, score, event_score, status, source, reason,
                    reason_anomaly, reason_news, reason_ai,
                    added_at, last_scored_at, last_news_mention_at
                ) VALUES (?, ?, ?, ?, ?, '{initial_status}', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, market) DO UPDATE SET
                    -- score 取 max 保证不降级；event_score 跟随 score 同步（二者必须一致方向）
                    event_score = MAX(excluded.event_score, watch_pool.event_score),
                    score = MAX(excluded.score, watch_pool.score),
                    -- source 优先级：manual (最高) > news/news_ai/macro_theme > anomaly
                    --   手动添加总是升级为 manual（免自动淘汰）
                    --   新闻/AI 覆盖 anomaly（明确信号 vs 被动异动）
                    source = CASE
                        WHEN excluded.source = 'manual' THEN 'manual'
                        WHEN watch_pool.source = 'anomaly' AND excluded.source IN ('news','news_ai','macro_theme')
                            THEN excluded.source
                        ELSE watch_pool.source
                    END,
                    -- 按 source 独立保存 reason，每次只覆盖对应分类，不再跨来源拼接
                    reason_anomaly = CASE WHEN excluded.reason_anomaly != '' THEN excluded.reason_anomaly ELSE watch_pool.reason_anomaly END,
                    reason_news    = CASE WHEN excluded.reason_news    != '' THEN excluded.reason_news    ELSE watch_pool.reason_news END,
                    reason_ai      = CASE WHEN excluded.reason_ai      != '' THEN excluded.reason_ai      ELSE watch_pool.reason_ai END,
                    -- 主 reason 字段：保留与本次 source 对应的最新理由（兼容旧前端）
                    reason = excluded.reason,
                    last_scored_at = excluded.last_scored_at,
                    last_news_mention_at = CASE
                        WHEN excluded.source IN ('news','news_ai')
                            THEN excluded.last_scored_at
                        ELSE watch_pool.last_news_mention_at END,
                    -- v12.15.4: status 升级规则
                    --   1) 手动添加 → 总是升级到 monitoring（用户明确认可）
                    --   2) archived 标的得分 >= 60 → 复活为 candidate
                    --   3) 其它情况保持原 status（保护 monitoring 不被自动来源降级）
                    status = CASE
                        WHEN excluded.source = 'manual' THEN 'monitoring'
                        WHEN watch_pool.status = 'archived' AND excluded.score >= 60
                            THEN 'candidate'
                        ELSE watch_pool.status END,
                    archived_at = CASE
                        WHEN watch_pool.status = 'archived' AND excluded.score >= 60
                            THEN NULL ELSE watch_pool.archived_at END,
                    archived_reason = CASE
                        WHEN watch_pool.status = 'archived' AND excluded.score >= 60
                            THEN '' ELSE watch_pool.archived_reason END,
                    low_score_since = NULL
                RETURNING id
                """,
                (item_id, symbol, market, score, score, source, reason,
                 reason_anomaly, reason_news, reason_ai,
                 now, now, mention_ts),
            )
            row = await cursor.fetchone()
            await conn.commit()
            return row["id"] if row else item_id

    async def get_pool_items(
        self,
        status: Optional[str] = None,
        market: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """
        查询候选池，按评分降序。LEFT JOIN symbol_fundamentals 直接带上公司名称。
        默认排除 archived 条目。要查归档传 status='archived'；查全部传 status='all'。
        """
        async with self.acquire() as conn:
            sql = (
                "SELECT wp.*, sf.name AS stock_name "
                "FROM watch_pool wp "
                "LEFT JOIN symbol_fundamentals sf ON wp.symbol=sf.symbol AND wp.market=sf.market "
                "WHERE 1=1"
            )
            params: list = []
            if status == 'all':
                pass
            elif status:
                sql += " AND wp.status = ?"
                params.append(status)
            else:
                sql += " AND wp.status != 'archived'"
            if market:
                sql += " AND wp.market = ?"
                params.append(market)
            sql += " ORDER BY wp.score DESC, wp.added_at DESC LIMIT ?"
            params.append(limit)
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def remove_from_pool(self, item_id: str) -> bool:
        """硬删除候选池条目。"""
        async with self.acquire() as conn:
            cursor = await conn.execute("DELETE FROM watch_pool WHERE id = ?", (item_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def archive_pool_item(self, item_id: str, reason: str = ""):
        """
        归档候选池条目（软删除）。
        reason 用于记录淘汰原因（low_score / quality_fail / no_news / manual 等），
        便于用户查询"为什么这只股没了"。

        v12.19.1 (P1-A): 同步禁用对应 strategy_bindings (避免 monitor 继续空跑死 binding)
        持仓中的股票不归档 (是被外层 is_exempt 保护)，所以禁用 binding 不会影响活跃持仓。
        """
        async with self.acquire() as conn:
            # 1. 拿 symbol/market 用于禁 binding
            cur = await conn.execute(
                "SELECT symbol, market FROM watch_pool WHERE id=?", (item_id,)
            )
            row = await cur.fetchone()
            # 2. 标记 archived
            await conn.execute(
                "UPDATE watch_pool SET status = 'archived', archived_at = ?, archived_reason = ? WHERE id = ?",
                (int(time.time()), reason or "", item_id),
            )
            # 3. 同步禁用对应 strategy_bindings (avoid monitor 空跑)
            if row and row["symbol"] and row["market"]:
                try:
                    await conn.execute(
                        "UPDATE strategy_bindings SET enabled=0 WHERE symbol=? AND market=?",
                        (row["symbol"], row["market"]),
                    )
                except Exception:
                    pass  # strategy_bindings 表不存在也不阻塞 archive
            await conn.commit()

    async def restore_pool_item(self, item_id: str) -> bool:
        """从 archived 恢复为 candidate。返回是否成功。

        v12.19.1 (P1-A): 同步重新启用对应 strategy_bindings
        """
        async with self.acquire() as conn:
            # 1. 拿 symbol/market
            cur = await conn.execute(
                "SELECT symbol, market FROM watch_pool WHERE id=? AND status='archived'", (item_id,)
            )
            row = await cur.fetchone()
            # 2. 改 candidate
            cursor = await conn.execute(
                "UPDATE watch_pool SET status='candidate', archived_at=NULL, archived_reason='', "
                "low_score_since=NULL WHERE id=? AND status='archived'",
                (item_id,),
            )
            # 3. 重新启用 binding
            if cursor.rowcount > 0 and row and row["symbol"] and row["market"]:
                try:
                    await conn.execute(
                        "UPDATE strategy_bindings SET enabled=1 WHERE symbol=? AND market=?",
                        (row["symbol"], row["market"]),
                    )
                except Exception:
                    pass
            await conn.commit()
            return cursor.rowcount > 0

    async def update_pool_low_score_since(self, item_id: str, ts: Optional[int]):
        """设置/清除连续低分起始时间戳。"""
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE watch_pool SET low_score_since=? WHERE id=?",
                (ts, item_id),
            )
            await conn.commit()

    async def update_pool_news_mention(self, symbol: str, market: str, ts: int, importance: int = 1) -> bool:
        """
        同股复发新闻时刷新 last_news_mention_at。
        importance ≥ 3 时还会"唤醒"已归档股票（archived → candidate），
        因为重要新闻意味着它重新值得关注。

        v12.19.1 (P3-A): importance >= 3 同时立即给该股加 event_score（+5/+10/+15 for ★3/★4/★5），
        总 score 重算 = event_score + technical + fundamentals。
        避免新闻入库到 1h 后才 rescore 期间被忽略。
        1h 后正常 rescore 会重算 event_score 覆盖此临时值。

        返回 True 表示有归档股被唤醒。
        """
        woke_up = False
        # event_score boost 表
        EVENT_BOOST = {3: 5.0, 4: 10.0, 5: 15.0}
        boost = EVENT_BOOST.get(importance, 0)
        async with self.acquire() as conn:
            # 先正常刷新非归档股的 mention 时间
            await conn.execute(
                "UPDATE watch_pool SET last_news_mention_at=? WHERE symbol=? AND market=? AND status != 'archived'",
                (ts, symbol, market),
            )
            # P1 修复 (审计 #6): archived 股也刷新 last_news_mention_at
            # 之前 archived 后 30 天被新闻提及也不会刷,会被 cron 永久清表 — 死数据
            # 现在: archived 也刷 mention_at (但不唤醒,除非 importance>=3 走下面逻辑),
            # 至少让 archived 股因有持续新闻而不被 30d 自动清理
            await conn.execute(
                "UPDATE watch_pool SET last_news_mention_at=? "
                "WHERE symbol=? AND market=? AND status='archived' "
                "AND (last_news_mention_at IS NULL OR last_news_mention_at < ?)",
                (ts, symbol, market, ts),
            )
            # 高分新闻（importance ≥ 3）触发归档股唤醒 + event_score boost
            if importance >= 3:
                # 唤醒
                cur = await conn.execute(
                    "UPDATE watch_pool SET status='candidate', archived_at=NULL, archived_reason='', "
                    "low_score_since=NULL, last_news_mention_at=? "
                    "WHERE symbol=? AND market=? AND status='archived'",
                    (ts, symbol, market),
                )
                woke_up = (cur.rowcount or 0) > 0
                # v12.19.1 P3-A: event_score 立即提升 + 重算总 score
                # 取 max(当前 event_score, boost) — 不累加避免同一波新闻反复加
                if boost > 0:
                    try:
                        await conn.execute(
                            """UPDATE watch_pool
                               SET event_score = MAX(COALESCE(event_score, 0), ?),
                                   score = COALESCE(technical_score, 0)
                                         + COALESCE(fundamentals_score, 0)
                                         + MAX(COALESCE(event_score, 0), ?),
                                   last_scored_at = ?
                               WHERE symbol=? AND market=? AND status != 'archived'""",
                            (boost, boost, int(time.time()), symbol, market),
                        )
                    except Exception:
                        pass
            await conn.commit()
        return woke_up

    async def update_pool_item_score(self, item_id: str, score: float):
        """更新候选池条目评分（重评分用）。"""
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE watch_pool SET score = ?, last_scored_at = ? WHERE id = ?",
                (score, int(time.time()), item_id),
            )
            await conn.commit()
    #   - positions (Phase 5)
    #   - position_advices (Phase 5)
    #   - llm_cost_log (Phase 3B)
