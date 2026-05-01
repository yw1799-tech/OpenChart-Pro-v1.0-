"""
Telegram 自动交易事件推送（Bot API）。

需要：
  - TELEGRAM_ENABLED=true
  - TELEGRAM_BOT_TOKEN（找 @BotFather 创建 bot 获取，形如 123456:AAA-BBB...）
  - TELEGRAM_CHAT_ID（先和你的 bot 发一条 /start，再访问
    https://api.telegram.org/bot<TOKEN>/getUpdates 找 result[].message.chat.id）

行为：
  - fire-and-forget：发送失败不阻断主交易流程
  - 节流：60 秒内同 (symbol, action) 最多发 1 条
  - 复用模块级 aiohttp session
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

import aiohttp

import backend.config as config

logger = logging.getLogger(__name__)

_throttle: Dict[tuple, float] = {}
_THROTTLE_SEC = 60

_SESSION: Optional[aiohttp.ClientSession] = None


async def _get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=5, ttl_dns_cache=300),
            timeout=aiohttp.ClientTimeout(total=10),
        )
    return _SESSION


async def close_session():
    global _SESSION
    if _SESSION and not _SESSION.closed:
        await _SESSION.close()
        _SESSION = None


def _format_trade_message(action: str, symbol: str, market: str,
                          qty: float, price: float, amount_usd: float,
                          reason: str = "", extra: Dict = None) -> str:
    """v12.13 恢复精简版（持仓+池总览改为独立 4h 推送 _position_summary_loop）。"""
    ACTION_LABEL = {
        "open": "📥 开仓", "add": "➕ 加仓",
        "reduce": "➖ 减仓", "close": "🏁 平仓",
    }
    MKT_LABEL = {"crypto": "加密", "us": "美股", "hk": "港股", "cn": "A股"}
    title = ACTION_LABEL.get(action, action)
    mkt = MKT_LABEL.get(market, market)
    lines = [
        f"🤖 OpenChart Pro 自动交易",
        f"{title}  {symbol} ({mkt})",
        f"数量: {qty:.4f}",
        f"价格: {price:.4f}",
        f"金额: ${amount_usd:.2f}",
    ]
    extra = extra or {}
    if extra.get("realized_pnl_usd") is not None:
        pnl = extra["realized_pnl_usd"]
        pct = extra.get("realized_pnl_pct", 0)
        sign = "+" if pnl >= 0 else ""
        emoji = "📈" if pnl >= 0 else "📉"
        lines.append(f"{emoji} 整单盈亏: {sign}${pnl:.2f} ({sign}{pct:.2f}%)")
    if reason:
        lines.append(f"理由: {reason[:160]}")
    return "\n".join(lines)


async def send_summary(text: str) -> Optional[str]:
    """v12.13: 提供给 _position_summary_loop 用的通用发送函数（不走交易事件节流）。"""
    if not getattr(config, "TELEGRAM_ENABLED", False):
        return "disabled"
    try:
        return await _send(text)
    except Exception as e:
        logger.warning(f"[telegram-summary] 发送失败: {type(e).__name__}: {e}")
        return f"{type(e).__name__}: {e}"


async def _send(text: str) -> Optional[str]:
    token = getattr(config, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("[telegram] TOKEN 或 CHAT_ID 未配置")
        return "missing_token_or_chat_id"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # v11.5: 改用纯文本（disable parse_mode）—— 不再依赖 Markdown，避免 reason 含 _*[]() 被 400 拒
    # 不影响实用性，emoji 已经够清晰；想要粗体可以改回 HTML mode 并 escape
    body = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    s = await _get_session()
    async with s.post(url, json=body) as r:
        resp_text = await r.text()
        if r.status >= 400:
            logger.warning(f"[telegram] HTTP {r.status}: {resp_text[:200]}")
            return f"http_{r.status}: {resp_text[:120]}"
        logger.info(f"[telegram] 发送成功 chat_id={chat_id}")
        return None


async def send_trade_event(
    action: str, symbol: str, market: str,
    qty: float, price: float, amount_usd: float,
    reason: str = "", extra: Dict = None,
):
    if not getattr(config, "TELEGRAM_ENABLED", False):
        return

    now = time.time()
    key = (symbol, action)
    if key in _throttle and now - _throttle[key] < _THROTTLE_SEC:
        logger.debug(f"[telegram] {symbol}/{action} 节流期内，跳过")
        return
    _throttle[key] = now
    if len(_throttle) > 200:
        cutoff = now - _THROTTLE_SEC
        for k in list(_throttle.keys()):
            if _throttle[k] < cutoff:
                del _throttle[k]

    text = _format_trade_message(action, symbol, market, qty, price, amount_usd, reason, extra)
    try:
        await _send(text)
    except Exception as e:
        logger.warning(f"[telegram] 发送失败: {type(e).__name__}: {e}")


async def send_test_message() -> Dict:
    if not getattr(config, "TELEGRAM_ENABLED", False):
        return {"ok": False, "error": "TELEGRAM_ENABLED=False，请先开启"}
    if not getattr(config, "TELEGRAM_BOT_TOKEN", ""):
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN 未配置"}
    if not getattr(config, "TELEGRAM_CHAT_ID", ""):
        return {"ok": False, "error": "TELEGRAM_CHAT_ID 未配置"}
    text = "🧪 OpenChart Pro 测试消息\n\nTelegram 通知配置成功，未来开仓/加仓/减仓/平仓事件将推送到此对话。"
    try:
        err = await _send(text)
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "chat_id": config.TELEGRAM_CHAT_ID}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
