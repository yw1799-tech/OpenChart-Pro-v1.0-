"""
Elliott Wave 核心数据结构与 Fibonacci 工具

完整实现 Elliott Wave 理论所需的所有基础类型。
"""

import numpy as np
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple, Any


# ============================================================
# 枚举类型
# ============================================================

class Direction(Enum):
    UP = 1
    DOWN = -1


class PatternType(Enum):
    """所有支持的波浪模式 — 覆盖 Elliott Wave 完整理论"""

    # === 推动浪 (Motive Waves) ===
    IMPULSE = "impulse"                         # 标准推动浪 5-3-5-3-5
    IMPULSE_EXT1 = "impulse_ext1"               # 浪1延伸
    IMPULSE_EXT3 = "impulse_ext3"               # 浪3延伸 (最常见)
    IMPULSE_EXT5 = "impulse_ext5"               # 浪5延伸
    LEADING_DIAGONAL = "leading_diagonal"       # 引导楔形 (浪1/A位置)
    ENDING_DIAGONAL = "ending_diagonal"         # 终结楔形 (浪5/C位置)
    TRUNCATED_5TH = "truncated_5th"             # 失败第五浪

    # === 调整浪 (Corrective Waves) ===
    ZIGZAG = "zigzag"                           # 锯齿形 5-3-5
    DOUBLE_ZIGZAG = "double_zigzag"             # 双锯齿 W-X-Y (两个zigzag)
    FLAT_REGULAR = "flat_regular"               # 规则平台 3-3-5
    FLAT_EXPANDED = "flat_expanded"             # 扩展平台 3-3-5 (B超A起点)
    FLAT_RUNNING = "flat_running"               # 顺势平台 3-3-5
    TRIANGLE_CONTRACTING = "triangle_contracting"   # 收缩三角 3-3-3-3-3
    TRIANGLE_EXPANDING = "triangle_expanding"       # 扩展三角 3-3-3-3-3
    COMBINATION_WXY = "combination_wxy"         # 双重组合 W-X-Y


MOTIVE_TYPES = {
    PatternType.IMPULSE, PatternType.IMPULSE_EXT1, PatternType.IMPULSE_EXT3,
    PatternType.IMPULSE_EXT5, PatternType.LEADING_DIAGONAL,
    PatternType.ENDING_DIAGONAL, PatternType.TRUNCATED_5TH,
}

CORRECTIVE_TYPES = {
    PatternType.ZIGZAG, PatternType.DOUBLE_ZIGZAG,
    PatternType.FLAT_REGULAR, PatternType.FLAT_EXPANDED, PatternType.FLAT_RUNNING,
    PatternType.TRIANGLE_CONTRACTING, PatternType.TRIANGLE_EXPANDING,
    PatternType.COMBINATION_WXY,
}

# 中文名映射
PATTERN_NAMES_CN = {
    PatternType.IMPULSE: "推动浪",
    PatternType.IMPULSE_EXT1: "推动浪(浪1延伸)",
    PatternType.IMPULSE_EXT3: "推动浪(浪3延伸)",
    PatternType.IMPULSE_EXT5: "推动浪(浪5延伸)",
    PatternType.LEADING_DIAGONAL: "引导楔形",
    PatternType.ENDING_DIAGONAL: "终结楔形",
    PatternType.TRUNCATED_5TH: "失败五浪",
    PatternType.ZIGZAG: "锯齿形",
    PatternType.DOUBLE_ZIGZAG: "双锯齿",
    PatternType.FLAT_REGULAR: "规则平台",
    PatternType.FLAT_EXPANDED: "扩展平台",
    PatternType.FLAT_RUNNING: "顺势平台",
    PatternType.TRIANGLE_CONTRACTING: "收缩三角",
    PatternType.TRIANGLE_EXPANDING: "扩展三角",
    PatternType.COMBINATION_WXY: "双重组合",
}


# ============================================================
# 数据结构
# ============================================================

@dataclass
class Swing:
    """ZigZag 枢轴点"""
    index: int
    price: float
    is_high: bool
    time: Optional[Any] = None

    def __hash__(self):
        return hash((self.index, self.price, self.is_high))


@dataclass
class Wave:
    """单个波浪段"""
    start: Swing
    end: Swing
    label: str      # "1","2","3","4","5","A","B","C","D","E","W","X","Y"

    @property
    def direction(self) -> Direction:
        return Direction.UP if self.end.price > self.start.price else Direction.DOWN

    @property
    def length(self) -> float:
        return abs(self.end.price - self.start.price)

    @property
    def duration(self) -> int:
        return abs(self.end.index - self.start.index)

    def retrace_ratio(self, other: "Wave") -> float:
        """self 对 other 的回撤/扩展比率"""
        return self.length / other.length if other.length > 0 else 0.0


@dataclass
class RuleResult:
    """单条规则的验证结果"""
    name: str
    passed: bool
    is_hard: bool       # True=铁律(必须通过), False=指引(贡献置信度)
    score: float = 0.0  # 0-1, 对置信度的贡献
    detail: str = ""


@dataclass
class WavePattern:
    """一个完整的波浪模式"""
    pattern_type: PatternType
    direction: Direction
    waves: List[Wave]
    degree: int = 0             # 0=主级, 1=次级, 2=小级
    rule_results: List[RuleResult] = field(default_factory=list)
    sub_patterns: Dict[str, "WavePattern"] = field(default_factory=dict)
    confidence: float = 0.0

    @property
    def is_valid(self) -> bool:
        """所有硬规则是否通过"""
        return all(r.passed for r in self.rule_results if r.is_hard)

    @property
    def start_price(self) -> float:
        return self.waves[0].start.price

    @property
    def end_price(self) -> float:
        return self.waves[-1].end.price

    @property
    def start_index(self) -> int:
        return self.waves[0].start.index

    @property
    def end_index(self) -> int:
        return self.waves[-1].end.index

    @property
    def total_length(self) -> float:
        return abs(self.end_price - self.start_price)

    @property
    def cn_name(self) -> str:
        return PATTERN_NAMES_CN.get(self.pattern_type, self.pattern_type.value)

    def summary(self) -> str:
        d = "↑" if self.direction == Direction.UP else "↓"
        labels = "-".join(w.label for w in self.waves)
        hard_pass = sum(1 for r in self.rule_results if r.is_hard and r.passed)
        hard_total = sum(1 for r in self.rule_results if r.is_hard)
        return (f"{self.cn_name}{d} [{labels}] "
                f"置信度={self.confidence:.1%} 硬规则={hard_pass}/{hard_total}")

    def detail_report(self) -> str:
        lines = [self.summary()]
        for w in self.waves:
            d = "↑" if w.direction == Direction.UP else "↓"
            lines.append(f"  {w.label}: {w.start.price:.2f}→{w.end.price:.2f} "
                         f"{d} 幅度={w.length:.2f} K线={w.duration}")
        lines.append("  规则:")
        for r in self.rule_results:
            tag = "硬" if r.is_hard else "软"
            status = "✓" if r.passed else "✗"
            lines.append(f"    [{tag}] {status} {r.name} score={r.score:.2f} {r.detail}")
        return "\n".join(lines)


@dataclass
class Prediction:
    """基于已识别模式的预测"""
    pattern: WavePattern
    next_wave_label: str        # 预测的下一浪标签
    target_prices: Dict[str, float]  # fib_ratio -> 目标价
    direction: Direction
    confidence: float


# ============================================================
# Fibonacci 工具
# ============================================================

# 标准 Fibonacci 比率
FIB_RATIOS = [0.236, 0.382, 0.500, 0.618, 0.786, 1.000, 1.272, 1.618, 2.000, 2.618, 4.236]


def fib_score(actual: float, expected: List[float], tolerance: float = 0.05) -> float:
    """
    计算实际比率与 Fibonacci 期望值的匹配度 (0.0 ~ 1.0)

    tolerance 内 = 1.0, tolerance*3 外 = 0.0, 之间线性插值
    """
    if not expected:
        return 0.5
    min_dist = min(abs(actual - e) for e in expected)
    if min_dist <= tolerance:
        return 1.0
    elif min_dist <= tolerance * 3:
        return 1.0 - (min_dist - tolerance) / (tolerance * 2)
    return 0.0


def nearest_fib(ratio: float) -> Tuple[float, float]:
    """找最近的 Fibonacci 比率, 返回 (fib_ratio, distance)"""
    best = min(FIB_RATIOS, key=lambda f: abs(ratio - f))
    return best, abs(ratio - best)


def compute_confidence(results: List[RuleResult]) -> float:
    """
    根据规则结果计算总置信度

    硬规则不通过 → 0.0
    否则：硬规则占30% + 软规则加权平均占50% + 规则覆盖度占20%

    规则覆盖度: 规则越多、验证越全面，置信度越高
    防止规则少的模式(如三角形2条)轻易胜过规则多的模式(如推动浪12条)
    """
    hard_rules = [r for r in results if r.is_hard]
    soft_rules = [r for r in results if not r.is_hard]

    if hard_rules and not all(r.passed for r in hard_rules):
        return 0.0

    hard_score = 1.0 if not hard_rules else sum(r.score for r in hard_rules) / len(hard_rules)
    soft_score = 0.5 if not soft_rules else sum(r.score for r in soft_rules) / len(soft_rules)

    # 覆盖度: 6条规则以上=满分, 越少越低
    n_total = len(results)
    coverage = min(1.0, n_total / 6.0)

    return hard_score * 0.3 + soft_score * 0.5 + coverage * 0.2
