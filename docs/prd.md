# 产品需求文档

> 从代码反推的产品需求，记录产品功能、目标用户、核心流程。

## 产品概述

OpenChart Pro 是一款本地部署的多市场金融图表分析平台，支持加密货币（OKX/Binance）、美股、港股、A股，集成缠论分析、艾略特波浪、斐波那契回撤、AI 智能研判、策略回测、公式编辑、条件警报等功能。`python run.py` 一键启动，浏览器访问即用。

## 目标用户

| 画像 | 特征 |
|------|------|
| 中文交易者/投资者 | 界面全中文，支持红涨绿跌/绿涨红跌切换，A股港股原生支持 |
| 缠论实践者 | 内置缠论引擎（笔/线段/中枢/买卖点），多级别联立研判 |
| 技术派量化爱好者 | 类 Pine Script 公式编辑器、回测引擎、参数优化 |
| 加密货币交易者（优先级最高） | 默认 BTC-USDT、OKX 为首选交易所 |

## 核心功能

### 功能 1：多市场K线图表
- **用途**：展示 OHLCV K线，支持 9 种周期
- **使用场景**：选择市场 → 搜索品种 → 查看实时K线，支持向左懒加载历史数据
- **相关代码**：`backend/main.py` (market_router), `frontend/js/chart.js`, `backend/data/`

### 功能 2：缠论分析引擎
- **用途**：自动识别笔/线段/中枢/买卖点，叠加到K线主图
- **使用场景**：指标面板勾选"缠论分析"
- **相关代码**：`backend/chanlun_engine/chanlun_service.py`, `frontend/js/chart.js`

### 功能 3：缠论多级别综合研判
- **用途**：并行分析三个周期（1H/4H/1D），综合给出操作建议
- **相关代码**：`frontend/js/chanlun_verdict.js`, `backend/main.py` (chanlun_router)

### 功能 4：艾略特波浪分析
- **用途**：自动识别推动浪/调整浪，预测目标价位
- **相关代码**：`backend/elliott_wave/`, `frontend/js/chart.js`

### 功能 5：斐波那契分析
- **用途**：基于 ZigZag 自动计算回撤和扩展水平线
- **相关代码**：`backend/main.py` (fibonacci_router)

### 功能 6：技术指标系统
- **用途**：20+ 内置指标（MA/EMA/BOLL/MACD/RSI/KDJ 等）
- **相关代码**：`frontend/js/indicators.js`, `backend/indicators/`

### 功能 7：公式编辑器（OpenScript）
- **用途**：用户编写自定义指标/策略脚本
- **相关代码**：`frontend/js/formula.js`, `backend/indicators/formula/`

### 功能 8：条件警报
- **用途**：设置价格条件，触发浏览器通知/声音/Webhook
- **相关代码**：`frontend/js/alerts.js`, `backend/alerts/`

### 功能 9：策略回测
- **用途**：基于历史K线回测交易策略，生成报告
- **相关代码**：`frontend/js/backtest.js`, `backend/backtest/`

### 功能 10：AI 智能选股
- **用途**：AI 根据新闻/政策初筛 + 技术信号扫描
- **相关代码**：`frontend/js/screener.js`, `backend/screener/`

### 功能 11：AI 综合研判
- **用途**：对当前品种综合分析，通过 LLM 给出开仓建议
- **相关代码**：`frontend/js/aijudge.js`, `backend/main.py` (aijudge_router)

### 功能 12：加密货币仪表盘
- **用途**：恐惧贪婪指数、资金费率、链上数据、经济日历
- **相关代码**：`frontend/js/dashboard.js`, `backend/crypto_dashboard/`

### 功能 13：自选列表
- **用途**：管理关注品种，显示实时价格和涨跌幅
- **相关代码**：`frontend/js/watchlist.js`

### 功能 14：实盘交易 [预留模块]
- **用途**：OKX 交易所接入，含风控引擎
- **相关代码**：`backend/trading/`
- **[待确认]**：前端尚未暴露入口

### 功能 15：WebSocket 实时推送
- **用途**：K线实时更新、警报通知、回测进度
- **相关代码**：`frontend/js/websocket.js`, `backend/ws/hub.py`

## 数据模型

### SQLite 数据库表

| 表名 | 用途 |
|------|------|
| watchlist | 自选品种列表 |
| alerts | 条件警报配置 |
| alert_history | 警报触发记录 |
| backtest_reports | 回测报告 |
| formulas | 用户自定义公式 |
| news_cache | 新闻缓存 |
| screener_tasks | 选股异步任务 |
| settings | 全局配置 KV 表 |

### 配置优先级
SQLite settings 表 > config.py 默认值

## 已知的设计决定

1. **单体架构**：所有路由在一个 main.py（2767行），选择了"简单部署"
2. **OKX 为首选交易所**：默认配置、WebSocket 地址、限流逻辑都针对 OKX
3. **LLM 选型**：DeepSeek 优先，Qwen 备选，前端还支持 OpenAI/Anthropic/Ollama
4. **前端无框架**：纯原生 JS，模块间通过 window 全局变量通信
5. **缠论引擎**：直接引用 chan.py 源码，放在 backend/chanlun_engine/
6. **回测双实现**：优先 VectorBT，失败回退纯 numpy
7. **交易模块已编码但未接入前端**：风控引擎注释写"预留空壳"
