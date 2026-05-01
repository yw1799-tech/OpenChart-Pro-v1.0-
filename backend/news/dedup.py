"""
新闻去重模块（Phase 3A 实现 URL/内容 hash；Phase 3B 增加 SimHash 语义去重）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional


def url_hash(url: str) -> str:
    """URL 规范化后 SHA1 hash。空 URL 返回基于时间+随机的唯一值（防多新闻共享空 hash）。"""
    if not url:
        # 关键：返回唯一占位，避免多个 url 缺失的新闻共享 "" 被去重误删
        import time, random
        return f"empty-{int(time.time()*1000)}-{random.randint(0, 999999)}"
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


def _tokenize(text: str):
    """中英混合粗分词：英文单词 + 中文 2-gram。"""
    if not text:
        return []
    text = re.sub(r"[\s\W_]+", " ", text.lower())
    tokens = []
    # 英文词
    for w in re.findall(r"[a-z0-9]+", text):
        if len(w) >= 2:
            tokens.append(w)
    # 中文 2-gram
    zh = re.findall(r"[\u4e00-\u9fa5]+", text)
    for phrase in zh:
        for i in range(len(phrase) - 1):
            tokens.append(phrase[i:i + 2])
    return tokens


def simhash_text(text: str) -> Optional[str]:
    """
    64-bit SimHash 指纹（hex 字符串 16 位）。
    纯 Python 实现，无外部依赖。
    """
    tokens = _tokenize(text or "")
    if not tokens:
        return None
    bits = [0] * 64
    for tk in tokens:
        h = int(hashlib.md5(tk.encode("utf-8")).hexdigest()[:16], 16)
        for i in range(64):
            if h & (1 << i):
                bits[i] += 1
            else:
                bits[i] -= 1
    fingerprint = 0
    for i in range(64):
        if bits[i] > 0:
            fingerprint |= (1 << i)
    return format(fingerprint, "016x")


def simhash_distance(h1: str, h2: str) -> int:
    """两个 SimHash 指纹的汉明距离。"""
    if not h1 or not h2:
        return 64
    try:
        n1 = int(h1, 16)
        n2 = int(h2, 16)
    except ValueError:
        return 64
    return bin(n1 ^ n2).count("1")


# 相似度阈值：64-bit 汉明距离 ≤ 3 视为"语义重复"（同一事件的不同表述）
SIMHASH_DUP_THRESHOLD = 3
