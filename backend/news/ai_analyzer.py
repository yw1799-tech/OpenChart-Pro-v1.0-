"""
LLM 深度解读引擎（PRD F5.7 / TDD §6.3.6 Phase 3B）。

设计原则：
  - 不做选股决策（决策已在规则引擎完成）
  - 只对 ★★★★+ 新闻 + 用户主动点击的新闻 做深度解读
  - 异步回填 flash_news.ai_analysis，不阻塞主流程
  - 日预算硬上限 (config.LLM_DAILY_BUDGET, 默认 $5)
  - 超支只告警，不降级（按用户要求）

支持的 LLM:
  - DeepSeek (deepseek-chat)
  - 通义千问 (qwen-turbo / qwen-plus)
  统一用 openai 库的兼容模式调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import backend.config as config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Prompt 模板
# ═══════════════════════════════════════════════════════════════════

DEEP_ANALYSIS_PROMPT = """你是一位资深金融新闻分析师。任务是对一条财经新闻做客观深度解读,识别其对具体品种的可量化影响。

【输入】
新闻标题: {title}
新闻来源: {source}
新闻正文: {content}
关联品种(粗筛): {categories}

【用户关注列表】(impacts 优先从此列表挑选;若新闻确与本列表外品种强相关,可补充)
{watch_context}

【分析原则】
1. 用数据说话,不预设立场。新闻偏多就标 bullish,偏空就标 bearish,信息不足或两可就 neutral。
2. impacts 只列**真实强相关**的品种(strength >= 0.6);宁缺毋滥,不确定直接空数组。
3. 宏观/政策类新闻若对具体品种无清晰直接传导路径,impacts 直接 []。
4. 如果"新闻正文"为空(只有标题),strength 上限设 0.6,且 catalyst_timing 倾向 uncertain。
5. 价格字段没把握就 null;不要编造数据。
6. impacts 不要重复同一 symbol。

【输出严格 JSON】(不要解释/不要 markdown 包裹)
{{
  "overall_view": "bullish|bearish|neutral",
  "summary": "30 字以内的核心摘要",
  "catalyst_timing": "immediate|1-3d|1-2w|delayed|uncertain",
  "catalyst_explanation": "一句话说清:事件最可能何时反映到股价。immediate(盘中跳空) / 1-3d / 1-2w(需消化) / delayed(等财报或数据兑现) / uncertain(信息不足)",
  "impacts": [
    {{"symbol": "品种代码", "direction": "bullish|bearish|neutral", "horizon": "1d|1-5d|1-3m", "strength": 0.0-1.0, "reason": "一句话原因(基于新闻明确表述)"}}
  ],
  "reasons": ["事件偏多/偏空的具体证据 1", "证据 2"],
  "risks": ["潜在风险 1", "风险 2"],
  "key_levels": {{"support": 数字 or null, "resistance": 数字 or null}},
  "historical_analogies": ["类似历史事件 1(如有)"]
}}

【参考示例】
正例: "苹果 Q3 财报营收超预期 12%" → impacts 含 AAPL(direction=bullish, strength=0.8, reason 引用 12% 数据)
反例: "欧洲央行讲话维持利率不变" → 不要因为关注列表有港股零售小盘就强行关联,impacts 应仅含明确相关品种(若无则 [])
"""


SIGNAL_VERIFY_PROMPT = """你是一位资深量化交易分析师。任务是对策略触发的交易信号做**独立验证**:基于技术、新闻、衍生品数据,客观判断该信号当前是否值得跟单。

【分析原则】
1. 用数据说话,不预设立场。证据支持信号就 confirm,证据反对就 reject,模糊则 warn,绝不为否定而否定。
2. 系统置信度仅供参考,你的判断基于下方原始数据独立得出。
3. 数据缺失的维度直接跳过,不在缺失数据上推断 — 在 reason 里注明"X 数据缺失,本判断未计入"。

## 信号基本信息
品种: {symbol} ({market})  周期: {interval}  方向: {action}
价格: {price}  触发策略: {strategy} (系统置信度 {confidence})
触发理由: {reason}

## 技术面快照(主要判断依据)
{tech_snapshot}

## 最近 24 小时相关新闻 ({news_count} 条)
{news_summary}

## 加密衍生品市场情绪(仅加密信号有,否则为空)
{crypto_insights}

## 该品种上次 AI 诊断结论(独立第二意见)
{diagnosis_context}

## 多策略共振情况(近 30min 同品种+同方向其他策略)
{consensus_context}

## 市场天气(A 股北向资金 / 美股+加密 CNN 恐贪指数)
{market_weather}

## 验证维度

**A. 技术面**(主要):
- 均线排列、RSI、MACD、量能是否支持该方向?
- 价格与关键阻力/支撑距离是否合理(BUY 离支撑近离阻力远;SELL 反之)?
- 假突破风险(超买后追多、无量突破、距阻力 < 1% 等)?

**B. 新闻面**: 近期新闻情绪与信号方向是否一致?有未反映的重大利好/利空?

**C. 加密衍生品**(若提供):
- 资金费率年化 > 100% → 多头拥挤,BUY 降权
- 资金费率显著为负 → 空头拥挤,SELL 降权
- OI 上升 + 价上升 = 真多头(有效背书)
- OI 下降 + 价上升 = 空头回补(短命反弹,BUY 警惕)
- 大户多空比与散户严重背离(大户空、散户多) = 散户接盘风险,BUY 应 reject 或 warn
- 主动买盘占比 < 45% → BUY 降权
- 恐贪 > 85 → BUY 应 warn;< 15 → SELL 应 warn

**D. 诊断对比**:
- 诊断 sell/reduce + 信号 BUY = 严重冲突 → reject,reason 注明"与诊断反向"
- 诊断 buy/strong_buy + 信号 BUY = 同向加强,技术也支持则 confirm
- 诊断 hold + 信号 BUY = 非冲突,按信号自身质量判
- 诊断暂无 = 忽略该维度

## 输出严格 JSON

{{
  "verdict": "confirm|warn|reject",
  "ai_confidence": 0-100,
  "reason": "100 字内,引用技术快照具体数据说明判断依据(如 RSI=62 未超买、距阻力 2.6% 等),避免空话",
  "ai_stop_loss": 数字 或 null,
  "ai_take_profit": 数字 或 null
}}

**SL/TP 要求**:
- 基于技术快照"关键位"(20 日高低、支撑阻力、ATR)给出
- BUY: SL 在当前价下方且不低于 20 日低;TP 在 20 日高附近或之前
- SELL: 反之
- 与系统给的相同则填 null
- 风险回报比建议 1:2 以上

## 判定阈值

- **confirm (ai_confidence ≥ 65)**: 方向与主趋势一致 + 主要风险可控 → 值得跟单
  * 不要求"完美",方向清晰即可 confirm,中等风险在 reason 里提示
  * 例: 多头趋势 + MA 多头排列 + 未超买 = BUY confirm(即便距阻力 3-5%)
- **warn (40-64)**: 信号成立但有具体可量化风险 → 减仓跟或观察
  * 例: 多头趋势但 RSI=72 已超买 → BUY warn
- **reject (< 40)**: 技术面方向矛盾 / 新闻明确推翻 / 关键位严重不利 → 放弃
  * 信号方向与主趋势反向(多头排列里出 SELL、空头排列里出 BUY)
  * 距关键阻力/支撑 < 0.5% 几乎无空间
  * 明确利空新闻 + 技术面假突破

## 共振 + 市场天气调整规则(对 ai_confidence 微调)

**A. 多策略共振**(基于"多策略共振情况"):
- 0 个其他同向信号(孤狼): 默认;若仅"勉强 confirm"边缘特征,标 warn
- 1 个其他同向 confirm/warn(双重): 技术 + 新闻也支持时 +5
- ≥2 个其他同向 confirm(三重+): +10~15
- 共振信号都是 reject: 本信号必须 warn 或 reject

**B. 市场天气**(基于"市场天气"):
- A 股 BUY + 北向"强流入": +5;+ "强流出": -10
- 美股/加密 BUY + CNN F&G ≥80: -10;+ ≤20: +5
- 美股/加密 SELL 反向

**C. ADX 趋势强度**(基于技术快照 ADX):
- ADX ≥25 + 趋势策略(ma_cross / volume_breakout / chanlun / donchian_breakout): +5
- ADX ≤20 + 反转策略(bollinger_reversion / rsi_divergence): +5
- 趋势/反转策略错配(震荡市出强买突破): -5

**D. 通用约束**:
- 加减分总和上限 ±15(避免叠加膨胀)
- 共振是充分非必要;孤狼若主体技术 + 诊断都强支持,仍可 confirm,但 ai_confidence 不应虚高

## 提醒
- "可能回调""需要观望"这类保守担忧应该 warn,不是 reject
- confirm 不是"背书会赚钱",而是"信号合理,值得跟单"
- 加密衍生品规则独立判断,与上述阈值叠加
- 数值映射(reason→verdict)由后端处理,你只需如实给出 ai_confidence 和判定
"""


POSITION_ADVICE_PROMPT = """你是一位资深持仓顾问。任务是基于完整信息,对用户的持仓给出客观、可执行的操作建议。

【分析原则】
1. 用数据说话,不预设立场。证据支持持有就 hold,支持加仓就 add,支持减仓/平仓就 reduce/close。
2. 信息不充分时倾向 reduce 锁部分风险,优于盲目 hold 等待。
3. 与候选池诊断/策略信号/前次建议矛盾时,必须在 reason 给出可量化证据(数据变化/事件触发等)。
4. 数据缺失的维度直接跳过,不在缺失数据上推断。

【输入】
持仓品种: {symbol} ({market})
数量: {quantity}  成本价: {avg_cost}  当前价: {current_price}
浮动盈亏: {pnl_pct}%

最近 24 小时相关新闻 ({news_count} 条):
{news_summary}

【候选池 AI 诊断】(若存在,需对照避免前后矛盾):
{pool_diagnosis_context}

【最近 24 小时该品种策略信号】(系统集体立场,与你建议矛盾时需解释):
{recent_signals}

【你之前对此持仓的建议历史】(逻辑需一致;方向反转必须给出"市场已变化"的具体证据):
{advice_history}

【输出严格 JSON】

{{
  "advice": "hold|reduce|add|close",
  "urgency": "low|medium|high",
  "reason": "100 字以内核心理由;与诊断/信号/前次建议矛盾时必须说明",
  "key_factors": ["因素 1", "因素 2"],
  "alignment": "aligned_with_system|partial|conflict",
  "alignment_note": "一句话说明本建议与系统信号/诊断的一致/部分一致/冲突关系",
  "suggested_action": {{
    "reduce_pct": 0-100 或 null,   // advice=reduce 时必填(减仓百分比);其他情况 null
    "add_pct": 0-100 或 null,      // advice=add 时必填(加仓百分比);其他情况 null
    "target_price": 数字 或 null,  // 目标价(基于阻力位/技术目标位)
    "stop_loss": 数字 或 null      // 止损价(基于支撑位/ATR)
  }}
}}

【硬性约束】(影响 confidence 评分,违反则 reason 必须说清楚为何例外):

1. **行情一致性**: 浮亏 > 5% 且无明确利好催化时,不应给 hold(应 reduce 或 close)
2. **不反复变卦**: 30 min 内给过 reduce/close,现在改 hold/add 必须有"市场反转"的具体证据(如股价反弹 > 2%、重大利好发布);否则维持原方向
3. **RSI 不是单一信号**: 仅 RSI > 80 不足以触发 reduce,需配合"K 线收阴 + 量能放大 + 趋势转折"才算超买减仓
4. **避免沉没成本陷阱**: 浮亏 > 8% 且无明确利好时,应 close 或 reduce ≥ 50%;不要用"基本面中性偏多"作为继续持有的理由
5. **不确定优先 reduce**: 信息不充分时,reduce 锁部分风险比 hold 等待更稳健

【提醒】
- 仅对"明确利好/利空"给 add/close;不确定优先 reduce
- 最近策略信号全部 SELL/reduce 时若你建议 hold/add,reason 必须说清反向理由
- reduce_pct / add_pct 是该字段语义统一: reduce 时填减仓比例, add 时填加仓比例(以现持仓量为基数), 其他 advice 都填 null
"""


CRYPTO_DIAGNOSIS_PROMPT = """你是一位资深加密衍生品交易分析师。任务是综合技术、衍生品情绪、新闻和市场情绪,给出客观的诊断与可执行建议。

【分析原则】
1. 用数据说话,不预设立场。技术看多就标多,衍生品拥挤就指出风险,中性就承认中性。
2. 数据缺失的维度直接跳过(对应 view 字段标"数据缺失,本判断不计入");不在缺失数据上推断。
3. 5 档评级要分清楚,不要把所有模糊情况一律 hold(同时也不要为了展现观点强行给极端评级)。

【输入】
品种: {symbol}
当前价: {last_price}
24h 涨跌: {change_pct_24h}% (最高 {high_24h} / 最低 {low_24h})
24h 成交额: {vol_24h} USDT

期货情绪(过去 24h):
- 资金费率: {funding_rate}% (年化 {funding_annual}%) → 正值 = 多头付空头(多拥挤);负值 = 空头付多头(空拥挤)
- 持仓量变化: {oi_change}% (24h) → OI 升 + 价升 = 真多头;OI 降 + 价升 = 空头回补
- 普通账户多空比: {ls_ratio} (散户信号)
- 精英交易员多空比: {top_trader_ratio} (大户信号,通常更专业)
- 主动买卖: 买盘占比 {buy_pct}% ({taker_signal})

恐慌贪婪指数: {fng_value} ({fng_label})

技术面快照:
{tech_snapshot}

最近 48 小时相关新闻 ({news_count} 条):
{news_summary}

上一次诊断(供对比,无则为空):
{previous_diagnosis}

【输出严格 JSON】(不要解释/不要 markdown 包裹)

{{
  "rating": "strong_buy|buy|hold|reduce|sell",
  "confidence": 0-100,
  "summary": "80 字以内核心结论,说清当前状态",
  "change_from_last": "若上次诊断存在,说本次 vs 上次的关键变化(25 字内,如'资金费率从 0.01% 升到 0.08%,多头拥挤显化');无则为空",
  "market_regime": "趋势多头|趋势空头|震荡|顶部拥挤|底部反转",
  "strengths": ["做多理由 1", "理由 2", "理由 3"],
  "risks": ["风险 1", "风险 2", "风险 3"],
  "technical_view": "技术面一句话(趋势/位置/动量),数据缺失时标注",
  "derivatives_view": "期货情绪一句话(funding/OI/大户多空比综合),数据缺失时标注",
  "news_view": "新闻一句话(催化/利空),数据缺失时标注",
  "key_levels": {{"support": 数字 或 null, "resistance": 数字 或 null, "stop_loss": 数字 或 null, "take_profit": 数字 或 null}},
  "operations": {{
     "open_position": "是否适合开新仓;如何开(方向/仓位/触发条件)",
     "add_position": "已有仓位时是否加仓;何价何条件加",
     "reduce_position": "是否减仓;何价何条件减",
     "close_position": "是否清仓;什么信号出现必须清"
  }},
  "exit_triggers": ["撤出硬条件 1(可量化,如'funding 年化 > 150% 且大户多空比 < 0.7')", "条件 2(可填 1 条但需说明为何只 1 条)"],
  "horizon": "1-3d|3-14d|1-3m"
}}

【评级标准】
- **strong_buy**: 技术多头排列 + 精英多空比 > 1.3 + 资金费率不拥挤(年化 < 100%) + 无重大利空,**四者俱全**
- **buy**: 至少满足下列任一,且无明确利空:
  * 技术向好(MA 多头排列 / MACD 金叉 / 突破阻力)
  * 衍生品偏多(精英多空比 > 1.2 且 OI 配合上升)
  * 利好催化 + 价格未透支
- **hold**: 方向不明确 / 关键指标矛盾 / 拉锯整理
- **reduce**: 趋势转弱(跌破 MA20 / 资金费率拥挤过热) 或 出现可量化利空
- **sell**: 空头排列 + 跌破支撑 + 负面催化,三者至少俱两

【硬性约束】
1. 必须基于提供的数据,不编造
2. operations 四项都要填(即便"不建议开新仓"也要明确说)
3. key_levels 基于技术快照 20 日高低 + ATR,缺数据时填 null
4. 资金费率年化 > 100% 必须在 risks 标注"多头拥挤,回撤风险"
5. 大户与散户多空比严重背离(大户空、散户多)必须在 risks 标注"散户接盘风险"
6. exit_triggers 至少 1 条可量化硬条件;无法量化的"小心风险"不算
7. 若上次诊断存在且 rating 变化,change_from_last 必须说清触发因素
8. 数据缺失维度: 在对应 view 字段写"数据缺失,本判断未计入";rating 不应仅基于缺失维度推断
"""


STOCK_DIAGNOSIS_PROMPT = """你是一位资深股票分析师。任务是综合技术、基本面、新闻三维度,给出客观全面诊断。

【分析原则】
1. 用数据说话,不预设立场。证据支持就明确给方向,不要用 hold 当"安全选项"。
2. 5 档评级要用活,优质标的若"无明确利空 + 至少一维向好",应果断给 buy。
3. 数据缺失的维度直接跳过(对应 view 标"数据缺失,本判断不计入")。
4. confidence 反映把握度;基本面缺失时上限建议 70。

【输入】
品种: {symbol} ({market_label})
名称: {name}
当前价: {price}
入池来源: {source}
入池理由: {reason}

评分(0-100): 综合 {total_score} = 事件 {event_score} + 技术 {technical_score} + 基本面 {fundamentals_score}

基本面快照:
{fundamentals_text}

技术面快照(最近 60 根日 K):
{tech_snapshot}

最近 7 天相关新闻 ({news_count} 条):
{news_summary}

上一次诊断(供对比,无则为空):
{previous_diagnosis}

【输出严格 JSON】(不要解释/不要 markdown)

{{
  "rating": "strong_buy|buy|hold|reduce|sell",
  "confidence": 0-100,
  "summary": "60 字以内核心结论",
  "change_from_last": "若上次诊断存在,说本次 vs 上次的关键变化(20 字内);无则为空",
  "strengths": ["优势 1", "优势 2", "优势 3"],
  "risks": ["风险 1", "风险 2", "风险 3"],
  "technical_view": "技术面一句话(趋势/位置/动量),数据缺失时标注",
  "fundamental_view": "基本面一句话(市值/估值/流动性),数据缺失时标注",
  "news_view": "新闻面一句话(近期催化/利空),数据缺失时标注",
  "key_levels": {{"support": 数字 或 null, "resistance": 数字 或 null, "stop_loss": 数字 或 null}},
  "horizon": "1-5d|1-3m|3-12m",
  "next_action": "可执行的具体触发条件(如'价突破 X 且成交量 > 1.5 倍均量时小仓位试多'),非'建议关注'空话",
  "exit_triggers": ["撤出硬条件 1(引用具体数据,如'跌破 MA20'/'RSI > 75 且量比 > 1.5'/'出现 ★4+ 利空新闻')", "条件 2"]
}}

【评级标准】(避免一律 hold,五档用活)
- **strong_buy**: 强势多头(MA 多头排列 + MACD 金叉 + 放量) + 明确利好催化 + 基本面支持,三者俱全(稀有)
- **buy**: 至少下列一条,且无明确利空:
  * 技术向好(MA 多头排列 或 站稳 MA20 + 放量 或 MACD 金叉)
  * 基本面优(市值/估值/流动性三项中至少两项良好)
  * 估值合理偏低 + 赛道有热点
- **hold**: 方向不明 / 关键指标矛盾 / 支撑阻力间拉锯 / 等待信号确认
- **reduce**: 趋势转弱(跌破 MA20 或 MACD 死叉) 或 可量化利空催化
- **sell**: 空头排列 + 跌破关键支撑 + 负面催化,三者至少俱两

【硬性约束】
1. 基于提供的数据,不编造
2. 风险/优势各 2-3 条具体内容(如"RSI=65 接近超买",不写"动量过热"这种空话)
3. key_levels 基于技术快照 20 日高低/支撑阻力/ATR,无把握时 null
4. next_action 必须可执行,引用具体数据
5. exit_triggers 至少 1 条可量化条件;"发现异常"这种模糊话不算
6. 上次诊断存在且本次评级变化时,change_from_last 必须说清触发因素
7. 数据缺失维度: 对应 view 字段写"数据缺失,本判断未计入";rating 不应仅基于缺失维度推断
"""


# ═══════════════════════════════════════════════════════════════════
# 市场推断（与 scheduler._infer_stock_market 保持一致）
# ═══════════════════════════════════════════════════════════════════


def _infer_stock_market(symbol: str) -> str:
    """根据 symbol 推断市场：us/hk/cn/crypto/unknown。"""
    if not symbol:
        return "unknown"
    s = symbol.upper()
    if s.endswith("-USDT") or s.endswith("-USD") or s.endswith("-USDC"):
        return "crypto"
    if s.endswith(".HK"):
        return "hk"
    if s.isdigit() and len(s) == 6:
        return "cn"
    if s.isalpha() and 1 <= len(s) <= 5:
        return "us"
    return "unknown"


# ═══════════════════════════════════════════════════════════════════
# 价格预估（输入/输出 token × 单价）
# ═══════════════════════════════════════════════════════════════════

# 美元 / 1K tokens（按 ¥7/USD 折算 deepseek 官方人民币价；可能变动）
# v12.10: 加入 V4 系列定价（deepseek-chat/reasoner 即将弃用，二者对应 v4-flash 的非思考/思考模式）
PROVIDER_PRICING = {
    # V3 系列（旧名，即将弃用 — 留兼容估算）
    "deepseek-chat":     {"input": 0.00027, "output": 0.0011},
    "deepseek-reasoner": {"input": 0.00055, "output": 0.00219},
    # V4 系列（缓存未命中价；缓存命中时 deepseek 实际收 1/10，这里按未命中保守估）
    "deepseek-v4-flash": {"input": 0.000143, "output": 0.000286},  # ¥1/¥2 per 1M
    "deepseek-v4-pro":   {"input": 0.000429, "output": 0.000857},  # 优惠期 ¥3/¥6 per 1M（5/31 后涨到 ¥12/¥24）
    "qwen-turbo":        {"input": 0.0002,  "output": 0.0006},
    "qwen-plus":         {"input": 0.0008,  "output": 0.0020},
}
# 提供商兜底（按模型名拿不到时用）
PROVIDER_PRICING_FALLBACK = {
    "deepseek": PROVIDER_PRICING["deepseek-v4-pro"],   # 按 V4-pro 保守估
    "qwen":     PROVIDER_PRICING["qwen-turbo"],
}

# v12.10: 按 path 分流模型 + 思考强度
# 依据 V4 PDF Table 7：Flash-Max 在 HLE/LiveCodeBench/SimpleQA 中文 等多个基准 ≈ Pro-High，
#                    但价格 1/3。所以中频 path 用 Flash-Max 是性价比甜点；
#                    只有低频 + 质量决定教训闭环的 review 才上 Pro-Max。
PATH_LLM_CONFIG = {
    # 高频信号确认：thinking=True+low 确保结构化 JSON 可靠输出（no-thinking 实测 raw= 为空）
    "signal_verify":         {"model": "deepseek-v4-flash", "thinking": True,  "effort": "low"},
    # 中频结构化输出：news/diagnose 类 → Flash + thinking
    "news":                  {"model": "deepseek-v4-flash", "thinking": True,  "effort": "high"},
    "diagnose":              {"model": "deepseek-v4-flash", "thinking": True,  "effort": "max"},
    "crypto_diag":           {"model": "deepseek-v4-flash", "thinking": True,  "effort": "max"},
    "position_advice":       {"model": "deepseek-v4-flash", "thinking": True,  "effort": "high"},
    # 低频 + 决定经验闭环：trade_review/weekly_review → Pro Max（极限推理 + 长上下文）
    "trade_review":          {"model": "deepseek-v4-pro",   "thinking": True,  "effort": "max"},
    "trade_review_weekly":   {"model": "deepseek-v4-pro",   "thinking": True,  "effort": "max"},
}
PATH_LLM_DEFAULT = {"model": "deepseek-v4-flash", "thinking": True, "effort": "high"}


def _estimate_cost(provider: str, input_tokens: int, output_tokens: int, model: str = "") -> float:
    """优先按 model 精确取价；取不到时按 provider 兜底（保守往高了估）。"""
    model_lc = (model or "").lower()
    p = None
    for k, v in PROVIDER_PRICING.items():
        if k in model_lc:
            p = v
            break
    if p is None:
        p = PROVIDER_PRICING_FALLBACK.get(provider, {"input": 0.001, "output": 0.002})
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1000


# ═══════════════════════════════════════════════════════════════════
# 主分析器
# ═══════════════════════════════════════════════════════════════════


class NewsAIAnalyzer:
    """
    LLM 深度解读 + 持仓建议。共用日预算池。
    """

    def __init__(self, db, ws_hub=None):
        self.db = db
        self.ws_hub = ws_hub
        self._client = None
        self._provider = None
        self._model = None
        # 当日成本快照 (避免每次都查 DB)
        self._today_cost: float = 0.0
        self._today_date: str = ""
        # 并发限制 (LLM 单账户限速)
        # 全局 Semaphore 提到 8（deepseek-reasoner 单账户限速 60 RPM，8 并发 + 单次 ~5s ≈ 96 RPM）
        # 用户充值后允许更高吞吐；如果触发 429 再调回 6
        # crypto_diag 单独 Semaphore(2)，6 币诊断时也能并发跑 2 个
        self._semaphore = asyncio.Semaphore(8)
        self._crypto_semaphore = asyncio.Semaphore(2)
        # 持仓新闻批量缓冲：{ (symbol, market): {"news": [..], "first_ts": ms} }
        # 同品种新闻 30 分钟窗口内累积，到期统一喂给 LLM 一次
        self._pos_news_buffer: Dict[tuple, Dict[str, Any]] = {}
        self._pos_buffer_lock = asyncio.Lock()
        self._pos_batch_task: Optional[asyncio.Task] = None
        # 窗口参数
        self.POS_BATCH_WINDOW_SEC = 1800   # 30 分钟
        self.POS_BATCH_MAX_NEWS = 8        # 或新闻达到 8 条立即触发
        # 保存 fire-and-forget 任务引用
        self._bg_tasks: set = set()

    async def shutdown(self):
        """应用关闭时取消所有 fire-and-forget 任务，避免僵尸任务或孤立 DB 操作。"""
        # 取消批量 position news flusher
        t = self._pos_batch_task
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        # 取消所有后台任务
        for task in list(self._bg_tasks):
            if not task.done():
                task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        # 关闭 AsyncOpenAI 客户端持有的 httpx.AsyncClient（避免 Unclosed warning）
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
        logger.info("NewsAIAnalyzer shutdown 完成")

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        def _done(task):
            self._bg_tasks.discard(task)
            if not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.warning(f"[ai_analyzer] 后台任务异常: {type(exc).__name__}: {exc}")
        t.add_done_callback(_done)
        return t

    # ───────── 客户端懒初始化 ─────────

    def _ensure_client(self) -> bool:
        """读取运行时配置初始化 OpenAI 兼容异步客户端。返回是否可用。
        用 AsyncOpenAI 原生异步，免 asyncio.to_thread 的线程切换开销。
        """
        try:
            from openai import AsyncOpenAI
        except ImportError:
            logger.warning("openai 库未安装，LLM 不可用")
            return False

        provider = config.LLM_PROVIDER or "deepseek"
        if provider == "deepseek":
            api_key = config.DEEPSEEK_API_KEY
            base_url = config.DEEPSEEK_BASE_URL
            model = config.DEEPSEEK_MODEL
        elif provider == "qwen":
            api_key = config.QWEN_API_KEY
            base_url = config.QWEN_BASE_URL
            model = config.QWEN_MODEL
        else:
            logger.warning(f"未知 LLM 提供商: {provider}")
            return False

        if not api_key:
            logger.debug(f"LLM provider={provider} API Key 未配置，跳过深度解读")
            return False

        # 配置变化时重新创建客户端
        if (self._client is None or
                self._provider != provider or
                getattr(self._client, "_base_url_str", None) != base_url):
            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            self._client._base_url_str = base_url
            self._provider = provider
            self._model = model
            disp = model if model else "AUTO (per-path: v4-flash / v4-pro)"
            logger.info(f"LLM 客户端初始化（异步）: provider={provider}, model={disp}")

        return True

    # ───────── 预算控制 ─────────

    async def _refresh_today_cost(self):
        """从 DB 加载当日累计成本（每次调用前快速查）。"""
        # v12.18.2: 修复时区 bug — 服务器东京但用户在北京，"今日成本"按北京日历算
        # 之前 time.strftime + time.mktime 拿东京 today 00:00，比北京 today 00:00 早 1h
        from datetime import datetime as _dt2
        from zoneinfo import ZoneInfo as _ZI2
        _now_bj = _dt2.now(_ZI2("Asia/Shanghai"))
        today = _now_bj.strftime("%Y-%m-%d")
        if self._today_date != today:
            # 新一天，重置
            self._today_date = today
            self._today_cost = 0.0

        try:
            _day_start_bj = _now_bj.replace(hour=0, minute=0, second=0, microsecond=0)
            day_start_ms = int(_day_start_bj.timestamp() * 1000)
            async with self.db.acquire() as conn:
                cursor = await conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0) as total FROM llm_cost_log WHERE called_at >= ?",
                    (day_start_ms,),
                )
                row = await cursor.fetchone()
                self._today_cost = float(row["total"]) if row else 0.0
        except Exception as e:
            logger.debug(f"读取当日 LLM 成本失败: {e}")

    async def _can_call(self, hard_stop: bool = True) -> bool:
        """
        检查日预算。v12.11 修：默认改为 hard_stop=True（之前默认 False 让所有 path 超支后照烧）。
        - hard_stop=True (默认): 超支返回 False，调用方必须自己判断后早返回
        - hard_stop=False: 仅告警不阻断（保留供个别 force=True 真正必须执行的关键路径）
        告警降频：同一阶段（80% / 100%）每 5 分钟最多告警一次，避免日志洪流。
        """
        await self._refresh_today_cost()
        budget = config.LLM_DAILY_BUDGET
        now = time.time()
        WARN_INTERVAL = 300

        if self._today_cost >= budget:
            if hard_stop:
                if (now - getattr(self, "_warn_over_at", 0)) > WARN_INTERVAL:
                    logger.warning(
                        f"⛔ LLM 日预算 ${budget:.2f} 已超支 (当前 ${self._today_cost:.2f})，"
                        f"硬熔断本次调用（5 分钟内不再重复告警）"
                    )
                    self._warn_over_at = now
                return False
            if (now - getattr(self, "_warn_over_at", 0)) > WARN_INTERVAL:
                logger.warning(
                    f"⚠️ LLM 日预算 ${budget:.2f} 已超支 (当前 ${self._today_cost:.2f})，软警告继续调用（force 路径）"
                )
                self._warn_over_at = now
        elif self._today_cost >= budget * 0.8:
            if (now - getattr(self, "_warn_near_at", 0)) > WARN_INTERVAL:
                logger.warning(
                    f"⚠️ LLM 日成本接近预算: ${self._today_cost:.2f} / ${budget:.2f}"
                )
                self._warn_near_at = now
        return True

    async def _record_cost(
        self,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
        news_id: Optional[str] = None,
        path: str = "",
    ):
        """成本入库 llm_cost_log，path 区分 news/signal_verify/diagnose/position_advice。"""
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO llm_cost_log
                    (called_at, model, input_tokens, output_tokens, cost_usd, news_id, path)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(time.time() * 1000),
                        self._model or "unknown",
                        input_tokens,
                        output_tokens,
                        cost_usd,
                        news_id,
                        path,
                    ),
                )
                await conn.commit()
            self._today_cost += cost_usd
        except Exception as e:
            logger.debug(f"成本入库失败: {e}")

    # ───────── LLM 调用核心 ─────────

    async def _call_llm(
        self,
        prompt: str,
        news_id: Optional[str] = None,
        max_tokens: int = 800,
        path: str = "",
        force: bool = False,  # v12.11: True = 即使超预算也跑（force 路径专用）
    ) -> Optional[Dict[str, Any]]:
        """
        统一 LLM 调用入口。返回解析后的 JSON dict 或 None。
        path: 用于成本统计 + 路由模型/思考强度（PATH_LLM_CONFIG）。
        v12.10: 全面升级到 V4 系列 — Flash 替代旧 chat、Pro 替代旧 reasoner（review 类用 Pro，其余用 Flash）。
                旧 deepseek-chat / deepseek-reasoner 即将被官方弃用，必须迁移。
        """
        if not self._ensure_client():
            return None
        # v12.11: _can_call 默认 hard_stop=True；非 force 路径超预算直接拒（避免失控烧钱）
        if not await self._can_call(hard_stop=not force):
            logger.warning(f"⛔ LLM 调用被预算熔断 (path={path}, force={force})")
            return None

        # v12.10: 按 path 选模型 + 思考强度（PATH_LLM_CONFIG）
        # v12.11 修：仅当用户完全没设（空值）时走 path 路由；显式写了任何 model 名都尊重用户
        # 旧名 deepseek-chat / deepseek-reasoner 即将弃用但仍 honor，让用户主动迁移
        path_cfg = PATH_LLM_CONFIG.get(path, PATH_LLM_DEFAULT)
        if self._provider == "deepseek":
            user_override = (self._model or "").strip()
            model = path_cfg["model"] if user_override == "" else user_override
        else:
            model = self._model  # 非 deepseek provider 走原 model
        thinking_on = path_cfg.get("thinking", False)
        effort = path_cfg.get("effort")

        # V4 思考模式参数（OpenAI 兼容格式 → extra_body）
        # v12.11 修：thinking 关闭时不传 disabled 字段（API 默认即为关），避免冗余/未来兼容风险
        extra_body: Dict[str, Any] = {}
        if thinking_on:
            extra_body["thinking"] = {"type": "enabled"}
            if effort:
                extra_body["reasoning_effort"] = effort

        # 思考模式输出 token 上限放大（思考链 reasoning_content 与 content 同级返回）
        # v12.12 修：16384（原 8192 对 trade_review thinking+max 不够 — reasoning 消耗同 budget）
        # max effort timeout 提到 300s（weekly review Pro+Max 实测可达 200-300s）
        OUTPUT_HARD_CAP = 16384
        if thinking_on:
            mul = 8 if effort == "max" else 5
            effective_max_tokens = min(max(max_tokens * mul, 4000), OUTPUT_HARD_CAP)
            effective_timeout = 300 if effort == "max" else 120
        else:
            effective_max_tokens = min(max_tokens, OUTPUT_HARD_CAP)
            effective_timeout = 30

        # 选择并发车道：crypto_diag 走专用信号量（避免被 signal_verify/news 挤占）
        sem = self._crypto_semaphore if path == "crypto_diag" else self._semaphore
        async with sem:
            try:
                # AsyncOpenAI 原生异步，无需 to_thread；免去每次独占一个线程池 worker
                resp = await self._client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=effective_max_tokens,
                    timeout=effective_timeout,
                    extra_body=extra_body if extra_body else None,
                )
            except Exception as e:
                logger.warning(f"LLM 调用失败 (path={path}, model={model}): {type(e).__name__}: {e}")
                return None

        # 解析返回
        # v12.11 修：strip ```json/``` 围栏；reasoning_content 字段（V4 思考模式）单独 debug log
        try:
            import re as _re
            msg = resp.choices[0].message
            content = (getattr(msg, "content", None) or "").strip()
            # 思考链 debug log（不参与解析，但便于排错）
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                logger.debug(f"[reasoning] path={path} len={len(reasoning)} head={reasoning[:200]!r}")
            # v12.11: 用正则提取 ```json ... ``` 围栏（兼容前置说明文字、末尾少 ```、json 标签缺失）
            fence_match = _re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", content)
            if fence_match:
                content = fence_match.group(1)
            elif content.startswith("```"):
                # 末尾少 ``` 的兜底：剥首行 ```xxx 标记
                lines = content.split("\n", 1)
                if len(lines) > 1:
                    content = lines[1].strip()
                    if content.endswith("```"):
                        content = content[:-3].strip()
            try:
                # 1) 直接尝试解析整段（最快路径）
                result = json.loads(content)
            except json.JSONDecodeError:
                # 2) 失败 → 提取首个 { 到末尾 } 之间的子串
                start = content.find("{")
                end = content.rfind("}")
                if start >= 0 and end > start:
                    result = json.loads(content[start:end + 1])
                else:
                    raise
        except (json.JSONDecodeError, IndexError, AttributeError) as e:
            raw_content = resp.choices[0].message.content if resp.choices else 'no choices'
            logger.warning(f"LLM 返回解析失败 (path={path}): {e}, raw={(raw_content or '')[:300]}")
            return None

        # 记录成本
        try:
            usage = resp.usage
            input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0
            cost = _estimate_cost(self._provider, input_tokens, output_tokens, model=model)
            await self._record_cost(cost, input_tokens, output_tokens, news_id, path=path)
            think_tag = f"+{effort}" if thinking_on and effort else ("+think" if thinking_on else "")
            logger.info(
                f"💡 LLM 调用成功 ({self._provider}/{model}{think_tag}, path={path}): "
                f"in={input_tokens} out={output_tokens} cost=${cost:.4f}"
            )
        except Exception as e:
            logger.debug(f"成本统计异常: {e}")

        return result

    # ───────── 构建关注列表上下文（供 LLM 挑选 impacts）─────────

    # 5 分钟缓存：候选池/持仓变化慢，每条 ★4+ 新闻都重查 + 组装是浪费
    _watch_ctx_cache: Optional[tuple] = None  # (ts_sec, text)

    async def _build_watch_context(self) -> str:
        """
        返回分组的关注清单文本，供 deep_analyze_news prompt 注入：
          - 加密 6 币种
          - 持仓（含加密+股票）
          - 候选池 Top 30 按评分降序
        加 5 分钟缓存：减 50 次 DB 查询/天 + 减少 input tokens 重复。
        """
        now = time.time()
        cached = self._watch_ctx_cache
        if cached and (now - cached[0]) < 300:  # 5 分钟
            return cached[1]
        lines = []
        try:
            import backend.config as config
            lines.append("- 加密: " + ", ".join(config.CRYPTO_SYMBOLS))
        except Exception:
            pass
        # 持仓
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute("SELECT symbol, market FROM positions")
                rows = await cur.fetchall()
            if rows:
                holding_strs = [f"{r['symbol']}({r['market']})" for r in rows]
                lines.append("- 当前持仓: " + ", ".join(holding_strs))
        except Exception:
            pass
        # 候选池
        try:
            items = await self.db.get_pool_items(limit=30)
            if items:
                items.sort(key=lambda x: x.get("score", 0) or 0, reverse=True)
                pool_strs = [f"{it['symbol']}({it['market']},{int(it.get('score',0))}分)" for it in items[:30]]
                lines.append("- 候选池 Top30: " + ", ".join(pool_strs))
        except Exception:
            pass
        text = "\n".join(lines) if lines else "（当前用户无特别关注）"
        self._watch_ctx_cache = (now, text)
        return text

    # ───────── 新闻深度解读 ─────────

    async def deep_analyze_news(self, news: Dict[str, Any], force: bool = False) -> Optional[Dict[str, Any]]:
        """
        对单条新闻做深度解读。
        news 字段: id, title, source, content, categories, importance
        force=False (默认): 后台补全走此路径，预算超支会阻断。
        force=True (用户主动触发): 即便超支也调用。
        """
        title = news.get("title", "")
        if not title:
            return None
        if not self._ensure_client():
            return None
        # 后台任务 hard_stop 阻断；用户主动只告警
        if not await self._can_call(hard_stop=not force):
            return None
        # 构建用户关注上下文（持仓 + 候选池 Top 30 + 加密 6 币种）
        watch_context = await self._build_watch_context()
        prompt = DEEP_ANALYSIS_PROMPT.format(
            title=title[:200],
            source=news.get("source", ""),
            content=(news.get("content") or "")[:1500],
            categories=", ".join(news.get("categories") or []) or "无",
            watch_context=watch_context,
        )
        result = await self._call_llm(prompt, news_id=news.get("id"), path="news", force=force)
        if result:
            # 入库回填
            try:
                async with self.db.acquire() as conn:
                    await conn.execute(
                        "UPDATE flash_news SET ai_analysis = ? WHERE id = ?",
                        (json.dumps(result, ensure_ascii=False), news["id"]),
                    )
                    await conn.commit()
            except Exception as e:
                logger.debug(f"AI 分析回填失败: {e}")
            # AI 识别的影响品种 → 候选池（闭环 F6.1 扩展）
            try:
                await self._auto_add_ai_impacts_to_pool(news, result)
            except Exception as e:
                logger.debug(f"AI 影响品种入池失败: {e}")
        return result

    # ───────── AI impacts → 候选池 ─────────

    async def _enqueue_pending_review(self, symbol, market, source, score, reason):
        """暂存到 pool_pending_review，后台 30 分钟重试。"""
        import time as _t
        now = int(_t.time())
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO pool_pending_review
                       (symbol, market, source, score, reason, first_attempt_at, last_attempt_at, attempts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                       ON CONFLICT(symbol, market) DO UPDATE SET
                           last_attempt_at=excluded.last_attempt_at,
                           attempts=pool_pending_review.attempts+1""",
                    (symbol, market, source, score, reason, now, now),
                )
                await conn.commit()
        except Exception as e:
            logger.debug(f"暂存待审失败 {symbol}: {e}")

    async def _auto_add_ai_impacts_to_pool(
        self, news: Dict[str, Any], ai_result: Dict[str, Any]
    ) -> int:
        """
        把 LLM 识别的 impacts[].symbol 推入候选池。
        加密 6 币种跳过；仅 direction=bullish/bearish 且 strength>=0.6 才入池（宁缺毋滥）。
        score = 基础 50 + importance × 5 + strength × 20 (上限 100)
        返回新增入池数量。
        """
        impacts = ai_result.get("impacts") or []
        if not impacts:
            return 0
        importance = int(news.get("importance") or 2)
        title = (news.get("title") or "")[:80]
        added = 0
        for imp in impacts:
            try:
                sym = (imp.get("symbol") or "").strip()
                direction = imp.get("direction") or "neutral"
                strength = float(imp.get("strength") or 0)
                if not sym or direction == "neutral" or strength < 0.6:
                    if sym:
                        logger.info(f"[ai-impacts] 拒 {sym}: direction={direction} strength={strength}")
                    continue
                # symbol 格式校验：必须是合法股票/加密代码
                if len(sym) > 12 or not all(c.isalnum() or c in '.-' for c in sym):
                    logger.info(f"[ai-impacts] 拒 {sym}: 非法 symbol 格式")
                    continue
                market = _infer_stock_market(sym)
                if market not in ("us", "hk", "cn"):
                    continue
                # v11.4 修复：先算 score（pending_review 入参依赖），再做质量硬筛
                from backend.watchpool.scorer import event_score_ai
                score = event_score_ai(importance, strength)
                # 质量硬筛选（严格）
                try:
                    from backend.watchpool.quality_filter import is_eligible
                    ok, reason = await is_eligible(self.db, sym, market)
                    if not ok:
                        logger.info(f"[ai-filter] 拒绝 {sym}/{market}: {reason}")
                        if "数据源" in reason:
                            await self._enqueue_pending_review(sym, market, "news_ai", score, f"AI {direction}({strength:.2f})")
                        continue
                except Exception as e:
                    logger.debug(f"[ai-filter] 调用异常 {sym}: {e}")
                # 保留完整上下文进入 reason（含方向/强度/时间 horizon，供候选池诊断复用）
                horizon = imp.get("horizon") or "1-5d"
                imp_reason = (imp.get("reason") or "")[:60]
                reason = (
                    f"AI ★{importance} {direction}/{horizon}(强度{strength:.2f}) "
                    f"· {imp_reason} · 新闻:{title}"
                )
                # 判断是已存在还是新入：先查一次
                existing = None
                try:
                    async with self.db.acquire() as conn:
                        cur = await conn.execute(
                            "SELECT id, score, status FROM watch_pool WHERE symbol=? AND market=?",
                            (sym, market),
                        )
                        existing = await cur.fetchone()
                except Exception:
                    pass
                pool_id = await self.db.add_to_pool(
                    symbol=sym,
                    market=market,
                    source="news_ai",
                    score=score,
                    reason=reason,
                )
                action = "added" if not existing else "updated"
                if self.ws_hub is not None:
                    try:
                        await self.ws_hub.broadcast_pool_update(
                            action,
                            {
                                "id": pool_id,
                                "symbol": sym,
                                "market": market,
                                "source": "news_ai",
                                "score": score,
                                "prev_score": (existing["score"] if existing else None),
                                "direction": direction,
                                "strength": strength,
                                "reason": f"AI {direction} 强度{strength:.2f}",
                            },
                        )
                    except Exception:
                        pass
                added += 1
            except ValueError:
                # market CHECK 拒绝
                pass
            except Exception as e:
                logger.debug(f"AI impacts 单条入池失败 {imp}: {e}")
        if added:
            logger.info(f"🤖→🧺 AI 分析驱动入池 {added} 只 (news={news.get('id')})")
        return added

    # ───────── 持仓建议（增强 PortfolioAdvisor） ─────────

    # ───────── 策略信号 AI 二次验证 ─────────

    async def _load_previous_diagnosis(self, symbol: str, market: str) -> str:
        """给诊断函数调用：从 ai_diagnosis_history 拉上一次诊断，供 LLM 对比。"""
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT diagnosis, rating, confidence, diagnosed_at FROM ai_diagnosis_history
                       WHERE symbol=? AND market=?
                       ORDER BY diagnosed_at DESC LIMIT 1""",
                    (symbol, market),
                )
                row = await cur.fetchone()
            if not row:
                return "（无历史诊断）"
            age_h = (time.time() - (row["diagnosed_at"] or 0)) / 3600
            try:
                d = json.loads(row["diagnosis"])
            except Exception:
                d = {}
            summary = (d.get("summary") or "")[:100]
            na = (d.get("next_action") or "")[:80]
            return (
                f"- 时间：{age_h:.0f} 小时前\n"
                f"- 评级：{row['rating']} (conf {row['confidence']})\n"
                f"- 当时摘要：{summary}\n"
                f"- 当时建议：{na}"
            )
        except Exception as e:
            logger.debug(f"[prev-diag] {symbol} 失败: {e}")
            return "（无历史诊断）"

    async def _build_diagnosis_context(self, symbol: str, market: str) -> str:
        """
        统一的诊断上下文构建：
          - 股票 (us/hk/cn): 读 watch_pool.ai_diagnosis
          - 加密 (crypto):    读 crypto_diagnosis
        返回 rating / confidence / summary / 上次对比 等文本。
        没有诊断就返回"无诊断"。
        """
        if not symbol:
            return "（无诊断）"
        try:
            async with self.db.acquire() as conn:
                if market == "crypto":
                    cur = await conn.execute(
                        "SELECT diagnosis, rating, confidence, diagnosed_at FROM crypto_diagnosis WHERE symbol=?",
                        (symbol,),
                    )
                else:
                    cur = await conn.execute(
                        "SELECT ai_diagnosis AS diagnosis, '' AS rating, 0 AS confidence, ai_diagnosed_at AS diagnosed_at "
                        "FROM watch_pool WHERE symbol=? AND market=? AND status!='archived' LIMIT 1",
                        (symbol, market),
                    )
                row = await cur.fetchone()
            if not row or not row["diagnosis"]:
                return "（该品种暂无 AI 诊断）"
            try:
                diag = json.loads(row["diagnosis"]) if isinstance(row["diagnosis"], str) else (row["diagnosis"] or {})
            except Exception:
                diag = {}
            rating = diag.get("rating") or row["rating"] or "unknown"
            conf = diag.get("confidence") or row["confidence"] or 0
            summary = (diag.get("summary") or "")[:120]
            na = (diag.get("next_action") or "")[:80]
            ts = row["diagnosed_at"] or 0
            age_h = (time.time() - ts) / 3600 if ts else 0
            # 加上一次诊断对比（让 AI 理解走势变化）
            prev_txt = ""
            try:
                m_key = market if market == "crypto" else market
                async with self.db.acquire() as conn:
                    cur = await conn.execute(
                        """SELECT rating, confidence, diagnosed_at FROM ai_diagnosis_history
                           WHERE symbol=? AND market=? AND diagnosed_at < ?
                           ORDER BY diagnosed_at DESC LIMIT 1""",
                        (symbol, m_key, ts),
                    )
                    prow = await cur.fetchone()
                if prow:
                    prev_age_h = (time.time() - prow["diagnosed_at"]) / 3600
                    prev_txt = f"\n- 上一次诊断（{prev_age_h:.0f}h 前）: {prow['rating']} (conf {prow['confidence']})"
            except Exception:
                pass
            return (
                f"- 当前诊断: **{rating}** (置信度 {conf}, {age_h:.0f}h 前)\n"
                f"- 摘要: {summary}\n"
                f"- 建议动作: {na}"
                f"{prev_txt}"
            )
        except Exception as e:
            logger.debug(f"[diag-ctx] {symbol} 构建失败: {e}")
            return "（诊断查询失败）"

    async def _build_crypto_verify_context(self, symbol: str) -> str:
        """
        给加密信号 AI 验证提供期货市场情绪上下文：
          - 资金费率 + 年化
          - 持仓量 24h 变化
          - 散户多空比 + 大户多空比（重点）
          - 主动买卖占比
          - 恐慌贪婪指数
        优先读 crypto_insights_snapshot 缓存（1 小时内），否则实时拉。
        """
        data = None
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT payload, updated_at FROM crypto_insights_snapshot WHERE symbol=?",
                    (symbol,),
                )
                row = await cur.fetchone()
            if row and (int(time.time()) - (row["updated_at"] or 0)) < 3600:
                data = json.loads(row["payload"])
        except Exception:
            pass
        if data is None:
            # 实时拉
            try:
                from backend.crypto_dashboard.sentiment import SentimentData
                data = await SentimentData().get_insights(symbol)
                # 回写缓存
                try:
                    async with self.db.acquire() as conn:
                        await conn.execute(
                            "INSERT OR REPLACE INTO crypto_insights_snapshot (symbol, payload, updated_at) VALUES (?, ?, ?)",
                            (symbol, json.dumps(data, ensure_ascii=False), int(time.time())),
                        )
                        await conn.commit()
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f"[verify-crypto-ctx] {symbol} 拉取 insights 失败: {e}")
                return "（无法获取期货数据）"
        if not data:
            return "（无期货数据）"
        # 组装文本
        f = (data.get("funding_rate") or {}).get("current") or {}
        oi = data.get("oi_history") or {}
        ls = (data.get("long_short_ratio") or {}).get("current") or {}
        ls_sig = (data.get("long_short_ratio") or {}).get("signal", "-")
        top = (data.get("top_trader_ratio") or {}).get("current") or {}
        top_sig = (data.get("top_trader_ratio") or {}).get("signal", "-")
        taker = (data.get("taker_volume") or {}).get("current") or {}
        taker_sig = (data.get("taker_volume") or {}).get("signal", "-")
        fng = data.get("fear_greed") or {}
        def _fmt(v, fmt="{:.4f}"):
            try:
                return fmt.format(float(v)) if v is not None else "-"
            except Exception:
                return "-"
        lines = [
            f"- 资金费率: {_fmt(f.get('rate_pct'), '{:+.4f}')}% (年化 {_fmt(f.get('annualized_pct'), '{:+.1f}')}%)",
            f"- 持仓量 24h 变化: {_fmt(oi.get('oi_change_24h_pct'), '{:+.2f}')}%",
            f"- 散户多空比: {_fmt(ls.get('ratio'), '{:.2f}')} ({ls_sig})",
            f"- 💎 大户持仓多空比: {_fmt(top.get('ratio'), '{:.2f}')} ({top_sig})  ← 大户方向比散户更具参考价值",
            f"- 主动买盘占比: {_fmt(taker.get('buy_pct'), '{:.1f}')}% ({taker_sig})",
            f"- 恐慌贪婪指数: {fng.get('value', '-')} ({fng.get('label_cn', '-')})",
        ]
        return "\n".join(lines)

    async def verify_signal(self, signal_data: Dict[str, Any], recent_news: List[Dict] = None) -> Optional[Dict]:
        """
        对一个触发的策略信号做 AI 二次验证。
        signal_data 必须含: id, symbol, market, action, strategy_name, interval, confidence, price, reason
        recent_news: 最近 24 小时相关新闻列表（可选）
        返回 {verdict, ai_confidence, reason} 或 None
        """
        if not signal_data:
            return None
        recent_news = recent_news or []
        news_summary = "\n".join(
            f"- ★{n.get('importance',1)} [{n.get('sentiment','neutral')}] {n.get('title','')[:80]}"
            for n in recent_news[:6]
        ) or "无相关新闻"

        # 加密信号：注入期货衍生品情绪（大户多空比、资金费率、OI 变化等）
        crypto_ctx = "（非加密信号，不适用）"
        market = signal_data.get("market", "")
        symbol = signal_data.get("symbol", "")
        if market == "crypto" and symbol:
            crypto_ctx = await self._build_crypto_verify_context(symbol)

        # 诊断上下文：股票读 watch_pool，加密读 crypto_diagnosis
        diagnosis_ctx = await self._build_diagnosis_context(symbol, market)

        prompt = SIGNAL_VERIFY_PROMPT.format(
            symbol=signal_data.get("symbol", ""),
            market=signal_data.get("market", ""),
            interval=signal_data.get("interval", "1H"),
            action=signal_data.get("action", "").upper(),
            price=signal_data.get("price", 0),
            strategy=signal_data.get("strategy_name", ""),
            confidence=signal_data.get("confidence", 0),
            reason=signal_data.get("reason", ""),
            tech_snapshot=signal_data.get("tech_snapshot", "（未提供技术快照）"),
            news_count=len(recent_news),
            news_summary=news_summary,
            crypto_insights=crypto_ctx,
            diagnosis_context=diagnosis_ctx,
            consensus_context=signal_data.get("consensus_context", "无（本信号是当前 30min 内首个）"),
            market_weather=signal_data.get("market_weather", "（暂无市场天气数据）"),
        )
        # v12.5 Phase A: 注入近期复盘提炼的教训（反馈闭环）
        try:
            from backend.trading.reviewer import get_top_lessons_for_prompt
            lessons_block = await get_top_lessons_for_prompt(self.db, market, top_n=5)
            if lessons_block:
                prompt += "\n" + lessons_block
        except Exception:
            pass
        # reasoner 会先消耗 reasoning_tokens 才产 content；900→effective 4500
        # 之前 1500 (effective 7500) 过多浪费 output token，实测 output max ~4400 用不到
        # 输出 JSON 核心字段 ~400 tokens 足够；给 reasoning 留 4000 足矣，省 30-40% cost
        result = await self._call_llm(prompt, max_tokens=900, path="signal_verify")
        if not result:
            return None
        # 校验 verdict 合法性
        VALID = {"confirm", "warn", "reject"}
        ai_conf = int(result.get("ai_confidence", 0) or 0)
        raw_verdict = str(result.get("verdict", "")).lower().strip()

        # 按 ai_confidence 数值映射 verdict，避免 LLM 用更严标准（给 70 分却标 warn）
        # v12.13: 阈值收紧 60→65（COIN 事故复盘：conf=62 + reason="建议减仓或等待"被强升 confirm，
        #         开仓后 -8.20% 一刀切 -$477。65+ 分要求让"勉强 confirm"信号被 warn 拦截）
        # 阈值：≥65 confirm / 40-64 warn / <40 reject
        if ai_conf >= 65:
            forced_verdict = "confirm"
        elif ai_conf >= 40:
            forced_verdict = "warn"
        else:
            forced_verdict = "reject"

        # 关键：LLM raw_verdict 是 reject 或 warn 必须尊重（LLM 读到 prompt 外的风险）
        # v12.13: 加 warn 进尊重列表 — 之前只防 reject 翻案，但 warn 被强升 confirm 是另一类隐患
        # （LLM 自己说"建议减仓或等待"标 warn，被强升 confirm 后系统按 confirm 开仓 → 严重错入场）
        if raw_verdict in ("reject", "warn"):
            verdict = raw_verdict
        else:
            verdict = forced_verdict

        if verdict != raw_verdict and raw_verdict in VALID:
            logger.debug(f"[verify-override] LLM 给 {raw_verdict}(conf={ai_conf}) → 按数值强制为 {verdict}")

        # SL/TP 解析（可能为 null/数字/字符串）
        def _to_float(v):
            if v is None: return None
            try:
                f = float(v)
                if f != f or f == float('inf') or f == float('-inf'): return None
                return f
            except (TypeError, ValueError):
                return None

        # 关键：缠论策略不使用传统 SL/TP（出场靠反向信号 + 诊断变化）
        # 即使 LLM 主动给了 SL/TP，也要强制清掉，保持缠论理论纯粹
        strategy_name = signal_data.get("strategy_name", "")
        if strategy_name == "chanlun":
            ai_sl = None
            ai_tp = None
        else:
            ai_sl = _to_float(result.get("ai_stop_loss"))
            ai_tp = _to_float(result.get("ai_take_profit"))

        return {
            "verdict": verdict,
            "ai_confidence": ai_conf,
            "reason": str(result.get("reason", ""))[:300],
            "ai_stop_loss": ai_sl,
            "ai_take_profit": ai_tp,
        }

    async def diagnose_crypto(
        self,
        symbol: str,
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        加密货币全面诊断：融合期货市场情绪 + 技术面 + 新闻。
        symbol: 现货代码如 BTC-USDT / ETH-USDT ...
        force=True: 用户主动触发（绕过日预算）。
        结果写入 crypto_diagnosis + ai_diagnosis_history 两张表。
        """
        import numpy as np
        from backend.data.models import Interval, Market
        from backend.data.cache import cached_get_klines
        from backend.indicators.builtin import calc_ma, calc_rsi, calc_macd, calc_volume_ma
        from backend.crypto_dashboard.sentiment import SentimentData

        if not self._ensure_client():
            return None
        if not await self._can_call(hard_stop=not force):
            return None

        # 1) 拉市场数据
        sd = SentimentData()
        try:
            insights = await sd.get_insights(symbol)
        except Exception as e:
            logger.warning(f"[crypto-diag] {symbol} insights 拉取失败: {e}")
            return None

        # 缓存 insights（供前端查询，不用重复拉）
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO crypto_insights_snapshot (symbol, payload, updated_at) VALUES (?, ?, ?)",
                    (symbol, json.dumps(insights, ensure_ascii=False), int(time.time())),
                )
                await conn.commit()
        except Exception:
            pass

        # 2) 技术面快照
        tech_snapshot = "（K 线数据不足）"
        try:
            candles = await cached_get_klines(
                db=self.db, market=Market.CRYPTO, symbol=symbol, interval=Interval.H1, limit=100,
            )
            if candles and len(candles) >= 30:
                closes = np.array([c.close for c in candles], dtype=np.float64)
                highs = np.array([c.high for c in candles], dtype=np.float64)
                lows = np.array([c.low for c in candles], dtype=np.float64)
                volumes = np.array([c.volume for c in candles], dtype=np.float64)
                last_close = float(closes[-1])
                ma5 = float(calc_ma(closes, 5)[-1])
                ma10 = float(calc_ma(closes, 10)[-1])
                ma20 = float(calc_ma(closes, 20)[-1])
                rsi = float(calc_rsi(closes, 14)[-1])
                macd_out = calc_macd(closes)
                hist = macd_out["histogram"]
                hist_last = float(hist[-1])
                hist_prev = float(hist[-2]) if len(hist) >= 2 else 0.0
                macd_state = ("金叉" if hist_last > 0 and hist_prev < 0 else
                              "死叉" if hist_last < 0 and hist_prev > 0 else
                              "多头强化" if hist_last > 0 and hist_last > hist_prev else
                              "多头减弱" if hist_last > 0 else
                              "空头强化" if hist_last < hist_prev else "空头减弱")
                vma20 = float(calc_volume_ma(volumes, 20)[-1])
                last_vol = float(volumes[-1])
                vol_ratio = last_vol / vma20 if vma20 > 0 else 0
                hi20 = float(highs[-20:].max()); lo20 = float(lows[-20:].min())
                ma_trend = ("多头排列" if ma5 > ma10 > ma20 else
                            "空头排列" if ma5 < ma10 < ma20 else "纠缠")
                tech_snapshot = (
                    f"- 当前价 {last_close:.4f}\n"
                    f"- 均线：MA5={ma5:.4f} / MA10={ma10:.4f} / MA20={ma20:.4f}（{ma_trend}）\n"
                    f"- RSI(14)={rsi:.1f} | MACD HIST={hist_last:.4f}（{macd_state}）\n"
                    f"- 量比={vol_ratio:.2f}x\n"
                    f"- 20 根 1H 区间：高 {hi20:.4f} / 低 {lo20:.4f}"
                )
        except Exception as e:
            logger.debug(f"[crypto-diag] {symbol} 技术快照异常: {e}")

        # 3) 最近 48h 相关新闻
        news_lines = []
        news_ids = []
        try:
            cutoff = int(time.time() * 1000) - 48 * 3600 * 1000
            # v12.11: 改 LIKE '%"symbol"%'（带 JSON 引号）防 symbol="F" 时匹到 FB/AAPL 等几乎所有新闻
            # categories 字段是 JSON 数组（如 ["AAPL","MSFT"]），用引号边界精确匹配
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT id, title, source, importance, sentiment
                       FROM flash_news WHERE published_at > ? AND categories LIKE ?
                       ORDER BY published_at DESC LIMIT 10""",
                    (cutoff, f'%"{symbol}"%'),
                )
                for r in await cur.fetchall():
                    rd = dict(r)
                    news_ids.append(rd["id"])
                    news_lines.append(
                        f"- ★{rd['importance']} [{rd['sentiment']}] {rd['title'][:80]}"
                    )
        except Exception:
            pass

        # 4) 组装 prompt
        ticker = insights.get("ticker", {})
        funding = (insights.get("funding_rate") or {}).get("current") or {}
        oi_hist = insights.get("oi_history", {})
        ls = (insights.get("long_short_ratio") or {}).get("current") or {}
        top = (insights.get("top_trader_ratio") or {}).get("current") or {}
        taker = (insights.get("taker_volume") or {}).get("current") or {}
        fng = insights.get("fear_greed", {}) or {}

        # 拉上一次诊断做对比
        prev_diag_txt = await self._load_previous_diagnosis(symbol, "crypto")

        prompt = CRYPTO_DIAGNOSIS_PROMPT.format(
            symbol=symbol,
            last_price=ticker.get("last", "-"),
            change_pct_24h=ticker.get("change_pct_24h", "-"),
            high_24h=ticker.get("high24h", "-"),
            low_24h=ticker.get("low24h", "-"),
            vol_24h=f"{ticker.get('vol_ccy_24h', 0):.0f}",
            funding_rate=f"{funding.get('rate_pct', 0):+.4f}" if funding else "-",
            funding_annual=f"{funding.get('annualized_pct', 0):+.1f}" if funding else "-",
            oi_change=f"{oi_hist.get('oi_change_24h_pct', 0):+.2f}" if oi_hist.get("oi_change_24h_pct") is not None else "-",
            ls_ratio=f"{ls.get('ratio', 0):.2f}" if ls else "-",
            top_trader_ratio=f"{top.get('ratio', 0):.2f}" if top else "-",
            buy_pct=f"{taker.get('buy_pct', 50):.1f}" if taker else "-",
            taker_signal=(insights.get("taker_volume") or {}).get("signal", "-"),
            fng_value=fng.get("value", "-"),
            fng_label=fng.get("label_cn", "-"),
            tech_snapshot=tech_snapshot,
            news_count=len(news_lines),
            news_summary="\n".join(news_lines) or "（近 48h 无相关新闻）",
            previous_diagnosis=prev_diag_txt,
        )
        # v12.5 Phase A: 注入复盘教训
        try:
            from backend.trading.reviewer import get_top_lessons_for_prompt
            lb = await get_top_lessons_for_prompt(self.db, "crypto", top_n=5)
            if lb: prompt += "\n" + lb
        except Exception:
            pass

        result = await self._call_llm(prompt, max_tokens=1000, path="crypto_diag", force=force)
        if not result or not isinstance(result, dict):
            return None

        now = int(time.time())
        diag_json = json.dumps(result, ensure_ascii=False)
        try:
            async with self.db.acquire() as conn:
                # 保存最新诊断（覆盖）
                await conn.execute(
                    """INSERT OR REPLACE INTO crypto_diagnosis
                       (symbol, diagnosis, rating, confidence, price, diagnosed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (symbol, diag_json,
                     str(result.get("rating", ""))[:32],
                     int(result.get("confidence") or 0),
                     float(ticker.get("last") or 0),
                     now),
                )
                # 追加历史（复用 ai_diagnosis_history 表，market='crypto'）
                await conn.execute(
                    """INSERT INTO ai_diagnosis_history
                       (symbol, market, diagnosis, rating, confidence, diagnosed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (symbol, "crypto", diag_json,
                     str(result.get("rating", ""))[:32],
                     int(result.get("confidence") or 0), now),
                )
                await conn.commit()
        except Exception as e:
            logger.debug(f"[crypto-diag] {symbol} 入库失败: {e}")
        logger.info(f"🪙 加密诊断完成 {symbol} → {result.get('rating')} (conf {result.get('confidence')})")
        # 自动交易 hook: rating=sell/reduce/hold（从 buy/strong_buy 降级）→ 减仓/清仓
        try:
            from backend.main import auto_trader
            if auto_trader and auto_trader.enabled:
                self._spawn(auto_trader.on_diagnosis_updated(symbol, "crypto", result.get("rating", "")))
        except Exception:
            pass
        return result

    async def diagnose_stock(
        self,
        symbol: str,
        market: str,
        pool_item: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        对候选池中一只股票做全面诊断，融合：
          - watch_pool 元数据（评分、入池来源/理由）
          - symbol_fundamentals 基本面（市值/PE/流动性等）
          - 60 根日 K 计算的技术面快照
          - 最近 7 天涉及该 symbol 的新闻
        返回结构化 JSON dict（含 rating / confidence / strengths / risks / next_action 等）。
        force=False 时若日预算已超支会跳过；force=True (用户主动触发) 即便超支也调用。
        """
        from backend.data.models import Interval, Market
        from backend.indicators.builtin import calc_ma, calc_rsi, calc_macd, calc_volume_ma
        from backend.data.cache import cached_get_klines
        from backend.watchpool.quality_filter import _load_any
        import numpy as np

        if not self._ensure_client():
            return None
        # 后台批量诊断走 hard_stop=True；用户手动触发 force=True 不阻断
        if not await self._can_call(hard_stop=not force):
            return None

        # ─ 10 分钟防抖：除非 force，10 分钟内已诊断过的同股直接复用旧结果
        # 避免同一批次新闻命中同一 symbol 多次重复诊断（典型场景：宏观新闻提及 5 个股）
        # v12.11: force=True 也加 2min 防抖（避免一天 10 条新闻 = 10 次完整诊断 ≈ $0.5/股）
        debounce_sec = 120 if force else 600
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT ai_diagnosis, ai_diagnosed_at FROM watch_pool "
                    "WHERE symbol=? AND market=? AND status != 'archived' LIMIT 1",
                    (symbol, market),
                )
                row = await cur.fetchone()
            if row and row["ai_diagnosed_at"]:
                age = int(time.time()) - int(row["ai_diagnosed_at"])
                if age < debounce_sec and row["ai_diagnosis"]:
                    try:
                        cached = json.loads(row["ai_diagnosis"])
                        logger.debug(f"[diag-debounce] {symbol}/{market} 复用 {age}s 前的诊断 (force={force}, threshold={debounce_sec}s)")
                        return cached
                    except Exception:
                        pass
        except Exception:
            pass

        # 1) 拉 watch_pool 元信息（如果未传入）
        if pool_item is None:
            try:
                async with self.db.acquire() as conn:
                    cur = await conn.execute(
                        "SELECT * FROM watch_pool WHERE symbol=? AND market=? LIMIT 1",
                        (symbol, market),
                    )
                    row = await cur.fetchone()
                    pool_item = dict(row) if row else {}
            except Exception:
                pool_item = {}

        # 2) 基本面文本
        fund_row = await _load_any(self.db, symbol, market, max_stale_days=30)
        if fund_row:
            mc = fund_row.get("market_cap") or 0
            pe = fund_row.get("pe") or 0
            pb = fund_row.get("pb") or 0
            avg_to = fund_row.get("avg_turnover") or 0
            avg_vol = fund_row.get("avg_volume") or 0
            fundamentals_text = (
                f"- 名称：{fund_row.get('name','-')} | 价格：{fund_row.get('price', 0):.2f}\n"
                f"- 市值：{mc/1e8:.1f} 亿 | PE：{pe:.2f} | PB：{pb:.2f}\n"
                f"- 20日均成交额：{avg_to/1e4:.0f} 万 | 均量：{avg_vol/1e4:.0f} 万股\n"
                f"- 上市天数：{fund_row.get('listed_days', 0)} | ST：{fund_row.get('is_st', 0)} | GEM：{fund_row.get('is_gem', 0)}"
            )
        else:
            fundamentals_text = "（无可用基本面数据）"

        # 3) 技术快照（沿用 monitor 的 _build_tech_snapshot 风格但精简）
        tech_snapshot = "（K线数据不足，无法计算技术面）"
        last_close = None
        try:
            try:
                m = Market(market)
            except ValueError:
                m = None
            if m is not None:
                candles = await cached_get_klines(
                    db=self.db, market=m, symbol=symbol, interval=Interval.D1, limit=60,
                )
                if candles and len(candles) >= 30:
                    closes = np.array([c.close for c in candles], dtype=np.float64)
                    highs = np.array([c.high for c in candles], dtype=np.float64)
                    lows = np.array([c.low for c in candles], dtype=np.float64)
                    volumes = np.array([c.volume for c in candles], dtype=np.float64)
                    last_close = float(closes[-1])
                    prev_close = float(closes[-2])
                    pct_change = (last_close - prev_close) / prev_close * 100 if prev_close else 0
                    ma5 = float(calc_ma(closes, 5)[-1])
                    ma10 = float(calc_ma(closes, 10)[-1])
                    ma20 = float(calc_ma(closes, 20)[-1])
                    ma50 = float(calc_ma(closes, 50)[-1]) if len(closes) >= 50 else float('nan')
                    rsi = float(calc_rsi(closes, 14)[-1])
                    macd_out = calc_macd(closes)
                    hist = macd_out["histogram"]
                    hist_last = float(hist[-1])
                    hist_prev = float(hist[-2]) if len(hist) >= 2 else 0.0
                    macd_state = ("金叉" if hist_last > 0 and hist_prev < 0 else
                                  "死叉" if hist_last < 0 and hist_prev > 0 else
                                  "多头强化" if hist_last > 0 and hist_last > hist_prev else
                                  "多头减弱" if hist_last > 0 else
                                  "空头强化" if hist_last < hist_prev else "空头减弱")
                    vma20 = float(calc_volume_ma(volumes, 20)[-1])
                    last_vol = float(volumes[-1])
                    vol_ratio = last_vol / vma20 if vma20 > 0 else 0
                    hi20 = float(highs[-20:].max()); lo20 = float(lows[-20:].min())
                    dist_hi = (hi20 - last_close) / last_close * 100 if last_close else 0
                    dist_lo = (last_close - lo20) / last_close * 100 if last_close else 0
                    ma_trend = ("多头排列" if ma5 > ma10 > ma20 else
                                "空头排列" if ma5 < ma10 < ma20 else "纠缠")
                    tech_snapshot = (
                        f"- 当前价 {last_close:.4f}（相对前根 {pct_change:+.2f}%）\n"
                        f"- 均线：MA5={ma5:.4f} / MA10={ma10:.4f} / MA20={ma20:.4f} / MA50={ma50:.4f}（{ma_trend}）\n"
                        f"- RSI(14)={rsi:.1f} | MACD HIST={hist_last:.4f}（{macd_state}）\n"
                        f"- 量比={vol_ratio:.2f}x（vs 20日均量）\n"
                        f"- 20日区间：高 {hi20:.4f} / 低 {lo20:.4f} | 距高 {dist_hi:.2f}% 距低 {dist_lo:.2f}%"
                    )
        except Exception as e:
            logger.debug(f"[diagnose] {symbol} tech snapshot 异常: {e}")

        # 4) 最近 7 天相关新闻
        recent_news_lines = []
        try:
            week_ago = int(time.time() * 1000) - 7 * 24 * 3600 * 1000
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT title, source, importance, sentiment, published_at
                       FROM flash_news
                       WHERE published_at > ? AND categories LIKE ?
                       ORDER BY published_at DESC LIMIT 10""",
                    (week_ago, f'%"{symbol}"%'),  # v12.11: 带引号边界精确匹配
                )
                rows = await cur.fetchall()
                for n in rows:
                    recent_news_lines.append(
                        f"- ★{n['importance']} [{n['sentiment']}] {n['title'][:80]}"
                    )
        except Exception:
            pass
        news_summary = "\n".join(recent_news_lines) or "（最近 7 天无相关新闻）"

        # 5) 市场标签
        market_label = {"cn": "A 股", "us": "美股", "hk": "港股"}.get(market, market.upper())

        # 6) 上一次诊断（用于 AI 说明变化）
        prev_diag_txt = await self._load_previous_diagnosis(symbol, market)

        prompt = STOCK_DIAGNOSIS_PROMPT.format(
            symbol=symbol,
            market_label=market_label,
            name=(fund_row or {}).get("name", "-"),
            price=last_close if last_close is not None else (fund_row or {}).get("price", "-"),
            source=pool_item.get("source", "-"),
            reason=(pool_item.get("reason") or "")[:200] or "-",
            total_score=pool_item.get("score", 0) or 0,
            event_score=pool_item.get("event_score", 0) or 0,
            technical_score=pool_item.get("technical_score", 0) or 0,
            fundamentals_score=pool_item.get("fundamentals_score", 0) or 0,
            fundamentals_text=fundamentals_text,
            tech_snapshot=tech_snapshot,
            previous_diagnosis=prev_diag_txt,
            news_count=len(recent_news_lines),
            news_summary=news_summary,
        )
        # v12.5 Phase A: 注入复盘教训
        try:
            from backend.trading.reviewer import get_top_lessons_for_prompt
            mkt = pool_item.get("market") or "us"
            lb = await get_top_lessons_for_prompt(self.db, mkt, top_n=5)
            if lb: prompt += "\n" + lb
        except Exception:
            pass
        result = await self._call_llm(prompt, max_tokens=900, path="diagnose", force=force)
        if not result or not isinstance(result, dict):
            return None
        # 入库（覆盖 watch_pool 最新诊断 + 追加 ai_diagnosis_history）
        diag_json = json.dumps(result, ensure_ascii=False)
        now = int(time.time())
        try:
            async with self.db.acquire() as conn:
                await conn.execute(
                    "UPDATE watch_pool SET ai_diagnosis=?, ai_diagnosed_at=? WHERE symbol=? AND market=?",
                    (diag_json, now, symbol, market),
                )
                await conn.execute(
                    """INSERT INTO ai_diagnosis_history
                       (symbol, market, diagnosis, rating, confidence, diagnosed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (symbol, market, diag_json,
                     str(result.get("rating", ""))[:32],
                     int(result.get("confidence") or 0), now),
                )
                await conn.commit()
        except Exception as e:
            logger.debug(f"[diagnose] {symbol} 入库失败: {e}")
        logger.info(f"🩺 AI 诊断完成 {symbol}/{market} → {result.get('rating')} (conf {result.get('confidence')})")
        # 自动交易 hook
        try:
            from backend.main import auto_trader
            if auto_trader and auto_trader.enabled:
                self._spawn(auto_trader.on_diagnosis_updated(symbol, market, result.get("rating", "")))
        except Exception:
            pass
        return result

    async def suggest_position_targets(self, position: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        v11.2：为已有持仓主动调用 AI 给出止盈/止损价位。
        用途：补全手动添加 / 旧持仓 / AI 没给 SL/TP 的持仓。
        返回 {"ai_stop_loss": float|None, "ai_take_profit": float|None, "reason": str}
        """
        if not position:
            return None
        symbol = position["symbol"]
        market = position.get("market", "")
        side = (position.get("side") or "long").lower()
        avg = float(position.get("avg_cost") or 0)
        if avg <= 0 or market not in ("cn", "hk", "us", "crypto"):
            return None

        # 当前价 + 简单技术面（20 日高低 / RSI 通过近 K 线计算太重，prompt 直接给数字让 LLM 自由判断）
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    f"SELECT close, high, low FROM [klines_{market}_1d] "
                    f"WHERE symbol=? ORDER BY timestamp DESC LIMIT 20",
                    (symbol,),
                )
                rows = await cur.fetchall()
        except Exception:
            rows = []
        if not rows:
            return None
        cur_price = float(rows[0]["close"])
        hi20 = max(float(r["high"]) for r in rows)
        lo20 = min(float(r["low"]) for r in rows)
        pnl_pct = (cur_price - avg) / avg * 100 if side == "long" else (avg - cur_price) / avg * 100

        side_label = '多' if side == 'long' else '空'
        side_full = 'long=做多' if side == 'long' else 'short=做空'
        tp_guide = (
            '多头: 应在当前价上方,参考 20 日高或合理目标位(avg × 1.15 ~ 1.30 之间为常见)'
            if side == 'long'
            else '空头: 应在当前价下方,参考 20 日低或合理目标位(avg × 0.70 ~ 0.85 之间为常见)'
        )
        sl_guide = (
            '多头: 应在当前价下方,参考 20 日低或不低于 avg × 0.92(留 8% 容忍)'
            if side == 'long'
            else '空头: 应在当前价上方,参考 20 日高或不高于 avg × 1.08'
        )
        range_pct = ((hi20 - lo20) / cur_price * 100) if cur_price > 0 else 0
        prompt = f"""你是一位资深风险管理交易员。任务是为已有持仓给出客观的止盈价和止损价,基于技术位而非情绪。

【分析原则】
1. 用数据说话: 优先引用 20 日高低、支撑阻力位
2. 数据不充分时返回 null,不要编造数字 — 后端会按规则兜底
3. SL 必须与持仓方向匹配,且不能超出常见安全区(避免 LLM 幻觉给出反向止损)

【持仓信息】
品种: {symbol} ({market})
方向: {side}({side_full})
成本均价: {avg:.4f}
当前价: {cur_price:.4f}
浮动盈亏: {pnl_pct:+.2f}%
近 20 日区间: 高 {hi20:.4f} / 低 {lo20:.4f}
20 日波动幅度: {range_pct:.2f}% (低于 5% 时区间窄,SL/TP 可能无意义)

【止盈价 ai_take_profit】
- {tp_guide}
- 优先取数字;若 20 日区间 < 5% 或缺乏可参考阻力位,可返回 null

【止损价 ai_stop_loss】
- {sl_guide}
- 优先取数字;若 20 日区间 < 5% 或缺乏可参考支撑位,可返回 null

【输出严格 JSON】
{{
  "ai_take_profit": 数字 或 null,
  "ai_stop_loss": 数字 或 null,
  "reason": "100 字内说明依据,必须引用 20 日高低或具体百分比;若返回 null 需说明原因"
}}
"""
        result = await self._call_llm(prompt, max_tokens=300, path="position_advice")
        if not result:
            return None

        def _to_float(v):
            if v is None: return None
            try:
                f = float(v)
                if f != f or f == float('inf') or f == float('-inf'): return None
                return f
            except (TypeError, ValueError):
                return None

        sl = _to_float(result.get("ai_stop_loss"))
        tp = _to_float(result.get("ai_take_profit"))

        # 合理性校验：方向必须与 side 匹配，且不能离当前价太离谱（5x 以外当无效）
        if side == "long":
            if sl and (sl >= cur_price or sl <= 0 or sl < avg * 0.5):
                logger.info(f"[suggest-targets] {symbol} long SL={sl} 不合理，丢弃")
                sl = None
            if tp and (tp <= cur_price or tp > avg * 5):
                logger.info(f"[suggest-targets] {symbol} long TP={tp} 不合理，丢弃")
                tp = None
        else:
            if sl and (sl <= cur_price or sl > avg * 1.5):
                sl = None
            if tp and (tp >= cur_price or tp <= avg * 0.2):
                tp = None

        return {
            "ai_stop_loss": sl,
            "ai_take_profit": tp,
            "reason": str(result.get("reason", ""))[:300],
        }

    async def deep_position_advice(
        self,
        position: Dict[str, Any],
        recent_news: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """
        对持仓做 LLM 深度建议（PortfolioAdvisor 规则版的增强版）。
        若该品种在候选池且有诊断，自动注入 prompt 上下文。
        """
        if not position:
            return None
        news_summary = "\n".join(
            f"- ★{n.get('importance',1)} [{n.get('sentiment','neutral')}] {n.get('title','')[:80]}"
            for n in recent_news[:8]
        ) or "无相关新闻"

        cur_price = float(position.get("current_price", 0))
        avg = float(position.get("avg_cost", 0))
        pnl_pct = ((cur_price - avg) / avg * 100) if avg > 0 else 0

        # 候选池诊断上下文（避免持仓建议与候选池诊断矛盾）
        pool_diag_ctx = "（候选池无该品种或无诊断）"
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT ai_diagnosis, ai_diagnosed_at FROM watch_pool "
                    "WHERE symbol=? AND market=? AND status != 'archived' LIMIT 1",
                    (position["symbol"], position.get("market", "")),
                )
                row = await cur.fetchone()
            if row and row["ai_diagnosis"]:
                try:
                    diag = json.loads(row["ai_diagnosis"])
                except Exception:
                    diag = {}
                rating = diag.get("rating") or ""
                summary = (diag.get("summary") or "")[:80]
                na = (diag.get("next_action") or "")[:80]
                ts = row["ai_diagnosed_at"] or 0
                age_h = (time.time() - ts) / 3600 if ts else 0
                pool_diag_ctx = (
                    f"评级: {rating} (置信度 {diag.get('confidence', 0)})，"
                    f"{age_h:.0f} 小时前诊断。\n"
                    f"摘要: {summary}\n"
                    f"建议下一步: {na}"
                )
        except Exception as e:
            logger.debug(f"[position-advice] 查诊断失败: {e}")

        # 最近 24h 该品种的策略信号（帮助 LLM 对齐系统立场）
        recent_sigs = "（最近 24h 无信号）"
        try:
            cutoff = int(time.time() * 1000) - 24 * 3600 * 1000
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT action, strategy_name, confidence, ai_verdict, ai_confidence, generated_at
                       FROM signals WHERE symbol=? AND market=? AND generated_at > ?
                       ORDER BY generated_at DESC LIMIT 5""",
                    (position["symbol"], position.get("market", ""), cutoff),
                )
                rows = await cur.fetchall()
            if rows:
                lines = []
                for r in rows:
                    age_h = (time.time() * 1000 - r["generated_at"]) / 3600_000
                    v = r["ai_verdict"] or "-"
                    lines.append(
                        f"- {age_h:.1f}h 前 {r['action'].upper()} "
                        f"{r['strategy_name']} 系统 conf={r['confidence']} | AI {v} ({r['ai_confidence'] or 0})"
                    )
                recent_sigs = "\n".join(lines)
        except Exception as e:
            logger.debug(f"[pos-advice] recent signals 查失败: {e}")

        # v12.14 (B6 修复): 注入此持仓最近 5 条 advice，让 LLM 看到自己之前的判断
        advice_history_text = "（无历史建议）"
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT advice, reason, advised_at FROM position_advices
                       WHERE position_id=? ORDER BY advised_at DESC LIMIT 5""",
                    (position.get("id"),),
                )
                hist_rows = await cur.fetchall()
            if hist_rows:
                lines = []
                for r in hist_rows:
                    ts = (r["advised_at"] or 0)
                    age_min = (time.time() * 1000 - ts) / 60_000 if ts else 0
                    rs = (r["reason"] or "")[:60]
                    lines.append(f"- {age_min:.0f}min 前: {r['advice']} — {rs}")
                advice_history_text = "\n".join(lines)
        except Exception as e:
            logger.debug(f"[pos-advice] advice_history 查失败: {e}")

        prompt = POSITION_ADVICE_PROMPT.format(
            symbol=position["symbol"],
            market=position.get("market", ""),
            quantity=position.get("quantity", 0),
            avg_cost=avg,
            current_price=cur_price,
            pnl_pct=f"{pnl_pct:.2f}",
            news_count=len(recent_news),
            news_summary=news_summary,
            pool_diagnosis_context=pool_diag_ctx,
            recent_signals=recent_sigs,
            advice_history=advice_history_text,
        )
        return await self._call_llm(prompt, max_tokens=600, path="position_advice")

    # ───────── 持仓新闻批量建议 (30 分钟窗口) ─────────

    async def enqueue_position_news(
        self, symbol: str, market: str, news: Dict[str, Any]
    ):
        """
        把一条"涉及持仓"的新闻塞进批量缓冲。
        首条到达时启动后台任务，窗口到期或达到 N 条时统一调用 LLM 一次。
        """
        key = (symbol, market)
        async with self._pos_buffer_lock:
            bucket = self._pos_news_buffer.get(key)
            if not bucket:
                bucket = {"news": [], "first_ts": int(time.time() * 1000)}
                self._pos_news_buffer[key] = bucket
            # 去重（同 id 不塞两次）
            nid = news.get("id")
            if nid and any(n.get("id") == nid for n in bucket["news"]):
                return
            bucket["news"].append({
                "id": nid,
                "title": news.get("title", ""),
                "importance": news.get("importance", 1),
                "sentiment": news.get("sentiment", "neutral"),
                "source": news.get("source", ""),
                "published_at": news.get("published_at"),
            })
            # 达到上限立即 flush
            if len(bucket["news"]) >= self.POS_BATCH_MAX_NEWS:
                self._spawn(self._flush_position_bucket(key))
                return
        # 懒启动后台 flusher
        if self._pos_batch_task is None or self._pos_batch_task.done():
            self._pos_batch_task = asyncio.create_task(self._position_batch_loop())

    async def _position_batch_loop(self):
        """后台循环：每 60s 扫一次缓冲，达到窗口的 bucket 执行 flush。"""
        try:
            while True:
                await asyncio.sleep(60)
                now_ms = int(time.time() * 1000)
                due_keys = []
                async with self._pos_buffer_lock:
                    for k, v in self._pos_news_buffer.items():
                        if now_ms - v["first_ts"] >= self.POS_BATCH_WINDOW_SEC * 1000:
                            due_keys.append(k)
                for k in due_keys:
                    await self._flush_position_bucket(k)
                # 全部清空则退出循环（下次 enqueue 再启动）
                async with self._pos_buffer_lock:
                    if not self._pos_news_buffer:
                        return
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception(f"position_batch_loop 异常: {e}")

    async def _flush_position_bucket(self, key: tuple):
        """取出该持仓缓冲的所有新闻，调 LLM 持仓建议、落库、WS 推送。"""
        async with self._pos_buffer_lock:
            bucket = self._pos_news_buffer.pop(key, None)
        if not bucket or not bucket["news"]:
            return
        symbol, market = key

        # 查当前持仓（可能用户已平仓 → 跳过）
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    "SELECT * FROM positions WHERE symbol=? AND market=?",
                    (symbol, market),
                )
                row = await cur.fetchone()
            if not row:
                return
            position = dict(row)
        except Exception as e:
            logger.debug(f"查询持仓失败 {key}: {e}")
            return

        # 取当前价（从最近一条日线 close 读取）— 若拿不到真实价则跳过这次建议
        # 不再用 avg_cost 兜底（会导致 pnl=0%，给 LLM 错误信号）
        current_price = None
        if market not in ("cn", "hk", "us", "crypto"):
            return None  # 白名单防 SQL 注入
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    f"SELECT close FROM [klines_{market}_1d] WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                    (symbol,),
                )
                r = await cur.fetchone()
                if r:
                    current_price = float(r["close"])
        except Exception as e:
            logger.debug(f"[position-advice] 读取 {symbol} 最新价失败: {e}")
        if current_price is None or current_price <= 0:
            logger.info(f"[position-advice] {symbol} 无可用当前价，跳过持仓建议")
            return
        position["current_price"] = current_price

        advice = await self.deep_position_advice(position, bucket["news"])
        if not advice:
            return

        # LLM 输出校验：advice 必须是 hold/reduce/add/close 之一
        VALID_ADVICES = {"hold", "reduce", "add", "close"}
        VALID_URGENCY = {"low", "medium", "high"}
        adv_value = str(advice.get("advice", "")).lower().strip()
        if adv_value not in VALID_ADVICES:
            logger.warning(f"[ai-advice] LLM 返回非法 advice='{adv_value}' for {symbol}, 改用 hold")
            advice["advice"] = "hold"
        else:
            advice["advice"] = adv_value
        urg_value = str(advice.get("urgency", "")).lower().strip()
        if urg_value not in VALID_URGENCY:
            advice["urgency"] = "medium"
        else:
            advice["urgency"] = urg_value

        # 落库 position_advices
        try:
            triggered = {
                "news_count": len(bucket["news"]),
                "news_ids": [n.get("id") for n in bucket["news"] if n.get("id")],
                "window_sec": self.POS_BATCH_WINDOW_SEC,
            }
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO position_advices
                       (position_id, symbol, advice, reason, triggered_by, advised_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        position["id"],
                        symbol,
                        advice.get("advice", "hold"),
                        advice.get("reason", ""),
                        json.dumps(triggered, ensure_ascii=False),
                        int(time.time() * 1000),
                    ),
                )
                await conn.commit()
        except Exception as e:
            logger.debug(f"position_advices 落库失败: {e}")

        # WS 推送
        if self.ws_hub is not None:
            try:
                broadcast = getattr(self.ws_hub, "broadcast_position_advice", None) \
                    or getattr(self.ws_hub, "broadcast_alert", None)
                if broadcast:
                    await broadcast({
                        "type": "position_advice",
                        "data": {
                            "position_id": position["id"],
                            "symbol": symbol,
                            "market": market,
                            "advice": advice.get("advice"),
                            "urgency": advice.get("urgency", "medium"),
                            "reason": advice.get("reason", ""),
                            "key_factors": advice.get("key_factors", []),
                            "suggested_action": advice.get("suggested_action", {}),
                            "news_count": len(bucket["news"]),
                        },
                    })
            except Exception as e:
                logger.debug(f"position_advice WS 推送失败: {e}")

        logger.info(
            f"🤖→📊 持仓建议 {symbol}/{market}: {advice.get('advice')} "
            f"({advice.get('urgency','')}, 基于 {len(bucket['news'])} 条新闻)"
        )

    async def generate_advice_for_position(self, position_id: str, force: bool = False) -> Optional[Dict[str, Any]]:
        """
        主动为指定持仓生成 AI 建议（不依赖新闻缓冲）。
        用于：定期巡检循环 + 用户手动触发"刷新建议"按钮。
        force=True 时即使预算超支也调用（用户主动）。
        """
        # 拉持仓
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute("SELECT * FROM positions WHERE id=?", (position_id,))
                row = await cur.fetchone()
            if not row:
                return None
            position = dict(row)
        except Exception:
            return None

        symbol = position["symbol"]
        market = position["market"]

        # 当前价
        current_price = None
        if market not in ("cn", "hk", "us", "crypto"):
            return None  # 白名单防 SQL 注入
        try:
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    f"SELECT close FROM [klines_{market}_1d] WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                    (symbol,),
                )
                r = await cur.fetchone()
                if r:
                    current_price = float(r["close"])
        except Exception:
            pass
        if current_price is None or current_price <= 0:
            logger.info(f"[advice-on-demand] {symbol} 无可用现价，跳过")
            return None
        position["current_price"] = current_price

        # 拉近 24h 相关新闻（即使没有也调，让 LLM 基于价格 + 仓位状态判断）
        recent_news = []
        try:
            cutoff_ms = int(time.time() * 1000) - 24 * 3600 * 1000
            async with self.db.acquire() as conn:
                cur = await conn.execute(
                    """SELECT id, title, source, importance, sentiment FROM flash_news
                       WHERE published_at > ? AND categories LIKE ?
                       ORDER BY importance DESC, published_at DESC LIMIT 8""",
                    (cutoff_ms, f'%"{symbol}"%'),  # v12.11: 精确匹配
                )
                for r in await cur.fetchall():
                    recent_news.append(dict(r))
        except Exception:
            pass

        # 检查预算（force 时跳过）
        if not force and not await self._can_call(hard_stop=True):
            return None

        advice = await self.deep_position_advice(position, recent_news)
        if not advice:
            return None

        # 校验
        VALID_ADVICES = {"hold", "reduce", "add", "close"}
        VALID_URGENCY = {"low", "medium", "high"}
        adv_value = str(advice.get("advice", "")).lower().strip()
        advice["advice"] = adv_value if adv_value in VALID_ADVICES else "hold"
        urg_value = str(advice.get("urgency", "")).lower().strip()
        advice["urgency"] = urg_value if urg_value in VALID_URGENCY else "medium"

        # 落库
        try:
            triggered = {
                "trigger": "manual" if force else "scheduled",
                "news_count": len(recent_news),
                "news_ids": [n.get("id") for n in recent_news if n.get("id")],
            }
            async with self.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO position_advices
                       (position_id, symbol, advice, reason, triggered_by, advised_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (position["id"], symbol, advice.get("advice", "hold"),
                     advice.get("reason", ""),
                     json.dumps(triggered, ensure_ascii=False),
                     int(time.time() * 1000)),
                )
                await conn.commit()
        except Exception as e:
            logger.debug(f"[advice-on-demand] 落库失败: {e}")

        # WS 推送
        if self.ws_hub is not None:
            try:
                broadcast = getattr(self.ws_hub, "broadcast_position_advice", None)
                if broadcast:
                    await broadcast({
                        "position_id": position["id"], "symbol": symbol, "market": market,
                        "advice": advice.get("advice"), "urgency": advice.get("urgency", "medium"),
                        "reason": advice.get("reason", ""),
                        "key_factors": advice.get("key_factors", []),
                        "suggested_action": advice.get("suggested_action", {}),
                        "news_count": len(recent_news),
                    })
            except Exception:
                pass

        logger.info(f"🤖→📊 主动持仓建议 {symbol}/{market}: {advice.get('advice')} ({'manual' if force else 'scheduled'})")
        return advice

    # ───────── 成本查询 ─────────

    async def get_cost_status(self) -> Dict[str, Any]:
        """前端"今日成本"查询用。"""
        await self._refresh_today_cost()
        budget = config.LLM_DAILY_BUDGET
        # v12.10: model 字段返回路由后的实际策略，而非 init 时的 _model
        # _model 是用户在 .env 配的（可能是旧的 deepseek-reasoner）；实际路由按 path 走
        user_override = (self._model or "").strip()
        is_legacy = user_override in ("", "deepseek-chat", "deepseek-reasoner")
        if is_legacy:
            routing_desc = "AUTO (v4-flash / v4-pro per path)"
        else:
            routing_desc = f"OVERRIDE: {user_override}"
        return {
            "today_cost_usd": round(self._today_cost, 4),
            "daily_budget": budget,
            "budget_remaining_pct": max(0, round((1 - self._today_cost / budget) * 100, 1)),
            "provider": self._provider,
            "model": routing_desc,
            "configured_model": self._model,  # 用户实际配置（可能是旧名）
            "status": "ok" if self._today_cost < budget * 0.8
            else ("warning" if self._today_cost < budget else "exceeded"),
        }
