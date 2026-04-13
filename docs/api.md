# API 文档

> 41 个 REST 端点 + 1 个 WebSocket，按功能模块分组。

基础地址：`http://localhost:8888`

---

## 行情数据 `/api`

### GET /api/markets
获取支持的市场列表
- **返回**：`["crypto", "us", "hk", "cn"]`

### GET /api/symbols
获取品种列表
- **参数**：`market` (必填), `q` (搜索关键词，可选)
- **返回**：`[{symbol, name, market, exchange}]`

### GET /api/klines
获取K线数据
- **参数**：`symbol` (必填), `interval` (默认 1H), `market` (默认 crypto), `limit` (默认 1000), `end_time` (可选，懒加载用)
- **返回**：`{symbol, market, interval, candles: [{timestamp, open, high, low, close, volume, turnover}]}`

---

## 技术指标 `/api/indicators`

### GET /api/indicators
获取可用指标列表
- **返回**：`[{id, name, description, params, pane}]`

### POST /api/indicators/calculate
计算指标值
- **Body**：`{indicator_id, candles, params}`
- **返回**：计算结果数组

---

## 公式编辑器 `/api/formula`

### POST /api/formula/validate
验证 OpenScript 公式语法
- **Body**：`{code}`
- **返回**：`{valid, errors}`

### POST /api/formula/execute
执行公式计算
- **Body**：`{code, candles, params}`
- **返回**：`{results, plots}`

---

## 条件警报 `/api/alerts`

### GET /api/alerts
获取所有警报配置

### POST /api/alerts
创建新警报
- **Body**：`{symbol, market, condition_type, condition, notify_methods, repeat_mode, cooldown}`

### PUT /api/alerts/{alert_id}
更新警报

### DELETE /api/alerts/{alert_id}
删除警报

### GET /api/alerts/history
获取警报触发历史
- **参数**：`limit` (默认 50)

---

## 策略回测 `/api/backtest`

### POST /api/backtest/run
运行回测
- **Body**：`{symbol, interval, market, strategy_code, initial_capital, commission, slippage, ...}`
- **返回**：`{backtest_id, summary, equity_curve, trades, monthly_returns}`

### GET /api/backtest/report/{backtest_id}
获取回测报告

### POST /api/backtest/optimize
参数优化
- **Body**：`{symbol, interval, market, strategy_code, param_ranges}`

---

## AI 选股 `/api/screener`

### POST /api/screener/filter
条件筛选
- **Body**：`{market, filters}`

### POST /api/screener/ai-recommend
AI 推荐品种
- **Body**：`{market}`

### GET /api/screener/ai-status/{task_id}
查询异步任务状态

### POST /api/screener/ai-analyze
AI 分析单个品种

### POST /api/screener/tech-signals
技术信号扫描
- **Body**：`{symbols, market}`

---

## AI 研判 `/api/aijudge`

### POST /api/aijudge/analyze
综合研判
- **Body**：`{symbol, interval, market}`
- **返回**：LLM 生成的分析报告

---

## 加密货币仪表盘 `/api/dashboard`

### GET /api/dashboard/fear-greed
恐惧贪婪指数

### GET /api/dashboard/funding-rate
资金费率

### GET /api/dashboard/open-interest
未平仓合约

### GET /api/dashboard/long-short-ratio
多空比

### GET /api/dashboard/exchange-flow
交易所资金流向

### GET /api/dashboard/whale-transactions
巨鲸交易

### GET /api/dashboard/calendar
经济日历

### GET /api/dashboard/onchain
链上数据（活跃地址/NUPL/矿工数据）

---

## 自选列表 `/api/watchlist`

### GET /api/watchlist
获取自选列表
- **参数**：`market`

### POST /api/watchlist
添加品种
- **Body**：`{symbol, market, note}`

### PUT /api/watchlist/reorder
拖拽排序
- **Body**：`{ids: [排序后的ID数组]}`

### PUT /api/watchlist/{item_id}
更新备注

### DELETE /api/watchlist/{item_id}
删除品种

---

## 全局设置 `/api/settings`

### GET /api/settings
获取所有设置（KV 格式）

### PUT /api/settings
批量更新设置
- **Body**：`{settings: {key: value, ...}}`

---

## 缠论分析 `/api/chanlun`

### GET /api/chanlun
获取K线并分析（服务端获取数据）
- **参数**：`symbol`, `interval`, `market`, `limit`
- **返回**：`{bi_list, seg_list, zs_list, bsp_list}`

### POST /api/chanlun/from-data
用前端K线数据分析（推荐，确保数据一致）
- **Body**：`{candles: [{timestamp, open, high, low, close, volume}]}`
- **返回**：`{bi_list, seg_list, zs_list, bsp_list}`

### POST /api/chanlun/elliott-wave/from-data
艾略特波浪分析
- **Body**：`{candles, bar_offset}`
- **返回**：`{patterns, predictions}`

### GET /api/chanlun/verdict
多级别综合研判
- **参数**：`symbol`, `market`
- **返回**：三个周期的缠论分析 + 综合操作建议

---

## 斐波那契 `/api/fibonacci`

### POST /api/fibonacci/analyze
自动斐波那契分析
- **Body**：`{candles}`
- **返回**：`{retracements, extensions}`

---

## WebSocket `/ws`

### 连接
```javascript
ws = new WebSocket('ws://localhost:8888/ws')
```

### 订阅K线
```json
{"action": "subscribe", "symbol": "BTC-USDT", "interval": "1H", "market": "crypto"}
```

### 接收数据
```json
{"type": "kline", "data": {timestamp, open, high, low, close, volume}}
{"type": "alert_triggered", "data": {alert_id, symbol, price}}
```
