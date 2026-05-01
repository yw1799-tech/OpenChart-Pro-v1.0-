"""
OpenChart Pro — 全局配置模块

优先级：环境变量 > SQLite `config` 表（运行时动态值） > 本文件默认值
修改 LLM/Webhook/采集开关 等业务参数通过前端 PUT /api/settings → 写 DB → 内存热更新，无需重启。
机密 API Key、HOST/PORT/DEBUG 等基础设施参数走环境变量，避免明文进 Git。

章节编号与 TDD §3 一一对应。
"""

import os


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    try:
        return int(v) if v else default
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    v = os.getenv(key)
    try:
        return float(v) if v else default
    except (TypeError, ValueError):
        return default


def _env_list(key: str, default: list) -> list:
    v = os.getenv(key)
    if not v:
        return default
    return [x.strip() for x in v.split(",") if x.strip()]


# ═══════════════════════════════════════════════════════════════════
# 1. 服务器
# ═══════════════════════════════════════════════════════════════════
HOST = os.getenv("OPENCHART_HOST", "127.0.0.1")  # 默认仅本机；外网用 0.0.0.0 需走反代
PORT = _env_int("OPENCHART_PORT", 8888)
DEBUG = _env_bool("OPENCHART_DEBUG", False)
DB_PATH = os.getenv("OPENCHART_DB_PATH", "./data/openchart.db")
# CORS 允许的来源域名列表，逗号分隔；默认允许所有（开发场景）
ALLOWED_ORIGINS = _env_list("OPENCHART_ALLOWED_ORIGINS", ["*"])


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
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

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

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
# v12.10: 默认空 — 让 ai_analyzer 走 PATH_LLM_CONFIG 按 path 自动选 v4-flash / v4-pro
# 仅当用户在 .env 显式指定（如试新模型）时才覆盖 path 配置
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "")

QWEN_API_KEY = os.getenv("QWEN_API_KEY", "")
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen-turbo")

# 日预算硬上限（美元）：超过后后台任务停止 LLM 调用（force=True 路径仍可调）
# v12.10: V4 系列价格大幅下降（Flash $0.0003/1K、Pro $0.0009/1K vs 旧 reasoner $0.0022/1K）
# 同样 5000 次/日额度，预算可降到 10 USD；保留 15 USD 给 review/diagnose 偶发 max-effort 长链
LLM_DAILY_BUDGET = 15.0


# ═══════════════════════════════════════════════════════════════════
# 4.6 Telegram 自动交易事件推送（Bot API，免费、无量限）
# ═══════════════════════════════════════════════════════════════════
# 总开关
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() in ("true", "1", "yes")
# 找 @BotFather 创建 bot 取得 token（形如 123456:AAAA-BBBB...）
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# 用户/群 chat_id：先 /start bot，再 GET https://api.telegram.org/bot<TOKEN>/getUpdates
# 群 chat_id 是负数（例 -1001234567890），私聊为正数
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ═══════════════════════════════════════════════════════════════════
# 5. 新闻采集（Phase 3A 首批 10 个验证源，详见 news/sources.py）
# ═══════════════════════════════════════════════════════════════════
NEWS_DEDUP_WINDOW_HOURS = 24  # URL + SimHash 去重窗口
NEWS_SIMHASH_THRESHOLD = 3  # SimHash 汉明距离阈值，≤3 视为相似


# ═══════════════════════════════════════════════════════════════════
# 6. 第三方 API Key
# ═══════════════════════════════════════════════════════════════════
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")        # 美股新闻
GLASSNODE_API_KEY = os.getenv("GLASSNODE_API_KEY", "")    # 链上数据（Phase 6 仪表盘）
CRYPTOQUANT_API_KEY = os.getenv("CRYPTOQUANT_API_KEY", "") # 链上数据（Phase 6 仪表盘）


# ═══════════════════════════════════════════════════════════════════
# 7. 候选池（仅股票市场使用）— v12.13 分层 + 总上限 300
# ═══════════════════════════════════════════════════════════════════
# 分层策略（按 score 划分，超额按 score 淘最低；豁免来源不淘）：
#   Tier 1 (≥70):  上限 220  — 高分主力，全保留
#   Tier 2 (50-69): 上限 60  — 中等机会，淘汰最低 score
#   Tier 3 (40-49): 上限 20  — 边缘，仅留 score 最高
#   Tier 4 (<40):   立即 archive — 垃圾区不留
# 豁免（即使 score 低也不淘）：持仓 / watchlist / source IN (manual / news / news_ai / macro_theme)
WATCHPOOL_MAX_SIZE = 300                  # 总上限（Tier 1+2+3 合计）

WATCHPOOL_TIER1_MIN_SCORE = 70
WATCHPOOL_TIER1_MAX = 220
WATCHPOOL_TIER2_MIN_SCORE = 50
WATCHPOOL_TIER2_MAX = 200             # v12.15.5: 60 → 200（用户要求放宽，让优胜劣汰自然发生）
WATCHPOOL_TIER3_MIN_SCORE = 40
WATCHPOOL_TIER3_MAX = 200             # v12.15.5: 20 → 200（用户要求放宽）
WATCHPOOL_TIER4_ARCHIVE_BELOW = 0         # v12.15.5: 0 = 禁用 Tier 4 自动归档（用户要求保留低分股观察）
                                          # 历史值=40 — 但导致 506 只优质股被错杀（Visa/JNJ/腾讯等）
                                          # 改 0 后 Tier 4 永不触发；超额淘汰 (Tier 1/2/3) + 30d 无新闻仍生效

WATCHPOOL_MIN_SCORE = 40                  # 兼容旧引用（实际改用 TIER4_ARCHIVE_BELOW）
WATCHPOOL_EXPIRE_NO_NEWS_DAYS = 30        # 30 天无新闻提及则淘汰（豁免来源也适用）
WATCHPOOL_EXPIRE_LOW_SCORE_DAYS = 14      # （兼容字段，已被 Tier 4 立即 archive 取代）
WATCHPOOL_RESCORE_INTERVAL = 3600


# ═══════════════════════════════════════════════════════════════════
# 8. 策略信号
# ═══════════════════════════════════════════════════════════════════
SIGNAL_MIN_CONFIDENCE = 75  # 置信度阈值（0-100），低于此不触发；75 才调 LLM 二次验证
SIGNAL_DEDUP_WINDOW = 300  # 同品种同方向去重窗口（秒），5 分钟避免重启后短时间重复


# ═══════════════════════════════════════════════════════════════════
# 8B. 候选池质量硬筛选（拦截新闻/AI/涨幅榜推入的低质量标的）
# ═══════════════════════════════════════════════════════════════════
# 每只拟入池品种会先经 quality_filter 判定，不达标直接拒绝（不入 DB，不推 WS）
# 用户手动添加（source='manual'）绕过筛选，视为明确意图
# 指标 24h 缓存在 SQLite `symbol_fundamentals` 表，避免重复调上游
POOL_FILTER_ENABLED = True
POOL_FILTER_CACHE_HOURS = 24

# A 股（沪深 + 科创 + 北交所；排除 ST/*ST/退市/上市<60 天）
POOL_CN_MIN_MARKET_CAP = 5_000_000_000      # 流通市值 ≥ 50 亿人民币
POOL_CN_MIN_AVG_TURNOVER = 50_000_000       # 20 日均成交额 ≥ 5000 万
POOL_CN_MIN_PRICE = 2.0                     # 价格 ≥ 2 元
POOL_CN_MIN_LISTED_DAYS = 60
POOL_CN_EXCLUDE_ST = True

# 港股（主板；GEM 创业板整体拒绝）
POOL_HK_MIN_MARKET_CAP = 2_000_000_000      # 市值 ≥ 20 亿 HKD
POOL_HK_MIN_AVG_TURNOVER = 10_000_000       # 20 日均成交额 ≥ 1000 万 HKD
POOL_HK_MIN_PRICE = 0.5                     # 价格 ≥ 0.5 HKD（拒绝仙股）
POOL_HK_EXCLUDE_GEM = True                  # 拒绝 GEM 创业板

# 美股（主板；拒绝 OTC）
POOL_US_MIN_MARKET_CAP = 500_000_000        # 市值 ≥ $500M
POOL_US_MIN_AVG_VOLUME = 1_000_000          # 10 日均成交量 ≥ 100 万股
POOL_US_MIN_PRICE = 3.0                     # 价格 ≥ $3（拒绝 penny stock）


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
