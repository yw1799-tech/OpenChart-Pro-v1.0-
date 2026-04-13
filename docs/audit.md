# 项目体检报告 - 2026-04-13

## 1. 项目类型

加密货币/股票行情图表分析平台，集成缠论分析、艾略特波浪、斐波那契回撤、AI 研判、回测系统、实盘交易（OKX）等功能。属于**全栈 Web 应用**。

## 2. 技术栈

- **后端**：Python 3.14 + FastAPI + Uvicorn + WebSocket + aiosqlite
- **前端**：原生 HTML/CSS/JS（无框架），使用 KLineChart 和 Chart.js（本地引入）
- **关键库**：pandas, numpy, vectorbt（回测）, yfinance, aiohttp, httpx, openai SDK, feedparser, beautifulsoup4, RestrictedPython, apscheduler
- **数据库**：SQLite（`data/openchart.db`）
- **依赖文件**：只有一个 `requirements.txt`，没有 pyproject.toml 或 setup.py

## 3. 项目结构

```
backend/              # Python 后端
  main.py             # FastAPI 入口（2767 行，巨大单文件）
  config.py           # 全局配置
  alerts/             # 警报系统
  backtest/           # 回测引擎
  chanlun_engine/     # 缠论分析引擎（chan.py 项目源码）
  data/               # 数据源（Binance/OKX/Yahoo/东方财富）
  elliott_wave/       # 艾略特波浪
  indicators/         # 技术指标
  screener/           # AI 分析/新闻
  trading/            # 实盘交易
  ws/                 # WebSocket Hub
frontend/             # 纯静态前端
  index.html
  css/app.css
  js/                 # 十几个 JS 模块
  lib/                # 第三方库（本地 min.js）
data/                 # SQLite 数据库
```

## 4. 入口文件

`run.py`（根目录），执行 `python run.py` 启动，自动打开浏览器访问 `http://localhost:8888`。
后端实际入口是 `backend/main.py`（FastAPI app）。

## 5. 现有文档

- **没有 README.md**
- 有 `使用说明.md`、`项目规范化执行指令.md`、`规范化进度表.md`（规范化引导文档）
- 有 `OpenChart Pro -- 完整开发规格文档.txt` 和 `验收审计清单.txt`
- **缺少正式的 README 和 API 文档**

## 6. 依赖管理

- 有 `requirements.txt`（17 个依赖，带版本下限约束）
- **没有锁文件**（无 poetry.lock、Pipfile.lock）
- **没有** pyproject.toml 或 setup.py
- 前端无依赖管理，JS 库直接放在 `frontend/lib/`

## 7. 版本控制

- 有 `.gitignore`，覆盖了 `__pycache__/`、`*.pyc`、`.env`、`data/*.db`、`server.log`
- **严重问题**：**34 个 .pyc 文件已被 git 追踪**（.gitignore 添加晚了，历史遗留）
- **server.log 已被追踪**（同上）
- **41 个图片文件**被追踪（chanlun_engine/Image/ 目录）

## 8. 代码风格

- **没有任何格式化工具配置**（无 ruff、flake8、black、prettier、eslint）
- 后端 Python 风格基本统一（有 docstring、类型注解），但 main.py 2767 行违反单一职责
- 前端 JS 无统一规范
- 无 .editorconfig

## 9. 测试

- **零测试文件**，没有 test_ 文件、tests 目录或测试框架配置
- chanlun_engine/ 下有审计脚本，但不是自动化测试

## 10. 最明显的 3 个问题

1. **`backend/main.py` 有 2767 行**——所有 API 路由、数据模型、业务逻辑堆在一个文件
2. **34 个 .pyc 文件和 server.log 被 git 追踪**——编译产物和日志不应在版本控制中
3. **chanlun_engine 是整个第三方项目源码复制进来的**——包含 LICENSE、README、Image 目录、Demo 脚本，无法跟踪上游更新

## 11. 敏感信息风险

- `backend/config.py` 中所有 API Key 字段值为空字符串 `""`，目前安全
- 没有 .env 文件存在，.gitignore 已包含 `.env` 规则
- **没有发现硬编码的 API Key**
- **风险点**：config.py 的空字符串默认值，如果有人填入真实值后忘记用 .env 覆盖，会直接提交到 git

## 12. 改进优先级（只做 3 件事）

1. **清理 git 追踪的垃圾文件**：移除全部 .pyc 和 server.log，确保 .gitignore 生效
2. **拆分 `backend/main.py`**：按功能拆为独立 router 模块，main.py 只做初始化
3. **添加 pyproject.toml + 代码规范工具**：统一项目元数据，配置 ruff，敏感配置改为从环境变量读取
