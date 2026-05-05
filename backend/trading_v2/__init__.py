"""
v2 决策层 — 开仓链路重构 (v12.23.0+)

═══════════════════════════════════════════════════════════════════
  设计哲学 (灵魂 — 任何修改本目录代码必须先证明符合 3 原则)
═══════════════════════════════════════════════════════════════════

原则 1: 质量 > 频次 > 时机
  当 PASS / REJECT 边界模糊时, 倾向 REJECT.
  宁可错过 100 个平庸机会, 不要错入 10 个 trap.

原则 2: 接受动量基因, 只挑动量初段
  不试图反转 / 低吸 — 那不是这套系统的基因.
  但屏蔽"已涨过"的标的, 只在动量初段+中段入场.

原则 3: AI 是独立审计, 不是信号盖章
  打分必须分散 60-95, 70-79 不得过半 (后端配额监控).
  AI 必须有"reject 的勇气" — 实际 reject 率 ≥ 15%.

═══════════════════════════════════════════════════════════════════
  架构
═══════════════════════════════════════════════════════════════════

backend/trading_v2/
├── __init__.py          (本文件 — 设计哲学)
├── feature_flag.py      (灰度开关: 按 symbol 哈希 % 100 < V2_PCT 走 v2)
├── decision_engine.py   (主决策入口: signal → Decision)
├── quality_gates.py     (Layer 1: 5+ 道质量门, 全过才进 AI)
├── ai_verifier_v2.py    (Layer 2: AI 强分级 + 后端配额监控, Phase 2)
├── entry_timing.py      (Layer 4: 等回踩/突破/分批, Phase 3)

数据流:
  signal (from strategies.py)
    ↓
  feature_flag.use_v2(signal) → 是否走 v2
    ↓ (yes)
  decision_engine.evaluate(signal, context) → Decision
    ↓
  Decision.allow=True → auto_trader._try_open_position
  Decision.allow=False → log rejected_v2 + return

═══════════════════════════════════════════════════════════════════
"""

__version__ = "v12.23.0-phase1"
