"""
data-pipeline/kbd/html_utils.py — HTML 图片 URL 提取公共函数

提供统一的"提取图片 URL + 去重保序"逻辑，供 fetcher 和 image_proc 共用。
避免两侧规则不一致导致序号错位。

设计原则：
  - 单次遍历 img 标签，同时产出 URL 和位置信息
  - 使用原始 src 字符串定位，避免相对路径/query/文件名重复问题
  - extract_image_urls() 是 extract_image_urls_with_positions() 的薄封装
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup  # type: ignore[import-untyped]


def extract_image_urls_with_positions(html: str, base_url: str) -> list[tuple[str, int]]:
    """
    从 HTML 中提取图片 URL 及其首次出现的字符位置，去重保序。

    单次遍历所有 <img> 标签，同时产出 URL 和位置，确保序号一致性。
    使用原始 src 字符串在 HTML 中定位，避免相对路径/query/文件名重复问题。

    提取规则（唯一权威来源）：
      1. BeautifulSoup lxml 解析
      2. 查找所有 <img> 标签（按 HTML 顺序）
      3. 提取 src 或 data-src 属性
      4. 跳过 data: 协议（内嵌图片）
      5. urljoin 解析绝对 URL
      6. 去重保序（seen set）
      7. 使用原始 src 字符串在原始 HTML 中定位（re.escape + re.search）

    Args:
        html:     HTML 内容字符串
        base_url: 基础 URL，用于解析相对 src

    Returns:
        [(abs_url, char_pos), ...] 去重后按出现顺序排列
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    result: list[tuple[str, int]] = []

    for img in soup.find_all("img"):
        # 获取原始 src（可能是相对路径，带 query，或 data-src）
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue

        abs_url = urljoin(base_url, src)
        if abs_url in seen:
            continue
        seen.add(abs_url)

        # 在原始 HTML 中找该 src 首次出现的字符位置
        # 使用原始 src 定位（而非 abs_url），避免相对路径匹配失败
        escaped_src = re.escape(src)
        m = re.search(escaped_src, html)
        pos = m.start() if m else 0

        result.append((abs_url, pos))

    return result


def extract_image_urls(html: str, base_url: str) -> list[str]:
    """
    从 HTML 中提取所有 img src，解析为绝对 URL，去重保序。

    基于 extract_image_urls_with_positions() 实现，确保规则一致。
    仅返回 URL 列表，供不需要位置信息的场景使用。

    Args:
        html:     HTML 内容字符串
        base_url: 基础 URL，用于解析相对 src

    Returns:
        URL 列表，按 HTML 中出现顺序排列，已去重
    """
    return [url for url, _pos in extract_image_urls_with_positions(html, base_url)]
