# OpenChart Pro — 验收审计清单

> **用途**：开发完成后（或每个 Phase 完成后），对照此文档逐条验收。
> **使用方式**：直接发给 VS Code + Claude Code，告诉它："按照验收审计清单，逐条运行检查，报告通过/失败。"
> 也可以你自己手动对着浏览器一条条过。

---

## 使用说明

每条检查项的格式：

```
- [ ] 编号. 检查内容
      验证方法：具体操作步骤
      预期结果：应该看到什么
      对应规格：文档章节号
```

通过打 `[x]`，失败留 `[ ]` 并记录实际结果。

---

## 一、Phase 1 验收 — 核心基建

### 1.1 服务启动

- [ ] **P1-01** 一键启动
      验证方法：`python run.py`
      预期结果：终端输出 uvicorn 启动信息，2秒后自动打开浏览器 `http://localhost:8888`
      对应规格：六、run.py

- [ ] **P1-02** 静态文件服务
      验证方法：浏览器访问 `http://localhost:8888`
      预期结果：显示 index.html 页面，暗色主题加载正常，无 404 错误
      对应规格：4.0.1

- [ ] **P1-03** API 路由优先级
      验证方法：访问 `http://localhost:8888/api/markets`
      预期结果：返回 JSON 数组而非 404（证明 `app.mount("/")` 在所有路由之后）
      对应规格：4.0.1 注释 "⚠️ 必须在所有 app.include_router() 之后"

- [ ] **P1-04** CORS 配置
      验证方法：浏览器控制台执行 `fetch('/api/markets').then(r=>r.json()).then(console.log)`
      预期结果：无 CORS 错误，正常返回数据
      对应规格：4.0.1

### 1.2 数据库初始化

- [ ] **P1-05** SQLite 数据库文件创建
      验证方法：启动后检查 `data/openchart.db` 是否存在
      预期结果：文件存在且大小 > 0
      对应规格：2.1 DB_PATH

- [ ] **P1-06** 必要表创建
      验证方法：`sqlite3 data/openchart.db ".tables"`
      预期结果：至少包含 `watchlist, alerts, alert_history, backtest_reports, formulas, news_cache, screener_tasks, config`
      对应规格：五、数据库设计

- [ ] **P1-07** K线分表动态创建
      验证方法：请求一次 `GET /api/klines?symbol=BTC-USDT&interval=1H` 后再查表
      预期结果：`klines_crypto_1H` 表被自动创建
      对应规格：五、"按需创建，不必一次全建"

### 1.3 配置系统

- [ ] **P1-08** config.py 默认值加载
      验证方法：`GET /api/settings`
      预期结果：返回所有配置字段，值与 config.py 默认值一致
      对应规格：2.1 + 3.9

- [ ] **P1-09** 配置写入和热更新
      验证方法：`PUT /api/settings` 修改 `candle_color_scheme` 为 `"chinese"`，然后 `GET /api/settings`
      预期结果：返回的 `candle_color_scheme` 为 `"chinese"`，无需重启服务
      对应规格：2.1 "PUT /api/settings → 写入 DB → 内存热更新"

- [ ] **P1-10** 配置优先级
      验证方法：直接在 SQLite 的 config 表插入一条 `key='timezone', value='"UTC"'`，重启服务后 `GET /api/settings`
      预期结果：timezone 返回 `"UTC"`（DB 优先于 config.py 的 "Asia/Shanghai"）
      对应规格：2.1 "优先级：SQLite DB > config.py 默认值"

### 1.4 基础 API

- [ ] **P1-11** 市场列表
      验证方法：`GET /api/markets`
      预期结果：返回 4 个市场，每个含 `id, name, default_symbol, currency`
      对应规格：3.1

- [ ] **P1-12** 品种搜索
      验证方法：`GET /api/symbols?market=crypto&q=BTC`
      预期结果：返回含 BTC-USDT 的品种数组，每项含 `symbol, name, market`
      对应规格：3.1

- [ ] **P1-13** K线数据
      验证方法：`GET /api/klines?symbol=BTC-USDT&interval=1H&limit=500`
      预期结果：返回 JSON 含 `symbol, interval, candles` 数组，candles 每项含 `timestamp, open, high, low, close, volume`
      对应规格：3.1

### 1.5 OKX 数据源

- [ ] **P1-14** OKX REST 历史K线
      验证方法：请求 500 根 1H K线
      预期结果：返回 500 根（内部通过 after 参数分页拉取，每次 100 根，共 5 次请求）
      对应规格：2.3.2 "单次最多返回 100 根"

- [ ] **P1-15** OKX WebSocket 连接
      验证方法：打开浏览器，观察控制台 WebSocket 连接日志
      预期结果：连接建立成功，K线数据开始实时更新
      对应规格：2.3.2 WS 订阅

- [ ] **P1-16** OKX WebSocket 心跳
      验证方法：保持页面打开 2 分钟，观察 WebSocket 连接是否断开
      预期结果：连接保持稳定（后端每 25 秒发送 ping）
      对应规格：九-1 "每 25 秒发送 ping"

### 1.6 WebSocket 推送

- [ ] **P1-17** 前端 WebSocket 连接
      验证方法：页面加载后观察右上角状态灯
      预期结果：状态灯显示 🟢 绿色 + "实时"
      对应规格：4.16

- [ ] **P1-18** 断线重连（指数退避）
      验证方法：手动停止后端，观察前端状态灯和控制台
      预期结果：状态灯变 🔴 红色 + "断线重连中..."，控制台显示重连间隔 3s→6s→12s→24s→30s
      对应规格：4.4 reconnectDelay

- [ ] **P1-19** 重连后恢复
      验证方法：重启后端，等待前端自动重连
      预期结果：状态灯恢复 🟢 绿色，K线继续更新，重连延迟重置为 3s
      对应规格：4.4 "连接成功，重置延迟"

- [ ] **P1-20** K线实时推送
      验证方法：观察 K线图表
      预期结果：最后一根 K 线持续更新（非收盘K线），收盘时新增一根
      对应规格：2.10 kline 消息

- [ ] **P1-21** 订阅确认
      验证方法：切换品种后观察 WebSocket 消息
      预期结果：收到 `{"type": "subscription_result", "status": "ok"}` 消息
      对应规格：2.10 subscription_result

### 1.7 K线图表

- [ ] **P1-22** KLineChart 初始化
      验证方法：页面加载
      预期结果：图表正常渲染，显示暗色主题蜡烛图
      对应规格：4.3

- [ ] **P1-23** UMD 引入方式
      验证方法：浏览器控制台输入 `typeof klinecharts`
      预期结果：返回 `"object"`（全局对象存在），不是 `"undefined"`
      对应规格：4.0 "使用 UMD 版本"

- [ ] **P1-24** 时间周期切换
      验证方法：点击工具栏 1m/5m/15m/30m/1H/4H/1D/1W/1M 按钮
      预期结果：图表重新加载对应周期数据，当前选中按钮高亮
      对应规格：4.1 工具栏

- [ ] **P1-25** 数字快捷键切换周期
      验证方法：按键盘 1~9
      预期结果：对应切换到 1m/5m/15m/30m/1H/4H/1D/1W/1M
      对应规格：4.17

### 1.8 搜索组件

- [ ] **P1-26** 搜索弹窗
      验证方法：点击工具栏搜索框 或 按 `/` 键
      预期结果：弹出搜索面板，显示热门品种列表
      对应规格：4.6

- [ ] **P1-27** 搜索功能
      验证方法：输入 "BTC" 或 "比特币"
      预期结果：结果按市场分组显示，每组最多 5 条
      对应规格：4.6 "前端本地搜索"

- [ ] **P1-28** 选择品种切换
      验证方法：点击搜索结果中的一个品种
      预期结果：(1) 主图切换 (2) WebSocket 订阅切换 (3) 信息面板更新 (4) 搜索面板关闭
      对应规格：4.6 选中行为

### 1.9 设置页面

- [ ] **P1-29** 设置对话框打开
      验证方法：点击工具栏 [⚙设置]
      预期结果：弹出设置模态框，所有字段已从后端加载当前值
      对应规格：4.7

- [ ] **P1-30** LLM 配置按提供商切换
      验证方法：在设置中切换 LLM 提供商从 DeepSeek 到通义千问
      预期结果：表单切换显示对应的 API Key / Base URL / Model 字段
      对应规格：4.7 "当选择 DeepSeek 时显示 / 当选择通义千问时显示"

- [ ] **P1-31** 设置保存
      验证方法：修改一项设置后点 [保存设置]，刷新页面后重新打开设置
      预期结果：修改后的值保持不变（已写入 SQLite）
      对应规格：4.7 "所有设置统一通过 PUT /api/settings 保存"

### 1.10 Toast 通知

- [ ] **P1-32** Toast 显示
      验证方法：触发一个通知场景（如 WebSocket 断线）
      预期结果：右上角弹出 Toast，3秒后自动消失，error 类型需手动关闭
      对应规格：4.16 Toast 规格

---

## 二、Phase 2 验收 — 指标和多市场

### 2.1 内置指标

- [ ] **P2-01** 指标列表 API
      验证方法：`GET /api/indicators`
      预期结果：返回 20+ 指标，每个含 `name, category, overlay, params, outputs`
      对应规格：2.4.2

- [ ] **P2-02** 指标分类完整性
      验证方法：检查返回的 category 值
      预期结果：包含 `trend, momentum, volume, volatility, strength` 五类
      对应规格：2.4.2 注释

- [ ] **P2-03** 指标选择面板
      验证方法：点击工具栏 [指标]
      预期结果：弹出模态框，分5组显示所有指标，已添加指标在顶部显示
      对应规格：4.13

- [ ] **P2-04** 主图指标添加
      验证方法：添加 MA 指标
      预期结果：MA 均线叠加在 K 线上（candle_pane），可调整参数
      对应规格：4.3 "主图叠加指标"

- [ ] **P2-05** 副图指标添加
      验证方法：添加 MACD 和 RSI
      预期结果：各自创建独立副图 pane，最多 4 个副图
      对应规格：4.3 pane 管理策略

- [ ] **P2-06** 指标移除
      验证方法：在已添加列表点击 × 移除指标
      预期结果：图表上对应指标消失，副图 pane 同步删除
      对应规格：4.13

- [ ] **P2-07** 指标计算 API
      验证方法：`POST /api/indicators/calculate` 请求 MACD
      预期结果：返回 `dif, dea, histogram` 三个数组，长度与K线数量一致
      对应规格：3.2

- [ ] **P2-08** 所有指标可计算
      验证方法：依次请求计算 MA/EMA/BOLL/SAR/MACD/RSI/KDJ/CCI/OBV/ATR/DMI/TRIX/ADL/STOCH 等
      预期结果：全部返回有效数据，无报错
      对应规格：2.4.1

### 2.2 多市场数据

- [ ] **P2-09** 美股数据
      验证方法：切换到美股市场，选择 AAPL
      预期结果：显示 AAPL 的 K 线（15分钟延迟），信息面板显示 USD 价格
      对应规格：2.3.4

- [ ] **P2-10** 港股数据
      验证方法：切换到港股市场，选择 0700.HK
      预期结果：显示腾讯控股 K 线，信息面板显示 HKD 价格
      对应规格：2.3.4

- [ ] **P2-11** A股数据
      验证方法：切换到A股市场，选择 600519
      预期结果：显示贵州茅台 K 线，信息面板显示 CNY 价格 + 涨停价/跌停价/换手率
      对应规格：2.3.5 + 4.18

- [ ] **P2-12** A股周期限制
      验证方法：在A股市场查看时间周期选项
      预期结果：1m 和 4H 被隐藏或禁用
      对应规格：4.11 时间周期

- [ ] **P2-13** 市场切换联动
      验证方法：在工具栏切换市场
      预期结果：(1)自选列表刷新 (2)默认品种加载 (3)时间周期选项调整 (4)信息面板货币切换 (5)WebSocket 重新订阅
      对应规格：4.11

- [ ] **P2-14** 仪表盘按钮可见性
      验证方法：切换到美股/港股/A股
      预期结果：[仪表盘] 按钮灰色禁用，只有加密市场时高亮可用
      对应规格：4.11

### 2.3 自选列表

- [ ] **P2-15** 自选列表加载
      验证方法：`GET /api/watchlist?market=crypto`
      预期结果：返回该市场的自选品种列表
      对应规格：3.8

- [ ] **P2-16** 添加品种
      验证方法：点击 [+添加]，搜索并选择品种
      预期结果：品种出现在自选列表中，实时价格和涨跌幅更新
      对应规格：4.15

- [ ] **P2-17** 右键菜单
      验证方法：右键点击自选列表中的品种
      预期结果：显示菜单（置顶/查看详情/设置警报/删除）
      对应规格：4.15

- [ ] **P2-18** 自选列表市场独立
      验证方法：在加密市场添加品种，切换到美股再切回来
      预期结果：加密市场自选列表保持不变
      对应规格：4.11 "每个市场独立维护"

---

## 三、Phase 3 验收 — 公式和警报

### 3.1 公式编辑器

- [ ] **P3-01** CodeMirror 编辑器加载
      验证方法：点击底部面板 [公式编辑器] 标签
      预期结果：左侧显示 CodeMirror 6 代码编辑器，有语法高亮
      对应规格：4.5

- [ ] **P3-02** OpenScript 模式
      验证方法：输入 `plot(ema(close, 20))` 并点击 [▶运行]
      预期结果：EMA(20) 曲线绘制在主图上
      对应规格：2.5.1

- [ ] **P3-03** Python 模式
      验证方法：切换到 Python 模式，编写 calculate 函数并运行
      预期结果：自定义指标绘制在图表上
      对应规格：2.5.2

- [ ] **P3-04** 沙箱安全
      验证方法：在 Python 模式中尝试 `import os; os.listdir('/')`
      预期结果：报错，拒绝执行
      对应规格：2.5.2 "禁止：import os/sys/subprocess"

- [ ] **P3-05** open 变量不冲突
      验证方法：在 OpenScript 中使用 `plot(open)` 绘制开盘价
      预期结果：正常绘制，不触发 Python 内置 `open()` 函数
      对应规格：2.5.1 "open 变量与 Python 内置 open() 同名"

- [ ] **P3-06** 公式验证 API
      验证方法：`POST /api/formula/validate` 发送语法错误代码
      预期结果：返回 `{"valid": false, "errors": [...]}`
      对应规格：3.3

- [ ] **P3-07** 执行超时
      验证方法：写一个死循环公式运行
      预期结果：5秒（OpenScript）或 10秒（Python）后超时返回错误
      对应规格：2.5.1/2.5.2

- [ ] **P3-08** Ctrl+Enter 快捷键
      验证方法：在公式编辑器中按 Ctrl+Enter
      预期结果：运行当前公式
      对应规格：4.17

### 3.2 警报系统

- [ ] **P3-09** 创建价格警报
      验证方法：点击 [警报] → 设置 BTC-USDT 价格 > 70000 → [创建警报]
      预期结果：警报创建成功，出现在右侧面板警报列表
      对应规格：4.8 + 3.4

- [ ] **P3-10** 6种触发类型
      验证方法：在创建对话框切换不同触发类型
      预期结果：各类型显示对应的条件表单（价格/指标/交叉/成交量/涨跌幅/自定义公式）
      对应规格：4.8

- [ ] **P3-11** 通知方式多选
      验证方法：同时勾选 浏览器通知 + 声音提醒
      预期结果：创建的警报 `notify_methods` 为 `["browser", "sound"]`
      对应规格：3.4 notify_methods

- [ ] **P3-12** 浏览器通知权限
      验证方法：首次创建含浏览器通知的警报
      预期结果：弹出系统授权弹窗，拒绝后提示 "警报将仅通过声音/Webhook通知"
      对应规格：4.14

- [ ] **P3-13** 警报触发
      验证方法：设置一个即将满足的条件（如当前价格附近），等待触发
      预期结果：(1)浏览器通知弹出 (2)声音播放 (3)WebSocket 收到 alert 消息 (4)底部警报日志新增记录
      对应规格：2.6.2

- [ ] **P3-14** 警报历史 API 过滤与分页
      验证方法：`GET /api/alerts/history?limit=10&offset=0&symbol=BTC-USDT&market=crypto&days=7`
      预期结果：只返回 crypto 市场 BTC-USDT 最近 7 天的警报历史，最多 10 条，offset=10 时返回下一页
      对应规格：3.4

---

## 四、Phase 4 验收 — 回测和选股

### 4.1 回测系统

- [ ] **P4-01** 回测界面布局
      验证方法：点击底部面板 [回测报告] 标签
      预期结果：左栏策略定义 + 右栏报告展示
      对应规格：4.9

- [ ] **P4-02** 运行回测
      验证方法：配置策略（RSI<30买入, RSI>70卖出）→ 点击 [运行回测]
      预期结果：WebSocket 推送进度，完成后右栏显示报告
      对应规格：3.5 + 2.7.1

- [ ] **P4-03** 回测报告完整性
      验证方法：检查报告内容
      预期结果：包含资金曲线图、统计摘要（收益率/Sharpe/最大回撤/胜率/盈亏比等）、月度热力图、交易记录
      对应规格：2.7.2

- [ ] **P4-04** 回测 API 含 market 字段
      验证方法：检查 `POST /api/backtest/run` 请求 Body
      预期结果：包含 `market` 字段
      对应规格：3.5

- [ ] **P4-05** 手续费率自动填充
      验证方法：切换品种为加密货币 / 美股
      预期结果：手续费率自动填充 0.1% / 0.03%
      对应规格：4.9 手续费率自动填充

- [ ] **P4-06** 参数优化
      验证方法：勾选参数扫描，设置 RSI周期 10~30 步长 5 → 点击 [运行优化]
      预期结果：返回最优参数 + 热力图
      对应规格：3.5 optimize

- [ ] **P4-07** 回测进度 WebSocket
      验证方法：运行回测时观察 WebSocket 消息
      预期结果：收到 `backtest_progress` 和 `backtest_complete` 消息，前端进度条更新
      对应规格：2.10

### 4.2 选股系统

- [ ] **P4-08** 选股界面布局
      验证方法：点击底部面板 [选股结果] 标签
      预期结果：左栏规则配置 + 右栏结果（上方表格 + 下方 AI 推荐）
      对应规格：4.10

- [ ] **P4-09** 规则筛选（同步）
      验证方法：配置 RSI<30 + 价格>MA(200) → 点击 [🔍开始筛选]
      预期结果：1~5秒返回结果表格，可排序
      对应规格：链路 A (2.8.4)

- [ ] **P4-10** 多市场筛选
      验证方法：同时勾选加密 + 美股
      预期结果：API 发送 `markets: ["crypto", "us"]`，结果合并显示，每行带 market 标识
      对应规格：3.6

- [ ] **P4-11** 所有筛选条件可用
      验证方法：逐一测试 16 种 FILTER_TYPE
      预期结果：所有条件类型正常工作
      对应规格：2.8.1 FILTER_TYPES

- [ ] **P4-12** AI 分析（异步）
      验证方法：配置 LLM API Key → 点击 [🤖AI分析]
      预期结果：按钮变为 [⏳分析中...]，右栏显示进度文字，完成后显示推荐卡片
      对应规格：链路 B (2.8.4)

- [ ] **P4-13** AI 分析前置检查
      验证方法：不配置 LLM API Key 时点击 [🤖AI分析]
      预期结果：Toast 提示 "请先在设置(⚙)中配置 LLM API Key"，不发请求
      对应规格：4.10.1

- [ ] **P4-14** AI 推荐卡片
      验证方法：AI 分析完成后检查卡片内容
      预期结果：每张卡片含评分(0-100)、理由、技术信号标签、关联新闻链接
      对应规格：4.10.1

- [ ] **P4-15** AI 评分制统一
      验证方法：检查所有 AI 评分显示
      预期结果：全部使用 0-100 分制，无 0-5 分制残留
      对应规格：3.6 "score: 85"

- [ ] **P4-16** 选股结果交互
      验证方法：点击结果表格行 / AI 卡片 [查看K线]
      预期结果：主图表切换到该品种
      对应规格：4.10.1

---

## 五、Phase 5 验收 — 加密仪表盘

- [ ] **P5-01** 仪表盘标签页
      验证方法：加密市场下点击 [仪表盘] 或底部面板 [仪表盘] 标签
      预期结果：显示仪表盘页面
      对应规格：4.5

- [ ] **P5-02** 恐惧贪婪指数
      验证方法：`GET /api/dashboard/fear-greed`
      预期结果：返回 value(0-100)、label、history 数组
      对应规格：2.9.2 + 3.7

- [ ] **P5-03** 资金费率
      验证方法：`GET /api/dashboard/funding-rate?symbol=BTC-USDT-SWAP`
      预期结果：返回当前费率 + 历史费率
      对应规格：2.9.2

- [ ] **P5-04** 持仓量和多空比
      验证方法：`GET /api/dashboard/open-interest` 和 `GET /api/dashboard/long-short-ratio`
      预期结果：返回有效数据
      对应规格：2.9.2 + 3.7

- [ ] **P5-05** 经济日历
      验证方法：`GET /api/dashboard/calendar`
      预期结果：返回宏观事件和加密事件列表，每项含 time/event/importance
      对应规格：2.9.3

- [ ] **P5-06** 仪表盘实时更新
      验证方法：观察 WebSocket dashboard_update 消息
      预期结果：仪表盘数据定期刷新
      对应规格：2.10 dashboard_update

---

## 六、跨模块一致性检查

这部分不按 Phase 划分，而是在**全部开发完成后**做的整体一致性检查。

### 6.1 数据模型一致性

- [ ] **X-01** Alert 数据模型 ↔ DB 表 ↔ API Body
      验证方法：对比 `models.py Alert` dataclass、`alerts` 表字段、`POST /api/alerts` Body
      预期结果：所有字段完全对应，包括 `notify_methods`、`label`、`market`
      对应规格：2.2 + 五 + 3.4

- [ ] **X-02** BacktestResult ↔ report JSON ↔ API 返回
      验证方法：对比 `BacktestResult` dataclass、2.7.2 report JSON、`GET /api/backtest/report/{id}` 返回
      预期结果：结构一致，summary 嵌套对象字段对齐
      对应规格：2.2 + 2.7.2 + 3.5

- [ ] **X-03** watchlist 表 UNIQUE 约束
      验证方法：尝试同一市场添加同一品种两次
      预期结果：报错或忽略（UNIQUE(symbol, market) 约束生效）
      对应规格：五 watchlist 表

### 6.2 API 完整性

- [ ] **X-04** 所有 API 端点可访问
      验证方法：逐一请求第三章定义的所有 API
      预期结果：无 404，全部返回正确格式

      完整端点列表：
      ```
      GET  /api/markets
      GET  /api/symbols
      GET  /api/klines
      GET  /api/indicators
      POST /api/indicators/calculate
      POST /api/formula/validate
      POST /api/formula/execute
      GET  /api/alerts
      POST /api/alerts
      PUT  /api/alerts/{id}
      DELETE /api/alerts/{id}
      GET  /api/alerts/history
      POST /api/backtest/run
      GET  /api/backtest/report/{id}
      POST /api/backtest/optimize
      POST /api/screener/filter
      POST /api/screener/ai-analyze
      GET  /api/screener/ai-status/{task_id}
      GET  /api/dashboard/fear-greed
      GET  /api/dashboard/funding-rate
      GET  /api/dashboard/open-interest
      GET  /api/dashboard/long-short-ratio
      GET  /api/dashboard/exchange-flow
      GET  /api/dashboard/whale-transactions
      GET  /api/dashboard/calendar
      GET  /api/dashboard/onchain
      GET  /api/watchlist
      POST /api/watchlist
      DELETE /api/watchlist/{symbol}
      PUT  /api/watchlist/reorder
      GET  /api/settings
      PUT  /api/settings
      ```

### 6.3 WebSocket 消息完整性

- [ ] **X-05** 所有 WS 消息类型有前端 handler
      验证方法：检查 websocket.js 中的 `ws.on()` 注册
      预期结果：覆盖 `kline, alert, backtest_progress, backtest_complete, dashboard_update, subscription_result, screener_progress`（共 7 种）
      对应规格：2.10 + 4.4

### 6.4 错误处理

- [ ] **X-06** REST API 超时重试
      验证方法：模拟后端慢响应
      预期结果：前端 10秒超时，3次重试（1s/2s/4s 间隔）
      对应规格：4.0.2

- [ ] **X-07** 数据源降级
      验证方法：模拟 OKX 不可用
      预期结果：自动切换 Binance + Toast 提示用户
      对应规格：4.0.2 + 4.16

- [ ] **X-08** LLM API 超时
      验证方法：模拟 LLM 30秒无响应
      预期结果：重试 1 次，仍失败返回错误，AI 推荐区显示错误信息
      对应规格：2.8.4 错误处理表

### 6.5 前端 UI 状态

- [ ] **X-09** 所有空状态显示
      验证方法：清空数据后查看各面板
      预期结果：
        - 自选列表空 → "暂无自选品种，点击 [+添加] 开始"
        - 选股结果空 → "未找到符合条件的品种"
        - 回测未运行 → "配置策略后点击 [运行回测] 开始"
        - 警报日志空 → "暂无警报记录"
      对应规格：4.16

- [ ] **X-10** 所有快捷键生效
      验证方法：逐一测试 4.17 定义的所有快捷键
      预期结果：`/` 搜索、`Esc` 关闭、`1-9` 切周期、`Ctrl+S` 保存、`Ctrl+Enter` 运行、`Alt+A/I/D` 打开面板、`Ctrl+Z` 图表撤销（编辑器焦点时为文字撤销）、`Tab/Shift+Tab` 切标签、`F11` 全屏
      对应规格：4.17

### 6.6 性能基准

- [ ] **X-11** K线加载速度
      验证方法：记录 500 根 1H K线的加载时间
      预期结果：< 3秒（含网络延迟）

- [ ] **X-12** 指标计算速度
      验证方法：5000 只 A 股批量计算 RSI+MA
      预期结果：< 1秒（NumPy 向量化计算）
      对应规格：2.8.4 链路A "向量化计算，5000只股票<1秒"

- [ ] **X-13** 选股规则筛选
      验证方法：全市场筛选（含 A 股 5000 只）
      预期结果：1~5秒返回
      对应规格：2.8.4 "链路 A — 规则筛选（同步，1~5秒）"

---

## 七、给 Claude Code 的验收指令模板

开发完成后，你可以直接复制下面的指令发给 Claude Code：

### 单 Phase 验收

```
请按照 OpenChart_Pro_验收审计清单.md 中的 "Phase 1 验收" 部分，
逐条运行检查。对每一项：
1. 执行验证方法中描述的操作
2. 对比预期结果
3. 标记 [x] 通过 或 [ ] 失败
4. 失败项记录实际结果和可能原因
最后给出总结：通过/总数，列出所有失败项。
```

### 全量验收

```
请完整执行 OpenChart_Pro_验收审计清单.md 中的所有检查项（Phase 1~5 + 跨模块一致性），
逐条运行并报告结果。重点关注：
1. 所有 API 端点可正常访问
2. WebSocket 所有消息类型有前端处理
3. 数据模型字段在 models.py / DB表 / API Body 三处完全一致
4. 所有错误处理场景覆盖
给出总通过率和失败项清单。
```

### 修复后回归验收

```
上次验收失败的项有：P1-03, P2-11, P4-12（列出具体编号）
请只针对这些项重新运行检查，确认修复是否生效。
```

---

## 八、变更日志

| 日期 | 版本 | 说明 |
|------|------|------|
| 2026-03-25 | v1.0 | 初始版本，覆盖 Phase 1~5 + 跨模块检查，共 99 项 |