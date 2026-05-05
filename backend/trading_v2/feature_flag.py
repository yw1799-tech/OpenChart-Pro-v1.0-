"""
v2 决策层灰度开关.

灰度策略: 按 signal_id 哈希 % 100 < V2_GRAYSCALE_PCT 走 v2.
- 同一信号反复评估时决策一致 (用 hash 保证)
- 灰度比例可热改 (改配置无需重启)
- 0% = 全部走 v1 (保守起点)
- 100% = 全部走 v2 (完成切换)

开关也支持按 market 维度精细化 (例如美股 30%, 加密 0%).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def use_v2(signal_id: str, market: Optional[str] = None) -> bool:
    """决定该信号是否走 v2 决策层.

    返回 True = 走 v2 (decision_engine.evaluate)
    返回 False = 走 v1 (auto_trader._handle_signal 现有路径)
    """
    pct = _resolve_grayscale_pct(market)
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    bucket = _hash_bucket(signal_id)
    return bucket < pct


def _resolve_grayscale_pct(market: Optional[str]) -> int:
    """从配置读取当前灰度比例, 支持按 market 精细化."""
    try:
        from backend import config
    except Exception:
        return 0
    # 优先取按 market 配置 (V2_GRAYSCALE_PCT_BY_MARKET = {"us": 30, "hk": 0, ...})
    by_market = getattr(config, "V2_GRAYSCALE_PCT_BY_MARKET", None)
    if isinstance(by_market, dict) and market and market in by_market:
        return int(by_market[market] or 0)
    # 全局兜底
    return int(getattr(config, "V2_GRAYSCALE_PCT", 0) or 0)


def _hash_bucket(signal_id: str) -> int:
    """signal_id → 0-99 (稳定哈希)."""
    if not signal_id:
        return 100  # 异常 id 视为 100, 不走 v2
    h = hashlib.md5(signal_id.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 100


def current_pct(market: Optional[str] = None) -> int:
    """供监控/前端展示当前灰度比例."""
    return _resolve_grayscale_pct(market)
