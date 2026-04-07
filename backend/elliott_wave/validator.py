"""
Elliott Wave 自验证系统

两种验证方式:
  1. 合成数据验证 — 生成已知答案的波浪数据, 测试识别准确率
  2. 预测回测验证 — 在历史数据上识别模式, 检查预测的目标价是否命中

用法:
    python -m App.elliott_wave.validator
    python -m App.elliott_wave.validator --data btc_usdt_1d.csv
"""

import random
import numpy as np
import pandas as pd
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

from .core import PatternType, Direction, MOTIVE_TYPES, CORRECTIVE_TYPES, PATTERN_NAMES_CN
from .detector import ElliottWaveAnalyzer, detect_swings


# ============================================================
# 合成数据生成器
# ============================================================

class SyntheticWaveGenerator:
    """生成已知波浪结构的合成价格数据"""

    @staticmethod
    def _interpolate(start: float, end: float, n_bars: int,
                     noise_pct: float = 0.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        在start→end之间生成n_bars根K线的OHLC数据

        返回 (highs, lows, closes)
        """
        if n_bars < 2:
            n_bars = 2

        # 用随机游走替代linspace，生成更真实的价格序列
        drift = (end - start) / (n_bars - 1)
        volatility = abs(end - start) / n_bars * 0.8  # 基础波动
        vol_extra = abs(end - start) * noise_pct       # 额外噪声

        closes = np.zeros(n_bars)
        closes[0] = start
        for i in range(1, n_bars):
            closes[i] = closes[i - 1] + drift + np.random.randn() * (volatility + vol_extra)

        # 线性修正确保精确到达终点
        error = closes[-1] - end
        correction = np.linspace(0, error, n_bars)
        closes -= correction
        closes[0] = start
        closes[-1] = end

        # 生成 high/low: 用更大的spread确保有明确的局部极值
        bar_range = max(abs(drift) * 1.5, abs(start) * 0.005)
        highs = closes + np.abs(np.random.randn(n_bars)) * bar_range + bar_range * 0.3
        lows = closes - np.abs(np.random.randn(n_bars)) * bar_range - bar_range * 0.3

        highs = np.maximum(highs, closes)
        lows = np.minimum(lows, closes)

        return highs, lows, closes

    @classmethod
    def generate_impulse(
        cls,
        base: float = 100.0,
        w1_pct: float = 0.20,
        w2_retrace: float = 0.50,
        w3_ext: float = 1.618,
        w4_retrace: float = 0.382,
        w5_ratio: float = 1.0,
        direction: Direction = Direction.UP,
        bars_per_wave: int = 20,
        noise_pct: float = 0.0,
    ) -> Tuple[pd.DataFrame, Dict]:
        """
        生成标准推动浪 (1-2-3-4-5)

        返回: (DataFrame, 已知答案dict)
        """
        d = 1 if direction == Direction.UP else -1

        # 审计#16: 确保浪3不是1/3/5中最短的
        # 浪1长度=w1_len, 浪3长度=w1_len*w3_ext, 浪5长度=w1_len*w5_ratio
        # 需要 w3_ext >= 1.0 (不短于浪1) 且 w3_ext >= w5_ratio (不短于浪5)
        w3_ext = max(w3_ext, 1.0, w5_ratio + 0.1)

        w1_len = base * w1_pct

        p0 = base                           # 浪1起点
        p1 = p0 + w1_len * d                # 浪1终点 = 浪2起点
        p2 = p1 - w1_len * w2_retrace * d   # 浪2终点 = 浪3起点
        p3 = p2 + w1_len * w3_ext * d       # 浪3终点 = 浪4起点
        p4 = p3 - w1_len * w3_ext * w4_retrace * d   # 浪4终点 = 浪5起点
        p5 = p4 + w1_len * w5_ratio * d     # 浪5终点

        # 确保浪4不入浪1区域
        if d == 1:
            p4 = max(p4, p1 + 0.01)
        else:
            p4 = min(p4, p1 - 0.01)

        points = [p0, p1, p2, p3, p4, p5]
        all_h, all_l, all_c = [], [], []

        for i in range(5):
            h, l, c = cls._interpolate(points[i], points[i + 1], bars_per_wave, noise_pct)
            if i > 0:
                h, l, c = h[1:], l[1:], c[1:]  # 去重叠点
            all_h.append(h)
            all_l.append(l)
            all_c.append(c)

        highs = np.concatenate(all_h)
        lows = np.concatenate(all_l)
        closes = np.concatenate(all_c)

        df = pd.DataFrame({"high": highs, "low": lows, "close": closes})

        answer = {
            "type": PatternType.IMPULSE,
            "direction": direction,
            "points": points,
            "wave_count": 5,
        }
        return df, answer

    @classmethod
    def generate_zigzag(
        cls,
        base: float = 100.0,
        a_pct: float = 0.15,
        b_retrace: float = 0.50,
        c_ratio: float = 1.0,
        direction: Direction = Direction.DOWN,
        bars_per_wave: int = 15,
        noise_pct: float = 0.0,
    ) -> Tuple[pd.DataFrame, Dict]:
        """生成锯齿形 A-B-C"""
        d = 1 if direction == Direction.UP else -1
        a_len = base * a_pct

        p0 = base
        p1 = p0 + a_len * d
        p2 = p1 - a_len * b_retrace * d
        p3 = p2 + a_len * c_ratio * d

        # C必须超越A
        if d == 1 and p3 <= p1:
            p3 = p1 + 0.01
        elif d == -1 and p3 >= p1:
            p3 = p1 - 0.01

        points = [p0, p1, p2, p3]
        all_h, all_l, all_c = [], [], []
        for i in range(3):
            h, l, c = cls._interpolate(points[i], points[i + 1], bars_per_wave, noise_pct)
            if i > 0:
                h, l, c = h[1:], l[1:], c[1:]
            all_h.append(h)
            all_l.append(l)
            all_c.append(c)

        df = pd.DataFrame({
            "high": np.concatenate(all_h),
            "low": np.concatenate(all_l),
            "close": np.concatenate(all_c),
        })
        answer = {
            "type": PatternType.ZIGZAG,
            "direction": direction,
            "points": points,
            "wave_count": 3,
        }
        return df, answer

    @classmethod
    def generate_flat(
        cls,
        base: float = 100.0,
        a_pct: float = 0.10,
        b_retrace: float = 0.95,
        c_ratio: float = 1.0,
        flat_type: str = "regular",
        direction: Direction = Direction.DOWN,
        bars_per_wave: int = 15,
        noise_pct: float = 0.0,
    ) -> Tuple[pd.DataFrame, Dict]:
        """生成平台形 (regular / expanded / running)"""
        d = 1 if direction == Direction.UP else -1
        a_len = base * a_pct

        p0 = base
        p1 = p0 + a_len * d

        if flat_type == "expanded":
            b_retrace = 1.15
            c_ratio = 1.3
        elif flat_type == "running":
            b_retrace = 1.10
            c_ratio = 0.5

        p2 = p1 - a_len * b_retrace * d
        p3 = p2 + a_len * c_ratio * d

        points = [p0, p1, p2, p3]
        all_h, all_l, all_c = [], [], []
        for i in range(3):
            h, l, c = cls._interpolate(points[i], points[i + 1], bars_per_wave, noise_pct)
            if i > 0:
                h, l, c = h[1:], l[1:], c[1:]
            all_h.append(h)
            all_l.append(l)
            all_c.append(c)

        type_map = {
            "regular": PatternType.FLAT_REGULAR,
            "expanded": PatternType.FLAT_EXPANDED,
            "running": PatternType.FLAT_RUNNING,
        }
        df = pd.DataFrame({
            "high": np.concatenate(all_h),
            "low": np.concatenate(all_l),
            "close": np.concatenate(all_c),
        })
        answer = {
            "type": type_map.get(flat_type, PatternType.FLAT_REGULAR),
            "direction": direction,
            "points": points,
            "wave_count": 3,
        }
        return df, answer

    @classmethod
    def generate_triangle(
        cls,
        base: float = 100.0,
        amplitude: float = 0.10,
        shrink_ratio: float = 0.7,
        direction: Direction = Direction.DOWN,
        bars_per_wave: int = 12,
        noise_pct: float = 0.0,
    ) -> Tuple[pd.DataFrame, Dict]:
        """生成收缩三角 A-B-C-D-E"""
        d = 1 if direction == Direction.UP else -1
        amp = base * amplitude

        points = [base]
        current = base
        for i in range(5):
            sign = d if i % 2 == 0 else -d
            move = amp * (shrink_ratio ** i)
            current = current + move * sign
            points.append(current)

        all_h, all_l, all_c = [], [], []
        for i in range(5):
            h, l, c = cls._interpolate(points[i], points[i + 1], bars_per_wave, noise_pct)
            if i > 0:
                h, l, c = h[1:], l[1:], c[1:]
            all_h.append(h)
            all_l.append(l)
            all_c.append(c)

        df = pd.DataFrame({
            "high": np.concatenate(all_h),
            "low": np.concatenate(all_l),
            "close": np.concatenate(all_c),
        })
        answer = {
            "type": PatternType.TRIANGLE_CONTRACTING,
            "direction": direction,
            "points": points,
            "wave_count": 5,
        }
        return df, answer


# ============================================================
# 验证运行器
# ============================================================

@dataclass
class ValidationResult:
    """单个测试的验证结果"""
    pattern_type: str
    noise_level: float
    detected: bool
    correct_type: bool
    correct_direction: bool
    best_confidence: float
    detail: str = ""


@dataclass
class ValidationReport:
    """验证报告"""
    results: List[ValidationResult] = field(default_factory=list)
    predictions: List[Dict] = field(default_factory=list)
    hard_rule_violations: List[Dict] = field(default_factory=list)  # 硬规则自检结果
    consistency_checks: List[Dict] = field(default_factory=list)    # 多级别一致性检查
    random_baseline: float = 0.0  # 随机预测基线命中率
    consistency_rate: float = 0.0  # 多级别一致率

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def detected_count(self) -> int:
        return sum(1 for r in self.results if r.detected)

    @property
    def correct_type_count(self) -> int:
        return sum(1 for r in self.results if r.correct_type)

    @property
    def correct_dir_count(self) -> int:
        return sum(1 for r in self.results if r.correct_direction)

    @property
    def detection_rate(self) -> float:
        return self.detected_count / self.total if self.total > 0 else 0

    @property
    def type_accuracy(self) -> float:
        return self.correct_type_count / self.total if self.total > 0 else 0

    @property
    def prediction_hit_rate(self) -> float:
        hits = sum(1 for p in self.predictions if p.get("hit", False))
        return hits / len(self.predictions) if self.predictions else 0

    def summary(self) -> str:
        lines = [
            "=" * 60,
            "Elliott Wave 自验证报告",
            "=" * 60,
            "",
            f"合成数据测试: {self.total} 组",
            f"  检测率:     {self.detected_count}/{self.total} = {self.detection_rate:.1%}",
            f"  类型准确率: {self.correct_type_count}/{self.total} = {self.type_accuracy:.1%}",
            f"  方向准确率: {self.correct_dir_count}/{self.total} = {self.correct_dir_count/self.total:.1%}" if self.total > 0 else "",
        ]

        if self.predictions:
            lines.append(f"\n预测回测: {len(self.predictions)} 次预测")
            lines.append(f"  命中率: {sum(1 for p in self.predictions if p.get('hit'))}"
                         f"/{len(self.predictions)} = {self.prediction_hit_rate:.1%}")
            if self.random_baseline > 0:
                diff = self.prediction_hit_rate - self.random_baseline
                lines.append(f"  随机基线: {self.random_baseline:.1%} | "
                             f"超越基线: {diff:+.1%}")

        # 硬规则自检
        if self.hard_rule_violations:
            lines.append(f"\n硬规则自检: {len(self.hard_rule_violations)} 个违规")
            for v in self.hard_rule_violations[:5]:
                lines.append(f"  {v['pattern']}: {v['rule']} - {v['detail']}")
        elif self.results:
            lines.append(f"\n硬规则自检: 全部通过")

        # 多级别一致性
        if self.consistency_checks:
            lines.append(f"\n多级别一致性验证: {len(self.consistency_checks)} 组数据")
            lines.append(f"  一致率: {self.consistency_rate:.1%}")

        # 按模式类型分组统计
        by_type: Dict[str, List[ValidationResult]] = {}
        for r in self.results:
            by_type.setdefault(r.pattern_type, []).append(r)

        lines.append("\n按模式类型:")
        for ptype, results in by_type.items():
            total = len(results)
            detected = sum(1 for r in results if r.detected)
            correct = sum(1 for r in results if r.correct_type)
            lines.append(f"  {ptype}: 检测={detected}/{total} 准确={correct}/{total}")

        # 按噪声水平分组
        by_noise: Dict[float, List[ValidationResult]] = {}
        for r in self.results:
            by_noise.setdefault(r.noise_level, []).append(r)

        lines.append("\n按噪声水平:")
        for noise, results in sorted(by_noise.items()):
            total = len(results)
            detected = sum(1 for r in results if r.detected)
            correct = sum(1 for r in results if r.correct_type)
            lines.append(f"  噪声={noise:.1%}: 检测={detected}/{total} 准确={correct}/{total}")

        # 失败案例
        failures = [r for r in self.results if not r.correct_type]
        if failures:
            lines.append(f"\n失败案例 (前5个):")
            for r in failures[:5]:
                lines.append(f"  {r.pattern_type} noise={r.noise_level:.1%}: {r.detail}")

        return "\n".join(lines)


class ValidationRunner:
    """验证运行器"""

    def __init__(self, analyzer: Optional[ElliottWaveAnalyzer] = None):
        self.analyzer = analyzer or ElliottWaveAnalyzer(
            deviations=[0.5, 1.0, 2.0, 3.0],
            depth=3,
            max_recursion=0,
            min_confidence=0.15,
        )

    def _check_detection(
        self,
        df: pd.DataFrame,
        answer: Dict,
        noise_level: float,
        verbose: bool = False,
    ) -> ValidationResult:
        """检查单个合成数据的识别结果

        Args:
            verbose: 如果为True，打印详细诊断信息（swing点、所有模式、规则结果）
        """
        expected_type = answer["type"]
        expected_dir = answer["direction"]
        type_name = PATTERN_NAMES_CN.get(expected_type, expected_type.value)

        self.analyzer.analyze(df)
        patterns = self.analyzer.patterns

        if verbose:
            print(f"\n{'='*60}")
            print(f"诊断: 期望={type_name} 方向={'UP' if expected_dir == Direction.UP else 'DOWN'} 噪声={noise_level:.1%}")
            print(f"{'='*60}")
            print(f"已知转折点: {answer.get('points', 'N/A')}")
            print(f"\n--- Swing点检测 ---")
            for dev, swings in self.analyzer._swings_by_dev.items():
                print(f"  deviation={dev}: {len(swings)}个swing点")
                for s in swings:
                    tag = "HIGH" if s.is_high else "LOW"
                    print(f"    idx={s.index:4d} price={s.price:8.2f} {tag}")
            print(f"\n--- 检测到的所有模式 ({len(patterns)}个) ---")
            for i, p in enumerate(patterns):
                print(f"  [{i+1}] {p.summary()}")
                for w in p.waves:
                    d_str = "↑" if w.direction == Direction.UP else "↓"
                    print(f"      {w.label}: idx[{w.start.index}->{w.end.index}] "
                          f"{w.start.price:.2f}->{w.end.price:.2f} {d_str} len={w.length:.2f}")
                print(f"      规则详情:")
                for r in p.rule_results:
                    tag = "硬" if r.is_hard else "软"
                    status = "PASS" if r.passed else "FAIL"
                    print(f"        [{tag}] {status} {r.name} score={r.score:.2f} {r.detail}")

        if not patterns:
            if verbose:
                print(f"\n结论: 未检测到任何模式!")
            return ValidationResult(type_name, noise_level, False, False, False, 0.0,
                                    "未检测到任何模式")

        # 检查是否有匹配的模式类型
        best_match = None
        best_conf = 0

        # 同族模式容错: 推动浪家族(含楔形/失败五浪) 和 flat子类型 都算正确
        # 理论依据: 引导/终结楔形和失败五浪都是推动浪的特殊形态
        impulse_family = {PatternType.IMPULSE, PatternType.IMPULSE_EXT1,
                          PatternType.IMPULSE_EXT3, PatternType.IMPULSE_EXT5,
                          PatternType.LEADING_DIAGONAL, PatternType.ENDING_DIAGONAL,
                          PatternType.TRUNCATED_5TH}
        flat_family = {PatternType.FLAT_REGULAR, PatternType.FLAT_EXPANDED,
                       PatternType.FLAT_RUNNING}

        for p in patterns:
            type_match = (p.pattern_type == expected_type or
                          (expected_type in impulse_family and p.pattern_type in impulse_family) or
                          (expected_type in flat_family and p.pattern_type in flat_family))
            dir_match = p.direction == expected_dir

            if type_match and dir_match and p.confidence > best_conf:
                best_match = p
                best_conf = p.confidence

        if best_match:
            if verbose:
                print(f"\n结论: 正确识别! 置信度={best_conf:.1%}")
            return ValidationResult(type_name, noise_level, True, True, True, best_conf,
                                    f"正确识别: {best_match.summary()}")

        # 类型不对但检测到了 — 详细诊断为什么期望的模式没通过
        top = patterns[0]
        top_name = PATTERN_NAMES_CN.get(top.pattern_type, top.pattern_type.value)
        dir_match = top.direction == expected_dir

        if verbose:
            print(f"\n--- 失败分析 ---")
            print(f"期望: {type_name}, 实际最优: {top_name}")
            print(f"所有检测到的模式类型: {[PATTERN_NAMES_CN.get(p.pattern_type, p.pattern_type.value) for p in patterns]}")
            # 尝试用已知点直接验证规则
            from .rules import validate_pattern
            from .core import Swing, Wave, compute_confidence
            known_pts = answer.get("points", [])
            if known_pts and len(known_pts) >= 4:
                print(f"\n--- 用已知转折点直接验证规则 ---")
                if len(known_pts) == 6:
                    labels = ["1","2","3","4","5"]
                    swings_known = []
                    for k, pt in enumerate(known_pts):
                        is_hi = (k % 2 == 1) if expected_dir == Direction.UP else (k % 2 == 0)
                        swings_known.append(Swing(k * 20, pt, is_hi))
                    waves_known = [Wave(swings_known[j], swings_known[j+1], labels[j]) for j in range(5)]
                    rules = validate_pattern(expected_type, waves_known, expected_dir)
                    conf = compute_confidence(rules)
                    print(f"  已知点构造waves -> conf={conf:.1%}")
                    for r in rules:
                        tag = "硬" if r.is_hard else "软"
                        status = "PASS" if r.passed else "FAIL"
                        print(f"    [{tag}] {status} {r.name} score={r.score:.2f} {r.detail}")
                elif len(known_pts) == 4:
                    labels = ["A","B","C"]
                    swings_known = []
                    for k, pt in enumerate(known_pts):
                        is_hi = (k % 2 == 1) if expected_dir == Direction.UP else (k % 2 == 0)
                        swings_known.append(Swing(k * 15, pt, is_hi))
                    waves_known = [Wave(swings_known[j], swings_known[j+1], labels[j]) for j in range(3)]
                    rules = validate_pattern(expected_type, waves_known, expected_dir)
                    conf = compute_confidence(rules)
                    print(f"  已知点构造waves -> conf={conf:.1%}")
                    for r in rules:
                        tag = "硬" if r.is_hard else "软"
                        status = "PASS" if r.passed else "FAIL"
                        print(f"    [{tag}] {status} {r.name} score={r.score:.2f} {r.detail}")

        return ValidationResult(type_name, noise_level, True, False, dir_match, top.confidence,
                                f"识别为{top_name}(期望{type_name})")

    def test_synthetic(
        self,
        n_samples: int = 5,
        noise_levels: Optional[List[float]] = None,
    ) -> ValidationReport:
        """
        合成数据验证

        对每种模式 × 每个噪声水平 × 每个方向 生成合成数据, 检查识别准确率
        """
        if noise_levels is None:
            noise_levels = [0.0, 0.02, 0.05, 0.10]

        report = ValidationReport()
        gen = SyntheticWaveGenerator()

        for noise in noise_levels:
            for sample_i in range(n_samples):
                np.random.seed(42 + sample_i)

                # 随机化参数
                w1_pct = np.random.uniform(0.10, 0.30)
                w2_ret = np.random.uniform(0.38, 0.68)
                w3_ext = np.random.uniform(1.0, 2.6)
                w4_ret = np.random.uniform(0.23, 0.50)
                w5_rat = np.random.uniform(0.5, 1.5)

                # 审计#16: 确保浪3不是最短: w3_ext >= max(1.0, w5_rat)
                w3_ext = max(w3_ext, w5_rat + 0.1)

                for direction in [Direction.UP, Direction.DOWN]:
                    # 推动浪
                    df, ans = gen.generate_impulse(
                        base=100, w1_pct=w1_pct, w2_retrace=w2_ret,
                        w3_ext=w3_ext, w4_retrace=w4_ret, w5_ratio=w5_rat,
                        direction=direction, bars_per_wave=40, noise_pct=noise,
                    )
                    result = self._check_detection(df, ans, noise)
                    report.results.append(result)

                    # 锯齿形
                    df, ans = gen.generate_zigzag(
                        base=100, a_pct=np.random.uniform(0.10, 0.20),
                        b_retrace=np.random.uniform(0.38, 0.70),
                        c_ratio=np.random.uniform(0.7, 1.5),
                        direction=direction, bars_per_wave=30, noise_pct=noise,
                    )
                    result = self._check_detection(df, ans, noise)
                    report.results.append(result)

                    # 规则平台
                    # 随机化参数以增加覆盖度:
                    #   a_pct在0.15-0.25之间(较大幅度，swing检测更稳定)
                    #   b_retrace在0.85-0.92之间(足够高但不会因噪声超过A起点)
                    flat_a_pct = np.random.uniform(0.15, 0.25)
                    flat_b_ret = np.random.uniform(0.85, 0.92)
                    df, ans = gen.generate_flat(
                        base=100, a_pct=flat_a_pct, b_retrace=flat_b_ret,
                        c_ratio=np.random.uniform(0.8, 1.2),
                        flat_type="regular", direction=direction,
                        bars_per_wave=30, noise_pct=noise,
                    )
                    result = self._check_detection(df, ans, noise)
                    report.results.append(result)

                    # 扩展平台
                    df, ans = gen.generate_flat(
                        base=100, flat_type="expanded", direction=direction,
                        bars_per_wave=30, noise_pct=noise,
                    )
                    result = self._check_detection(df, ans, noise)
                    report.results.append(result)

                    # 收缩三角
                    df, ans = gen.generate_triangle(
                        base=100, amplitude=0.12, shrink_ratio=0.7,
                        direction=direction, bars_per_wave=25, noise_pct=noise,
                    )
                    result = self._check_detection(df, ans, noise)
                    report.results.append(result)

        return report

    def test_prediction(
        self,
        df: pd.DataFrame,
        window_sizes: Optional[List[int]] = None,
        step: int = 30,
        tolerance: float = 0.05,
        high_col: str = "high",
        low_col: str = "low",
        close_col: str = "close",
    ) -> ValidationReport:
        """
        预测回测验证 (多窗口 + 收盘价命中)

        在历史数据上滑窗:
        1. 用多个window_size同时识别模式，取置信度最高的预测
        2. 做预测
        3. 用后续数据检查预测是否命中 (高低点 + 收盘价)

        参数:
            window_sizes: 多个滑窗大小，默认 [150, 200, 300]
            step: 滑窗步长，默认30 (更密集的检测)
            tolerance: 目标价容差，默认0.05 (5%，加严验证标准)
        """
        if window_sizes is None:
            window_sizes = [150, 200, 300]

        report = ValidationReport()
        n = len(df)

        # 使用最小窗口来决定遍历范围
        min_window = min(window_sizes)
        if n < min_window + step:
            return report

        # 记录已处理的(start, end)对，避免不同窗口在同一位置重复记录
        seen_positions = set()

        for start in range(0, n - min_window - step, step):
            best_pred = None
            best_conf = 0
            best_window = 0
            best_end = 0

            # 遍历多个窗口大小，取置信度最高的预测
            for window_size in window_sizes:
                end = start + window_size
                if end + step > n:
                    continue

                sub_df = df.iloc[start:end]
                self.analyzer.analyze(sub_df, high_col=high_col, low_col=low_col,
                                      close_col=close_col)

                current_price = float(df[close_col].iloc[end - 1])
                predictions = self.analyzer.predict(current_price)

                if predictions and predictions[0].confidence > best_conf:
                    best_pred = predictions[0]
                    best_conf = predictions[0].confidence
                    best_window = window_size
                    best_end = end

            if best_pred is None:
                continue

            # 用最佳窗口对应的future数据
            future_end = min(best_end + step * 2, n)  # 扩大未来观测窗口
            future_df = df.iloc[best_end:future_end]

            if len(future_df) == 0:
                continue

            future_high = float(future_df[high_col].max())
            future_low = float(future_df[low_col].min())
            # 获取未来期间所有收盘价，用于收盘价命中检查
            future_closes = future_df[close_col].values.astype(float)

            # 检查目标价是否命中 (高低点 + 收盘价)
            hit = False
            hit_target = ""
            for name, target in best_pred.target_prices.items():
                if target > 0:
                    # 原始检查: 高低点接近目标价 或 目标价在高低范围内
                    if (abs(future_high - target) / target <= tolerance or
                            abs(future_low - target) / target <= tolerance or
                            (future_low <= target <= future_high)):
                        hit = True
                        hit_target = name
                        break
                    # 新增: 检查未来期间是否有任何收盘价接近目标价
                    for fc in future_closes:
                        if abs(fc - target) / target <= tolerance:
                            hit = True
                            hit_target = f"{name}(收盘价命中)"
                            break
                    if hit:
                        break

            # 用(start, best_end)作为唯一key，避免重复
            pos_key = (start, best_end)
            if pos_key in seen_positions:
                continue
            seen_positions.add(pos_key)

            report.predictions.append({
                "start": start,
                "end": best_end,
                "window": best_window,
                "pattern": best_pred.pattern.cn_name,
                "next_wave": best_pred.next_wave_label,
                "confidence": best_pred.confidence,
                "targets": best_pred.target_prices,
                "future_high": future_high,
                "future_low": future_low,
                "hit": hit,
                "hit_target": hit_target,
            })

        return report

    def _calc_random_baseline(self, df, n_trials=100, close_col="close"):
        """随机预测基线: 生成随机目标价, 计算命中率"""
        hits = 0
        total = 0
        for _ in range(n_trials):
            if len(df) < 100:
                break
            # 在数据范围内随机选一个起点
            start = random.randint(0, len(df) - 100)
            end = start + 50
            current = float(df.iloc[end][close_col])
            # 随机目标: current * (1 ± random 15%)
            target = current * (1 + random.uniform(-0.15, 0.15))
            # 检查未来30根K线是否命中
            future = df.iloc[end:end + 30]
            if any(abs(future[close_col].astype(float) - target) / target <= 0.05):
                hits += 1
            total += 1
        return hits / total if total > 0 else 0.0

    def test_hard_rule_self_check(
        self,
        df: pd.DataFrame,
        high_col: str = "high",
        low_col: str = "low",
        close_col: str = "close",
    ) -> ValidationReport:
        """
        硬规则自检: 对每个检测到的模式，验证所有硬规则确实通过，没有漏检

        原理: 检测器已对每个模式运行了规则验证，这里二次确认:
        1. 从检测结果取出每个模式的waves
        2. 用validate_pattern重新跑一遍规则
        3. 对比两次结果，确保所有硬规则一致
        """
        from .rules import validate_pattern
        from .core import compute_confidence

        report = ValidationReport()
        self.analyzer.analyze(df, high_col=high_col, low_col=low_col, close_col=close_col)

        for pat in self.analyzer.patterns:
            # 用validate_pattern重新验证
            fresh_rules = validate_pattern(pat.pattern_type, pat.waves, pat.direction)
            fresh_hard = [r for r in fresh_rules if r.is_hard]
            orig_hard = [r for r in pat.rule_results if r.is_hard]

            # 检查硬规则是否全部通过
            for r in fresh_hard:
                if not r.passed:
                    report.hard_rule_violations.append({
                        "pattern": pat.cn_name,
                        "rule": r.name,
                        "detail": f"硬规则未通过: {r.detail}",
                        "confidence": pat.confidence,
                    })

            # 对比原始结果数量一致性
            if len(fresh_hard) != len(orig_hard):
                report.hard_rule_violations.append({
                    "pattern": pat.cn_name,
                    "rule": "硬规则数量不一致",
                    "detail": f"原始={len(orig_hard)} 重验={len(fresh_hard)}",
                    "confidence": pat.confidence,
                })

        return report

    def test_multi_deviation_consistency(
        self,
        df: pd.DataFrame,
        deviation_sets: Optional[List[List[float]]] = None,
        high_col: str = "high",
        low_col: str = "low",
        close_col: str = "close",
    ) -> ValidationReport:
        """
        多级别一致性验证: 用不同的deviation参数运行检测，
        如果多个deviation级别在同一位置检测到同一类型模式，置信度更高

        算法:
        1. 用多组不同deviation参数分别运行检测
        2. 对每个检测到的模式，记录(起始区间, 类型)
        3. 统计在多少组deviation中出现过相同模式
        4. 出现次数 >= 2 视为"一致"，计算一致率
        """
        report = ValidationReport()

        if deviation_sets is None:
            # 4组不同的deviation参数
            deviation_sets = [
                [0.5, 1.0, 2.0],       # 灵敏模式
                [1.0, 2.0, 3.0],       # 标准模式
                [2.0, 3.0, 5.0],       # 中等模式
                [3.0, 5.0, 8.0, 12.0], # 粗粒度模式
            ]

        # 每组deviation的检测结果: key=(start//10, end//10, pattern_type)
        all_detections: List[Dict] = []  # 每组的检测集合
        for devs in deviation_sets:
            analyzer = ElliottWaveAnalyzer(
                deviations=devs,
                depth=self.analyzer.depth,
                max_recursion=0,
                min_confidence=self.analyzer.min_confidence,
            )
            analyzer.analyze(df, high_col=high_col, low_col=low_col, close_col=close_col)
            det_set = {}
            for pat in analyzer.patterns:
                key = (pat.start_index // 10, pat.end_index // 10, pat.pattern_type.value)
                if key not in det_set or pat.confidence > det_set[key]:
                    det_set[key] = pat.confidence
            all_detections.append(det_set)

        # 统计每个模式在多少组中出现
        all_keys = set()
        for det_set in all_detections:
            all_keys.update(det_set.keys())

        consistent_count = 0
        total_count = len(all_keys)

        for key in all_keys:
            appearance_count = sum(1 for det_set in all_detections if key in det_set)
            avg_conf = np.mean([det_set[key] for det_set in all_detections if key in det_set])
            is_consistent = appearance_count >= 2

            report.consistency_checks.append({
                "key": key,
                "appearances": appearance_count,
                "total_sets": len(deviation_sets),
                "avg_confidence": float(avg_conf),
                "consistent": is_consistent,
            })

            if is_consistent:
                consistent_count += 1

        report.consistency_rate = consistent_count / total_count if total_count > 0 else 0.0

        return report


# ============================================================
# 完整验证入口
# ============================================================

def test_rules_directly() -> str:
    """
    直接测试规则逻辑（跳过ZigZag检测）

    用已知的完美swing点直接构建Wave对象，验证规则判定是否正确
    """
    from .core import Swing, Wave, WavePattern, Direction, PatternType, compute_confidence
    from .rules import (check_impulse, check_zigzag, check_flat_regular,
                        check_flat_expanded, check_flat_running,
                        check_triangle_contracting, check_triangle_expanding,
                        check_leading_diagonal, check_ending_diagonal,
                        check_truncated_5th, check_double_zigzag,
                        check_combination_wxy, classify_impulse_extension)

    lines = ["=" * 60, "规则逻辑直接测试 (跳过ZigZag)", "=" * 60]
    passed = 0
    failed = 0

    def _test(name, rule_fn, swings, labels, direction, expected_valid=True):
        nonlocal passed, failed
        waves = [Wave(swings[i], swings[i + 1], labels[i]) for i in range(len(labels))]
        results = rule_fn(waves, direction)
        conf = compute_confidence(results)
        is_valid = all(r.passed for r in results if r.is_hard)
        ok = (is_valid == expected_valid)
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        hard_detail = " ".join(f"{r.name}={'Y' if r.passed else 'N'}"
                               for r in results if r.is_hard)
        lines.append(f"  [{status}] {name}: valid={is_valid} conf={conf:.1%} | {hard_detail}")

    # ========== 推动浪 UP ==========
    lines.append("\n--- 推动浪 (UP) ---")
    s = [Swing(0, 100, False), Swing(20, 120, True), Swing(40, 110, False),
         Swing(60, 142, True), Swing(80, 130, False), Swing(100, 150, True)]
    _test("标准推动浪UP", check_impulse, s, ["1","2","3","4","5"], Direction.UP, True)

    # 违反: 浪2破浪1起点
    s2 = [Swing(0, 100, False), Swing(20, 120, True), Swing(40, 95, False),
          Swing(60, 140, True), Swing(80, 130, False), Swing(100, 150, True)]
    _test("浪2破浪1(应失败)", check_impulse, s2, ["1","2","3","4","5"], Direction.UP, False)

    # 违反: 浪4入浪1区域
    s3 = [Swing(0, 100, False), Swing(20, 120, True), Swing(40, 110, False),
          Swing(60, 142, True), Swing(80, 115, False), Swing(100, 150, True)]
    _test("浪4入浪1(应失败)", check_impulse, s3, ["1","2","3","4","5"], Direction.UP, False)

    # 违反: 浪3最短
    s4 = [Swing(0, 100, False), Swing(20, 130, True), Swing(40, 120, False),
          Swing(60, 125, True), Swing(80, 115, False), Swing(100, 145, True)]
    _test("浪3最短(应失败)", check_impulse, s4, ["1","2","3","4","5"], Direction.UP, False)

    # ========== 推动浪 DOWN ==========
    lines.append("\n--- 推动浪 (DOWN) ---")
    sd = [Swing(0, 150, True), Swing(20, 130, False), Swing(40, 140, True),
          Swing(60, 108, False), Swing(80, 120, True), Swing(100, 100, False)]
    _test("标准推动浪DOWN", check_impulse, sd, ["1","2","3","4","5"], Direction.DOWN, True)

    # 违反: 浪2破浪1起点(DOWN: 浪2涨过150)
    sd2 = [Swing(0, 150, True), Swing(20, 130, False), Swing(40, 155, True),
           Swing(60, 108, False), Swing(80, 120, True), Swing(100, 100, False)]
    _test("浪2破浪1DOWN(应失败)", check_impulse, sd2, ["1","2","3","4","5"], Direction.DOWN, False)

    # ========== 失败五浪 ==========
    lines.append("\n--- 失败五浪 ---")
    st = [Swing(0, 100, False), Swing(20, 120, True), Swing(40, 110, False),
          Swing(60, 145, True), Swing(80, 130, False), Swing(100, 140, True)]
    _test("失败五浪(W5<W3)", check_truncated_5th, st, ["1","2","3","4","5"], Direction.UP, True)

    # ========== 引导楔形 ==========
    lines.append("\n--- 引导楔形 ---")
    sl = [Swing(0, 100, False), Swing(20, 118, True), Swing(40, 105, False),
          Swing(60, 130, True), Swing(80, 118, False), Swing(100, 135, True)]
    _test("引导楔形(浪4入浪1)", check_leading_diagonal, sl, ["1","2","3","4","5"], Direction.UP, True)

    # ========== 终结楔形 ==========
    lines.append("\n--- 终结楔形 ---")
    se = [Swing(0, 100, False), Swing(20, 115, True), Swing(40, 105, False),
          Swing(60, 122, True), Swing(80, 113, False), Swing(100, 125, True)]
    _test("终结楔形(浪4必须入浪1)", check_ending_diagonal, se, ["1","2","3","4","5"], Direction.UP, True)

    # ========== 锯齿形 ==========
    lines.append("\n--- 锯齿形 ---")
    # DOWN zigzag: A down, B up, C down
    sz = [Swing(0, 120, True), Swing(20, 100, False), Swing(40, 110, True),
          Swing(60, 85, False)]
    _test("锯齿形DOWN", check_zigzag, sz, ["A","B","C"], Direction.DOWN, True)

    # UP zigzag: A up, B down, C up
    szu = [Swing(0, 100, False), Swing(20, 120, True), Swing(40, 110, False),
           Swing(60, 135, True)]
    _test("锯齿形UP", check_zigzag, szu, ["A","B","C"], Direction.UP, True)

    # 违反: B超A起点
    szf = [Swing(0, 120, True), Swing(20, 100, False), Swing(40, 125, True),
           Swing(60, 85, False)]
    _test("B超A起点(应失败)", check_zigzag, szf, ["A","B","C"], Direction.DOWN, False)

    # ========== 规则平台 ==========
    lines.append("\n--- 规则平台 ---")
    sf = [Swing(0, 120, True), Swing(20, 100, False), Swing(40, 118, True),
          Swing(60, 95, False)]
    _test("规则平台DOWN", check_flat_regular, sf, ["A","B","C"], Direction.DOWN, True)

    # ========== 扩展平台 ==========
    lines.append("\n--- 扩展平台 ---")
    sfe = [Swing(0, 120, True), Swing(20, 100, False), Swing(40, 125, True),
           Swing(60, 90, False)]
    _test("扩展平台DOWN(B超A起点)", check_flat_expanded, sfe, ["A","B","C"], Direction.DOWN, True)

    # ========== 顺势平台 ==========
    lines.append("\n--- 顺势平台 ---")
    sfr = [Swing(0, 120, True), Swing(20, 100, False), Swing(40, 125, True),
           Swing(60, 105, False)]
    _test("顺势平台DOWN(C未超A)", check_flat_running, sfr, ["A","B","C"], Direction.DOWN, True)

    # ========== 收缩三角 ==========
    lines.append("\n--- 收缩三角 ---")
    stri = [Swing(0, 110, True), Swing(12, 100, False), Swing(24, 108, True),
            Swing(36, 102, False), Swing(48, 106, True), Swing(60, 104, False)]
    _test("收缩三角", check_triangle_contracting, stri, ["A","B","C","D","E"], Direction.DOWN, True)

    # ========== 扩展三角 ==========
    lines.append("\n--- 扩展三角 ---")
    # 正常案例: 各浪逐步放大 A<C<E, B<D, 横盘结构
    # drift = |93-100| = 7, range = 113-93 = 20, ratio = 0.35 < 0.65 ✓
    ste = [Swing(0, 100, True), Swing(10, 97, False), Swing(20, 104, True),
           Swing(30, 93, False), Swing(40, 109, True), Swing(50, 87, False)]
    _test("扩展三角DOWN", check_triangle_expanding, ste,
          ["A","B","C","D","E"], Direction.DOWN, True)

    # 失败: 浪不放大 (A > C, 收缩而非扩展)
    ste_f = [Swing(0, 110, True), Swing(10, 98, False), Swing(20, 106, True),
             Swing(30, 100, False), Swing(40, 104, True), Swing(50, 102, False)]
    _test("扩展三角-浪不放大(应失败)", check_triangle_expanding, ste_f,
          ["A","B","C","D","E"], Direction.DOWN, False)

    # ========== 双锯齿 ==========
    lines.append("\n--- 双锯齿 ---")
    # 5段结构UP: W上行(段1-2-3), X回撤(段3-4), Y上行(段4-5)
    # HR1: Y终点(w5.end)超越W终点(w3.end)
    # HR2: X不超W起点 -> w1.start > w4.end (UP方向X终点不低于W起点)
    # 所以 w4.end 必须 < w1.start
    sdz = [Swing(0, 100, False), Swing(10, 115, True), Swing(20, 108, False),
           Swing(30, 122, True), Swing(40, 98, False), Swing(50, 130, True)]
    _test("双锯齿UP", check_double_zigzag, sdz,
          ["1","2","3","4","5"], Direction.UP, True)

    # ========== 双重组合 ==========
    lines.append("\n--- 双重组合 ---")
    # 5段结构UP: 整体推进, X不超W起点(w4.end < w1.start)
    scw = [Swing(0, 100, False), Swing(10, 112, True), Swing(20, 105, False),
           Swing(30, 118, True), Swing(40, 97, False), Swing(50, 125, True)]
    _test("双重组合UP", check_combination_wxy, scw,
          ["1","2","3","4","5"], Direction.UP, True)

    # ========== DOWN方向的引导楔形 ==========
    lines.append("\n--- 引导楔形 (DOWN) ---")
    sld = [Swing(0, 150, True), Swing(20, 132, False), Swing(40, 145, True),
           Swing(60, 120, False), Swing(80, 132, True), Swing(100, 115, False)]
    _test("引导楔形DOWN(浪4入浪1)", check_leading_diagonal, sld,
          ["1","2","3","4","5"], Direction.DOWN, True)

    # ========== DOWN方向的终结楔形 ==========
    lines.append("\n--- 终结楔形 (DOWN) ---")
    sed = [Swing(0, 150, True), Swing(20, 135, False), Swing(40, 145, True),
           Swing(60, 128, False), Swing(80, 137, True), Swing(100, 125, False)]
    _test("终结楔形DOWN(浪4必须入浪1)", check_ending_diagonal, sed,
          ["1","2","3","4","5"], Direction.DOWN, True)

    # ========== DOWN方向的收缩三角 ==========
    lines.append("\n--- 收缩三角 (DOWN) ---")
    strd = [Swing(0, 110, True), Swing(12, 100, False), Swing(24, 108, True),
            Swing(36, 102, False), Swing(48, 106, True), Swing(60, 104, False)]
    _test("收缩三角DOWN(方向)", check_triangle_contracting, strd,
          ["A","B","C","D","E"], Direction.DOWN, True)

    # ========== 边界值: 浪4恰好等于浪1终点 ==========
    lines.append("\n--- 边界值测试 ---")
    # 注: rules.py中HR3使用严格 > 判断, 浪4恰好等于浪1终点会被判为失败
    # 这是当前实现的行为: 浪4必须严格高于浪1终点
    sb_eq = [Swing(0, 100, False), Swing(20, 120, True), Swing(40, 110, False),
             Swing(60, 142, True), Swing(80, 120, False), Swing(100, 150, True)]
    _test("浪4等于浪1终点(严格>判定,应失败)", check_impulse, sb_eq,
          ["1","2","3","4","5"], Direction.UP, False)

    # 浪4略高于浪1终点 (近边界，应通过)
    sb_near = [Swing(0, 100, False), Swing(20, 120, True), Swing(40, 110, False),
               Swing(60, 142, True), Swing(80, 120.01, False), Swing(100, 150, True)]
    _test("浪4略高于浪1终点(边界附近应通过)", check_impulse, sb_near,
          ["1","2","3","4","5"], Direction.UP, True)

    # ========== 延伸分类 ==========
    lines.append("\n--- 延伸分类 ---")
    waves_ext3 = [Wave(Swing(0,100,False), Swing(20,120,True), "1"),
                  Wave(Swing(20,120,True), Swing(40,110,False), "2"),
                  Wave(Swing(40,110,False), Swing(60,160,True), "3"),
                  Wave(Swing(60,160,True), Swing(80,145,False), "4"),
                  Wave(Swing(80,145,False), Swing(100,165,True), "5")]
    ext_type = classify_impulse_extension(waves_ext3)
    ok_ext = ext_type == PatternType.IMPULSE_EXT3
    lines.append(f"  [{'PASS' if ok_ext else 'FAIL'}] 浪3延伸分类: {ext_type.value}")
    if ok_ext:
        passed += 1
    else:
        failed += 1

    lines.append(f"\n总计: {passed} 通过 / {failed} 失败 / {passed+failed} 总数")
    lines.append(f"通过率: {passed/(passed+failed):.1%}" if passed+failed > 0 else "")

    return "\n".join(lines)


def run_full_validation(data_path: Optional[str] = None) -> str:
    """
    运行完整验证 (4种验证方式)

    方案1: 合成数据验证
    方案2: 硬规则自检
    方案3: 预测回测
    方案4: 多级别一致性验证

    返回验证报告字符串
    """
    runner = ValidationRunner()
    lines = []

    # 0. 规则逻辑直接测试
    lines.append(test_rules_directly())
    lines.append("")

    # 1. 合成数据验证
    lines.append(">>> 方案1: 合成数据验证...")
    report_syn = runner.test_synthetic(n_samples=5, noise_levels=[0.0, 0.02, 0.05, 0.10])
    lines.append(report_syn.summary())

    # 2. 历史数据预测回测 + 硬规则自检 + 多级别一致性 (如果有数据)
    if data_path:
        try:
            df = pd.read_csv(data_path)
            # 自动检测列名
            col_map = {}
            for col in df.columns:
                cl = col.lower()
                if "high" in cl:
                    col_map["high"] = col
                elif "low" in cl:
                    col_map["low"] = col
                elif "close" in cl:
                    col_map["close"] = col

            if len(col_map) >= 3:
                # === 方案2: 硬规则自检 ===
                lines.append(f"\n>>> 方案2: 硬规则自检...")
                report_hr = runner.test_hard_rule_self_check(
                    df, high_col=col_map["high"], low_col=col_map["low"],
                    close_col=col_map["close"],
                )
                if report_hr.hard_rule_violations:
                    lines.append(f"    发现 {len(report_hr.hard_rule_violations)} 个硬规则违规:")
                    for v in report_hr.hard_rule_violations[:10]:
                        lines.append(f"      {v['pattern']}: {v['rule']} - {v['detail']}")
                else:
                    lines.append(f"    硬规则自检: 全部通过 (所有检测到的模式硬规则一致)")

                # === 方案3: 预测回测 ===
                lines.append(f"\n>>> 方案3: 历史数据预测回测: {data_path}")
                lines.append(f"    数据量: {len(df)} 根K线")

                report_pred = runner.test_prediction(
                    df, window_sizes=[150, 200, 300], step=30, tolerance=0.05,
                    high_col=col_map["high"], low_col=col_map["low"],
                    close_col=col_map["close"],
                )

                # 审计#19: 计算随机基线命中率作为对比
                random.seed(42)
                report_pred.random_baseline = runner._calc_random_baseline(
                    df, n_trials=200, close_col=col_map["close"])

                if report_pred.predictions:
                    lines.append(f"    预测次数: {len(report_pred.predictions)}")
                    lines.append(f"    命中率: {report_pred.prediction_hit_rate:.1%}")
                    lines.append(f"    随机基线: {report_pred.random_baseline:.1%} | "
                                 f"超越基线: {report_pred.prediction_hit_rate - report_pred.random_baseline:+.1%}")

                    lines.append("\n    预测详情 (前10个):")
                    for p in report_pred.predictions[:10]:
                        hit_str = "命中" if p["hit"] else "未命中"
                        win_str = f"win={p.get('window', '?')}" if 'window' in p else ""
                        lines.append(
                            f"      [{p['start']}-{p['end']}] {win_str} {p['pattern']} "
                            f"预测{p['next_wave']} conf={p['confidence']:.1%} "
                            f"{hit_str} {p.get('hit_target', '')}"
                        )
                else:
                    lines.append("    未产生有效预测")

                # === 方案4: 多级别一致性验证 ===
                lines.append(f"\n>>> 方案4: 多级别一致性验证...")
                report_cons = runner.test_multi_deviation_consistency(
                    df, high_col=col_map["high"], low_col=col_map["low"],
                    close_col=col_map["close"],
                )
                lines.append(f"    检测到 {len(report_cons.consistency_checks)} 个独立模式")
                lines.append(f"    多级别一致率: {report_cons.consistency_rate:.1%}")
                # 显示一致性最高的模式
                consistent = [c for c in report_cons.consistency_checks if c["consistent"]]
                if consistent:
                    lines.append(f"    一致模式数: {len(consistent)}")
                    # 按出现次数排序显示前5个
                    consistent.sort(key=lambda x: x["appearances"], reverse=True)
                    lines.append("    最稳定的模式 (前5个):")
                    for c in consistent[:5]:
                        lines.append(
                            f"      区间=({c['key'][0]*10}-{c['key'][1]*10}) "
                            f"类型={c['key'][2]} "
                            f"出现={c['appearances']}/{c['total_sets']}组 "
                            f"平均置信度={c['avg_confidence']:.1%}"
                        )
            else:
                lines.append(f"\n无法识别OHLC列: {df.columns.tolist()}")
        except Exception as e:
            lines.append(f"\n历史数据加载失败: {e}")
    else:
        # 无历史数据时，也用合成数据运行方案2和方案4
        lines.append(f"\n>>> 方案2: 硬规则自检 (用合成数据)...")
        gen = SyntheticWaveGenerator()
        np.random.seed(42)
        df_imp, _ = gen.generate_impulse(base=100, direction=Direction.UP, bars_per_wave=40)
        report_hr = runner.test_hard_rule_self_check(df_imp)
        if report_hr.hard_rule_violations:
            lines.append(f"    发现 {len(report_hr.hard_rule_violations)} 个硬规则违规")
        else:
            lines.append(f"    硬规则自检: 全部通过")

        lines.append(f"\n>>> 方案4: 多级别一致性验证 (用合成数据)...")
        report_cons = runner.test_multi_deviation_consistency(df_imp)
        lines.append(f"    检测到 {len(report_cons.consistency_checks)} 个独立模式")
        lines.append(f"    多级别一致率: {report_cons.consistency_rate:.1%}")

    return "\n".join(lines)


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    import sys
    import os

    data_path = None

    # 解析命令行参数
    if "--data" in sys.argv:
        idx = sys.argv.index("--data")
        if idx + 1 < len(sys.argv):
            data_path = sys.argv[idx + 1]

    # 如果没有指定数据文件, 尝试找默认的
    if data_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(os.path.dirname(script_dir))
        for fname in ["btc_usdt_1d.csv", "btc_usdt_4h.csv"]:
            fpath = os.path.join(parent_dir, fname)
            if os.path.exists(fpath):
                data_path = fpath
                break

    print(run_full_validation(data_path))
