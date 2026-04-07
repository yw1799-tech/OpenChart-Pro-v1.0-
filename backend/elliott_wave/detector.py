"""
Elliott Wave 检测引擎

核心流程:
  1. 多粒度ZigZag检测swing点
  2. 滑窗匹配: 6点窗口(5浪模式) / 4点窗口(3浪模式)
  3. 规则验证 + 置信度评分
  4. 可选递归子浪分析
  5. 多重计数排名
"""

import numpy as np
import pandas as pd
from typing import List, Optional, Dict, Tuple
from .core import (
    Swing, Wave, WavePattern, Prediction, Direction, PatternType,
    MOTIVE_TYPES, CORRECTIVE_TYPES, PATTERN_NAMES_CN,
    compute_confidence, fib_score, FIB_RATIOS,
)
from .rules import validate_pattern, classify_impulse_extension, check_volume_confirmation


# ============================================================
# ZigZag Swing 检测 (内置, 不依赖外部)
# ============================================================

def _calc_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
              period: int = 10) -> np.ndarray:
    """计算ATR (RMA方式, 与TradingView一致)"""
    n = len(highs)
    atr = np.zeros(n)
    if n < 2:
        return atr
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    p = min(period, n)
    atr[p - 1] = np.mean(tr[:p])
    for i in range(p, n):
        atr[i] = (atr[i - 1] * (p - 1) + tr[i]) / p
    atr[:p - 1] = atr[p - 1]
    return atr


def detect_swings(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    deviation: float = 3.0,
    depth: int = 10,
    timestamps: Optional[np.ndarray] = None,
) -> List[Swing]:
    """
    ZigZag swing检测 (动态ATR deviation)

    deviation = ATR(10)/close*100 * multiplier
    """
    n = len(highs)

    # 自适应depth: 低波动率时降低depth，避免光滑数据检测不到pivot
    atr_check = _calc_atr(highs, lows, closes, period=10)
    avg_close = np.mean(closes[-min(20, n):])
    atr_ratio = np.mean(atr_check[-min(20, n):]) / avg_close if avg_close > 0 else 1.0
    use_relaxed = False  # 是否使用宽松比较(>=代替>)
    if atr_ratio < 0.001:
        # 极低波动(接近无噪声): depth=1，用宽松比较
        depth = 1
        use_relaxed = True
    elif atr_ratio < 0.003:
        depth = min(depth, 2)
        use_relaxed = True
    elif atr_ratio < 0.005:
        depth = min(depth, 3)

    if n < depth * 2 + 1:
        return []

    atr = _calc_atr(highs, lows, closes, period=10)
    dev_thresholds = np.where(closes > 0, atr / closes * 100.0 * deviation, deviation)

    # 找候选pivot
    cand_hi = {}
    cand_lo = {}
    for i in range(depth, n - depth):
        if use_relaxed:
            # 宽松模式: 允许相等(>=)，适合光滑数据
            is_hi = all(highs[i] >= highs[i - j] and highs[i] >= highs[i + j]
                        for j in range(1, depth + 1))
            # 额外要求: 至少比某个邻居严格大(排除完全平坦)
            if is_hi:
                is_hi = any(highs[i] > highs[i - j] or highs[i] > highs[i + j]
                            for j in range(1, depth + 1))
        else:
            is_hi = all(highs[i] > highs[i - j] and highs[i] > highs[i + j]
                        for j in range(1, depth + 1))
        if is_hi:
            cand_hi[i] = float(highs[i])

        if use_relaxed:
            is_lo = all(lows[i] <= lows[i - j] and lows[i] <= lows[i + j]
                        for j in range(1, depth + 1))
            if is_lo:
                is_lo = any(lows[i] < lows[i - j] or lows[i] < lows[i + j]
                            for j in range(1, depth + 1))
        else:
            is_lo = all(lows[i] < lows[i - j] and lows[i] < lows[i + j]
                        for j in range(1, depth + 1))
        if is_lo:
            cand_lo[i] = float(lows[i])

    candidates = [(i, p, True) for i, p in cand_hi.items()]
    candidates += [(i, p, False) for i, p in cand_lo.items()]
    candidates.sort(key=lambda x: x[0])

    if not candidates:
        return []

    # 应用deviation过滤 + 高低交替
    pivots: List[Swing] = []
    for idx, price, is_high in candidates:
        ts = timestamps[idx] if timestamps is not None else None
        if not pivots:
            pivots.append(Swing(idx, price, is_high, ts))
            continue
        last = pivots[-1]
        if is_high == last.is_high:
            if is_high and price > last.price:
                pivots[-1] = Swing(idx, price, is_high, ts)
            elif not is_high and price < last.price:
                pivots[-1] = Swing(idx, price, is_high, ts)
        else:
            pct = abs(price - last.price) / last.price * 100.0
            if pct >= dev_thresholds[idx]:
                pivots.append(Swing(idx, price, is_high, ts))
    return pivots


# ============================================================
# 滑窗模式匹配
# ============================================================

def _make_waves_5(swings: List[Swing], labels: List[str]) -> Tuple[List[Wave], Direction]:
    """从6个swing点构建5个Wave, 返回(waves, 趋势方向)"""
    waves = []
    for i in range(5):
        waves.append(Wave(swings[i], swings[i + 1], labels[i]))
    # 方向由第一段决定
    d = Direction.UP if swings[1].price > swings[0].price else Direction.DOWN
    return waves, d


def _make_waves_3(swings: List[Swing], labels: List[str]) -> Tuple[List[Wave], Direction]:
    """从4个swing点构建3个Wave"""
    waves = []
    for i in range(3):
        waves.append(Wave(swings[i], swings[i + 1], labels[i]))
    d = Direction.UP if swings[1].price > swings[0].price else Direction.DOWN
    return waves, d


def _find_significant_pivots(swings: List[Swing], n_points: int = 6) -> List[List[Swing]]:
    """
    从swing列表中选取最显著的n_points个pivot组合。

    策略:
    1. 极值锚定: 以全局最高/最低点为锚，向外扩展找交替pivot
    2. 等距采样: 等间距选取pivot
    3. 振幅排序: 按每个pivot的振幅(与前一个pivot的价差)排序，取最大的

    这是对抗ZigZag过多中间pivot的核心策略。
    """
    n = len(swings)
    if n < n_points:
        return []

    combos = []

    # === 策略1: 极值锚定 ===
    # 找全局最高和最低点
    max_idx = max(range(n), key=lambda i: swings[i].price)
    min_idx = min(range(n), key=lambda i: swings[i].price)

    # 从最低点开始(UP impulse潜力)
    if min_idx < n - n_points + 1:
        combo = _build_alternating_from(swings, min_idx, n_points)
        if combo:
            combos.append(combo)

    # 从最高点开始(DOWN impulse潜力)
    if max_idx < n - n_points + 1:
        combo = _build_alternating_from(swings, max_idx, n_points)
        if combo:
            combos.append(combo)

    # 从最高点向后(DOWN impulse的起点)
    if max_idx > 0:
        combo = _build_alternating_from(swings, max(0, max_idx - 1), n_points)
        if combo:
            combos.append(combo)

    # 从最低点向后(UP impulse从谷底开始)
    if min_idx > 0:
        combo = _build_alternating_from(swings, max(0, min_idx - 1), n_points)
        if combo:
            combos.append(combo)

    # 以最低点为中心左右扩展
    if min_idx >= 2 and min_idx < n - n_points + 2:
        for offset in range(max(0, min_idx - n_points + 2), min_idx + 1):
            combo = _build_alternating_from(swings, offset, n_points)
            if combo and combo not in combos:
                combos.append(combo)
                break

    # 以最高点为中心
    if max_idx >= 2 and max_idx < n - n_points + 2:
        for offset in range(max(0, max_idx - n_points + 2), max_idx + 1):
            combo = _build_alternating_from(swings, offset, n_points)
            if combo and combo not in combos:
                combos.append(combo)
                break

    # 最后n_points个swing(最近的模式)
    if n >= n_points:
        tail = swings[-n_points:]
        if _is_alternating(tail) and tail not in combos:
            combos.append(tail)

    # === 策略2: 等距采样 ===
    for stride in [2, 3]:
        sampled = swings[::stride]
        if len(sampled) >= n_points:
            sub = sampled[:n_points]
            if _is_alternating(sub):
                combos.append(sub)

    # === 策略3: 振幅最大的pivot ===
    if n > n_points:
        # 计算每个pivot的振幅
        amplitudes = []
        for i in range(1, n):
            amp = abs(swings[i].price - swings[i - 1].price)
            amplitudes.append((amp, i))
        amplitudes.sort(reverse=True)

        # 取振幅最大的n_points-1个分段对应的pivot
        top_indices = sorted(set([0] + [idx for _, idx in amplitudes[:n_points * 2]]))
        # 从中选出交替的n_points个
        alt_combo = []
        for idx in top_indices:
            if idx < n:
                if not alt_combo or swings[idx].is_high != alt_combo[-1].is_high:
                    alt_combo.append(swings[idx])
                    if len(alt_combo) == n_points:
                        break
        if len(alt_combo) == n_points:
            combos.append(alt_combo)

    return combos


def _build_alternating_from(swings: List[Swing], start: int, n_points: int) -> Optional[List[Swing]]:
    """从start位置开始，选取n_points个交替的high/low pivot"""
    result = [swings[start]]
    i = start + 1
    while len(result) < n_points and i < len(swings):
        if swings[i].is_high != result[-1].is_high:
            result.append(swings[i])
        i += 1
    return result if len(result) == n_points else None


def _is_alternating(swings: List[Swing]) -> bool:
    """检查swing列表是否高低交替"""
    return all(swings[i].is_high != swings[i + 1].is_high for i in range(len(swings) - 1))


def _select_6_from_swings(swings: List[Swing], max_skip: int = 2) -> List[List[Swing]]:
    """
    从swing列表中选取6个点的所有组合 (允许跳过中间pivot)

    这解决了ZigZag产生过多pivot导致窗口对不齐的问题。
    对于每种跳过方案，生成一组6个点。

    max_skip: 每个位置最多跳过几个中间点 (0=只用连续点)
    """
    n = len(swings)
    if n < 6:
        return []

    combos = []
    # 连续6点 (skip=0)
    combos.append(swings[:6])

    if max_skip >= 1 and n >= 7:
        # 尝试在每个位置跳过1个点
        for skip_pos in range(1, 5):  # 跳过位置1-4
            combo = list(swings[:skip_pos]) + list(swings[skip_pos + 1:skip_pos + 7 - skip_pos])
            if len(combo) >= 6:
                combos.append(combo[:6])

        # 用最大跨度: 首尾+中间均匀分布
        if n >= 8:
            step = max(1, (n - 1) // 5)
            indices = [min(i * step, n - 1) for i in range(6)]
            # 确保交替和唯一
            unique = []
            for idx in indices:
                if idx not in unique:
                    unique.append(idx)
            if len(unique) >= 6:
                combos.append([swings[i] for i in unique[:6]])

    return combos


def _select_4_from_swings(swings: List[Swing], max_skip: int = 2) -> List[List[Swing]]:
    """从swing列表中选取4个点的组合"""
    n = len(swings)
    if n < 4:
        return []

    combos = [swings[:4]]

    if max_skip >= 1 and n >= 5:
        for skip_pos in range(1, 3):
            combo = list(swings[:skip_pos]) + list(swings[skip_pos + 1:skip_pos + 5 - skip_pos])
            if len(combo) >= 4:
                combos.append(combo[:4])

        if n >= 6:
            step = max(1, (n - 1) // 3)
            indices = [min(i * step, n - 1) for i in range(4)]
            unique = list(dict.fromkeys(indices))
            if len(unique) >= 4:
                combos.append([swings[i] for i in unique[:4]])

    return combos


def _dp_find_best_pivots(swings: List[Swing], n_points: int = 6,
                         max_candidates_per_step: int = 3) -> List[List[Swing]]:
    """
    动态规划 + beam search 剪枝: 从所有swing点中找最优的n_points个交替pivot组合。

    算法思路:
    1. 枚举合法起点（按价格极端程度优先）
    2. 对每个起点，用DP逐步选择后续点：
       - 状态: (累计振幅, 路径)
       - 转移: 要求 index递增 且 is_high交替
       - 剪枝: 每步只保留振幅最大的 max_candidates_per_step 个候选（beam search）
    3. 振幅大的组合更可能是真实的波浪结构

    复杂度: O(n * n_points * max_candidates * n) ≈ O(n^2 * 18)
    当n=50时约 45000 次操作，完全可接受。

    参数:
        swings: 所有swing点列表（已按index排序，高低交替）
        n_points: 需要选取的点数 (6=5浪, 4=3浪)
        max_candidates_per_step: 每步最多保留的候选数（控制搜索宽度）

    返回:
        最优pivot组合列表（按振幅评分降序，已去重）
    """
    n = len(swings)
    if n < n_points:
        return []

    best_combos = []  # (总振幅评分, [Swing列表])

    # 尝试两种起点类型: 从high开始 和 从low开始
    for start_with_high in [True, False]:

        # 找所有合法起点
        start_indices = [i for i in range(n) if swings[i].is_high == start_with_high]

        # 限制起点数量: 优先选价格极端的起点
        if len(start_indices) > max_candidates_per_step * 2:
            if start_with_high:
                start_indices.sort(key=lambda i: swings[i].price, reverse=True)
            else:
                start_indices.sort(key=lambda i: swings[i].price)
            start_indices = start_indices[:max_candidates_per_step * 2]

        for start_i in start_indices:
            # 起点之后剩余的swing不够，跳过
            remaining = n - start_i
            if remaining < n_points:
                continue

            # dp_states: 当前步的候选列表 = [(累计振幅, 路径[swing下标列表]), ...]
            dp_states = [(0.0, [start_i])]

            for step in range(1, n_points):
                # 本步需要的is_high类型：与起点交替
                need_high = (start_with_high if step % 2 == 0 else not start_with_high)

                next_states = []
                for score, path in dp_states:
                    last_idx = path[-1]

                    # 找所有合法的下一个点
                    candidates = []
                    for j in range(last_idx + 1, n):
                        if swings[j].is_high != need_high:
                            continue
                        # 振幅 = 这一段的价格差
                        amp = abs(swings[j].price - swings[last_idx].price)
                        candidates.append((amp, j))

                    # 剪枝: 只取振幅最大的几个
                    candidates.sort(reverse=True)
                    for amp, j in candidates[:max_candidates_per_step]:
                        next_states.append((score + amp, path + [j]))

                if not next_states:
                    break

                # beam search: 只保留评分最高的状态
                next_states.sort(reverse=True, key=lambda x: x[0])
                dp_states = next_states[:max_candidates_per_step * 2]

            # 收集完成的组合
            for score, path in dp_states:
                if len(path) == n_points:
                    combo = [swings[i] for i in path]
                    if _is_alternating(combo):
                        best_combos.append((score, combo))

    # 按振幅排序，去重
    best_combos.sort(reverse=True, key=lambda x: x[0])
    seen = set()
    result = []
    for score, combo in best_combos:
        key = tuple(s.index for s in combo)
        if key not in seen:
            seen.add(key)
            result.append(combo)
        if len(result) >= max_candidates_per_step * 3:
            break

    return result


def _try_5wave_patterns(swings6: List[Swing]) -> List[WavePattern]:
    """尝试所有5浪模式"""
    results = []

    # 推动浪标签
    imp_labels = ["1", "2", "3", "4", "5"]
    waves, direction = _make_waves_5(swings6, imp_labels)

    # 1. 标准推动浪 (含延伸分类)
    rules = validate_pattern(PatternType.IMPULSE, waves, direction)
    conf = compute_confidence(rules)
    if conf > 0:
        ext_type = classify_impulse_extension(waves)
        pat = WavePattern(ext_type, direction, waves, rule_results=rules, confidence=conf)
        results.append(pat)

    # 2. 失败五浪
    rules = validate_pattern(PatternType.TRUNCATED_5TH, waves, direction)
    conf = compute_confidence(rules)
    if conf > 0:
        pat = WavePattern(PatternType.TRUNCATED_5TH, direction, waves,
                          rule_results=rules, confidence=conf)
        results.append(pat)

    # 3. 引导楔形
    rules = validate_pattern(PatternType.LEADING_DIAGONAL, waves, direction)
    conf = compute_confidence(rules)
    if conf > 0:
        pat = WavePattern(PatternType.LEADING_DIAGONAL, direction, waves,
                          rule_results=rules, confidence=conf)
        results.append(pat)

    # 4. 终结楔形
    rules = validate_pattern(PatternType.ENDING_DIAGONAL, waves, direction)
    conf = compute_confidence(rules)
    if conf > 0:
        pat = WavePattern(PatternType.ENDING_DIAGONAL, direction, waves,
                          rule_results=rules, confidence=conf)
        results.append(pat)

    # 5. 收缩三角 (ABCDE)
    tri_labels = ["A", "B", "C", "D", "E"]
    waves_tri, dir_tri = _make_waves_5(swings6, tri_labels)
    rules = validate_pattern(PatternType.TRIANGLE_CONTRACTING, waves_tri, dir_tri)
    conf = compute_confidence(rules)
    if conf > 0:
        pat = WavePattern(PatternType.TRIANGLE_CONTRACTING, dir_tri, waves_tri,
                          rule_results=rules, confidence=conf)
        results.append(pat)

    # 6. 扩展三角
    rules = validate_pattern(PatternType.TRIANGLE_EXPANDING, waves_tri, dir_tri)
    conf = compute_confidence(rules)
    if conf > 0:
        pat = WavePattern(PatternType.TRIANGLE_EXPANDING, dir_tri, waves_tri,
                          rule_results=rules, confidence=conf)
        results.append(pat)

    # 7. 双锯齿 (W-X-Y 5段)
    wxy5_labels = ["W1", "W2", "X", "Y1", "Y2"]
    waves_wxy, dir_wxy = _make_waves_5(swings6, wxy5_labels)
    rules = validate_pattern(PatternType.DOUBLE_ZIGZAG, waves_wxy, dir_wxy)
    conf = compute_confidence(rules)
    if conf > 0:
        pat = WavePattern(PatternType.DOUBLE_ZIGZAG, dir_wxy, waves_wxy,
                          rule_results=rules, confidence=conf)
        results.append(pat)

    # 8. 双重组合 WXY
    rules = validate_pattern(PatternType.COMBINATION_WXY, waves_wxy, dir_wxy)
    conf = compute_confidence(rules)
    if conf > 0:
        pat = WavePattern(PatternType.COMBINATION_WXY, dir_wxy, waves_wxy,
                          rule_results=rules, confidence=conf)
        results.append(pat)

    return results


def _try_3wave_patterns(swings4: List[Swing]) -> List[WavePattern]:
    """
    尝试所有3浪模式

    核心互斥逻辑: 计算B/A比率，根据阈值决定尝试哪些模式
    - B/A < 0.80: 只尝试Zigzag（明确的浅回撤）
    - B/A >= 0.90: 只尝试Flat系列（明确的深回撤）
    - 0.80 <= B/A < 0.90: 重叠带，两种都尝试（由硬规则自动筛选）
    """
    results = []
    abc_labels = ["A", "B", "C"]
    waves, direction = _make_waves_3(swings4, abc_labels)

    # 计算B/A比率，用于互斥分流
    wa, wb = waves[0], waves[1]
    ba_ratio = wb.length / wa.length if wa.length > 0 else 0.0

    try_zigzag = ba_ratio < 0.90    # Zigzag: B/A < 90%（含重叠带）
    try_flat = ba_ratio >= 0.80     # Flat: B/A >= 80%（含重叠带）

    if try_zigzag:
        rules = validate_pattern(PatternType.ZIGZAG, waves, direction)
        conf = compute_confidence(rules)
        if conf > 0:
            pat = WavePattern(PatternType.ZIGZAG, direction, waves,
                              rule_results=rules, confidence=conf)
            results.append(pat)

    if try_flat:
        # 规则平台
        rules = validate_pattern(PatternType.FLAT_REGULAR, waves, direction)
        conf = compute_confidence(rules)
        if conf > 0:
            pat = WavePattern(PatternType.FLAT_REGULAR, direction, waves,
                              rule_results=rules, confidence=conf)
            results.append(pat)

        # 扩展平台
        rules = validate_pattern(PatternType.FLAT_EXPANDED, waves, direction)
        conf = compute_confidence(rules)
        if conf > 0:
            pat = WavePattern(PatternType.FLAT_EXPANDED, direction, waves,
                              rule_results=rules, confidence=conf)
            results.append(pat)

        # 顺势平台
        rules = validate_pattern(PatternType.FLAT_RUNNING, waves, direction)
        conf = compute_confidence(rules)
        if conf > 0:
            pat = WavePattern(PatternType.FLAT_RUNNING, direction, waves,
                              rule_results=rules, confidence=conf)
            results.append(pat)

    return results


# ============================================================
# 递归子浪分析
# ============================================================

def _analyze_sub_waves(
    pattern: WavePattern,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    depth: int,
    max_depth: int,
    timestamps: Optional[np.ndarray] = None,
) -> WavePattern:
    """
    递归分析子浪结构

    推动浪: 浪1/3/5内部应为推动浪(5浪), 浪2/4应为调整浪(3浪)
    调整浪: 各子浪根据类型递归
    """
    if depth >= max_depth:
        return pattern

    for wave in pattern.waves:
        start_idx = wave.start.index
        end_idx = wave.end.index
        if end_idx - start_idx < 10:  # 子浪太短, 跳过 (降低门槛从20到10)
            continue

        sub_highs = highs[start_idx:end_idx + 1]
        sub_lows = lows[start_idx:end_idx + 1]
        sub_closes = closes[start_idx:end_idx + 1]
        sub_ts = timestamps[start_idx:end_idx + 1] if timestamps is not None else None

        # 根据父浪类型决定子浪应该是推动还是调整
        if pattern.pattern_type in MOTIVE_TYPES:
            if wave.label in ("1", "3", "5"):
                expected = "motive"
            elif wave.label in ("2", "4"):
                expected = "corrective"
            else:
                continue
        elif pattern.pattern_type in CORRECTIVE_TYPES:
            expected = "corrective"
        else:
            continue

        # 在子范围内检测
        sub_swings = detect_swings(sub_highs, sub_lows, sub_closes,
                                   deviation=2.0, depth=5, timestamps=sub_ts)
        if len(sub_swings) < 4:
            continue

        best_sub = None
        best_conf = 0

        if expected == "motive" and len(sub_swings) >= 6:
            for i in range(len(sub_swings) - 5):
                for p in _try_5wave_patterns(sub_swings[i:i + 6]):
                    if p.is_valid and p.confidence > best_conf:
                        best_sub = p
                        best_conf = p.confidence

        if expected == "corrective":
            for i in range(len(sub_swings) - 3):
                for p in _try_3wave_patterns(sub_swings[i:i + 4]):
                    if p.is_valid and p.confidence > best_conf:
                        best_sub = p
                        best_conf = p.confidence

        if best_sub:
            best_sub.degree = depth + 1
            pattern.sub_patterns[wave.label] = best_sub

    return pattern


# ============================================================
# 预测引擎
# ============================================================

def _predict_from_pattern(pattern: WavePattern, current_price: float) -> Optional[Prediction]:
    """基于已识别的模式预测下一步"""
    waves = pattern.waves
    d = 1 if pattern.direction == Direction.UP else -1

    if pattern.pattern_type in MOTIVE_TYPES and len(waves) >= 4:
        w1 = waves[0]

        # 如果识别到浪1-2-3-4, 预测浪5
        if len(waves) == 4:
            w3 = waves[2]
            w4 = waves[3]
            targets = {}
            base = w4.end.price
            r1 = w1.length
            targets["W5=W1"] = base + r1 * d
            targets["W5=0.618*W1"] = base + r1 * 0.618 * d
            targets["W5=1.618*W1"] = base + r1 * 1.618 * d
            return Prediction(pattern, "5", targets,
                              Direction.UP if d == 1 else Direction.DOWN,
                              pattern.confidence * 0.7)

        # 如果识别到完整5浪, 预测ABC调整
        if len(waves) == 5:
            total = pattern.total_length
            end = pattern.end_price
            targets = {}
            targets["A=0.382回撤"] = end - total * 0.382 * d
            targets["A=0.500回撤"] = end - total * 0.500 * d
            targets["A=0.618回撤"] = end - total * 0.618 * d
            return Prediction(pattern, "A", targets,
                              Direction.DOWN if d == 1 else Direction.UP,
                              pattern.confidence * 0.6)

    # === 三角形突破预测 ===
    if pattern.pattern_type in (PatternType.TRIANGLE_CONTRACTING,
                                 PatternType.TRIANGLE_EXPANDING):
        if len(waves) == 5:
            # 三角形突破目标 = 最宽处高度 + 突破点
            width = waves[0].length  # A浪是最宽的
            end = pattern.end_price
            # 突破方向通常与三角形前的趋势一致
            # 简化: 用A浪方向的反向作为突破方向
            break_d = 1 if waves[0].direction == Direction.DOWN else -1
            targets = {}
            targets["突破目标=A浪幅度"] = end + width * break_d
            targets["突破0.618倍"] = end + width * 0.618 * break_d
            targets["突破1.618倍"] = end + width * 1.618 * break_d
            return Prediction(pattern, "突破", targets,
                              Direction.UP if break_d == 1 else Direction.DOWN,
                              pattern.confidence * 0.6)

    # === 终结楔形回撤预测 ===
    if pattern.pattern_type == PatternType.ENDING_DIAGONAL:
        if len(waves) == 5:
            total = pattern.total_length
            end = pattern.end_price
            start = pattern.start_price
            targets = {}
            targets["回撤至起点"] = start
            targets["回撤0.618"] = end - total * 0.618 * d
            targets["回撤0.786"] = end - total * 0.786 * d
            return Prediction(pattern, "楔形回撤", targets,
                              Direction.DOWN if d == 1 else Direction.UP,
                              pattern.confidence * 0.7)

    # === 引导楔形回撤预测 ===
    if pattern.pattern_type == PatternType.LEADING_DIAGONAL:
        if len(waves) == 5:
            # 引导楔形完成后，预测浪2回撤（通常回撤61.8%-78.6%）
            total = pattern.total_length
            end = pattern.end_price
            targets = {}
            targets["回撤0.618"] = end - total * 0.618 * d
            targets["回撤0.786"] = end - total * 0.786 * d
            targets["回撤0.500"] = end - total * 0.500 * d
            return Prediction(pattern, "浪2回撤", targets,
                              Direction.DOWN if d == 1 else Direction.UP,
                              pattern.confidence * 0.65)

    if pattern.pattern_type in CORRECTIVE_TYPES:
        wa = waves[0]

        # 双锯齿/WXY完成后，预测新一轮推动浪
        if len(waves) == 5 and pattern.pattern_type in (
            PatternType.DOUBLE_ZIGZAG, PatternType.COMBINATION_WXY,
        ):
            total = pattern.total_length
            end = pattern.end_price
            new_d = -d  # 调整完成后反向推动
            targets = {}
            targets["新浪1=0.382*调整"] = end + total * 0.382 * new_d
            targets["新浪1=0.500*调整"] = end + total * 0.500 * new_d
            targets["新浪1=0.618*调整"] = end + total * 0.618 * new_d
            targets["新浪1=调整全幅"] = end + total * new_d
            return Prediction(pattern, "新浪1", targets,
                              Direction.UP if new_d == 1 else Direction.DOWN,
                              pattern.confidence * 0.5)

        # 识别到A-B, 预测C
        if len(waves) == 2:
            targets = {}
            base = waves[1].end.price
            targets["C=A"] = base + wa.length * d
            targets["C=0.618*A"] = base + wa.length * 0.618 * d
            targets["C=1.618*A"] = base + wa.length * 1.618 * d
            return Prediction(pattern, "C", targets,
                              Direction(d), pattern.confidence * 0.6)

        # 识别到完整ABC, 预测新一轮推动浪1
        # 使用 C 浪（最后一浪）的振幅作为参考，而非整个 ABC 范围，避免目标过远
        if len(waves) == 3:
            c_wave = waves[2]
            c_len = c_wave.length
            end = pattern.end_price
            new_d = -d
            targets = {}
            targets["新浪1=0.382*C"] = end + c_len * 0.382 * new_d
            targets["新浪1=0.500*C"] = end + c_len * 0.500 * new_d
            targets["新浪1=0.618*C"] = end + c_len * 0.618 * new_d
            # 过滤负价格（资产价格不能为负）
            targets = {k: v for k, v in targets.items() if v > 0}
            if not targets:
                return None
            return Prediction(pattern, "新浪1", targets,
                              Direction.UP if new_d == 1 else Direction.DOWN,
                              pattern.confidence * 0.5)

    return None


# ============================================================
# 主引擎
# ============================================================

class ElliottWaveAnalyzer:
    """
    Elliott Wave 自动分析器

    用法:
        >>> analyzer = ElliottWaveAnalyzer()
        >>> analyzer.analyze(df)
        >>> print(analyzer.summary())
        >>> predictions = analyzer.predict(current_price)
    """

    # 多粒度deviation参数
    # 多粒度deviation: 加入12.0以捕获更大级别的波浪结构
    DEFAULT_DEVIATIONS = [1.0, 2.0, 3.0, 5.0, 8.0, 12.0]

    def __init__(
        self,
        deviations: Optional[List[float]] = None,
        depth: int = 10,
        max_recursion: int = 2,
        min_confidence: float = 0.3,
    ):
        """
        参数:
            deviations: ZigZag偏差乘数列表 (多粒度检测)
            depth: ZigZag回看深度
            max_recursion: 最大递归深度 (0=不递归, 1=一级, 2=两级)
            min_confidence: 最低置信度阈值
        """
        self.deviations = deviations or self.DEFAULT_DEVIATIONS
        self.depth = depth
        self.max_recursion = max_recursion
        self.min_confidence = min_confidence

        self._patterns: List[WavePattern] = []
        self._swings_by_dev: Dict[float, List[Swing]] = {}
        self._highs: Optional[np.ndarray] = None
        self._lows: Optional[np.ndarray] = None
        self._closes: Optional[np.ndarray] = None
        self._timestamps: Optional[np.ndarray] = None
        self._volumes: Optional[np.ndarray] = None

    def analyze(
        self,
        df: pd.DataFrame,
        high_col: str = "high",
        low_col: str = "low",
        close_col: str = "close",
        time_col: Optional[str] = None,
        volume_col: Optional[str] = "volume",
    ) -> "ElliottWaveAnalyzer":
        """
        分析DataFrame数据

        参数:
            volume_col: 成交量列名，默认"volume"。设为None跳过成交量分析。
                        如果列不存在会自动跳过。
        """
        highs = df[high_col].values.astype(float)
        lows = df[low_col].values.astype(float)
        closes = df[close_col].values.astype(float)

        # 提取成交量数据 (可选)
        volumes = None
        if volume_col and volume_col in df.columns:
            volumes = df[volume_col].values.astype(float)

        timestamps = None
        if time_col and time_col in df.columns:
            timestamps = df[time_col].values
        elif isinstance(df.index, pd.DatetimeIndex):
            timestamps = df.index.values

        return self.analyze_arrays(highs, lows, closes, timestamps, volumes)

    def analyze_arrays(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        timestamps: Optional[np.ndarray] = None,
        volumes: Optional[np.ndarray] = None,
    ) -> "ElliottWaveAnalyzer":
        """分析numpy数组数据"""
        self._highs = highs
        self._lows = lows
        self._closes = closes
        self._timestamps = timestamps
        self._volumes = volumes
        self._patterns = []
        self._swings_by_dev = {}

        all_candidates = []

        # 确定要使用的depth列表: 如果默认depth找不到足够swing，自动降低depth重试
        depths_to_try = [self.depth]
        if self.depth > 1:
            depths_to_try.append(1)  # 总是尝试depth=1作为后备

        for dev in self.deviations:
            swings = None
            used_depth = self.depth
            for d in depths_to_try:
                swings = detect_swings(highs, lows, closes,
                                       deviation=dev, depth=d,
                                       timestamps=timestamps)
                used_depth = d
                if len(swings) >= 4:
                    break  # 找到足够swing，不需要降低depth

            self._swings_by_dev[dev] = swings

            if len(swings) < 4:
                continue

            # === 策略A: 显著pivot锚定 (全局最优) ===
            if len(swings) >= 6:
                for combo in _find_significant_pivots(swings, n_points=6):
                    for pat in _try_5wave_patterns(combo):
                        if pat.is_valid and pat.confidence >= self.min_confidence:
                            pat.degree = 0
                            all_candidates.append((dev, pat))

            if len(swings) >= 4:
                for combo in _find_significant_pivots(swings, n_points=4):
                    for pat in _try_3wave_patterns(combo):
                        if pat.is_valid and pat.confidence >= self.min_confidence:
                            pat.degree = 0
                            all_candidates.append((dev, pat))

            # === 策略B: 滑窗 + 跳跃选点 (局部匹配) ===
            if len(swings) >= 6:
                for i in range(len(swings) - 5):
                    end_j = min(i + 10, len(swings))
                    sub = swings[i:end_j]
                    for combo in _select_6_from_swings(sub, max_skip=2):
                        if not _is_alternating(combo):
                            continue
                        for pat in _try_5wave_patterns(combo):
                            if pat.is_valid and pat.confidence >= self.min_confidence:
                                pat.degree = 0
                                all_candidates.append((dev, pat))

            for i in range(len(swings) - 3):
                end_j = min(i + 7, len(swings))
                sub = swings[i:end_j]
                for combo in _select_4_from_swings(sub, max_skip=2):
                    if not _is_alternating(combo):
                        continue
                    for pat in _try_3wave_patterns(combo):
                        if pat.is_valid and pat.confidence >= self.min_confidence:
                            pat.degree = 0
                            all_candidates.append((dev, pat))

            # === 策略C: DP动态规划最优pivot选取 (全局搜索) ===
            # 当swing点较多时(>8个), DP能找到滑窗和锚定策略遗漏的最优组合
            if len(swings) >= 8:
                # 5浪模式: 选6个最优pivot
                for combo in _dp_find_best_pivots(swings, n_points=6,
                                                   max_candidates_per_step=3):
                    for pat in _try_5wave_patterns(combo):
                        if pat.is_valid and pat.confidence >= self.min_confidence:
                            pat.degree = 0
                            all_candidates.append((dev, pat))

                # 3浪模式: 选4个最优pivot
                for combo in _dp_find_best_pivots(swings, n_points=4,
                                                   max_candidates_per_step=3):
                    for pat in _try_3wave_patterns(combo):
                        if pat.is_valid and pat.confidence >= self.min_confidence:
                            pat.degree = 0
                            all_candidates.append((dev, pat))

        # 去重 + 合并
        self._patterns = self._deduplicate(all_candidates)

        # === 优化: 数据覆盖度奖励 ===
        # 5浪模式覆盖更多数据，应该比只匹配局部的3浪模式排名更高
        total_bars = len(highs) if highs is not None else 1
        for i, pat in enumerate(self._patterns):
            span = pat.end_index - pat.start_index
            coverage_ratio = span / total_bars if total_bars > 0 else 0

            # 覆盖度奖励: 覆盖>=60%数据的模式加分，<20%的模式轻微惩罚
            if coverage_ratio >= 0.6:
                bonus = 0.08
            elif coverage_ratio >= 0.4:
                bonus = 0.04
            elif coverage_ratio >= 0.2:
                bonus = 0.0
            else:
                bonus = -0.03  # 覆盖太少的局部模式轻微减分

            # 5浪模式额外奖励(需要更多点位对齐，本身更难匹配)
            if len(pat.waves) == 5:
                bonus += 0.05

            self._patterns[i].confidence = max(0.01,
                min(1.0, pat.confidence + bonus))

        # === 成交量确认加分 (可选，有volume数据时生效) ===
        if self._volumes is not None and len(self._volumes) > 0:
            for i, pat in enumerate(self._patterns):
                vol_results = check_volume_confirmation(pat.waves, self._volumes)
                if vol_results:
                    vol_bonus = 0.0
                    for vr in vol_results:
                        # 浪3成交量最大 → +0.05
                        if vr.name == "浪3成交量最大" and vr.score >= 0.8:
                            vol_bonus += 0.05
                        # 浪5成交量背离 → +0.03
                        elif vr.name == "浪5成交量背离" and vr.score >= 0.8:
                            vol_bonus += 0.03
                        # 调整浪量低于推动浪量 → +0.02
                        elif vr.name == "调整浪量<推动浪量" and vr.score >= 0.8:
                            vol_bonus += 0.02
                    # 将成交量规则结果添加到模式的规则列表中
                    pat.rule_results.extend(vol_results)
                    if vol_bonus > 0:
                        self._patterns[i].confidence = min(1.0,
                            pat.confidence + vol_bonus)

        # 递归子浪分析
        if self.max_recursion > 0 and highs is not None:
            for i, pat in enumerate(self._patterns):
                self._patterns[i] = _analyze_sub_waves(
                    pat, highs, lows, closes,
                    depth=1, max_depth=self.max_recursion + 1,
                    timestamps=timestamps,
                )
                # 子浪匹配加分 (提高权重 + 类型匹配奖励)
                if pat.sub_patterns:
                    bonus = len(pat.sub_patterns) * 0.08
                    # 子浪类型与期望一致时额外加分
                    for wlabel, subpat in pat.sub_patterns.items():
                        if pat.pattern_type in MOTIVE_TYPES:
                            if wlabel in ("1", "3", "5") and subpat.pattern_type in MOTIVE_TYPES:
                                bonus += 0.05
                            elif wlabel in ("2", "4") and subpat.pattern_type in CORRECTIVE_TYPES:
                                bonus += 0.05
                    self._patterns[i].confidence = min(1.0, pat.confidence + bonus)

        # 按置信度排序
        self._patterns.sort(key=lambda p: p.confidence, reverse=True)

        return self

    def _deduplicate(self, candidates: List[Tuple[float, WavePattern]]) -> List[WavePattern]:
        """去重: 同一区间的多个模式只保留置信度最高的同类型"""
        if not candidates:
            return []

        # 按 (起止索引范围, 模式类型) 分组
        groups: Dict[Tuple, List[WavePattern]] = {}
        for dev, pat in candidates:
            # 用起止索引范围做key (允许±10%重叠视为相同)
            key = (pat.start_index // 10, pat.end_index // 10, pat.pattern_type)
            if key not in groups:
                groups[key] = []
            groups[key].append(pat)

        result = []
        for key, pats in groups.items():
            best = max(pats, key=lambda p: p.confidence)
            result.append(best)

        return result

    @property
    def patterns(self) -> List[WavePattern]:
        """所有检测到的模式 (按置信度降序)"""
        return self._patterns

    @property
    def motive_patterns(self) -> List[WavePattern]:
        """推动浪模式"""
        return [p for p in self._patterns if p.pattern_type in MOTIVE_TYPES]

    @property
    def corrective_patterns(self) -> List[WavePattern]:
        """调整浪模式"""
        return [p for p in self._patterns if p.pattern_type in CORRECTIVE_TYPES]

    def get_latest_pattern(self, pattern_type: Optional[PatternType] = None) -> Optional[WavePattern]:
        """获取最近的(置信度最高的)模式"""
        pats = self._patterns
        if pattern_type:
            pats = [p for p in pats if p.pattern_type == pattern_type]
        return pats[0] if pats else None

    def predict(self, current_price: float) -> List[Prediction]:
        """基于所有检测到的模式做预测"""
        predictions = []
        for pat in self._patterns[:10]:  # 只用置信度前10的模式
            pred = _predict_from_pattern(pat, current_price)
            if pred:
                predictions.append(pred)
        predictions.sort(key=lambda p: p.confidence, reverse=True)
        return predictions

    def summary(self) -> str:
        """分析摘要"""
        lines = ["=" * 60, "Elliott Wave 分析报告", "=" * 60]

        lines.append(f"参数: deviations={self.deviations}, depth={self.depth}, "
                      f"max_recursion={self.max_recursion}")

        for dev, swings in self._swings_by_dev.items():
            lines.append(f"  deviation={dev}: {len(swings)}个swing点")

        lines.append(f"\n检测到 {len(self._patterns)} 个波浪模式:")
        lines.append(f"  推动浪: {len(self.motive_patterns)}")
        lines.append(f"  调整浪: {len(self.corrective_patterns)}")

        # 按类型统计
        type_counts: Dict[PatternType, int] = {}
        for p in self._patterns:
            type_counts[p.pattern_type] = type_counts.get(p.pattern_type, 0) + 1
        if type_counts:
            lines.append("\n模式分布:")
            for pt, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {PATTERN_NAMES_CN.get(pt, pt.value)}: {cnt}")

        # Top 5 模式详情
        lines.append(f"\nTop {min(5, len(self._patterns))} 模式:")
        for i, pat in enumerate(self._patterns[:5]):
            lines.append(f"\n  [{i+1}] {pat.summary()}")
            for w in pat.waves:
                d = "↑" if w.direction == Direction.UP else "↓"
                lines.append(f"      {w.label}: {w.start.price:.2f}→{w.end.price:.2f} {d}")
            if pat.sub_patterns:
                lines.append(f"      子浪: {list(pat.sub_patterns.keys())}")

        return "\n".join(lines)

    # ============================================================
    # 优化2: 多时间框架验证
    # ============================================================

    def analyze_multi_timeframe(
        self,
        dfs: Dict[str, pd.DataFrame],
        high_col: str = "high",
        low_col: str = "low",
        close_col: str = "close",
        volume_col: Optional[str] = "volume",
    ) -> Dict[str, object]:
        """
        多时间框架分析

        在多个时间框架上独立运行Elliott Wave分析，
        当多个框架在同一价格区域检测到同类型模式时，置信度提升。

        参数:
            dfs: 时间框架字典，如 {"1d": df_daily, "4h": df_4h, "1h": df_1h}
                 键名表示时间框架（从大到小排序效果更好）
            high_col, low_col, close_col: 列名
            volume_col: 成交量列名，None跳过

        返回:
            {
                "by_timeframe": {tf: [WavePattern, ...]},
                "cross_tf_patterns": [{pattern, timeframes, boosted_confidence}],
                "summary": str
            }
        """
        # 1. 每个时间框架独立分析
        tf_results: Dict[str, List[WavePattern]] = {}
        tf_analyzers: Dict[str, 'ElliottWaveAnalyzer'] = {}

        for tf_name, df in dfs.items():
            analyzer = ElliottWaveAnalyzer(
                deviations=self.deviations,
                depth=self.depth,
                max_recursion=self.max_recursion,
                min_confidence=self.min_confidence,
            )
            analyzer.analyze(df, high_col=high_col, low_col=low_col,
                             close_col=close_col, volume_col=volume_col)
            tf_results[tf_name] = list(analyzer.patterns)
            tf_analyzers[tf_name] = analyzer

        # 2. 跨时间框架价格区域匹配
        # 提取每个模式的价格范围，检查不同TF的模式是否重叠
        cross_tf_patterns = []

        # 收集所有(tf, pattern, price_range)
        all_pats = []
        for tf_name, patterns in tf_results.items():
            for pat in patterns:
                price_min = min(w.start.price for w in pat.waves)
                price_max = max(w.end.price for w in pat.waves)
                # 确保min < max
                if price_min > price_max:
                    price_min, price_max = price_max, price_min
                all_pats.append({
                    "tf": tf_name,
                    "pattern": pat,
                    "price_min": price_min,
                    "price_max": price_max,
                    "mid_price": (price_min + price_max) / 2,
                })

        # 按价格中点排序，然后检查相邻模式是否来自不同TF且价格重叠
        all_pats.sort(key=lambda x: x["mid_price"])

        # 聚类: 价格区域重叠且模式类型相同/相似的归为一组
        used = set()
        for i, p1 in enumerate(all_pats):
            if i in used:
                continue
            cluster = [p1]
            used.add(i)

            for j, p2 in enumerate(all_pats):
                if j in used or j == i:
                    continue
                # 检查价格范围重叠 (允许20%容差)
                range1 = p1["price_max"] - p1["price_min"]
                range2 = p2["price_max"] - p2["price_min"]
                tolerance = max(range1, range2) * 0.2

                overlap = (p1["price_min"] - tolerance <= p2["price_max"] and
                           p2["price_min"] - tolerance <= p1["price_max"])

                if overlap and p1["tf"] != p2["tf"]:
                    cluster.append(p2)
                    used.add(j)

            if len(cluster) >= 2:
                # 多个TF在同一价格区域有模式 → 置信度加倍
                tfs_involved = list(set(c["tf"] for c in cluster))
                best_pat = max(cluster, key=lambda c: c["pattern"].confidence)
                boosted = min(1.0, best_pat["pattern"].confidence * (1.0 + 0.3 * (len(tfs_involved) - 1)))

                cross_tf_patterns.append({
                    "pattern_type": best_pat["pattern"].cn_name,
                    "direction": best_pat["pattern"].direction,
                    "timeframes": tfs_involved,
                    "tf_count": len(tfs_involved),
                    "original_confidence": best_pat["pattern"].confidence,
                    "boosted_confidence": boosted,
                    "price_range": (best_pat["price_min"], best_pat["price_max"]),
                })

        # 3. 生成摘要
        lines = ["=" * 60, "多时间框架分析报告", "=" * 60]
        for tf_name, patterns in tf_results.items():
            lines.append(f"\n[{tf_name}] 检测到 {len(patterns)} 个模式")
            for p in patterns[:3]:
                lines.append(f"  {p.summary()}")

        if cross_tf_patterns:
            lines.append(f"\n跨时间框架确认: {len(cross_tf_patterns)} 个")
            cross_tf_patterns.sort(key=lambda x: x["boosted_confidence"], reverse=True)
            for ctp in cross_tf_patterns[:5]:
                d_str = "↑" if ctp["direction"] == Direction.UP else "↓"
                lines.append(
                    f"  {ctp['pattern_type']}{d_str} "
                    f"TF={ctp['timeframes']} "
                    f"原始置信度={ctp['original_confidence']:.1%} "
                    f"增强置信度={ctp['boosted_confidence']:.1%}"
                )
        else:
            lines.append("\n无跨时间框架确认")

        return {
            "by_timeframe": tf_results,
            "cross_tf_patterns": cross_tf_patterns,
            "summary": "\n".join(lines),
        }

    # ============================================================
    # 优化3: Fibonacci集群汇聚
    # ============================================================

    def find_fib_clusters(self, current_price: float, tolerance_pct: float = 2.0) -> List[Dict]:
        """
        Fibonacci集群: 当多个不同模式的Fib目标价汇聚在同一区域时，
        该区域的支撑/阻力更强。

        从所有已检测模式的预测目标价中提取Fibonacci水平，
        按proximity聚类，统计每个集群的来源数量。

        参数:
            current_price: 当前价格 (用于生成预测)
            tolerance_pct: 聚类容差百分比 (默认2.0%)

        返回:
            [{price, count, sources, strength}] 按来源数量降序排列
            strength: "极强"(>=4来源) / "强"(3来源) / "中等"(2来源)
        """
        if not self._patterns:
            return []

        # 1. 从所有模式中提取目标价
        all_targets = []  # [(目标价, 来源描述)]

        predictions = self.predict(current_price)
        for pred in predictions:
            source = f"{pred.pattern.cn_name}-{pred.next_wave_label}"
            for name, target in pred.target_prices.items():
                if target > 0:
                    all_targets.append((target, f"{source}:{name}"))

        if not all_targets:
            return []

        # 2. 按价格排序后聚类
        all_targets.sort(key=lambda x: x[0])
        tolerance = current_price * tolerance_pct / 100.0

        clusters = []  # [{prices: [], sources: []}]

        for price, source in all_targets:
            merged = False
            for cluster in clusters:
                # 与集群中心价格的距离在容差范围内
                center = np.mean(cluster["prices"])
                if abs(price - center) <= tolerance:
                    cluster["prices"].append(price)
                    cluster["sources"].append(source)
                    merged = True
                    break
            if not merged:
                clusters.append({
                    "prices": [price],
                    "sources": [source],
                })

        # 3. 统计并排序
        result = []
        for cluster in clusters:
            count = len(set(cluster["sources"]))  # 去重的来源数量
            avg_price = float(np.mean(cluster["prices"]))

            # 强度判定
            if count >= 4:
                strength = "极强"
            elif count >= 3:
                strength = "强"
            elif count >= 2:
                strength = "中等"
            else:
                strength = "弱"

            result.append({
                "price": round(avg_price, 2),
                "count": count,
                "sources": list(set(cluster["sources"])),
                "strength": strength,
                "distance_pct": round((avg_price - current_price) / current_price * 100, 2),
            })

        # 按来源数量降序排列
        result.sort(key=lambda x: x["count"], reverse=True)

        return result
