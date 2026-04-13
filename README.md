# OpenChart Pro

专业级加密货币与股票行情分析平台，集成缠论、艾略特波浪、斐波那契等技术分析工具。

## 功能特性

- **K线图表**：基于 KLineCharts，支持 1m/5m/15m/30m/1H/4H/1D/1W/1M 多周期
- **缠论分析**：笔/线段/中枢/买卖点自动标注（基于 chan.py 引擎）
- **缠论研判**：多级别综合分析面板，给出操作建议
- **艾略特波浪**：5浪推动+ABC修正自动识别，预测目标线
- **斐波那契回撤**：自动计算关键支撑阻力位
- **多市场支持**：加密货币（OKX/Binance）、美股（Yahoo）、A股（东方财富）、港股
- **实时行情**：WebSocket 推送，实时更新K线和价格
- **AI 研判**：接入 DeepSeek/通义千问，多级别综合分析
- **价格警报**：支持价格突破、区间突破等多种警报类型
- **自选列表**：多品种实时监控
- **回测系统**：基于 vectorbt，支持自定义公式策略回测
- **选股/筛选**：AI 驱动的市场筛选
- **仪表盘**：链上数据、市场情绪、经济日历

## 技术栈

- **后端**：Python 3.12+ / FastAPI / Uvicorn / WebSocket
- **前端**：HTML + CSS + JavaScript（无框架）/ KLineCharts v9
- **数据库**：SQLite
- **数据源**：OKX API / Binance API / Yahoo Finance / 东方财富

## 环境要求

- Python 3.12 或更高版本
- pip 包管理器
- 网络连接（获取行情数据）

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动

```bash
python run.py
```

启动后自动打开浏览器访问 http://localhost:8888

### 3. 配置（可选）

打开页面右上角"设置"，可配置：
- 交易所选择（OKX / Binance）
- OKX API Key（实盘交易需要）
- AI 模型 API Key（AI 研判需要）
- K线颜色方案（国际/中国标准）

所有配置存储在 SQLite 数据库中（data/openchart.db），无需手动编辑文件。

## 目录结构

```
OpenChart Pro/
├── run.py                # 一键启动脚本
├── requirements.txt      # Python 依赖
├── backend/              # 后端代码
│   ├── main.py           # FastAPI 主入口
│   ├── config.py         # 默认配置
│   ├── chanlun_engine/   # 缠论分析引擎
│   ├── elliott_wave/     # 艾略特波浪
│   ├── data/             # 数据源适配器
│   ├── indicators/       # 技术指标
│   ├── trading/          # 实盘交易
│   ├── backtest/         # 回测引擎
│   └── ws/               # WebSocket 推送
├── frontend/             # 前端静态文件
│   ├── index.html
│   ├── css/
│   ├── js/
│   └── lib/              # 第三方JS库
├── data/                 # SQLite 数据库（自动创建）
└── docs/                 # 项目文档
```

## 常见问题

**Q: 启动报错 ModuleNotFoundError**
A: 确认已运行 `pip install -r requirements.txt` 安装所有依赖

**Q: K线图不显示数据**
A: 检查网络连接，确保能访问 OKX 或 Binance API

**Q: 缠论指标不显示**
A: 点击图表上方"指标"按钮，选择"缠论"启用

**Q: 艾略特波浪不显示**
A: 点击图表上方"指标"按钮，选择"艾略特"启用

## 许可证

私有项目，暂未开源。
