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
  当前实施状态 (v12.23.1)
═══════════════════════════════════════════════════════════════════

✅ 已实施 (Phase 1):
  - feature_flag.py    灰度开关 (按 signal_id 哈希 % 100 < V2_PCT 走 v2)
  - quality_gates.py   Layer 1: 4 道生效门 + 1 道 stub
       Gate 1 位置门    (距 20 日高 < 5% 拒)        [生效]
       Gate 2 已涨幅度  (当日已涨 > 4% 拒)          [生效]
       Gate 3 R:R       (优先 ai_*, R:R < 3.0 拒)   [生效]
       Gate 4 大盘环境  (SPY 当日跌 > 2% 拒美股)    [生效]
       Gate 5 财报临近  (距财报 < 3 天 拒)          [stub, Phase 2 启用]
  - decision_engine.py Phase 1 仅串联 Layer 1 (quality_gates)

⏭️ Phase 2 待实施:
  - ai_verifier_v2.py  Layer 2: AI 强分级 (60-69/70-79/80-89/90+) + 后端配额监控
  - 财报日历接入 (Gate 5 生效)

⏭️ Phase 3 待实施:
  - entry_timing.py    Layer 4: 限价等回踩 / 突破入场 / 分批建仓

═══════════════════════════════════════════════════════════════════
  数据流 (Phase 1)
═══════════════════════════════════════════════════════════════════

signal (strategies.py 22 策略生成)
  ↓
ai_analyzer.verify_signal (v1 现有 AI 验证 — Phase 2 重做)
  ↓
auto_trader._handle_signal (BUY 信号)
  ↓
feature_flag.use_v2(signal_id, market) → 灰度判定
  ├─ False (V2_GRAYSCALE_PCT=0 默认): 全部走 v1, v12.23.x 完全无影响
  └─ True (灰度命中):
        ↓
        decision_engine.evaluate_with_fallback(db, signal)
          ├─ quality_gates.run_all_gates → 4 道生效门串行
          │   任一 fail → Decision(allow=False, gate_failed=…)
          │   全过 → Decision(allow=True)
          └─ 异常 → fallback Decision(allow=True) 走 v1
        ↓
   v2 reject → _log_rejected + return
   v2 pass / fallback → 继续走 v1 路径 (冷却/上限/开仓)

═══════════════════════════════════════════════════════════════════
"""

__version__ = "v12.23.1-phase1-audit-fixed"
