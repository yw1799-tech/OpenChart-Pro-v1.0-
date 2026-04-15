"""
新闻管线模块（Phase 3A + 3B）。

子模块：
  - sources:        10 个验证新闻源配置
  - collector:      采集器基类 + RSS / REST / Scraper 三种适配器
  - dedup:          去重（URL hash + 内容 hash + SimHash, Phase 3B）
  - rule_engine:    L2 规则引擎（关键词权重 + 来源可信度 + 品种关联）
  - impact_analyzer: 宏观数据影响分析（CPI/FOMC/NFP, Phase 3B）
  - ai_analyzer:    L4 LLM 深度解读（Phase 3B）
  - scheduler:      采集调度器（APScheduler 集成）
"""
