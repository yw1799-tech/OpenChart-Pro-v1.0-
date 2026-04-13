# 技术方案文档

## 整体架构

```
浏览器 (前端)
  │
  ├── HTTP REST ──→ FastAPI (backend/main.py)
  │                    ├── 数据源适配器 (backend/data/)
  │                    │     ├── OKX API
  │                    │     ├── Binance API
  │                    │     ├── Yahoo Finance
  │                    │     └── 东方财富
  │                    ├── 缠论引擎 (backend/chanlun_engine/)
  │                    ├── 艾略特波浪 (backend/elliott_wave/)
  │                    ├── 回测引擎 (backend/backtest/)
  │                    ├── AI 分析 (backend/screener/)
  │                    └── SQLite (data/openchart.db)
  │
  └── WebSocket ──→ WS Hub (backend/ws/hub.py)
                       └── 交易所 WebSocket (实时K线/Ticker)
```

单体架构，一个 Python 进程包含所有功能。前端纯静态文件，由 FastAPI 的 StaticFiles 中间件直接托管。

## 技术选型

| 组件 | 选型 | 原因 |
|------|------|------|
| Web 框架 | FastAPI | 异步、自动文档、类型校验 |
| ASGI 服务器 | Uvicorn | FastAPI 标配 |
| 数据库 | SQLite + aiosqlite | 零配置、单文件、异步支持 |
| 前端图表 | KLineCharts v9 | 专业K线库，支持自定义指标覆盖层 |
| 缠论引擎 | chan.py（第三方源码） | 成熟的缠论实现，直接引入 |
| 回测 | vectorbt / numpy | vectorbt 功能强大，numpy 作为回退方案 |
| AI/LLM | OpenAI SDK（兼容接口） | 统一调用 DeepSeek/Qwen/OpenAI |
| 公式沙箱 | RestrictedPython | 安全执行用户自定义脚本 |

## 主要模块

### 模块 1：数据源适配器
- **职责**：统一不同交易所/数据源的K线获取接口
- **位置**：`backend/data/`
- **对外接口**：`get_fetcher(market)` 返回统一的 Fetcher 实例，提供 `get_klines()` 和 `get_symbols()` 方法
- **依赖**：aiohttp, httpx, yfinance

### 模块 2：缠论分析
- **职责**：接收K线数据，返回笔/线段/中枢/买卖点坐标
- **位置**：`backend/chanlun_engine/`
- **对外接口**：`chanlun_service.analyze(candles)` → dict
- **依赖**：chan.py 引擎（整个源码在目录内）
- **注意**：只修改 `chanlun_service.py`，不动其他文件

### 模块 3：艾略特波浪
- **职责**：识别推动浪/调整浪结构，生成预测目标
- **位置**：`backend/elliott_wave/`
- **对外接口**：`service.analyze(candles, bar_offset)` → dict
- **依赖**：numpy

### 模块 4：回测引擎
- **职责**：基于历史K线回测交易策略
- **位置**：`backend/backtest/`
- **对外接口**：`engine.run_backtest(candles, strategy_code, params)` → BacktestResult
- **依赖**：vectorbt（可选）, numpy, pandas

### 模块 5：WebSocket Hub
- **职责**：管理客户端连接，转发实时数据
- **位置**：`backend/ws/hub.py`
- **对外接口**：`/ws` 端点，客户端通过 JSON 消息订阅/取消订阅

### 模块 6：前端图表核心
- **职责**：K线渲染、指标叠加、缠论/艾略特波浪绘制
- **位置**：`frontend/js/chart.js`
- **依赖**：KLineCharts v9（`frontend/lib/klinecharts.min.js`）

## 关键数据流

### K线加载流程
```
用户选品种/周期
  → frontend/js/chart.js loadKlines()
  → GET /api/klines?symbol=&interval=&limit=1000
  → backend/main.py → data/fetcher.py → OKX/Binance/Yahoo API
  → 返回 candles JSON
  → chart.applyNewData(klines)
  → 如果缠论启用 → loadChanlun()
```

### 缠论分析流程
```
loadChanlun()
  → POST /api/chanlun/from-data {candles: chart.getDataList()}
  → chanlun_service.analyze(candles)
    → _build_kline_units() 转换格式
    → CChan.trigger_load() 运行 chan.py 引擎
    → 提取 bi_list/seg_list/zs_list/bsp_list
    → _klu_to_bar_index() 时间戳→bar索引映射
  → 返回 JSON
  → window._chanlunData = data
  → CHANLUN 指标 draw() 用 canvas 渲染
```

### 实时更新流程
```
页面加载 → websocket.js 连接 /ws
  → 发送 subscribe {symbol, interval}
  → 后端 ws/hub.py 转发交易所 WebSocket 数据
  → 前端 chart.updateData() 更新最新K线
```

## 第三方服务和库

| 服务/库 | 用途 |
|---------|------|
| OKX REST/WS API | 加密货币K线、实时行情、交易 |
| Binance REST API | 加密货币K线（备选） |
| Yahoo Finance | 美股/港股K线 |
| 东方财富 API | A股K线 |
| DeepSeek API | AI 研判、智能选股 |
| 通义千问 API | AI 研判（备选） |
| KLineCharts | 前端K线图表渲染 |
| Chart.js | 回测报告图表 |
| chan.py | 缠论分析引擎 |
| vectorbt | 策略回测 |
| RestrictedPython | 公式沙箱执行 |

## 已知技术债

1. **main.py 2767 行**：所有路由堆在一个文件，应拆分为独立 router 模块
2. **零测试**：没有自动化测试，全靠手动验证
3. **前端全局状态**：`window.currentMarket/Symbol/Interval` 全局可变，模块间隐式耦合
4. **双数据库管理**：`db/database.py` 的 DatabaseManager 和 `main.py` 的 `get_db()` 并存，建表逻辑可能不一致
5. **chanlun_engine 整体引入**：包含 LICENSE、Demo、Image 等非必要文件，无法跟踪上游更新
6. **前端无构建工具**：JS 文件直接引入，无压缩、无模块打包
7. **市场代码不一致**：前端用 `a` 表示 A 股，后端用 `cn`，靠 `toApiMarket()` 转换

## 性能相关

- K线初始加载 1000 根，左滑懒加载 500 根/次，交易所 API 限流已处理
- 缠论分析是同步阻塞操作，1000 根 K 线约 200-500ms
- WebSocket 断线自动重连，指数退避（最大 30 秒）

## 安全相关

- API Key 存储在 SQLite settings 表，不硬编码在代码中
- 公式编辑器使用 RestrictedPython 沙箱，防止用户代码执行危险操作
- 无用户认证系统（本地部署，假设单用户）
- config.py 中的空字符串默认值存在风险：如果填入真值后忘记用 .env 覆盖会提交到 git
