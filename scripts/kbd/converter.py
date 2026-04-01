"""
scripts/kbd/converter.py — 文件缓存 → 结构化 content_md

功能：
  1. 从 cache/{support_id}/raw.json 读取原始 API 响应（rows 字段）
  2. 解析 rows.content HTML，提取全部 9 个 section 的内容
  3. 从 cache/{support_id}/img_N.desc.txt 读取 Vision 描述
  4. 将 img 标签替换为视觉描述块，转换为 Markdown
  5. 组装单个 content_md 字符串
  6. 必填字段（问题描述/有效排查步骤/解决方案）缺失 → 写 abnormal.json，返回 None

Markdown 格式约定：
  ## 问题描述
  ...
  > **【截图说明】**：{vision_desc}
  ...
  （其余 section 相同格式）

Vision 描述文件查找规则：
  - 按图片在 HTML 中的出现顺序编号：img_0, img_1, ...
  - 跨所有 section 统一编号
  - 描述文件：cache/{support_id}/img_N.desc.txt
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import markdownify
from bs4 import BeautifulSoup, NavigableString, Tag

from .config import settings

logger = logging.getLogger("kbd.converter")

# ─── 9 个 Section 定义 ───────────────────────────────────────────────────────

# (API input.value, Markdown 标题, 是否必填)
_SECTIONS: list[tuple[str, str, bool]] = [
    ("*问题描述",         "问题描述",         True),
    ("告警信息",           "告警信息",         False),
    ("有效排查步骤",       "有效排查步骤",     True),
    ("根因",               "根因",             False),
    ("*解决方案",          "解决方案",         True),
    ("操作影响范围",       "操作影响范围",      False),
    ("是否是临时解决方案", "是否是临时解决方案", False),
    ("建议与总结",         "建议与总结",        False),
    ("排查内容",           "排查内容",          False),
]

# 必填 section Markdown 标题的快速查找集合
_MANDATORY_TITLES: frozenset[str] = frozenset(
    md_title for _, md_title, required in _SECTIONS if required
)


# ─── HTML → Markdown 转换器 ──────────────────────────────────────────────────

class _HciMarkdownConverter(markdownify.MarkdownConverter):
    """
    定制 Markdown 转换器：
    - 带 data-vision-desc 属性的 span 标签 → 视觉描述引用块
    - 普通 img 标签（未替换）→ [图片]
    """
    def convert_span(self, el: Tag, text: str, parent_tags: "set | None" = None, **kwargs) -> str:
        desc = el.get("data-vision-desc")
        if desc:
            return f"\n\n> **【截图说明】**：{desc}\n\n"
        return text

    def convert_img(self, el: Tag, text: str, parent_tags: "set | None" = None, **kwargs) -> str:
        alt = el.get("alt") or ""
        return f"\n\n> **【图片】**：{alt or '[无描述]'}\n\n"


def _load_vision_desc(support_id: str, seq: int) -> str:
    """读取 img_{seq}.desc.txt，不存在或为空返回空字符串"""
    desc_path = settings.KBD_CACHE_DIR / support_id / f"img_{seq}.desc.txt"
    if desc_path.exists():
        return desc_path.read_text(encoding="utf-8").strip()
    return ""


def _build_image_seq_map(support_id: str, content_html: str) -> dict[str, str]:
    """
    按图片在 content HTML 中的出现顺序，建立 {绝对URL: vision_desc} 映射。
    全 section 统一编号（跨 section）。
    """
    soup = BeautifulSoup(content_html, "lxml")
    img_map: dict[str, str] = {}
    seq = 0
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        abs_url = urljoin(settings.SANGFOR_API_BASE, src)
        if abs_url not in img_map:
            desc = _load_vision_desc(support_id, seq)
            img_map[abs_url] = desc
            seq += 1
    return img_map


def _html_to_md(html: str, image_map: dict[str, str]) -> str:
    """
    将 section 内容 HTML 转为 Markdown：
    - img 标签 → 视觉描述引用块（先替换为自定义 span）
    - 过滤多余空白
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "lxml")

    # 替换 img 为携带 vision_desc 的 span
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        abs_src = urljoin(settings.SANGFOR_API_BASE, src)
        desc = image_map.get(abs_src) or ""
        if desc:
            span = soup.new_tag("span", attrs={"data-vision-desc": desc})
        else:
            # 无描述：保留为普通 img（转换器会输出 [图片]）
            span = img  # 不替换，让转换器处理
            continue
        img.replace_with(span)

    md = _HciMarkdownConverter(
        heading_style=markdownify.ATX,
        bullets="-",
        strip=["script", "style", "input", "a"],
    ).convert(str(soup))

    # 规范化多余空行
    md = re.sub(r'\n{3,}', '\n\n', md)
    return md.strip()


# ─── Section 解析 ────────────────────────────────────────────────────────────

def _parse_sections(content_html: str) -> dict[str, str]:
    """
    从 rows.content HTML 解析 9 个 section 的 HTML 内容。

    返回 {md_title: content_html_str}（未出现的 section key 不在结果中）

    DOM 结构：
      div.mceNonEditable
        input[value="*问题描述"]（section 标题）
        a（锚点，忽略）
        div（content，取最后一个直接子 div）
    """
    soup = BeautifulSoup(content_html, "lxml")

    # 建立 API input.value → md_title 快速映射
    value_to_md: dict[str, str] = {
        api_val: md_title for api_val, md_title, _ in _SECTIONS
    }

    result: dict[str, str] = {}

    for wrapper_div in soup.find_all("div", class_="mceNonEditable"):
        inp = wrapper_div.find("input")
        if not inp:
            continue
        api_val = (inp.get("value") or "").strip()
        md_title = value_to_md.get(api_val)
        if not md_title:
            continue  # 未知 section，跳过

        # 取最后一个直接子 div 作为内容区
        content_divs = wrapper_div.find_all("div", recursive=False)
        if not content_divs:
            result[md_title] = ""
            continue
        content_div = content_divs[-1]
        result[md_title] = str(content_div)

    return result


# ─── 异常队列写入 ────────────────────────────────────────────────────────────

def _write_abnormal(support_id: str, title: str, missing: list[str]) -> None:
    """将缺少必填 section 的案例写入 abnormal.json"""
    abnormal_path = settings.KBD_CACHE_DIR / support_id / "abnormal.json"
    record = {
        "support_id": support_id,
        "title": title,
        "missing_sections": missing,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    abnormal_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.warning(
        "案例 %s 缺少必填 section %s，已写入 abnormal.json",
        support_id, missing,
    )


# ─── 主入口 ──────────────────────────────────────────────────────────────────


def _is_empty_content(html: str) -> bool:
    """判断 section HTML 内容是否为空（空白文本、空标签等，但含图片则不视为空）"""
    if not html or not html.strip():
        return True
    soup = BeautifulSoup(html, "lxml")
    if soup.get_text(strip=True):
        return False
    # 有图片也不视为空（图片本身是内容）
    if soup.find("img"):
        return False
    return True


def convert_case(support_id: str) -> str | None:
    """
    从文件缓存读取案例，转换为结构化 content_md 字符串。

    Returns:
        content_md 字符串，或 None（缺少必填 section 时）

    Side-effects:
        缺少必填 section → 写 cache/{support_id}/abnormal.json
    """
    raw_path = settings.KBD_CACHE_DIR / support_id / "raw.json"
    if not raw_path.exists():
        logger.warning("案例 %s raw.json 不存在，跳过转换", support_id)
        return None

    rows: dict[str, Any] = json.loads(raw_path.read_text(encoding="utf-8"))
    title: str = rows.get("name") or rows.get("title") or f"案例 {support_id}"
    content_html: str = rows.get("content") or ""

    if not content_html.strip():
        logger.warning("案例 %s content 为空，跳过转换", support_id)
        _write_abnormal(support_id, title, ["*全部（content 为空）"])
        return None

    # 建立图片序号→Vision描述映射（跨 section 统一编号）
    image_map = _build_image_seq_map(support_id, content_html)

    # 解析 9 个 section
    sections = _parse_sections(content_html)

    # 必填验证
    missing = [
        f"*{s}" if not s.startswith("*") else s
        for s in _MANDATORY_TITLES
        if not sections.get(s, "").strip()
           or sections.get(s, "").strip() == "<div></div>"
    ]
    missing = [
        md_title
        for _, md_title, required in _SECTIONS
        if required and _is_empty_content(sections.get(md_title, ""))
    ]

    if missing:
        _write_abnormal(support_id, title, missing)
        return None

    # 组装 content_md
    parts: list[str] = []
    for _, md_title, _ in _SECTIONS:
        section_html = sections.get(md_title, "")
        if _is_empty_content(section_html):
            continue  # 空 section 不写入
        section_md = _html_to_md(section_html, image_map)
        if section_md.strip():
            parts.append(f"## {md_title}\n\n{section_md}")

    if not parts:
        logger.warning("案例 %s 所有 section 转换后均为空", support_id)
        return None

    content_md = "\n\n".join(parts)
    return content_md


def convert_case_with_meta(support_id: str) -> dict[str, Any] | None:
    """
    转换案例，同时返回元数据（供 importer.py 使用）。

    Returns:
        {
          "support_id": str,
          "title": str,
          "support_url": str,
          "content_md": str,
          "metadata": dict,
        }
        或 None（缺少必填 section 时）
    """
    raw_path = settings.KBD_CACHE_DIR / support_id / "raw.json"
    if not raw_path.exists():
        return None

    rows: dict[str, Any] = json.loads(raw_path.read_text(encoding="utf-8"))
    title: str = rows.get("name") or rows.get("title") or f"案例 {support_id}"

    content_md = convert_case(support_id)
    if content_md is None:
        return None

    from .fetcher import _extract_metadata, _make_support_url
    return {
        "support_id": support_id,
        "title": title,
        "support_url": _make_support_url(support_id),
        "content_md": content_md,
        "metadata": _extract_metadata(rows),
    }
