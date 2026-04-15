"""
新闻去重模块（Phase 3A 实现 URL/内容 hash；Phase 3B 增加 SimHash 语义去重）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional


def url_hash(url: str) -> str:
    """URL 规范化后 SHA1 hash。"""
    if not url:
        return ""
    # 去掉常见追踪参数
    u = re.sub(r"[?&](utm_[^&]+|ref=[^&]+|fbclid=[^&]+)", "", url)
    u = u.rstrip("/?&#")
    return hashlib.sha1(u.lower().encode("utf-8")).hexdigest()


def content_hash(title: str, content: str = "") -> str:
    """
    标题 + 正文摘要的 SHA1 hash。
    用于"完全相同的转载"去重。
    """
    text = (title or "").strip() + "|" + (content or "")[:500].strip()
    # 去除空白和标点差异
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[，。！？、；：（）【】《》""'']+", "", text)
    return hashlib.sha1(text.lower().encode("utf-8")).hexdigest()


def make_news_id(source: str, title: str, published_at: int) -> str:
    """
    生成新闻唯一 ID（用于 DB 主键）。
    格式：{source 缩写}-{title hash 前 16 位}-{published_at}
    """
    src_short = re.sub(r"[^a-zA-Z0-9]", "", source)[:8].lower()
    title_h = hashlib.sha1((title or "").encode("utf-8")).hexdigest()[:16]
    return f"{src_short}-{title_h}-{published_at}"


# ───────── Phase 3B 占位：SimHash 语义去重 ─────────


def simhash_text(text: str) -> Optional[str]:
    """
    SimHash 64-bit 指纹（Phase 3B 用 simhash 库实现）。
    Phase 3A 暂不启用，返回 None。
    """
    return None


def simhash_distance(h1: str, h2: str) -> int:
    """
    计算两个 SimHash 指纹的汉明距离（Phase 3B 用）。
    """
    if not h1 or not h2:
        return 64
    n1 = int(h1, 16)
    n2 = int(h2, 16)
    return bin(n1 ^ n2).count("1")
