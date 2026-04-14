"""
Elliott Wave 自动分析工具

完整实现 Elliott Wave 理论:
  - 7种推动浪模式 (Impulse含3种延伸 / Leading Diagonal / Ending Diagonal / Truncated 5th)
  - 8种调整浪模式 (Zigzag / Double Zigzag / 3种Flat / 2种Triangle / WXY组合)
  - Fibonacci比率验证 + 置信度评分
  - 多粒度ZigZag检测 + 多重计数排名
  - 可选递归子浪分析 (最多3级)
  - 合成数据自验证 + 历史预测回测

用法:
    >>> from App.elliott_wave import ElliottWaveAnalyzer
    >>> analyzer = ElliottWaveAnalyzer()
    >>> analyzer.analyze(df)
    >>> print(analyzer.summary())
    >>> predictions = analyzer.predict(current_price)

验证:
    python -m App.elliott_wave.validator
"""

from .core import (
    Direction,
    PatternType,
    Swing,
    Wave,
    WavePattern,
    Prediction,
    RuleResult,
    MOTIVE_TYPES,
    CORRECTIVE_TYPES,
    PATTERN_NAMES_CN,
    FIB_RATIOS,
)

from .detector import (
    ElliottWaveAnalyzer,
    detect_swings,
)

from .rules import (
    check_volume_confirmation,
)

from .validator import (
    SyntheticWaveGenerator,
    ValidationRunner,
    run_full_validation,
)

__all__ = [
    "ElliottWaveAnalyzer",
    "Direction",
    "PatternType",
    "Swing",
    "Wave",
    "WavePattern",
    "Prediction",
    "RuleResult",
    "SyntheticWaveGenerator",
    "ValidationRunner",
    "run_full_validation",
    "detect_swings",
    "check_volume_confirmation",
    "MOTIVE_TYPES",
    "CORRECTIVE_TYPES",
    "PATTERN_NAMES_CN",
    "FIB_RATIOS",
]
