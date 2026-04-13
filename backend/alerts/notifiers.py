"""
通知分发模块。
支持 WebSocket 推送、Webhook（企业微信/钉钉/Telegram/Discord/自定义）、提示音。
"""

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import aiohttp

from backend.data.models import Alert

logger = logging.getLogger(__name__)


class NotifierBase(ABC):
    """通知器基类。"""

    @abstractmethod
    async def send(self, alert: Dict[str, Any], message: str) -> bool:
        """
        发送通知。

        Args:
            alert: 警报数据字典。
            message: 触发消息文本。

        Returns:
            是否发送成功。
        """
        ...


class WebSocketNotifier(NotifierBase):
    """通过 WebSocket 推送警报到前端浏览器。"""

    async def send(self, alert: Dict[str, Any], message: str) -> bool:
        try:
            from backend.ws.hub import hub

            alert_data = {
                "type": "alert_triggered",
                "alert_id": alert.get("id", ""),
                "symbol": alert.get("symbol", ""),
                "market": alert.get("market", ""),
                "condition_type": alert.get("condition_type", ""),
                "label": alert.get("label", ""),
                "message": message,
                "timestamp": int(time.time()),
            }
            await hub.broadcast_alert(alert_data)
            logger.info(f"WebSocket 通知已推送: {alert.get('symbol')} - {message}")
            return True
        except Exception as e:
            logger.error(f"WebSocket 通知发送失败: {e}")
            return False


class SoundNotifier(NotifierBase):
    """通过 WebSocket 通知前端播放提示音。"""

    async def send(self, alert: Dict[str, Any], message: str) -> bool:
        try:
            from backend.ws.hub import hub

            sound_data = {
                "type": "alert_sound",
                "alert_id": alert.get("id", ""),
                "symbol": alert.get("symbol", ""),
                "message": message,
                "sound": "alert",  # 前端根据此字段播放对应音效
                "timestamp": int(time.time()),
            }
            await hub.broadcast_alert(sound_data)
            logger.info(f"提示音通知已推送: {alert.get('symbol')}")
            return True
        except Exception as e:
            logger.error(f"提示音通知发送失败: {e}")
            return False


class WebhookNotifier(NotifierBase):
    """
    POST 到配置的 Webhook URL。
    支持平台：企业微信、钉钉、Telegram、Discord、自定义 HTTP POST。
    """

    def __init__(self, webhook_urls: Optional[List[str]] = None):
        self.webhook_urls = webhook_urls or []

    async def send(self, alert: Dict[str, Any], message: str) -> bool:
        if not self.webhook_urls:
            logger.debug("未配置 Webhook URL，跳过")
            return False

        symbol = alert.get("symbol", "")
        label = alert.get("label", "") or alert.get("condition_type", "")
        full_message = f"[{symbol}] {label}: {message}"
        timestamp = int(time.time())

        success_count = 0
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            for url in self.webhook_urls:
                try:
                    payload = self._format_payload(url, full_message, alert, timestamp)
                    headers = {"Content-Type": "application/json"}

                    async with session.post(url, json=payload, headers=headers) as resp:
                        if resp.status < 300:
                            success_count += 1
                            logger.info(f"Webhook 发送成功: {url} (status={resp.status})")
                        else:
                            body = await resp.text()
                            logger.warning(f"Webhook 返回异常: {url} status={resp.status} body={body[:200]}")
                except Exception as e:
                    logger.error(f"Webhook 发送失败: {url} -> {e}")

        return success_count > 0

    def _format_payload(self, url: str, message: str, alert: Dict[str, Any], timestamp: int) -> Dict[str, Any]:
        """根据 URL 特征自动适配不同平台的消息格式。"""
        url_lower = url.lower()

        # 企业微信
        if "qyapi.weixin.qq.com" in url_lower:
            return {
                "msgtype": "text",
                "text": {"content": message},
            }

        # 钉钉
        if "oapi.dingtalk.com" in url_lower:
            return {
                "msgtype": "text",
                "text": {"content": message},
            }

        # Telegram Bot API
        if "api.telegram.org" in url_lower:
            # URL 格式: https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>
            return {
                "text": message,
                "parse_mode": "HTML",
            }

        # Discord
        if "discord.com/api/webhooks" in url_lower or "discordapp.com/api/webhooks" in url_lower:
            return {
                "content": message,
                "embeds": [
                    {
                        "title": f"警报触发: {alert.get('symbol', '')}",
                        "description": message,
                        "color": 16744576,  # 橙色
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp)),
                    }
                ],
            }

        # 通用格式
        return {
            "event": "alert_triggered",
            "alert_id": alert.get("id", ""),
            "symbol": alert.get("symbol", ""),
            "market": alert.get("market", ""),
            "condition_type": alert.get("condition_type", ""),
            "message": message,
            "timestamp": timestamp,
        }


# ──────────────────────── 通知分发 ────────────────────────

# 通知方式 -> 通知器实例（延迟初始化）
_notifier_instances: Dict[str, NotifierBase] = {}


def _get_notifier(method: str, webhook_urls: Optional[List[str]] = None) -> Optional[NotifierBase]:
    """获取或创建通知器实例。"""
    if method in _notifier_instances:
        return _notifier_instances[method]

    notifier: Optional[NotifierBase] = None
    if method == "browser":
        notifier = WebSocketNotifier()
    elif method == "sound":
        notifier = SoundNotifier()
    elif method == "webhook":
        notifier = WebhookNotifier(webhook_urls=webhook_urls)
    else:
        logger.warning(f"未知的通知方式: {method}")
        return None

    _notifier_instances[method] = notifier
    return notifier


def configure_webhook_urls(urls: List[str]):
    """动态配置 Webhook URL 列表。"""
    notifier = _get_notifier("webhook", webhook_urls=urls)
    if isinstance(notifier, WebhookNotifier):
        notifier.webhook_urls = urls
    _notifier_instances["webhook"] = WebhookNotifier(webhook_urls=urls)


async def dispatch_notification(
    alert: Dict[str, Any],
    message: str,
    notify_methods: List[str],
    webhook_urls: Optional[List[str]] = None,
) -> Dict[str, bool]:
    """
    根据 notify_methods 列表分发通知到对应通知器。

    Args:
        alert: 警报数据字典。
        message: 触发消息文本。
        notify_methods: 通知方式列表，如 ["browser", "sound", "webhook"]。
        webhook_urls: Webhook URL 列表（可选，用于 webhook 方式）。

    Returns:
        各通知方式的发送结果 {method: success_bool}。
    """
    results: Dict[str, bool] = {}

    for method in notify_methods:
        notifier = _get_notifier(method, webhook_urls=webhook_urls)
        if notifier is None:
            results[method] = False
            continue
        try:
            success = await notifier.send(alert, message)
            results[method] = success
        except Exception as e:
            logger.error(f"通知分发异常 [{method}]: {e}")
            results[method] = False

    return results
