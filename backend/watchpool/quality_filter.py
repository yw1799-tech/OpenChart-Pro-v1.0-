"""
候选池质量硬筛选（PRD F6.2 扩展）。

每只拟入池品种先经此模块判定是否达标：
  - A 股：流通市值 / 20日均成交额 / 价格 / 上市天数 / ST 标记
  - 港股：市值 / 20日均成交额 / 价格 / GEM 板块
  - 美股：市值 / 10日均成交量 / 价格 / OTC

未达标：拒绝入池（日志记录原因）。
用户手动添加（source='manual'）绕过筛选。

基本面数据缓存在 `symbol_fundamentals` 表，TTL = POOL_FILTER_CACHE_HOURS 小时。
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

import aiohttp

import backend.config as config

logger = logging.getLogger(__name__)

# 东方财富熔断器：连续失败 5 次后冷却 5 分钟（云端 IP 封锁场景）
_EM_CIRCUIT: dict = {"fails": 0, "cooldown_until": 0.0}
_EM_FAIL_THRESHOLD = 5
_EM_COOLDOWN_SEC = 300  # 5 分钟

def _em_is_open() -> bool:
    """熔断器是否打开（True = 东方财富不可用，跳过）"""
    if _EM_CIRCUIT["fails"] < _EM_FAIL_THRESHOLD:
        return False
    if time.time() >= _EM_CIRCUIT["cooldown_until"]:
        _EM_CIRCUIT["fails"] = 0  # 冷却到期，重置
        return False
    return True

def _em_record_fail():
    _EM_CIRCUIT["fails"] += 1
    if _EM_CIRCUIT["fails"] >= _EM_FAIL_THRESHOLD:
        _EM_CIRCUIT["cooldown_until"] = time.time() + _EM_COOLDOWN_SEC
        logger.debug(f"[quality-em] 东方财富熔断，{_EM_COOLDOWN_SEC}s 内不再尝试基本面")

def _em_record_ok():
    _EM_CIRCUIT["fails"] = 0

# 模块级持久 session（避免每次请求 DNS/TCP/TLS 重来）
# 不同请求 header 不同，所以 session 不绑 header，每次 get 传
_SHARED_SESSION: Optional[aiohttp.ClientSession] = None

async def _get_session() -> aiohttp.ClientSession:
    global _SHARED_SESSION
    if _SHARED_SESSION is None or _SHARED_SESSION.closed:
        _SHARED_SESSION = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300, force_close=False),
            timeout=aiohttp.ClientTimeout(total=15),
        )
    return _SHARED_SESSION


# ═══════════════════════════════════════════════════════════════════
# 基本面上游拉取
# ═══════════════════════════════════════════════════════════════════


async def _fetch_cn_fundamentals(symbol: str, db=None) -> Optional[dict]:
    """
    东方财富 A 股个股基本面。带熔断器：IP 封锁场景下连续失败后自动跳过。
    """
    if _em_is_open():
        return None  # 熔断期间直接返回，不发请求

    from backend.data.eastmoney import _get_secid
    secid = _get_secid(symbol)
    url = "http://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "fields": "f43,f57,f58,f60,f116,f117,f84,f85,f127,f189,f14,f71,f162,f167,f168",
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    try:
        s = await _get_session()
        async with s.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            body = await r.json(content_type=None)
    except Exception as e:
        _em_record_fail()
        logger.debug(f"[quality] 东财 A 股基本面失败 {symbol}: {e}")
        return None
    data = (body or {}).get("data") or {}
    if not data:
        _em_record_fail()
        return None
    _em_record_ok()

    # f43 价格，除以100；f116 流通市值（元）；f189 上市日期
    price = _safe_float(data.get("f43")) / 100 if data.get("f43") else 0
    float_cap = _safe_float(data.get("f116"))
    listed_ymd = str(data.get("f189") or "")
    listed_days = _days_since(listed_ymd) if len(listed_ymd) == 8 else 9999
    # 东财 stock/get 名称字段是 f58，不是 f14（f14 是 clist/get 的）
    name = str(data.get("f58") or data.get("f14") or "")
    is_st = 1 if ("ST" in name.upper() or "退" in name) else 0

    # 20 日均成交额：通过日 K 线算（复用 cached_get_klines 持久连接）
    avg_turnover = await _fetch_cn_avg_turnover(symbol, days=20, db=db)

    return {
        "name": name,
        "price": price,
        "market_cap": float_cap,
        "avg_turnover": avg_turnover,
        "avg_volume": 0,
        "listed_days": listed_days,
        "is_st": is_st,
        "is_gem": 0,
        "is_otc": 0,
        "pe": _safe_float(data.get("f162")),
        "pb": _safe_float(data.get("f167")),
        "turnover_rate": _safe_float(data.get("f168")),
    }


async def _fetch_cn_fundamentals_tencent(symbol: str, db=None) -> Optional[dict]:
    """
    腾讯实时行情作为 A 股基本面备源（东方财富被封时使用）。
    qt.gtimg.cn 免费、无需 Key、东京云服务器可访问。
    字段解析（~分隔）：[1]名称 [3]价格 [34]PE(TTM) [36]PB [37]成交额(元) [44]总市值(亿) [45]流通市值(亿)
    """
    from backend.data.tencent_cn import _get_tencent_symbol
    tencent_sym = _get_tencent_symbol(symbol)  # e.g. sh600519
    url = f"https://qt.gtimg.cn/q={tencent_sym}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://stockapp.finance.qq.com/"}
    try:
        s = await _get_session()
        async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            text = await r.text(encoding="gbk", errors="ignore")
    except Exception as e:
        logger.debug(f"[quality] 腾讯 A 股实时行情拉取失败 {symbol}: {e}")
        return None
    # 解析: v_sh600519="1~贵州茅台~600519~1701.01~..."
    if '"' not in text:
        return None
    content = text.split('"', 2)[1] if '"' in text else ""
    fields = content.split("~")
    if len(fields) < 30:
        return None
    try:
        name = fields[1] if len(fields) > 1 else ""
        price = _safe_float(fields[3]) if len(fields) > 3 else 0
        pe = _safe_float(fields[34]) if len(fields) > 34 else 0
        pb = _safe_float(fields[46]) if len(fields) > 46 else 0
        # 总市值/流通市值字段 44/45（亿元）
        total_cap_yi = _safe_float(fields[44]) if len(fields) > 44 else 0
        float_cap_yi = _safe_float(fields[45]) if len(fields) > 45 else 0
        market_cap = (float_cap_yi or total_cap_yi) * 1e8
        # 当日成交额（元）
        turnover_today = _safe_float(fields[37]) if len(fields) > 37 else 0
    except Exception as e:
        logger.debug(f"[quality] 腾讯 A 股字段解析失败 {symbol}: {e}")
        return None
    if price == 0 and market_cap == 0:
        return None
    is_st = 1 if ("ST" in name.upper() or "退" in name) else 0
    # 均成交额：优先从 K 线缓存算，兜底用今日成交额
    avg_turnover = await _fetch_cn_avg_turnover(symbol, days=20, db=db)
    if avg_turnover == 0:
        avg_turnover = turnover_today
    return {
        "name": name,
        "price": price,
        "market_cap": market_cap,
        "avg_turnover": avg_turnover,
        "avg_volume": 0,
        "listed_days": 9999,
        "is_st": is_st,
        "is_gem": 0,
        "is_otc": 0,
        "pe": pe,
        "pb": pb,
        "turnover_rate": 0,
    }


async def _fetch_cn_avg_turnover(symbol: str, days: int = 20, db=None) -> float:
    """拉最近 days 根日K，返回平均成交额（元）。复用后端 cached_get_klines 避免限频。"""
    if db is not None:
        try:
            from backend.data.cache import cached_get_klines
            from backend.data.models import Interval, Market
            candles = await cached_get_klines(db=db, market=Market.CN, symbol=symbol, interval=Interval.D1, limit=days)
            turnovers = [c.turnover for c in candles if c.turnover > 0]
            return sum(turnovers) / len(turnovers) if turnovers else 0
        except Exception as e:
            logger.debug(f"[quality] cached_get_klines 取成交额失败 {symbol}: {e}")
    # 兜底：直接请求东财
    from backend.data.eastmoney import _get_secid
    url = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": _get_secid(symbol),
        "fields1": "f1,f2", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(days),
    }
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    try:
        s = await _get_session()
        async with s.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            body = await r.json(content_type=None)
    except Exception:
        return 0
    lines = ((body or {}).get("data") or {}).get("klines") or []
    turnovers = []
    for line in lines:
        parts = line.split(",")
        if len(parts) >= 7:
            try:
                turnovers.append(float(parts[6]))
            except (ValueError, IndexError):
                pass
    return sum(turnovers) / len(turnovers) if turnovers else 0


async def _fetch_hk_fundamentals(symbol: str) -> Optional[dict]:
    """腾讯港股基本面（市值、成交额）。"""
    code = symbol.replace(".HK", "").zfill(5)
    url = "https://qt.gtimg.cn/q=" + f"hk{code}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    try:
        s = await _get_session()
        async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            text = await r.text(encoding="gbk", errors="ignore")
    except Exception as e:
        logger.warning(f"[quality] 腾讯港股基本面拉取失败 {symbol}: {e}")
        return None
    # 格式: v_hk06193="..." 字段见腾讯 HK 字段表
    if "=" not in text or '"' not in text:
        return None
    content = text.split('"', 2)[1] if '"' in text else ""
    fields = content.split("~")
    if len(fields) < 50:
        return None
    try:
        name = fields[1]
        price = _safe_float(fields[3])
        market_cap_yi = _safe_float(fields[44])  # 总市值（亿 HKD）
        market_cap = market_cap_yi * 100_000_000
        turnover = _safe_float(fields[37])      # 当日成交额（元）
        pe = _safe_float(fields[39]) if len(fields) > 39 else 0       # 市盈率
        pb = _safe_float(fields[46]) if len(fields) > 46 else 0       # 市净率
        turnover_rate = _safe_float(fields[38]) if len(fields) > 38 else 0  # 换手率
    except (IndexError, ValueError):
        return None

    # 港股 GEM：去掉前导零后以 8 开头（08xxx / 8xxxx 都是 GEM）
    is_gem = 1 if code.lstrip("0").startswith("8") else 0

    # 20 日均成交额：从腾讯日K 拉
    avg_turnover = await _fetch_hk_avg_turnover(symbol, days=20)

    return {
        "name": name,
        "price": price,
        "market_cap": market_cap,
        "avg_turnover": avg_turnover or turnover,
        "avg_volume": 0,
        "listed_days": 9999,  # 腾讯不提供，跳过此检查
        "is_st": 0,
        "is_gem": is_gem,
        "is_otc": 0,
        "pe": pe,
        "pb": pb,
        "turnover_rate": turnover_rate,
    }


async def _fetch_hk_avg_turnover(symbol: str, days: int = 20) -> float:
    """腾讯港股 20 日均成交额。"""
    from backend.data.tencent_hk import fetch_hk_klines
    from backend.data.models import Interval
    candles = await fetch_hk_klines(symbol, Interval.D1, limit=days)
    if not candles:
        return 0
    turnovers = [c.turnover for c in candles if c.turnover > 0]
    # 腾讯返回的成交额单位是"万"，转成"元"
    turnovers = [t * 10_000 for t in turnovers]
    return sum(turnovers) / len(turnovers) if turnovers else 0


async def _fetch_us_fundamentals(symbol: str) -> Optional[dict]:
    """Yahoo 美股基本面。"""
    url = f"https://query2.finance.yahoo.com/v7/finance/quote"
    params = {
        "symbols": symbol,
        "fields": "marketCap,regularMarketPrice,averageDailyVolume10Day,averageDailyVolume3Month,longName,quoteType,exchange,trailingPE,priceToBook",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        s = await _get_session()
        async with s.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            body = await r.json(content_type=None)
    except Exception as e:
        logger.warning(f"[quality] Yahoo 美股基本面拉取失败 {symbol}: {e}")
        return None
    results = ((body or {}).get("quoteResponse") or {}).get("result") or []
    if not results:
        return None
    r0 = results[0]
    exchange = str(r0.get("exchange") or "")
    is_otc = 1 if "PNK" in exchange or "OTC" in exchange else 0
    return {
        "name": r0.get("longName") or r0.get("shortName") or "",
        "price": _safe_float(r0.get("regularMarketPrice")),
        "market_cap": _safe_float(r0.get("marketCap")),
        "avg_turnover": 0,
        "avg_volume": _safe_float(r0.get("averageDailyVolume10Day")),
        "listed_days": 9999,
        "is_st": 0,
        "is_gem": 0,
        "is_otc": is_otc,
        "pe": _safe_float(r0.get("trailingPE")),
        "pb": _safe_float(r0.get("priceToBook")),
        "turnover_rate": 0,
    }


# ═══════════════════════════════════════════════════════════════════
# 缓存读写
# ═══════════════════════════════════════════════════════════════════


async def _load_cached(db, symbol: str, market: str) -> Optional[dict]:
    """严格 TTL 缓存（用于入池筛选）：超过 POOL_FILTER_CACHE_HOURS 视为过期需重拉。"""
    ttl_sec = config.POOL_FILTER_CACHE_HOURS * 3600
    cutoff = int(time.time()) - ttl_sec
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT * FROM symbol_fundamentals WHERE symbol=? AND market=? AND updated_at>=?",
            (symbol, market, cutoff),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def _load_any(db, symbol: str, market: str, max_stale_days: int = 30) -> Optional[dict]:
    """
    宽松取数（用于评分）：取最近一次的基本面，最多 max_stale_days 天前的数据仍可用。
    基本面（市值/PE/流动性）短期相对稳定，过期数据比 0 强。
    """
    cutoff = int(time.time()) - max_stale_days * 86400
    async with db.acquire() as conn:
        cur = await conn.execute(
            "SELECT * FROM symbol_fundamentals WHERE symbol=? AND market=? AND updated_at>=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (symbol, market, cutoff),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def _save_cached(db, symbol: str, market: str, data: dict):
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT OR REPLACE INTO symbol_fundamentals
               (symbol, market, name, price, market_cap, avg_turnover, avg_volume,
                listed_days, is_st, is_gem, is_otc, pe, pb, turnover_rate, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                symbol, market,
                data.get("name", ""),
                data.get("price", 0) or 0,
                data.get("market_cap", 0) or 0,
                data.get("avg_turnover", 0) or 0,
                data.get("avg_volume", 0) or 0,
                data.get("listed_days", 9999),
                data.get("is_st", 0),
                data.get("is_gem", 0),
                data.get("is_otc", 0),
                data.get("pe", 0) or 0,
                data.get("pb", 0) or 0,
                data.get("turnover_rate", 0) or 0,
                int(time.time()),
            ),
        )
        await conn.commit()


# ═══════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════


_US_ETF_WHITELIST = {
    # 大盘/广基指数
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI",
    # 国债 / 债券
    "TLT", "TBT", "IEF", "SHY", "AGG", "BND", "LQD", "HYG",
    # 大宗 / 黄金 / 白银
    "GLD", "SLV", "GDX", "USO", "DBC", "DBA", "UNG",
    # 美元 / 外汇
    "UUP", "UDN", "FXE", "FXY", "FXB",
    # 波动率
    "VIX", "UVXY", "VXX",
    # 行业 SPDR (XL系列)
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLB", "XLU", "XLRE", "XLC",
    # ARK 系列
    "ARKK", "ARKQ", "ARKW", "ARKG", "ARKF",
    # 区域
    "EEM", "EFA", "FXI", "MCHI", "EWJ", "INDA", "VWO",
    # 杠杆 / 反向
    "TQQQ", "SQQQ", "SOXL", "SOXS",
}


async def is_eligible(db, symbol: str, market: str) -> Tuple[bool, str]:
    """
    判断是否达标入池。返回 (is_ok, reason)。
    market: 'us' | 'hk' | 'cn'
    """
    if not getattr(config, "POOL_FILTER_ENABLED", True):
        return True, "filter-disabled"

    # v12.13: 美股常见 ETF 跳过公司基本面规则（ETF 无 PE/营收，用 quoteSummary 拉不到 → 之前被错杀）
    # 这些 ETF 流动性 + 价格筛选自动通过（macro_impact 推宏观 ETF 是合理路径）
    if market == "us" and symbol.upper() in _US_ETF_WHITELIST:
        return True, "etf-whitelist"

    # 查缓存
    cached = await _load_cached(db, symbol, market)
    data = cached
    if not data:
        if market == "cn":
            data = await _fetch_cn_fundamentals(symbol, db=db)
            if not data:
                # 东方财富被封时（云服务器），回落腾讯实时行情
                data = await _fetch_cn_fundamentals_tencent(symbol, db=db)
                if data:
                    logger.info(f"[quality] {symbol}/cn 东财失败 → 腾讯备源成功")
        elif market == "hk":
            data = await _fetch_hk_fundamentals(symbol)
        elif market == "us":
            data = await _fetch_us_fundamentals(symbol)
            # Yahoo 失败/被限频 → 回落 NASDAQ 公开 API
            if not data:
                try:
                    from backend.data.us_aggregator import fetch_us_fundamentals_nasdaq
                    data = await fetch_us_fundamentals_nasdaq(symbol)
                    if data:
                        logger.info(f"[quality] {symbol} Yahoo 失败 → NASDAQ 回落成功")
                except Exception as e:
                    logger.debug(f"[quality] NASDAQ 回落异常 {symbol}: {e}")
        if not data:
            # 严格策略：拉不到数据 = 拒绝（用户明确要求严格执行）
            # 后台 _pool_rescore_loop 每小时会重试，限频解除后被拒的优质股会再次入池
            logger.info(f"[quality] {symbol}/{market} 基本面拉取失败 → 严格拒绝")
            return False, "数据源全部失败（严格模式拒绝；限频解除后会自动重试）"
        await _save_cached(db, symbol, market, data)

    # 应用阈值
    return _check_thresholds(symbol, market, data)


def _check_thresholds(symbol: str, market: str, d: dict) -> Tuple[bool, str]:
    price = float(d.get("price") or 0)
    cap = float(d.get("market_cap") or 0)
    turn = float(d.get("avg_turnover") or 0)
    vol = float(d.get("avg_volume") or 0)
    if market == "cn":
        if config.POOL_CN_EXCLUDE_ST and d.get("is_st"):
            return False, f"ST/退市({d.get('name')})"
        if (d.get("listed_days") or 9999) < config.POOL_CN_MIN_LISTED_DAYS:
            return False, f"上市仅 {d.get('listed_days')} 天 < {config.POOL_CN_MIN_LISTED_DAYS}"
        if price < config.POOL_CN_MIN_PRICE:
            return False, f"价 {price:.2f} < {config.POOL_CN_MIN_PRICE}"
        if cap < config.POOL_CN_MIN_MARKET_CAP:
            return False, f"流通市值 {cap/1e8:.1f}亿 < {config.POOL_CN_MIN_MARKET_CAP/1e8:.0f}亿"
        if turn < config.POOL_CN_MIN_AVG_TURNOVER:
            return False, f"20日均成交 {turn/1e4:.0f}万 < {config.POOL_CN_MIN_AVG_TURNOVER/1e4:.0f}万"
    elif market == "hk":
        if config.POOL_HK_EXCLUDE_GEM and d.get("is_gem"):
            return False, f"GEM 创业板({d.get('name')})"
        if price < config.POOL_HK_MIN_PRICE:
            return False, f"价 HK${price:.2f} < HK${config.POOL_HK_MIN_PRICE}"
        if cap < config.POOL_HK_MIN_MARKET_CAP:
            return False, f"市值 {cap/1e8:.1f}亿HKD < {config.POOL_HK_MIN_MARKET_CAP/1e8:.0f}亿"
        if turn < config.POOL_HK_MIN_AVG_TURNOVER:
            return False, f"20日均成交 {turn/1e4:.0f}万HKD < {config.POOL_HK_MIN_AVG_TURNOVER/1e4:.0f}万"
    elif market == "us":
        if d.get("is_otc"):
            return False, f"OTC 市场({d.get('name')})"
        if price < config.POOL_US_MIN_PRICE:
            return False, f"价 ${price:.2f} < ${config.POOL_US_MIN_PRICE}"
        if cap < config.POOL_US_MIN_MARKET_CAP:
            return False, f"市值 ${cap/1e6:.0f}M < ${config.POOL_US_MIN_MARKET_CAP/1e6:.0f}M"
        # 流动性检查：严格执行
        # vol=0（NASDAQ 对 NYSE 股返 N/A）→ 也拒绝，进入待审重试
        if vol == 0:
            return False, f"数据源流动性数据缺失(vol=0)，需重试验证"
        if vol < config.POOL_US_MIN_AVG_VOLUME:
            return False, f"10日均量 {vol/1e6:.2f}M股 < {config.POOL_US_MIN_AVG_VOLUME/1e6:.1f}M"
    elif market not in ("cn", "hk", "us"):
        return False, f"不支持的市场类型: {market}"
    return True, "ok"


# ═══════════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════════


def _safe_float(v) -> float:
    try:
        return float(v) if v not in (None, "-", "") else 0
    except (TypeError, ValueError):
        return 0


def _days_since(ymd: str) -> int:
    from datetime import datetime, date
    try:
        dt = datetime.strptime(ymd, "%Y%m%d").date()
        return (date.today() - dt).days
    except ValueError:
        return 9999
