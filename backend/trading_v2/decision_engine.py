"""
v2 决策引擎 — 主决策入口 (v12.23.0 Phase 1)

职责: 接收 signal, 串联 5 层决策, 返回最终 Decision.

Phase 1 实施 (本版):
  Layer 1: quality_gates (位置/已涨/R:R/大盘/财报)

Phase 2-3 待实施:
  Layer 2: ai_verifier_v2 (强分级 AI 验证 + 后端配额监控)
  Layer 4: entry_timing (等回踩/突破/分批)

Phase 1 当前: gate 不过 → reject; gate 全过 → 直接 allow (走 v1 后续路径).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    """v2 决策结果."""
    allow: bool                                  # 是否允许下单
    reason: str = ""                             # 拒绝原因 (allow=False 时)
    gate_failed: Optional[str] = None            # 哪道门失败 (gate_1_position 等)
    metadata: Dict[str, Any] = field(default_factory=dict)  # 各门的 metric (用于审计)
    decided_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))


async def evaluate(db, signal: Dict[str, Any]) -> Decision:
    """对一个信号做 v2 决策.

    Args:
        db: DatabaseManager
        signal: signals 表 row dict (含 id/symbol/market/action/price/strategy_name/
                stop_loss/take_profit/confidence 等)

    Returns:
        Decision
    """
    if not signal or not signal.get("id"):
        return Decision(allow=False, reason="signal 字段缺失", gate_failed="invalid_input")

    sig_id = signal["id"]
    symbol = signal.get("symbol", "?")
    market = signal.get("market", "?")
    action = (signal.get("action") or "").lower()
    strategy = signal.get("strategy_name", "?")

    # ── Layer 1: 质量门 ──
    from backend.trading_v2 import quality_gates
    try:
        passed, gate_name, meta = await quality_gates.run_all_gates(db, signal)
    except Exception as e:
        # 门评估异常 → fallback 为 allow (走 v1, 避免 v2 异常导致全部信号死掉)
        logger.warning(f"[v2-decision] {sig_id[:8]} {symbol} 质量门异常 fallback v1: {e}")
        return Decision(
            allow=True,
            reason="v2 异常 fallback v1",
            gate_failed="exception",
            metadata={"exception": str(e)},
        )

    if not passed:
        reason = meta.get("reason", "质量门未通过")
        logger.info(
            f"[v2-decision] REJECT {sig_id[:8]} {symbol}({market})/{action}/{strategy}: "
            f"{gate_name} — {reason}"
        )
        return Decision(allow=False, reason=reason, gate_failed=gate_name, metadata=meta)

    # ── Layer 2 + Layer 4: 待 Phase 2/3 实现 ──
    # 当前版本: 全过 → allow, 后续走 v1 的 AI 验证 + 执行路径

    logger.info(
        f"[v2-decision] PASS {sig_id[:8]} {symbol}({market})/{action}/{strategy}: "
        f"all_gates ok"
    )
    return Decision(allow=True, reason="all_gates_passed", gate_failed=None, metadata=meta)


async def evaluate_with_fallback(db, signal: Dict[str, Any]) -> Decision:
    """带 fallback 的 evaluate — 任何异常都返回 allow (走 v1).

    生产环境推荐用这个入口, evaluate() 留作纯逻辑测试.
    """
    try:
        return await evaluate(db, signal)
    except Exception as e:
        logger.error(
            f"[v2-decision] 评估异常 fallback v1: {type(e).__name__}: {e}",
            exc_info=True,
        )
        return Decision(
            allow=True,
            reason=f"v2 异常 fallback v1: {type(e).__name__}",
            gate_failed="exception",
            metadata={"exception": str(e)},
        )
