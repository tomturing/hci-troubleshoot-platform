"""
scripts/kbd/html_utils.py — HTML 图片 URL 提取公共函数

提供统一的"提取图片 URL + 去重保序"逻辑，供 fetcher 和 image_proc 共用。
避免两侧规则不一致导致序号错位。
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup  # type: ignore[import-untyped]


def extract_image_urls(html: str, base_url: str) -> list[str]:
    """
    从 HTML 中提取所有 img src，解析为绝对 URL，去重保序。

    提取规则（唯一权威来源）：
      1. BeautifulSoup lxml 解析
      2. 查找所有 <img> 标签
      3. 提取 src 或 data-src 属性
      4. 跳过 data: 协议（内嵌图片）
      5. urljoin 解析绝对 URL
      6. 去重保序（seen set + urls list）

    Args:
        html:     HTML 内容字符串
        base_url: 基础 URL，用于解析相对 src

    Returns:
        URL 列表，按 HTML 中出现顺序排列，已去重
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    urls: list[str] = []

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        abs_url = urljoin(base_url, src)
        if abs_url not in seen:
            seen.add(abs_url)
            urls.append(abs_url)

    return urls


def extract_image_urls_with_positions(html: str, base_url: str) -> list[tuple[str, int]]:
    """
    从 HTML 中提取图片 URL 及其首次出现的字符位置，去重保序。

    基于 extract_image_urls() 公共函数，确保序号一致。
    在公共函数返回的 URL 序列基础上，计算每个 URL 在原始 HTML 中的字符位置。

    Args:
        html:     HTML 内容字符串
        base_url: 基础 URL，用于解析相对 src

    Returns:
        [(abs_url, char_pos), ...] 去重后按出现顺序排列
    """
    # 使用公共函数获取 URL 序列（确保去重保序规则一致）
    urls = extract_image_urls(html, base_url)

    # 在原始 HTML 中找每个 URL 对应 src 的首次出现位置
    result: list[tuple[str, int]] = []
    for abs_url in urls:
        # src 可能是相对路径或绝对路径，需要从 abs_url 反推或在 HTML 中搜索
        # 简化：在 HTML 中搜索 src 原始值（可能是相对路径）
        # 通过解析 abs_url 的 path 部分，在 HTML 中匹配
        from urllib.parse import urlparse
        parsed = urlparse(abs_url)
        path_suffix = parsed.path.split("/")[-1] if parsed.path else ""

        # 尝试匹配 src 属性值（相对或绝对）
        # 先尝试完整路径，再尝试文件名
        patterns = [abs_url, path_suffix]
        pos = 0
        for pattern in patterns:
            if pattern:
                m = re.search(re.escape(pattern), html)
                if m:
                    pos = m.start()
                    break
        result.append((abs_url, pos))

    return result