# 归档模块说明

> 归档日期：2026-04-15  
> 归档原因：OpenChart Pro 重构为 v3.0 AI 辅助交易决策平台，以下模块不在新 PRD 范围内但保留备用

## 归档内容

### backend 模块

| 模块 | 原路径 | 行数 | 完整度 | 未来计划 |
|------|--------|------|-------|---------|
| `chanlun_engine/` | `backend/chanlun_engine/` | ~8385 | 10 项功能全部实现，生产就绪度 7/10 | **开发完成后接入 signals 作为"缠论买卖点"策略** |
| `elliott_wave/` | `backend/elliott_wave/` | ~4675 | 11 项功能基本全实现，生产就绪度 8/10 | **开发完成后接入 signals 作为"艾略特推动浪"策略** |
| `indicators_formula/` | `backend/indicators/formula/` | ~4048 | OpenScript 解析器 + Python 沙箱完整 | 作为独立项目"技术策略指标开发 + 回测"单独发布 |
| `screener/` | `backend/screener/` | ~1404 | 选股引擎 + AI 分析 + 新闻采集 | **news.py 的采集逻辑会被提取复用到新 `news/` 模块** |

### frontend 模块

| 模块 | 原路径 | 对应后端 |
|------|--------|---------|
| `aijudge.js` | `frontend/js/aijudge.js` | 对应 screener 的 AI 判断面板 |
| `chanlun_verdict.js` | `frontend/js/chanlun_verdict.js` | 对应 chanlun_engine 的前端展示 |
| `formula.js` | `frontend/js/formula.js` | 对应 indicators/formula 的公式编辑器 |
| `screener.js` | `frontend/js/screener.js` | 对应 screener 的选股面板 |

### 其他

| 文件 | 说明 |
|------|------|
| `chanlun_comparison.png` | 缠论对比验证图 |

## 恢复指南

如果未来需要将某个模块恢复使用：

```bash
# 示例：恢复缠论引擎
git mv archive/chanlun_engine backend/chanlun_engine
git mv archive/frontend_js/chanlun_verdict.js frontend/js/chanlun_verdict.js
```

## 计划中的重新接入（Phase 4 完成之后）

### 缠论引擎
- 在 `backend/signals/strategies.py` 新增 `ChanlunBSPStrategy`
- 包装 `chanlun_service.py` 的 API，把 1B/2B/3B 买卖点转为 `Signal` 对象
- 置信度基础分：1B=70, 2B=65, 3B=60（缠论规则最严格的 1 类买卖点置信度最高）

### 艾略特波浪
- 在 `backend/signals/strategies.py` 新增 `ElliottImpulseStrategy`
- 包装 `service.py` 的 `analyze()` 接口，推动浪 3/5 完成时触发信号
- 置信度对接波浪置信度：0.85+ → VERY_HIGH=90, 0.65+ → HIGH=75, 0.5+ → MEDIUM=60
