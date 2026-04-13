"""
艾略特波浪分析服务 — 双轨道分析
  主浪（蓝）: 大 deviation，识别主级结构
  子浪（橙）: 小 deviation，识别主浪内部结构
"""

import logging
import numpy as np
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


def _serialize_waves(waves, candles: list) -> list:
    """序列化波浪，用时间戳替代 bar index，前端用时间戳反查真实索引"""
    out = []
    n = len(candles)
    for w in waves:
        si = int(w.start.index)
        ei = int(w.end.index)
        begin_ts = candles[si]["timestamp"] if 0 <= si < n else None
        end_ts = candles[ei]["timestamp"] if 0 <= ei < n else None
        out.append(
            {
                "label": w.label,
                "begin_ts": begin_ts,
                "begin_y": float(w.start.price),
                "end_ts": end_ts,
                "end_y": float(w.end.price),
            }
        )
    return out


def _best_pattern(patterns, n_window, closes):
    """按 振幅×置信度×时效×推动浪偏好 综合评分，取最优"""
    if not patterns:
        return None
    from backend.elliott_wave.core import MOTIVE_TYPES

    def score(p):
        norm_amp = p.total_length / (closes[-1] if closes[-1] > 0 else 1.0)
        recency_w = 0.5 + 0.5 * (p.end_index / max(n_window - 1, 1))
        # 推动浪（5浪/楔形）优先级是调整浪的2倍，与TradingView行为一致
        motive_w = 2.0 if p.pattern_type in MOTIVE_TYPES else 1.0
        return norm_amp * p.confidence * recency_w * motive_w

    return max(patterns, key=score)


def analyze(candles: List[Dict[str, Any]], bar_offset: int = 0, visible_count: int = 0) -> Dict[str, Any]:
    """
    双轨道艾略特波浪分析

    candles    : 前端可见区域的K线切片
    bar_offset : 切片第一根在完整 chartData 中的索引
    """
    if not candles or len(candles) < 30:
        return {"patterns": [], "predictions": []}

    try:
        from backend.elliott_wave.detector import ElliottWaveAnalyzer
        from backend.elliott_wave.core import MOTIVE_TYPES, Direction, PATTERN_NAMES_CN
    except Exception as e:
        logger.error(f"艾略特波浪模块导入失败: {e}")
        return {"patterns": [], "predictions": [], "error": str(e)}

    n = len(candles)
    highs = np.array([float(c.get("high", 0)) for c in candles], dtype=np.float64)
    lows = np.array([float(c.get("low", 0)) for c in candles], dtype=np.float64)
    closes = np.array([float(c.get("close", 0)) for c in candles], dtype=np.float64)
    volumes = np.array([float(c.get("volume", 0)) for c in candles], dtype=np.float64)

    # ── 根据可见根数自适应 deviation（ATR倍数，需保证5浪中间回调能被捕捉）──
    # deviation 过大会导致 ZigZag 只产生 3 个 pivot，只能识别 ABC 而非 12345
    # BTC 周线 ATR% ≈ 4%，dev=2.0 → 门槛 8%，可捕捉 20%+ 的波浪回调
    if n <= 60:
        major_devs = [1.5, 2.0]
        minor_devs = [0.5, 0.8]
    elif n <= 150:
        major_devs = [1.8, 2.5]
        minor_devs = [0.7, 1.0]
    elif n <= 300:
        major_devs = [2.0, 3.0]
        minor_devs = [0.8, 1.2]
    elif n <= 600:
        major_devs = [2.5, 3.5]
        minor_devs = [1.0, 1.5]
    else:
        major_devs = [3.0, 4.5]
        minor_devs = [1.2, 2.0]

    # ── 主浪分析（大 deviation）──
    major_pat = None
    try:
        az = ElliottWaveAnalyzer(deviations=major_devs, min_confidence=0.45, max_recursion=0)
        az.analyze_arrays(highs, lows, closes, volumes=volumes)
        major_pat = _best_pattern(az.patterns, n, closes)
    except Exception as e:
        logger.error(f"主浪分析失败: {e}", exc_info=True)

    # ── 子浪分析（小 deviation）──
    # 策略：若主浪是完整5浪推动，在5浪结束点之后专门寻找 ABC 修正浪
    minor_pat = None
    minor_candles = candles  # 序列化时使用的 candles 引用（默认全量）
    try:
        from backend.elliott_wave.core import CORRECTIVE_TYPES

        major_end_idx = int(major_pat.end_index) if major_pat else -1
        major_is_complete_impulse = (
            major_pat is not None and major_pat.pattern_type in MOTIVE_TYPES and len(major_pat.waves) == 5
        )

        if major_is_complete_impulse:
            # ── 直接从5浪结束点后的 ZigZag pivot 手动构建 ABC 结构 ──
            # 这比依赖模式识别更可靠：C浪未完成时模式识别会失败
            from backend.elliott_wave.detector import detect_swings
            from backend.elliott_wave.core import Swing, Wave, WavePattern, PatternType, Direction

            wave5_end_price = float(major_pat.waves[-1].end.price)
            wave5_is_up = major_pat.direction == Direction.UP

            # 在全量数据上用小 deviation 找 pivot，然后筛选5浪之后的部分
            post_start = max(0, major_end_idx)
            post_highs = highs[post_start:]
            post_lows = lows[post_start:]
            post_closes = closes[post_start:]
            post_n = len(post_closes)

            if post_n >= 5:
                # 用逐步增大的 deviation 直到能找到至少 2 个 pivot
                abc_swings = []
                for dev in [0.5, 1.0, 1.5, 2.0, 3.0]:
                    swings = detect_swings(post_highs, post_lows, post_closes, deviation=dev, depth=2)
                    # 过滤：第一个 pivot 应与 wave5 方向相反（5浪上升→首先找低点A）
                    if wave5_is_up:
                        valid = [s for s in swings if not s.is_high][:1]  # 找A底
                        after_a = [s for s in swings if s.is_high and (not valid or s.index > valid[0].index)][
                            :1
                        ]  # B顶
                        abc_swings = valid + after_a
                    else:
                        valid = [s for s in swings if s.is_high][:1]  # 找A顶
                        after_a = [s for s in swings if not s.is_high and (not valid or s.index > valid[0].index)][
                            :1
                        ]  # B底
                        abc_swings = valid + after_a
                    if len(abc_swings) >= 1:
                        break

                if abc_swings:
                    # 构建 ABC 波浪：wave5顶/底 → A → B → (C=当前)
                    wave5_swing = Swing(index=major_end_idx, price=wave5_end_price, is_high=wave5_is_up)
                    abc_waves = []
                    prev_swing = wave5_swing
                    labels = ["A", "B", "C"]
                    for i, s in enumerate(abc_swings):
                        # 把局部索引转为全局索引
                        global_s = Swing(index=post_start + s.index, price=s.price, is_high=s.is_high)
                        abc_waves.append(Wave(start=prev_swing, end=global_s, label=labels[i]))
                        prev_swing = global_s

                    # 如果 C 浪还没找到 pivot（仍在进行中），用最新价格作为虚拟 C 点
                    if len(abc_waves) >= 1:
                        last_close = float(closes[-1])
                        last_idx = n - 1
                        c_is_high = not wave5_is_up  # 5浪上升→C浪最终是低点，暂时先不加虚拟C
                        # 只返回已有的 A(+B) 波浪结构
                        abc_direction = Direction.DOWN if wave5_is_up else Direction.UP

                        from backend.elliott_wave.core import PatternType

                        # 用 zigzag 类型代表ABC修正（显示时前端通过 degree=1 区分颜色）
                        minor_wave_pat = WavePattern(
                            pattern_type=PatternType.ZIGZAG,
                            direction=abc_direction,
                            waves=abc_waves,
                            degree=1,
                            confidence=0.75,
                        )
                        minor_pat = minor_wave_pat
                        # minor_candles 用全量 candles（索引已是全局索引）
        else:
            # 主浪不是完整5浪：子浪显示与主浪不重叠的其他模式
            az2 = ElliottWaveAnalyzer(deviations=minor_devs, min_confidence=0.35, max_recursion=0)
            az2.analyze_arrays(highs, lows, closes, volumes=volumes)
            candidates = [
                p
                for p in az2.patterns
                if not (major_pat and p.start_index == major_pat.start_index and p.end_index == major_pat.end_index)
            ]
            minor_pat = _best_pattern(candidates, n, closes) if candidates else None
    except Exception as e:
        logger.error(f"子浪分析失败: {e}", exc_info=True)

    # ── 格式化输出 ──
    patterns_out = []

    if major_pat:
        si, ei = int(major_pat.start_index), int(major_pat.end_index)
        patterns_out.append(
            {
                "pattern_type": major_pat.pattern_type.value,
                "pattern_name": PATTERN_NAMES_CN.get(major_pat.pattern_type, major_pat.pattern_type.value),
                "direction": 1 if major_pat.direction == Direction.UP else -1,
                "confidence": round(float(major_pat.confidence), 4),
                "is_motive": major_pat.pattern_type in MOTIVE_TYPES,
                "waves": _serialize_waves(major_pat.waves, candles),
                "degree": 0,
                "start_ts": candles[si]["timestamp"] if 0 <= si < n else None,
                "end_ts": candles[ei]["timestamp"] if 0 <= ei < n else None,
            }
        )

    if minor_pat:
        mc = minor_candles  # 可能是切片后的 candles
        mc_n = len(mc)
        si, ei = int(minor_pat.start_index), int(minor_pat.end_index)
        patterns_out.append(
            {
                "pattern_type": minor_pat.pattern_type.value,
                "pattern_name": PATTERN_NAMES_CN.get(minor_pat.pattern_type, minor_pat.pattern_type.value),
                "direction": 1 if minor_pat.direction == Direction.UP else -1,
                "confidence": round(float(minor_pat.confidence), 4),
                "is_motive": minor_pat.pattern_type in MOTIVE_TYPES,
                "waves": _serialize_waves(minor_pat.waves, mc),
                "degree": 1,
                "start_ts": mc[si]["timestamp"] if 0 <= si < mc_n else None,
                "end_ts": mc[ei]["timestamp"] if 0 <= ei < mc_n else None,
            }
        )

    # ── 预测 ──
    predictions_out = []
    try:
        if major_pat:
            current_price = float(closes[-1])

            # ── 优先：若 (a)(b) 已确认，预测 (c) 浪终点 ──
            c_pred_used = False
            if minor_pat and len(minor_pat.waves) >= 2 and minor_pat.pattern_type in CORRECTIVE_TYPES:
                wa = minor_pat.waves[0]  # (a) 浪
                wb = minor_pat.waves[1]  # (b) 浪
                a_len = abs(wa.end.price - wa.start.price)
                b_end = wb.end.price
                # (c) 方向与 (a) 相同（都是修正方向）
                c_dir = 1 if wa.end.price > wa.start.price else -1
                targets_c = {
                    "C=0.618*A": round(b_end + a_len * 0.618 * c_dir, 4),
                    "C=A": round(b_end + a_len * 1.000 * c_dir, 4),
                    "C=1.618*A": round(b_end + a_len * 1.618 * c_dir, 4),
                }
                # 过滤负值
                targets_c = {k: v for k, v in targets_c.items() if v > 0}
                if targets_c:
                    # end_ts 用 (b) 浪终点时间戳（预测从 b 顶/底发出）
                    b_end_idx = int(wb.end.index)
                    b_end_ts = mc[b_end_idx]["timestamp"] if 0 <= b_end_idx < len(mc) else None
                    predictions_out.append(
                        {
                            "pattern_type": minor_pat.pattern_type.value,
                            "pattern_name": "ABC修正",
                            "next_wave": "C",
                            "direction": c_dir,
                            "confidence": round(float(minor_pat.confidence) * 0.8, 4),
                            "targets": targets_c,
                            "end_ts": b_end_ts,
                            "origin_price": round(float(b_end), 4),  # 扇形线起点价格 = (b) 顶价格
                        }
                    )
                    c_pred_used = True

            # ── 兜底：若无 (a)(b)，从5浪顶预测 A 浪目标 ──
            if not c_pred_used:
                az._patterns = [major_pat]
                for pred in az.predict(current_price)[:1]:
                    predictions_out.append(
                        {
                            "pattern_type": pred.pattern.pattern_type.value,
                            "pattern_name": PATTERN_NAMES_CN.get(pred.pattern.pattern_type, ""),
                            "next_wave": pred.next_wave_label,
                            "direction": 1 if pred.direction == Direction.UP else -1,
                            "confidence": round(float(pred.confidence), 4),
                            "targets": {k: round(float(v), 4) for k, v in pred.target_prices.items()},
                            "end_ts": candles[int(major_pat.end_index)]["timestamp"]
                            if 0 <= int(major_pat.end_index) < n
                            else None,
                        }
                    )
    except Exception as e:
        logger.debug(f"预测生成失败: {e}")

    return {"patterns": patterns_out, "predictions": predictions_out}
