"""
OpenChart Pro — 全局配置模块

优先级：SQLite `config` 表（运行时动态值） > 本文件默认值
修改配置通过前端 PUT /api/settings → 写 DB → 内存热更新，无需重启。

章节编号与 TDD §3 一一对应。
"""

# ═══════════════════════════════════════════════════════════════════
# 1. 服务器
# ═══════════════════════════════════════════════════════════════════
HOST = "0.0.0.0"
PORT = 8888
DEBUG = True
DB_PATH = "./data/openchart.db"


# ═══════════════════════════════════════════════════════════════════
# 2. 加密货币（固定 6 币种，不使用候选池筛选）
# ═══════════════════════════════════════════════════════════════════
# 系统启动时自动加载到自选列表，并自动绑定全部内置策略开始监控。
CRYPTO_SYMBOLS = [
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "DOGE-USDT",
    "BNB-USDT",
    "XRP-USDT",
]

# 加密交易所选择："okx" | "binance"
CRYPTO_EXCHANGE = "okx"

# OKX API（公开数据无需 Key，交易需要，Phase 7）
OKX_BASE_URL = "https://www.okx.com"
OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
OKX_API_KEY = ""
OKX_SECRET_KEY = ""
OKX_PASSPHRASE = ""

# Binance（备用降级源）
BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_WS = "wss://stream.binance.com:9443/ws"


# ═══════════════════════════════════════════════════════════════════
# 3. 股票数据源
# ═══════════════════════════════════════════════════════════════════
YAHOO_POLL_INTERVAL = 10  # 秒
EASTMONEY_POLL_INTERVAL = 3  # 秒


# ═══════════════════════════════════════════════════════════════════
# 4. AI / LLM
# ═══════════════════════════════════════════════════════════════════
# 提供商："deepseek" | "qwen"（OpenAI 兼容模式，统一用 openai 库调用）
LLM_PROVIDER = "deepseek"

DEEPSEEK_API_KEY = ""
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

QWEN_API_KEY = ""
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_MODEL = "qwen-turbo"

# 日预算硬上限（美元）：超过后当日停止 LLM 调用，但规则引擎照常运行
LLM_DAILY_BUDGET = 5.0


# ═══════════════════════════════════════════════════════════════════
# 5. 新闻采集（Phase 3A 首批 10 个验证源，详见 news/sources.py）
# ═══════════════════════════════════════════════════════════════════
NEWS_DEDUP_WINDOW_HOURS = 24  # URL + SimHash 去重窗口
NEWS_SIMHASH_THRESHOLD = 3  # SimHash 汉明距离阈值，≤3 视为相似


# ═══════════════════════════════════════════════════════════════════
# 6. 第三方 API Key
# ═══════════════════════════════════════════════════════════════════
FINNHUB_API_KEY = ""  # 美股新闻
GLASSNODE_API_KEY = ""  # 链上数据（Phase 6 仪表盘）
CRYPTOQUANT_API_KEY = ""  # 链上数据（Phase 6 仪表盘）


# ═══════════════════════════════════════════════════════════════════
# 7. 候选池（仅股票市场使用）
# ═══════════════════════════════════════════════════════════════════
WATCHPOOL_MAX_SIZE = 30  # 最大容量，超出按评分淘汰最低的
WATCHPOOL_MIN_SCORE = 40  # 最低评分阈值
WATCHPOOL_EXPIRE_NO_NEWS_DAYS = 30  # 30 天无新闻提及则淘汰
WATCHPOOL_EXPIRE_LOW_SCORE_DAYS = 14  # 连续 14 天低分则淘汰
WATCHPOOL_RESCORE_INTERVAL = 3600  # 重评分间隔（秒）


# ═══════════════════════════════════════════════════════════════════
# 8. 策略信号
# ═══════════════════════════════════════════════════════════════════
SIGNAL_MIN_CONFIDENCE = 60  # 置信度阈值（0-100），低于此不触发
SIGNAL_DEDUP_WINDOW = 10  # 同品种同方向去重窗口（秒）


# ═══════════════════════════════════════════════════════════════════
# 9. 宏观数据（v1 仅监控 CPI / FOMC / NFP 三项）
# ═══════════════════════════════════════════════════════════════════
# 偏差判定阈值：
#   |实际值 - 预期值| / 预期值 < 0.5%  →  中性（不触发）
#   0.5% ≤ 偏差 < 1.0%                →  轻微（仅通知）
#   偏差 ≥ 1.0%                       →  明显（可触发信号）
MACRO_DEVIATION_NEUTRAL = 0.005
MACRO_DEVIATION_LIGHT = 0.01
MACRO_IMPACT_WINDOW_HOURS = 24  # 影响窗口，过后失效


# ═══════════════════════════════════════════════════════════════════
# 10. 图表样式（用户可在前端设置中修改）
# ═══════════════════════════════════════════════════════════════════
CANDLE_COLOR_SCHEME = "international"  # "international"(绿涨红跌) | "chinese"(红涨绿跌)
CANDLE_TYPE = "candle_solid"  # candle_solid | candle_stroke | ohlc | area
SHOW_GRID = True
TIMEZONE = "Asia/Shanghai"


# ═══════════════════════════════════════════════════════════════════
# 11. 通知
# ═══════════════════════════════════════════════════════════════════
WEBHOOK_URLS = []  # 微信/钉钉/Telegram/Discord webhook 地址列表
ENABLE_BROWSER_NOTIFICATION = True
ENABLE_SOUND = True
SOUND_VOLUME = 70  # 0-100


# ═══════════════════════════════════════════════════════════════════
# 12. 回测（Phase 5）
# ═══════════════════════════════════════════════════════════════════
BACKTEST_INITIAL_CAPITAL = 100000
BACKTEST_COMMISSION_CRYPTO = 0.001  # 加密默认 0.1%
BACKTEST_COMMISSION_STOCK = 0.0003  # 股票默认 0.03%
BACKTEST_SLIPPAGE = 0.0005  # 默认滑点 0.05%
