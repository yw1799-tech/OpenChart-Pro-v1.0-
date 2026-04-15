"""
品种识别注册表（PRD F5.3 / TDD 增强）。

合并 3 个识别源：
  1. 静态词典（symbol_dict.py 内置 200+ 主流品种 + 中英文别名）
  2. 动态词典（启动后定期从 DB 拉：watchlist + watch_pool + positions）
  3. 加密 6 币种固定（CRYPTO_SYMBOLS）

用途：
  rule_engine.score_news() 识别新闻涉及的 symbol → categories 字段
  → 触发候选池入池 / 评分加成 / 信号过滤等下游
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict, List, Optional, Set, Tuple

from backend.news.symbol_dict import STATIC_PATTERNS

logger = logging.getLogger(__name__)


class SymbolRegistry:
    """
    全局品种识别注册表。
    线程安全：读多写少 + asyncio.Lock 保证刷新原子性。
    """

    def __init__(self):
        # _patterns: { (symbol, market): [compiled regex, ...] }
        self._patterns: Dict[Tuple[str, str], List[re.Pattern]] = {}
        self._lock = asyncio.Lock()
        # 默认装入静态词典
        self._load_static()

    def _load_static(self):
        """静态词典装入。"""
        for (sym, market), aliases in STATIC_PATTERNS.items():
            self._patterns[(sym, market)] = self._compile_aliases(aliases)
        logger.info(f"SymbolRegistry 静态词典加载: {len(STATIC_PATTERNS)} 个品种")

    @staticmethod
    def _compile_aliases(aliases: List[str]) -> List[re.Pattern]:
        """把别名列表编译为正则。代码类用 \\b 包围避免误匹配；中文名直接子串匹配。"""
        compiled = []
        for alias in aliases:
            try:
                if re.match(r"^[A-Za-z0-9.\-_]+$", alias):
                    # 纯英数代码：用单词边界
                    compiled.append(re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE))
                else:
                    # 中文/混合：直接子串匹配
                    compiled.append(re.compile(re.escape(alias)))
            except re.error:
                pass
        return compiled

    async def refresh_from_db(self, db):
        """
        从数据库刷新动态词典：
          - watchlist 表（所有市场，包括加密 6 币种）
          - watch_pool 表（仅股票）
          - positions 表（持仓品种）
        每个 symbol 的别名暂只用 symbol 自身（无中文名时）。
        """
        async with self._lock:
            new_dynamic: Dict[Tuple[str, str], List[str]] = {}

            # 1. watchlist
            try:
                items = await db.get_watchlist()
                for it in items:
                    sym = it["symbol"]
                    market = it["market"]
                    name = it.get("name") or ""
                    aliases = [sym]
                    if name and name != sym:
                        aliases.append(name)
                    new_dynamic[(sym, market)] = aliases
            except Exception as e:
                logger.debug(f"refresh watchlist 失败: {e}")

            # 2. watch_pool
            try:
                items = await db.get_pool_items(limit=500)
                for it in items:
                    sym = it["symbol"]
                    market = it["market"]
                    aliases = [sym]
                    new_dynamic.setdefault((sym, market), aliases)
            except Exception as e:
                logger.debug(f"refresh pool 失败: {e}")

            # 3. positions
            try:
                async with db.acquire() as conn:
                    cursor = await conn.execute("SELECT symbol, market FROM positions")
                    rows = await cursor.fetchall()
                    for row in rows:
                        new_dynamic.setdefault((row["symbol"], row["market"]), [row["symbol"]])
            except Exception as e:
                logger.debug(f"refresh positions 失败: {e}")

            # 合并到主表（动态新增的不覆盖静态已有别名）
            added = 0
            for key, aliases in new_dynamic.items():
                if key in self._patterns:
                    continue  # 已在静态词典中
                self._patterns[key] = self._compile_aliases(aliases)
                added += 1
            if added > 0:
                logger.info(f"SymbolRegistry 动态新增 {added} 个品种 (总计 {len(self._patterns)})")

    def find_matches(self, text: str) -> List[str]:
        """
        在文本中查找所有匹配的 symbol。
        返回去重后的 symbol 列表。
        """
        if not text:
            return []
        matches: Set[str] = set()
        for (sym, _market), patterns in self._patterns.items():
            for p in patterns:
                if p.search(text):
                    matches.add(sym)
                    break  # 一个 symbol 命中任一别名即可
        return sorted(matches)

    def size(self) -> int:
        return len(self._patterns)


# 全局单例
registry = SymbolRegistry()
