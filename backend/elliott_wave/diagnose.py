"""
Elliott Wave 失败案例根因诊断脚本

对每种模式生成noise=5%的合成数据，运行检测并打印:
- 已知转折点
- 每个deviation检测到的swing点
- 所有候选模式及其置信度
- 正确模式的排名
- 如果正确模式不在结果中，打印失败原因
"""

import numpy as np
import sys
import os

# 确保项目根目录在path中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from App.elliott_wave.core import (
    PatternType,
    Direction,
    PATTERN_NAMES_CN,
    Swing,
    Wave,
    compute_confidence,
    MOTIVE_TYPES,
    CORRECTIVE_TYPES,
)
from App.elliott_wave.detector import ElliottWaveAnalyzer, detect_swings
from App.elliott_wave.rules import validate_pattern
from App.elliott_wave.validator import SyntheticWaveGenerator


def diagnose_single(name: str, df, answer: dict, analyzer: ElliottWaveAnalyzer):
    """诊断单个测试案例"""
    expected_type = answer["type"]
    expected_dir = answer["direction"]
    type_name = PATTERN_NAMES_CN.get(expected_type, expected_type.value)
    dir_str = "UP" if expected_dir == Direction.UP else "DOWN"
    points = answer.get("points", [])

    print(f"\n{'=' * 70}")
    print(f"模式: {type_name} 方向: {dir_str}")
    print(f"{'=' * 70}")
    print(f"已知转折点: {[f'{p:.2f}' for p in points]}")
    print(f"数据长度: {len(df)} bars")

    # 运行分析
    analyzer.analyze(df)
    patterns = analyzer.patterns

    # 打印每个deviation的swing点
    print(f"\n--- Swing点检测 ---")
    for dev, swings in sorted(analyzer._swings_by_dev.items()):
        prices = [f"{'H' if s.is_high else 'L'}{s.price:.1f}@{s.index}" for s in swings]
        print(f"  dev={dev}: {len(swings)}个 -> {prices}")

    # 打印所有检测到的模式
    print(f"\n--- 检测到 {len(patterns)} 个模式 ---")

    # 同族匹配逻辑
    impulse_family = {PatternType.IMPULSE, PatternType.IMPULSE_EXT1, PatternType.IMPULSE_EXT3, PatternType.IMPULSE_EXT5}
    flat_family = {PatternType.FLAT_REGULAR, PatternType.FLAT_EXPANDED, PatternType.FLAT_RUNNING}

    correct_rank = None
    for i, p in enumerate(patterns):
        pname = PATTERN_NAMES_CN.get(p.pattern_type, p.pattern_type.value)
        pdir = "UP" if p.direction == Direction.UP else "DOWN"
        wave_str = " | ".join([f"{w.label}:{w.start.price:.1f}->{w.end.price:.1f}" for w in p.waves])

        # 检查是否匹配
        type_match = (
            p.pattern_type == expected_type
            or (expected_type in impulse_family and p.pattern_type in impulse_family)
            or (expected_type in flat_family and p.pattern_type in flat_family)
        )
        dir_match = p.direction == expected_dir
        is_correct = type_match and dir_match
        marker = " <<<正确>>>" if is_correct else ""

        if is_correct and correct_rank is None:
            correct_rank = i + 1

        print(f"  [{i + 1}] {pname} {pdir} conf={p.confidence:.1%}{marker}")
        print(f"      {wave_str}")

        # 失败的硬规则
        failed_hard = [r for r in p.rule_results if r.is_hard and not r.passed]
        if failed_hard:
            print(f"      硬规则失败: {[r.name for r in failed_hard]}")

    # 结论
    print(f"\n--- 结论 ---")
    if correct_rank:
        print(f"  正确模式排名: #{correct_rank}/{len(patterns)}")
        if correct_rank > 1:
            print(
                f"  排名第1的是: {PATTERN_NAMES_CN.get(patterns[0].pattern_type, '?')} conf={patterns[0].confidence:.1%}"
            )
    elif len(patterns) > 0:
        print(f"  正确模式({type_name})未出现在结果中!")
        # 用已知点直接构造验证
        _diagnose_why_missing(expected_type, expected_dir, points)
    else:
        print(f"  未检测到任何模式!")

    return correct_rank


def _diagnose_why_missing(expected_type, expected_dir, points):
    """诊断为什么期望的模式没有被检测到"""
    if not points or len(points) < 4:
        print(f"  (无法用已知点诊断)")
        return

    d = 1 if expected_dir == Direction.UP else -1
    n_waves = len(points) - 1

    if n_waves == 5:
        labels = ["1", "2", "3", "4", "5"]
        if expected_type in CORRECTIVE_TYPES:
            labels = ["A", "B", "C", "D", "E"]
    elif n_waves == 3:
        labels = ["A", "B", "C"]
    else:
        print(f"  (不支持的波浪数: {n_waves})")
        return

    swings = []
    for k, pt in enumerate(points):
        if expected_dir == Direction.UP:
            is_hi = k % 2 == 1
        else:
            is_hi = k % 2 == 0
        swings.append(Swing(k * 20, pt, is_hi))

    waves = [Wave(swings[j], swings[j + 1], labels[j]) for j in range(n_waves)]
    rules = validate_pattern(expected_type, waves, expected_dir)
    conf = compute_confidence(rules)

    print(f"\n  用已知转折点直接验证规则 (conf={conf:.1%}):")
    for r in rules:
        tag = "硬" if r.is_hard else "软"
        status = "PASS" if r.passed else "FAIL"
        print(f"    [{tag}] {status} {r.name} score={r.score:.2f} {r.detail}")


def run_diagnosis():
    """对每种模式运行诊断"""
    gen = SyntheticWaveGenerator()
    analyzer = ElliottWaveAnalyzer(
        deviations=[0.5, 1.0, 2.0, 3.0],
        depth=3,
        max_recursion=0,
        min_confidence=0.15,
    )

    noise = 0.05
    np.random.seed(42)

    results = {}

    # 1. 推动浪 UP
    df, ans = gen.generate_impulse(
        base=100,
        w1_pct=0.20,
        w2_retrace=0.50,
        w3_ext=1.618,
        w4_retrace=0.382,
        w5_ratio=1.0,
        direction=Direction.UP,
        bars_per_wave=40,
        noise_pct=noise,
    )
    results["推动浪UP"] = diagnose_single("推动浪UP", df, ans, analyzer)

    # 2. 推动浪 DOWN
    df, ans = gen.generate_impulse(
        base=100,
        w1_pct=0.20,
        w2_retrace=0.50,
        w3_ext=1.618,
        w4_retrace=0.382,
        w5_ratio=1.0,
        direction=Direction.DOWN,
        bars_per_wave=40,
        noise_pct=noise,
    )
    results["推动浪DOWN"] = diagnose_single("推动浪DOWN", df, ans, analyzer)

    # 3. 锯齿形 DOWN
    df, ans = gen.generate_zigzag(
        base=100, a_pct=0.15, b_retrace=0.50, c_ratio=1.0, direction=Direction.DOWN, bars_per_wave=30, noise_pct=noise
    )
    results["锯齿形DOWN"] = diagnose_single("锯齿形DOWN", df, ans, analyzer)

    # 4. 锯齿形 UP
    df, ans = gen.generate_zigzag(
        base=100, a_pct=0.15, b_retrace=0.50, c_ratio=1.0, direction=Direction.UP, bars_per_wave=30, noise_pct=noise
    )
    results["锯齿形UP"] = diagnose_single("锯齿形UP", df, ans, analyzer)

    # 5. 规则平台
    df, ans = gen.generate_flat(
        base=100,
        a_pct=0.20,
        b_retrace=0.90,
        c_ratio=1.0,
        flat_type="regular",
        direction=Direction.DOWN,
        bars_per_wave=30,
        noise_pct=noise,
    )
    results["规则平台DOWN"] = diagnose_single("规则平台DOWN", df, ans, analyzer)

    # 6. 扩展平台
    df, ans = gen.generate_flat(
        base=100, flat_type="expanded", direction=Direction.DOWN, bars_per_wave=30, noise_pct=noise
    )
    results["扩展平台DOWN"] = diagnose_single("扩展平台DOWN", df, ans, analyzer)

    # 7. 收缩三角 DOWN
    df, ans = gen.generate_triangle(
        base=100, amplitude=0.12, shrink_ratio=0.7, direction=Direction.DOWN, bars_per_wave=25, noise_pct=noise
    )
    results["收缩三角DOWN"] = diagnose_single("收缩三角DOWN", df, ans, analyzer)

    # 8. 收缩三角 UP
    df, ans = gen.generate_triangle(
        base=100, amplitude=0.12, shrink_ratio=0.7, direction=Direction.UP, bars_per_wave=25, noise_pct=noise
    )
    results["收缩三角UP"] = diagnose_single("收缩三角UP", df, ans, analyzer)

    # ========== noise=0% 测试 ==========
    print(f"\n\n{'#' * 70}")
    print(f"# noise=0% 专项诊断")
    print(f"{'#' * 70}")
    np.random.seed(42)

    # noise=0% 推动浪
    df, ans = gen.generate_impulse(
        base=100,
        w1_pct=0.20,
        w2_retrace=0.50,
        w3_ext=1.618,
        w4_retrace=0.382,
        w5_ratio=1.0,
        direction=Direction.UP,
        bars_per_wave=40,
        noise_pct=0.0,
    )
    results["推动浪UP_noise0"] = diagnose_single("推动浪UP noise=0%", df, ans, analyzer)

    # noise=0% 锯齿形
    df, ans = gen.generate_zigzag(
        base=100, a_pct=0.15, b_retrace=0.50, c_ratio=1.0, direction=Direction.DOWN, bars_per_wave=30, noise_pct=0.0
    )
    results["锯齿形DOWN_noise0"] = diagnose_single("锯齿形DOWN noise=0%", df, ans, analyzer)

    # noise=0% 收缩三角
    df, ans = gen.generate_triangle(
        base=100, amplitude=0.12, shrink_ratio=0.7, direction=Direction.DOWN, bars_per_wave=25, noise_pct=0.0
    )
    results["收缩三角DOWN_noise0"] = diagnose_single("收缩三角DOWN noise=0%", df, ans, analyzer)

    # 汇总
    print(f"\n\n{'=' * 70}")
    print(f"诊断汇总")
    print(f"{'=' * 70}")
    for name, rank in results.items():
        if rank == 1:
            status = "正确(#1)"
        elif rank:
            status = f"正确但排名#{rank}"
        else:
            status = "未检测到"
        print(f"  {name:25s}: {status}")


if __name__ == "__main__":
    run_diagnosis()
