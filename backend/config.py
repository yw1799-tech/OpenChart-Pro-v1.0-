"""
全局配置文件 — 仅作为默认值模板。
运行时配置存储在 SQLite config 表（KV 格式）。
优先级：SQLite DB > config.py 默认值
"""

# 服务器配置
HOST = "0.0.0.0"
PORT = 8888
DEBUG = True

# 数据库
DB_PATH = "./data/openchart.db"

# 交易所选择: "okx" | "binance"
CRYPTO_EXCHANGE = "okx"

# OKX API
OKX_BASE_URL = "https://www.okx.com"
OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
OKX_API_KEY = ""
OKX_SECRET_KEY = ""
OKX_PASSPHRASE = ""

# Binance API
BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_WS = "wss://stream.binance.com:9443/ws"

# 股票数据源
YAHOO_POLL_INTERVAL = 10
EASTMONEY_POLL_INTERVAL = 3

# AI/LLM 配置: "deepseek" | "qwen"
LLM_PROVIDER = "deepseek"
DEEPSEEK_API_KEY = ""
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"
QWEN_API_KEY = ""
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_MODEL = "qwen-turbo"

# 新闻数据源
FINNHUB_API_KEY = ""
NEWS_POLL_INTERVAL = 60

# 链上数据
GLASSNODE_API_KEY = ""
CRYPTOQUANT_API_KEY = ""

# 图表样式
CANDLE_COLOR_SCHEME = "international"  # "international"(绿涨红跌) | "chinese"(红涨绿跌)
CANDLE_TYPE = "candle_solid"
SHOW_GRID = True
TIMEZONE = "Asia/Shanghai"

# 警报通知
WEBHOOK_URLS = []
ENABLE_BROWSER_NOTIFICATION = True
ENABLE_SOUND = True
SOUND_VOLUME = 70

# 回测默认参数
BACKTEST_INITIAL_CAPITAL = 100000
BACKTEST_COMMISSION_CRYPTO = 0.001
BACKTEST_COMMISSION_STOCK = 0.0003
BACKTEST_SLIPPAGE = 0.0005
