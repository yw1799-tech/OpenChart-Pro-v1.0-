"""
Elliott Wave 波浪规则验证引擎

覆盖完整理论：
  推动浪: Impulse(含3种延伸) / Leading Diagonal / Ending Diagonal / Truncated 5th
  调整浪: Zigzag / Double Zigzag / Flat(Regular/Expanded/Running) /
          Triangle(Contracting/Expanding) / Combination WXY

每个模式分硬规则(铁律,必须通过)和软规则(指引,贡献置信度)。
"""

from typing import List, Optional
import numpy as np
from .core import Wave, RuleResult, Direction, PatternType, fib_score


def _ratio(a: Wave, b: Wave) -> float:
    """b 对 a 的长度比率"""
    return b.length / a.length if a.length > 0 else 0.0


def _above(price_a: float, price_b: float, d: int) -> bool:
    """在趋势方向上, price_a 是否在 price_b '上方'"""
    return (price_a - price_b) * d > 0


def _above_or_eq(price_a: float, price_b: float, d: int) -> bool:
    return (price_a - price_b) * d >= 0


def _check_convergence(w1: Wave, w2: Wave, w3: Wave, w4: Wave, w5: Wave, d: int) -> RuleResult:
    """
    审计#1#2修复: 楔形收敛检验

    正确算法: 检查action趋势线(1-3-5极端点)与reaction趋势线(2-4极端点)
    是否在向前延伸时会交汇(收敛)。

    收敛条件:
    1. 两条线的斜率符号相同(同向) 但绝对值不同
    2. reaction线的斜率绝对值 < action线的斜率绝对值(浪的幅度在缩小)
    3. 或者直接检查: 浪1→3的距离 > 浪3→5的距离(action侧收敛)
       且 浪2→4的距离合理
    """
    # action侧: 浪1终点→浪3终点→浪5终点 (推动方向的极端点)
    dt_13 = w3.end.index - w1.end.index
    dt_35 = w5.end.index - w3.end.index
    dp_13 = (w3.end.price - w1.end.price) * d  # 归一化为正=趋势方向
    dp_35 = (w5.end.price - w3.end.price) * d

    # reaction侧: 浪2终点→浪4终点
    dt_24 = w4.end.index - w2.end.index
    dp_24 = (w4.end.price - w2.end.price) * d

    # 收敛检查: action侧的步进在减小(浪越来越小)
    action_shrink = dp_35 < dp_13 if dp_13 > 0 else False

    # reaction侧也在向action侧靠拢
    # 即: 浪4与浪3的距离 < 浪2与浪1的距离
    gap_12 = abs(w2.end.price - w1.end.price)
    gap_34 = abs(w4.end.price - w3.end.price)
    reaction_shrink = gap_34 < gap_12

    converging = action_shrink or reaction_shrink

    detail = f"action步进: {dp_13:.2f}→{dp_35:.2f} gap: {gap_12:.2f}→{gap_34:.2f}"

    return RuleResult("趋势线收敛", converging, True, 1.0 if converging else 0.0, detail)


# ============================================================
# 推动浪: Impulse (标准5浪 5-3-5-3-5)
# ============================================================


def check_impulse(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    验证标准推动浪 (1-2-3-4-5)

    硬规则 (违反=无效):
      HR1: 浪2不能回撤超过浪1起点
      HR2: 浪3不能是浪1/3/5中最短的
      HR3: 浪4不能进入浪1价格区域
      HR4: 浪3必须超越浪1终点
      HR5: 浪5必须超越浪3终点

    软规则 (指引):
      SR1: 浪2回撤浪1的38.2%-61.8%
      SR2: 浪3通常是浪1的1.618倍
      SR3: 浪4回撤浪3的23.6%-38.2%
      SR4: 浪5与浪1等长或0.618倍
      SR5: 交替原则(浪2与浪4回撤深度不同)
      SR6: 至少一浪延伸(通常浪3)
      SR7: 浪2不应回撤超过78.6%
    """
    w1, w2, w3, w4, w5 = waves
    d = 1 if direction == Direction.UP else -1
    results = []

    # --- 硬规则 ---
    hr1 = _above(w2.end.price, w1.start.price, d)
    results.append(
        RuleResult(
            "浪2不超浪1起点", hr1, True, 1.0 if hr1 else 0.0, f"W2端={w2.end.price:.2f} W1始={w1.start.price:.2f}"
        )
    )

    l1, l3, l5 = w1.length, w3.length, w5.length
    hr2 = not (l3 < l1 and l3 < l5)
    results.append(RuleResult("浪3非最短", hr2, True, 1.0 if hr2 else 0.0, f"W1={l1:.2f} W3={l3:.2f} W5={l5:.2f}"))

    hr3 = _above(w4.end.price, w1.end.price, d)
    results.append(
        RuleResult("浪4不入浪1区域", hr3, True, 1.0 if hr3 else 0.0, f"W4端={w4.end.price:.2f} W1端={w1.end.price:.2f}")
    )

    hr4 = _above(w3.end.price, w1.end.price, d)
    results.append(
        RuleResult("浪3超越浪1", hr4, True, 1.0 if hr4 else 0.0, f"W3端={w3.end.price:.2f} W1端={w1.end.price:.2f}")
    )

    hr5 = _above(w5.end.price, w3.end.price, d)
    results.append(
        RuleResult("浪5超越浪3", hr5, True, 1.0 if hr5 else 0.0, f"W5端={w5.end.price:.2f} W3端={w3.end.price:.2f}")
    )

    # --- 软规则 ---
    r21 = _ratio(w1, w2)
    results.append(
        RuleResult("浪2回撤比", True, False, fib_score(r21, [0.382, 0.500, 0.618], 0.08), f"W2/W1={r21:.3f}")
    )

    r31 = _ratio(w1, w3)
    results.append(
        RuleResult("浪3扩展比", True, False, fib_score(r31, [1.0, 1.272, 1.618, 2.618], 0.1), f"W3/W1={r31:.3f}")
    )

    r43 = _ratio(w3, w4)
    results.append(
        RuleResult("浪4回撤比", True, False, fib_score(r43, [0.236, 0.382, 0.500], 0.08), f"W4/W3={r43:.3f}")
    )

    r51 = _ratio(w1, w5)
    results.append(RuleResult("浪5比率", True, False, fib_score(r51, [0.618, 1.0, 1.618], 0.1), f"W5/W1={r51:.3f}"))

    diff_24 = abs(r21 - r43)
    results.append(
        RuleResult(
            "交替原则", True, False, min(1.0, diff_24 / 0.2), f"W2回撤={r21:.3f} W4回撤={r43:.3f} 差={diff_24:.3f}"
        )
    )

    min_l = min(l1, l3, l5) if min(l1, l3, l5) > 0 else 1
    has_ext = max(l1, l3, l5) >= 1.618 * min_l
    results.append(
        RuleResult("有延伸浪", True, False, 1.0 if has_ext else 0.3, f"最长/最短={max(l1, l3, l5) / min_l:.2f}")
    )

    results.append(
        RuleResult(
            "浪2不过深", True, False, 1.0 if r21 <= 0.786 else max(0, 1.0 - (r21 - 0.786) / 0.2), f"W2回撤={r21:.1%}"
        )
    )

    # 波浪通道: 2-4连线与1-3连线的平行度
    # 理想推动浪中，浪1终点-浪3终点连线 大致平行于 浪2终点-浪4终点连线
    dt_13 = w3.end.index - w1.end.index
    dt_24 = w4.end.index - w2.end.index
    if dt_13 > 0 and dt_24 > 0:
        slope_13 = (w3.end.price - w1.end.price) / dt_13
        slope_24 = (w4.end.price - w2.end.price) / dt_24
        if abs(slope_13) > 0:
            parallel = 1.0 - min(1.0, abs(slope_13 - slope_24) / abs(slope_13))
        else:
            parallel = 0.5
        results.append(
            RuleResult("波浪通道平行", True, False, parallel, f"斜率13={slope_13:.4f} 斜率24={slope_24:.4f}")
        )

    # 时间比率: 浪2与浪4的时间应有Fibonacci关系
    d2, d4 = w2.duration, w4.duration
    if d2 > 0 and d4 > 0:
        time_r = d4 / d2
        results.append(
            RuleResult(
                "浪2/4时间比",
                True,
                False,
                fib_score(time_r, [0.618, 1.0, 1.618, 2.618], 0.2),
                f"W4时间/W2时间={time_r:.2f}",
            )
        )
    return results


def classify_impulse_extension(waves: List[Wave]) -> PatternType:
    """判断推动浪的延伸类型"""
    w1, _, w3, _, w5 = waves
    l1, l3, l5 = w1.length, w3.length, w5.length
    if l1 >= l3 and l1 >= l5 and l1 >= 1.618 * min(l3, l5):
        return PatternType.IMPULSE_EXT1
    if l3 >= l1 and l3 >= l5 and l3 >= 1.618 * min(l1, l5):
        return PatternType.IMPULSE_EXT3
    if l5 >= l1 and l5 >= l3 and l5 >= 1.618 * min(l1, l3):
        return PatternType.IMPULSE_EXT5
    return PatternType.IMPULSE


# ============================================================
# 推动浪: Truncated 5th (失败第五浪)
# ============================================================


def check_truncated_5th(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    失败五浪: 浪5未能超越浪3终点

    硬规则: 与标准推动浪相同, 但浪5不需超越浪3
    额外硬规则: 浪3必须是最长浪 (失败五浪通常发生在浪3极强之后)
    """
    w1, w2, w3, w4, w5 = waves
    d = 1 if direction == Direction.UP else -1
    results = []

    hr1 = _above(w2.end.price, w1.start.price, d)
    results.append(RuleResult("浪2不超浪1起点", hr1, True, 1.0 if hr1 else 0.0))

    l1, l3, l5 = w1.length, w3.length, w5.length
    hr2 = not (l3 < l1 and l3 < l5)
    results.append(RuleResult("浪3非最短", hr2, True, 1.0 if hr2 else 0.0))

    hr3 = _above(w4.end.price, w1.end.price, d)
    results.append(RuleResult("浪4不入浪1区域", hr3, True, 1.0 if hr3 else 0.0))

    hr4 = _above(w3.end.price, w1.end.price, d)
    results.append(RuleResult("浪3超越浪1", hr4, True, 1.0 if hr4 else 0.0))

    # 失败五浪的关键: 浪5未超越浪3
    hr5_truncated = not _above(w5.end.price, w3.end.price, d)
    results.append(RuleResult("浪5未超浪3(失败)", hr5_truncated, True, 1.0 if hr5_truncated else 0.0))

    # 浪3必须是最长浪
    hr6 = l3 >= l1 and l3 >= l5
    results.append(RuleResult("浪3最长(失败条件)", hr6, True, 1.0 if hr6 else 0.0))

    # 软规则
    r21 = _ratio(w1, w2)
    results.append(
        RuleResult("浪2回撤比", True, False, fib_score(r21, [0.382, 0.500, 0.618], 0.08), f"W2/W1={r21:.3f}")
    )

    r51 = _ratio(w1, w5)
    results.append(
        RuleResult(
            "浪5偏短(失败特征)", True, False, fib_score(r51, [0.236, 0.382, 0.500, 0.618], 0.1), f"W5/W1={r51:.3f}"
        )
    )
    return results


# ============================================================
# 推动浪: Leading Diagonal (引导楔形)
# ============================================================


def check_leading_diagonal(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    引导楔形: 出现在浪1或浪A位置

    硬规则:
      HR1: 浪2不超浪1起点
      HR2: 浪3不最短
      HR3: 浪3超越浪1
      HR4: 浪5超越浪3
      HR5: 浪4可以进入浪1区域 (与标准推动浪的区别!)
      HR6: 趋势线收敛 (浪1-3线 与 浪2-4线 收敛)

    软规则:
      SR1: 浪5短于浪3
      SR2: 浪3短于浪1 (收敛特征)
      SR3: 浪2回撤浪1的62-78.6%
    """
    w1, w2, w3, w4, w5 = waves
    d = 1 if direction == Direction.UP else -1
    results = []

    hr1 = _above(w2.end.price, w1.start.price, d)
    results.append(RuleResult("浪2不超浪1起点", hr1, True, 1.0 if hr1 else 0.0))

    l1, l3, l5 = w1.length, w3.length, w5.length
    hr2 = not (l3 < l1 and l3 < l5)
    results.append(RuleResult("浪3非最短", hr2, True, 1.0 if hr2 else 0.0))

    hr3 = _above(w3.end.price, w1.end.price, d)
    results.append(RuleResult("浪3超越浪1", hr3, True, 1.0 if hr3 else 0.0))

    hr4 = _above(w5.end.price, w3.end.price, d)
    results.append(RuleResult("浪5超越浪3", hr4, True, 1.0 if hr4 else 0.0))

    # 审计#1: 重写收敛检验 — 检查两条趋势线是否会交汇
    _converge_result = _check_convergence(w1, w2, w3, w4, w5, d)
    results.append(_converge_result)

    # 软规则
    results.append(RuleResult("浪5短于浪3", True, False, 1.0 if l5 < l3 else 0.3, f"W5={l5:.2f} W3={l3:.2f}"))

    results.append(RuleResult("浪3短于浪1(收敛)", True, False, 1.0 if l3 < l1 else 0.4, f"W3={l3:.2f} W1={l1:.2f}"))

    r21 = _ratio(w1, w2)
    results.append(RuleResult("浪2回撤深", True, False, fib_score(r21, [0.618, 0.786], 0.08), f"W2/W1={r21:.3f}"))
    return results


# ============================================================
# 推动浪: Ending Diagonal (终结楔形)
# ============================================================


def check_ending_diagonal(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    终结楔形: 出现在浪5或浪C位置, 所有子浪均为三浪结构

    硬规则:
      HR1: 浪2不超浪1起点
      HR2: 浪3超越浪1
      HR3: 浪5超越浪3
      HR4: 浪4必须进入浪1区域 (终结楔形的标志!)
      HR5: 趋势线收敛

    软规则:
      SR1: 浪5短于浪3, 浪3短于浪1
      SR2: 浪4短于浪2
    """
    w1, w2, w3, w4, w5 = waves
    d = 1 if direction == Direction.UP else -1
    results = []

    hr1 = _above(w2.end.price, w1.start.price, d)
    results.append(RuleResult("浪2不超浪1起点", hr1, True, 1.0 if hr1 else 0.0))

    hr2 = _above(w3.end.price, w1.end.price, d)
    results.append(RuleResult("浪3超越浪1", hr2, True, 1.0 if hr2 else 0.0))

    hr3 = _above(w5.end.price, w3.end.price, d)
    results.append(RuleResult("浪5超越浪3", hr3, True, 1.0 if hr3 else 0.0))

    # 审计#4: 终结楔形也必须满足"浪3不是最短"(所有推动浪的通用铁律)
    l1, l3, l5 = w1.length, w3.length, w5.length
    hr_w3 = not (l3 < l1 and l3 < l5)
    results.append(RuleResult("浪3非最短", hr_w3, True, 1.0 if hr_w3 else 0.0, f"W1={l1:.2f} W3={l3:.2f} W5={l5:.2f}"))

    # 终结楔形标志: 浪4必须进入浪1区域
    hr4_overlap = not _above(w4.end.price, w1.end.price, d)
    results.append(RuleResult("浪4与浪1重叠(终结特征)", hr4_overlap, True, 1.0 if hr4_overlap else 0.0))

    # 审计#1#2: 重写收敛检验 — 检查1-3-5极端点连线与2-4连线是否会交汇
    _converge_result = _check_convergence(w1, w2, w3, w4, w5, d)
    results.append(_converge_result)

    # 软规则
    results.append(RuleResult("浪逐步缩短", True, False, 1.0 if l5 <= l3 <= l1 else (0.5 if l5 < l3 else 0.2)))

    l2, l4 = w2.length, w4.length
    results.append(RuleResult("浪4短于浪2", True, False, 1.0 if l4 < l2 else 0.3))
    return results


# ============================================================
# 调整浪: Zigzag (锯齿形 5-3-5)
# ============================================================


def check_zigzag(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    锯齿形 A-B-C: 典型调整浪

    direction = 锯齿形的整体方向 (DOWN = 向下调整, UP = 向上调整)

    硬规则:
      HR1: B浪不能回撤超过A浪的起点
      HR2: C浪必须超越A浪的终点

    软规则:
      SR1: B浪回撤A浪的38.2%-78.6%
      SR2: C浪等于A浪, 或是A的0.618-1.618倍
      SR3: B浪回撤不超过A浪的78.6%
    """
    wa, wb, wc = waves
    d = 1 if direction == Direction.UP else -1
    results = []

    # B浪不能回撤超过A浪起点
    # 如果是向下调整(d=-1): A下跌, B上涨, B不能涨过A起点
    # _above(wb.end, wa.start, -d) 应该为 False
    # 即: (wb.end - wa.start) * (-d) > 0 应该为 True
    # 等价于: (wa.start - wb.end) * d > 0
    # B回撤不超过A起点: B.end应在A.start和A.end之间
    hr1 = _above(wb.end.price, wa.start.price, d)
    results.append(
        RuleResult("B不超A起点", hr1, True, 1.0 if hr1 else 0.0, f"B端={wb.end.price:.2f} A始={wa.start.price:.2f}")
    )

    # C浪超越A浪终点
    hr2 = _above(wc.end.price, wa.end.price, d)
    results.append(
        RuleResult("C超越A终点", hr2, True, 1.0 if hr2 else 0.0, f"C端={wc.end.price:.2f} A端={wa.end.price:.2f}")
    )

    # HR3: Zigzag的B回撤不超过85% (区分Flat: Flat要求>=80-90%)
    rba = _ratio(wa, wb)
    hr3 = rba <= 0.85
    results.append(RuleResult("B回撤<85%(非Flat)", hr3, True, 1.0 if hr3 else 0.0, f"B/A={rba:.3f}"))

    # 软规则
    results.append(
        RuleResult("B回撤A比率", True, False, fib_score(rba, [0.382, 0.500, 0.618, 0.786], 0.08), f"B/A={rba:.3f}")
    )

    rca = _ratio(wa, wc)
    results.append(
        RuleResult("C与A比率", True, False, fib_score(rca, [0.618, 1.0, 1.272, 1.618], 0.1), f"C/A={rca:.3f}")
    )

    results.append(
        RuleResult(
            "B不过深", True, False, 1.0 if rba <= 0.786 else max(0, 1.0 - (rba - 0.786) / 0.2), f"B回撤={rba:.1%}"
        )
    )

    # 时间比例: Zigzag的A和C比B更快(陡峭)
    da, db, dc = wa.duration, wb.duration, wc.duration
    if da > 0 and db > 0 and dc > 0:
        ac_avg = (da + dc) / 2
        time_ratio = db / ac_avg
        results.append(
            RuleResult(
                "时间比例(B慢于AC)",
                True,
                False,
                fib_score(time_ratio, [1.0, 1.618, 2.0], 0.3),
                f"B时间/AC平均={time_ratio:.2f}",
            )
        )
    return results


# ============================================================
# 调整浪: Double Zigzag (双锯齿 W-X-Y)
# ============================================================


def check_double_zigzag(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    双锯齿 W-X-Y: 两个锯齿形用X浪连接

    在swing层面表现为5段: W(3段) + X(1段) + Y(1段 或 3段)
    简化为5个wave: wW, wX1, wX2, wY1, wY2 = 5浪
    实际上是7段, 但在swing检测中被简化为5段的滑窗

    这里我们检查5段结构: 段1-2-3视为第一个zigzag(W),
    段3-4视为连接浪(X), 段4-5视为第二个zigzag的主体(Y)

    硬规则:
      HR1: Y终点在趋势方向上超越W终点
      HR2: X浪不超越W起点

    软规则:
      SR1: W和Y大致等长
      SR2: X浪回撤W的38.2%-61.8%
    """
    # 5个waves: 视前3段为W部分, 中间为X, 后面为Y
    if len(waves) < 5:
        return [RuleResult("段数不足", False, True, 0.0)]

    w1, w2, w3, w4, w5 = waves
    d = 1 if direction == Direction.UP else -1
    results = []

    # W部分 = w1(方向) + w2(回撤) + w3(方向)
    # X = w3(终点)→w4(终点) = 反向
    # Y部分 = w4(终点)→w5(终点) = 方向

    # Y终点超越W终点 (w3终点)
    hr1 = _above(w5.end.price, w3.end.price, d)
    results.append(RuleResult("Y超越W", hr1, True, 1.0 if hr1 else 0.0))

    # X不超越W起点
    hr2 = _above(w1.start.price, w4.end.price, d)
    results.append(RuleResult("X不超W起点", hr2, True, 1.0 if hr2 else 0.0))

    # W部分长度 vs Y部分长度
    w_len = abs(w3.end.price - w1.start.price)
    y_len = abs(w5.end.price - w4.end.price)
    ratio_wy = y_len / w_len if w_len > 0 else 0
    results.append(
        RuleResult("W与Y等长", True, False, fib_score(ratio_wy, [0.618, 1.0, 1.618], 0.15), f"Y/W={ratio_wy:.3f}")
    )

    x_len = w4.length
    x_ratio = x_len / w_len if w_len > 0 else 0
    results.append(
        RuleResult("X回撤比", True, False, fib_score(x_ratio, [0.382, 0.500, 0.618], 0.1), f"X/W={x_ratio:.3f}")
    )
    return results


# ============================================================
# 调整浪: Flat Regular (规则平台 3-3-5)
# ============================================================


def check_flat_regular(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    规则平台 A-B-C:
      A浪3浪结构, B浪3浪结构回撤A的90%以上, C浪5浪结构约等于A

    硬规则:
      HR1: B回撤A的至少80% (宽松阈值, 理论90%)
      HR2: C超越A终点

    软规则:
      SR1: B回撤A的90%-105%
      SR2: C约等于A (0.8-1.2倍)
    """
    wa, wb, wc = waves
    d = 1 if direction == Direction.UP else -1
    results = []

    rba = _ratio(wa, wb)
    # 硬规则: B回撤A >= 80% (宽松阈值，理论要求90%+)
    hr1 = rba >= 0.80
    results.append(RuleResult("B回撤A>=80%", hr1, True, 1.0 if hr1 else 0.0, f"B/A={rba:.3f}"))

    hr2 = _above(wc.end.price, wa.end.price, d)
    results.append(RuleResult("C超越A终点", hr2, True, 1.0 if hr2 else 0.0))

    # B不超过A起点 (规则平台): B.end在A.start和A.end之间
    hr3 = _above_or_eq(wb.end.price, wa.start.price, d)
    results.append(RuleResult("B不超A起点(规则型)", hr3, True, 1.0 if hr3 else 0.0))

    results.append(RuleResult("B回撤90-105%", True, False, fib_score(rba, [0.90, 1.0], 0.08), f"B/A={rba:.3f}"))

    # B回撤深度加分: B/A越高(>=85%)越像Flat，低于85%像Zigzag
    flat_ba_score = min(1.0, max(0.0, (rba - 0.80) / 0.15))  # 80%→0, 95%→1
    results.append(
        RuleResult("B回撤深(Flat特征)", True, False, flat_ba_score, f"B/A={rba:.3f} Flat匹配度={flat_ba_score:.2f}")
    )

    rca = _ratio(wa, wc)
    results.append(RuleResult("C约等于A", True, False, fib_score(rca, [1.0, 1.272], 0.15), f"C/A={rca:.3f}"))

    # Flat时间比例: ABC三段时间较均匀 (区分Zigzag)
    da, db, dc = wa.duration, wb.duration, wc.duration
    if da > 0 and db > 0 and dc > 0:
        max_d = max(da, db, dc)
        min_d = min(da, db, dc)
        uniformity = min_d / max_d  # 越接近1越均匀
        results.append(
            RuleResult("时间均匀(Flat特征)", True, False, min(1.0, uniformity * 1.5), f"最短/最长={uniformity:.2f}")
        )
    return results


# ============================================================
# 调整浪: Flat Expanded (扩展平台)
# ============================================================


def check_flat_expanded(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    扩展平台: B超过A起点, C超过A终点

    硬规则:
      HR1: B超过A起点 (B > 100% 回撤)
      HR2: C超过A终点

    软规则:
      SR1: B回撤A的100%-138.2%
      SR2: C是A的1.0-1.618倍
    """
    wa, wb, wc = waves
    d = 1 if direction == Direction.UP else -1
    results = []

    # B超过A起点 (扩展特征)
    hr1 = _above(wb.end.price, wa.start.price, -d)  # B在A起点的"外侧"
    results.append(
        RuleResult("B超A起点(扩展)", hr1, True, 1.0 if hr1 else 0.0, f"B端={wb.end.price:.2f} A始={wa.start.price:.2f}")
    )

    hr2 = _above(wc.end.price, wa.end.price, d)
    results.append(RuleResult("C超越A终点", hr2, True, 1.0 if hr2 else 0.0))

    rba = _ratio(wa, wb)
    results.append(RuleResult("B回撤100-138%", True, False, fib_score(rba, [1.0, 1.236, 1.382], 0.1), f"B/A={rba:.3f}"))

    rca = _ratio(wa, wc)
    results.append(RuleResult("C扩展比", True, False, fib_score(rca, [1.0, 1.272, 1.618], 0.12), f"C/A={rca:.3f}"))

    # 时间比例: Expanded Flat的ABC三段时间较均匀 (与Regular Flat一致)
    da, db, dc = wa.duration, wb.duration, wc.duration
    if da > 0 and db > 0 and dc > 0:
        max_d = max(da, db, dc)
        min_d = min(da, db, dc)
        uniformity = min_d / max_d
        results.append(
            RuleResult("时间均匀(Flat特征)", True, False, min(1.0, uniformity * 1.5), f"最短/最长={uniformity:.2f}")
        )
    return results


# ============================================================
# 调整浪: Flat Running (顺势平台)
# ============================================================


def check_flat_running(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    顺势平台: B超过A起点, 但C未达到A终点 (显示趋势很强)

    硬规则:
      HR1: B超过A起点
      HR2: C未超过A终点 (顺势特征)

    软规则:
      SR1: B回撤A的100-138.2%
      SR2: C较短 (A的0.382-0.618倍)
    """
    wa, wb, wc = waves
    d = 1 if direction == Direction.UP else -1
    results = []

    hr1 = _above(wb.end.price, wa.start.price, -d)
    results.append(RuleResult("B超A起点", hr1, True, 1.0 if hr1 else 0.0))

    # C未超过A终点 (顺势: 调整很浅)
    hr2 = not _above(wc.end.price, wa.end.price, d)
    results.append(
        RuleResult("C未超A终点(顺势)", hr2, True, 1.0 if hr2 else 0.0, f"C端={wc.end.price:.2f} A端={wa.end.price:.2f}")
    )

    rba = _ratio(wa, wb)
    results.append(RuleResult("B回撤比", True, False, fib_score(rba, [1.0, 1.236, 1.382], 0.1), f"B/A={rba:.3f}"))

    rca = _ratio(wa, wc)
    results.append(RuleResult("C偏短(顺势)", True, False, fib_score(rca, [0.382, 0.500, 0.618], 0.1), f"C/A={rca:.3f}"))

    # 时间比例: Running Flat的ABC三段时间较均匀 (与Regular Flat一致)
    da, db, dc = wa.duration, wb.duration, wc.duration
    if da > 0 and db > 0 and dc > 0:
        max_d = max(da, db, dc)
        min_d = min(da, db, dc)
        uniformity = min_d / max_d
        results.append(
            RuleResult("时间均匀(Flat特征)", True, False, min(1.0, uniformity * 1.5), f"最短/最长={uniformity:.2f}")
        )
    return results


# ============================================================
# 调整浪: Triangle Contracting (收缩三角 3-3-3-3-3)
# ============================================================


def check_triangle_contracting(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    收缩三角 A-B-C-D-E:
      高点递降, 低点递升, 趋势线收敛

    direction = 三角形突破方向 (通常与之前的趋势方向一致)

    硬规则:
      HR1: 各浪逐步缩短 (A>C>E, B>D 近似)
      HR2: 趋势线收敛

    软规则:
      SR1: E浪在D-B连线内侧终止
      SR2: 各浪近似等比缩短
    """
    wa, wb, wc, wd, we = waves
    results = []

    la, lb, lc, ld, le = wa.length, wb.length, wc.length, wd.length, we.length

    # 审计#12: 硬规则score统一为1.0(通过)或0.0(不通过)
    shrink_checks = [la > lc, lc > le, la > le, lb > ld]
    shrink_count = sum(shrink_checks)
    hr1 = shrink_count >= 3
    results.append(
        RuleResult(
            "浪逐步缩短",
            hr1,
            True,
            1.0 if hr1 else 0.0,
            f"A={la:.2f} B={lb:.2f} C={lc:.2f} D={ld:.2f} E={le:.2f} 缩短={shrink_count}/4",
        )
    )

    # 审计#8: 直接检验极值点收敛 (高点递降 + 低点递升)
    # 奇数浪(A,C,E)的极端点收敛, 偶数浪(B,D)的极端点收敛
    highs = [max(w.start.price, w.end.price) for w in waves]
    lows = [min(w.start.price, w.end.price) for w in waves]
    # A,C,E的高点递降 (索引0,2,4)
    hi_ace_shrink = highs[0] >= highs[2] >= highs[4]
    # B,D的低点递升 (索引1,3)
    lo_bd_rise = lows[1] <= lows[3]
    extremes_converge = hi_ace_shrink and lo_bd_rise
    results.append(
        RuleResult(
            "极值点收敛",
            extremes_converge,
            True,
            1.0 if extremes_converge else 0.0,
            f"高点:{highs[0]:.1f}>={highs[2]:.1f}>={highs[4]:.1f} 低点:{lows[1]:.1f}<={lows[3]:.1f}",
        )
    )

    # 审计#9: 横盘阈值从0.85收紧到0.65 (三角形是横盘调整，不应有大趋势)
    total_drift = abs(waves[-1].end.price - waves[0].start.price)
    total_range = max(highs) - min(lows)
    drift_ratio = total_drift / total_range if total_range > 0 else 1.0
    is_sideways = drift_ratio < 0.65 if total_range > 0 else False
    results.append(
        RuleResult(
            "横盘结构(非趋势)", is_sideways, True, 1.0 if is_sideways else 0.0, f"漂移/振幅={drift_ratio:.2f} 阈值<0.65"
        )
    )

    # 软规则
    ratio_ca = lc / la if la > 0 else 0
    ratio_ec = le / lc if lc > 0 else 0
    results.append(RuleResult("C/A比率", True, False, fib_score(ratio_ca, [0.618, 0.786], 0.1), f"C/A={ratio_ca:.3f}"))
    results.append(RuleResult("E/C比率", True, False, fib_score(ratio_ec, [0.618, 0.786], 0.1), f"E/C={ratio_ec:.3f}"))
    return results


# ============================================================
# 调整浪: Triangle Expanding (扩展三角)
# ============================================================


def check_triangle_expanding(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    扩展三角 A-B-C-D-E:
      与收缩三角相反, 各浪逐步放大, 趋势线扩散

    硬规则:
      HR1: 各浪逐步放大 (A<C<E, B<D)
      HR2: 趋势线扩散
    """
    wa, wb, wc, wd, we = waves
    results = []

    la, lb, lc, ld, le = wa.length, wb.length, wc.length, wd.length, we.length

    expand_ace = la < lc and lc < le
    expand_bd = lb < ld
    hr1 = expand_ace and expand_bd
    results.append(RuleResult("浪逐步放大", hr1, True, 1.0 if hr1 else 0.0, f"A={la:.2f} C={lc:.2f} E={le:.2f}"))

    highs = [max(w.start.price, w.end.price) for w in waves]
    lows = [min(w.start.price, w.end.price) for w in waves]
    range_start = highs[0] - lows[0]
    range_end = highs[-1] - lows[-1]
    expanding = range_end > range_start
    results.append(RuleResult("价格区间扩散", expanding, True, 1.0 if expanding else 0.0))

    # 审计#9: 横盘阈值收紧到0.65
    total_drift = abs(waves[-1].end.price - waves[0].start.price)
    total_range = max(highs) - min(lows)
    drift_ratio = total_drift / total_range if total_range > 0 else 1.0
    is_sideways = drift_ratio < 0.65 if total_range > 0 else False
    results.append(
        RuleResult("横盘结构(非趋势)", is_sideways, True, 1.0 if is_sideways else 0.0, f"漂移/振幅={drift_ratio:.2f}")
    )

    ratio_ca = lc / la if la > 0 else 0
    results.append(
        RuleResult("C/A扩展比", True, False, fib_score(ratio_ca, [1.272, 1.618], 0.12), f"C/A={ratio_ca:.3f}")
    )
    return results


# ============================================================
# 调整浪: Combination WXY (双重组合)
# ============================================================


def check_combination_wxy(waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """
    双重组合 W-X-Y: W和Y是不同类型的调整浪, X是连接浪

    在5段swing中: W(段1-2-3方向), X(段3→4反向), Y(段4→5方向)

    硬规则:
      HR1: 整体在趋势方向上推进
      HR2: X不超过W起点

    软规则:
      SR1: W和Y大致等长
      SR2: X回撤W的38.2%-61.8%
      SR3: 整体横向运动(不是很陡)
    """
    if len(waves) < 5:
        return [RuleResult("段数不足", False, True, 0.0)]

    w1, w2, w3, w4, w5 = waves
    d = 1 if direction == Direction.UP else -1
    results = []

    # 整体推进
    hr1 = _above(w5.end.price, w1.start.price, d)
    results.append(RuleResult("整体推进", hr1, True, 1.0 if hr1 else 0.0))

    # X不超过W起点
    hr2 = _above(w1.start.price, w4.end.price, d)
    results.append(RuleResult("X不超W起点", hr2, True, 1.0 if hr2 else 0.0))

    # W和Y等长
    w_len = abs(w3.end.price - w1.start.price)
    y_len = abs(w5.end.price - w4.end.price)
    ratio = y_len / w_len if w_len > 0 else 0
    results.append(RuleResult("W与Y等长", True, False, fib_score(ratio, [0.618, 1.0, 1.618], 0.15), f"Y/W={ratio:.3f}"))

    # X回撤比
    x_len = w4.length
    x_ratio = x_len / w_len if w_len > 0 else 0
    results.append(
        RuleResult("X回撤W", True, False, fib_score(x_ratio, [0.382, 0.500, 0.618], 0.1), f"X/W={x_ratio:.3f}")
    )

    # 横向特征: 总跨度/高度比 > 2 (时间跨度大于价格跨度)
    total_bars = w5.end.index - w1.start.index
    total_price = abs(w5.end.price - w1.start.price)
    avg_price = (w1.start.price + w5.end.price) / 2
    sideways = total_price / avg_price < 0.1 if avg_price > 0 else False
    results.append(
        RuleResult(
            "横向运动",
            True,
            False,
            0.8 if sideways else 0.3,
            f"价格变化={total_price / avg_price:.1%}" if avg_price > 0 else "",
        )
    )
    return results


# ============================================================
# 成交量确认规则 (软规则，只在有volume数据时生效)
# ============================================================


def check_volume_confirmation(waves: List[Wave], volumes: Optional[np.ndarray] = None) -> List[RuleResult]:
    """
    成交量确认规则 (软规则，只在有volume数据时生效)

    SR1: 推动浪中浪3的平均成交量应该最大
    SR2: 浪5的成交量应小于浪3 (背离信号)
    SR3: 调整浪的成交量通常低于推动浪

    参数:
        waves: 波浪列表 (5浪推动浪 或 3浪调整浪)
        volumes: 整个数据范围的成交量数组 (需要覆盖所有波浪的index范围)

    返回:
        RuleResult列表 (全部为软规则)
    """
    results = []

    # 没有成交量数据时跳过
    if volumes is None or len(volumes) == 0:
        return results

    def _avg_volume(wave: "Wave") -> float:
        """计算单个波浪区间内的平均成交量"""
        start_idx = wave.start.index
        end_idx = wave.end.index
        if start_idx < 0 or end_idx >= len(volumes) or start_idx >= end_idx:
            return 0.0
        seg = volumes[start_idx : end_idx + 1]
        return float(np.mean(seg)) if len(seg) > 0 else 0.0

    # === 5浪推动浪的成交量规则 ===
    if len(waves) == 5:
        w1, w2, w3, w4, w5 = waves
        v1 = _avg_volume(w1)
        v2 = _avg_volume(w2)
        v3 = _avg_volume(w3)
        v4 = _avg_volume(w4)
        v5 = _avg_volume(w5)

        # 避免除零
        max_v = max(v1, v2, v3, v4, v5, 1e-10)

        # SR1: 浪3的平均成交量应该是推动浪(1/3/5)中最大的
        w3_largest = v3 >= v1 and v3 >= v5
        results.append(
            RuleResult("浪3成交量最大", True, False, 1.0 if w3_largest else 0.3, f"V1={v1:.0f} V3={v3:.0f} V5={v5:.0f}")
        )

        # SR2: 浪5的成交量应小于浪3 (量价背离信号)
        v5_diverge = v5 < v3
        results.append(
            RuleResult(
                "浪5成交量背离",
                True,
                False,
                1.0 if v5_diverge else 0.3,
                f"V5={v5:.0f} < V3={v3:.0f}: {'是' if v5_diverge else '否'}",
            )
        )

        # SR3: 调整浪(2/4)的成交量通常低于推动浪(1/3/5)
        avg_motive = (v1 + v3 + v5) / 3.0 if (v1 + v3 + v5) > 0 else 1.0
        avg_corrective = (v2 + v4) / 2.0
        corrective_lower = avg_corrective < avg_motive
        results.append(
            RuleResult(
                "调整浪量<推动浪量",
                True,
                False,
                1.0 if corrective_lower else 0.3,
                f"推动浪均量={avg_motive:.0f} 调整浪均量={avg_corrective:.0f}",
            )
        )

    # === 3浪调整浪的成交量规则 ===
    elif len(waves) == 3:
        wa, wb, wc = waves
        va = _avg_volume(wa)
        vb = _avg_volume(wb)
        vc = _avg_volume(wc)

        # 调整浪整体成交量通常低于之前的推动浪
        # 这里只检查B浪通常是最低量的
        b_lowest = vb <= va and vb <= vc
        results.append(
            RuleResult("B浪成交量最低", True, False, 0.8 if b_lowest else 0.4, f"VA={va:.0f} VB={vb:.0f} VC={vc:.0f}")
        )

    return results


# ============================================================
# 统一调度: 根据 PatternType 调用对应的规则检查
# ============================================================


def validate_pattern(pattern_type: PatternType, waves: List[Wave], direction: Direction) -> List[RuleResult]:
    """根据模式类型调用对应的验证函数"""
    checkers = {
        PatternType.IMPULSE: check_impulse,
        PatternType.IMPULSE_EXT1: check_impulse,  # 先用通用impulse验证
        PatternType.IMPULSE_EXT3: check_impulse,
        PatternType.IMPULSE_EXT5: check_impulse,
        PatternType.LEADING_DIAGONAL: check_leading_diagonal,
        PatternType.ENDING_DIAGONAL: check_ending_diagonal,
        PatternType.TRUNCATED_5TH: check_truncated_5th,
        PatternType.ZIGZAG: check_zigzag,
        PatternType.DOUBLE_ZIGZAG: check_double_zigzag,
        PatternType.FLAT_REGULAR: check_flat_regular,
        PatternType.FLAT_EXPANDED: check_flat_expanded,
        PatternType.FLAT_RUNNING: check_flat_running,
        PatternType.TRIANGLE_CONTRACTING: check_triangle_contracting,
        PatternType.TRIANGLE_EXPANDING: check_triangle_expanding,
        PatternType.COMBINATION_WXY: check_combination_wxy,
    }
    checker = checkers.get(pattern_type)
    if checker is None:
        return [RuleResult("未知模式", False, True, 0.0)]
    return checker(waves, direction)
