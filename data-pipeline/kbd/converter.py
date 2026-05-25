"""
data-pipeline/kbd/converter.py — 文件缓存 → 结构化 content_md

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
from typing import Any
from urllib.parse import urljoin

import markdownify
from bs4 import BeautifulSoup, Tag

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

def _desc_to_screenshot_block(desc: str) -> str:
    """
    将 desc.txt 内容（v1 或 v2 格式）转换为 content_md 中的截图块。

    v2 格式（含 BACKGROUND:/TYPE:/FULL_TEXT:/KEY:/TIPS: 行）：
      每行独立成一个 "> {line}" 行，整体以 "> **【截图说明】**" 为首行。

    v1 格式（旧版 0-4 字段）：
      兼容旧格式，首行拼接在 "【截图说明】**：" 后，后续行直接追加。

    Returns:
        content_md 片段，头尾各有空行分隔。
    """
    stripped = desc.strip()
    if not stripped:
        return ""

    lines = stripped.split("\n")
    # 检测 v2 格式：第一行以 BACKGROUND: 开头
    if lines[0].startswith("BACKGROUND:") or lines[0].startswith("TYPE:"):
        # v2：每行独立 ">" 行
        block_lines = ["> **【截图说明】**"]
        for line in lines:
            # 空行处理
            block_lines.append(f"> {line}" if line.strip() else ">")
        return "\n\n" + "\n".join(block_lines) + "\n\n"
    else:
        # v1 兼容：原有行内拼接格式
        return f"\n\n> **【截图说明】**：{stripped}\n\n"


class _HciMarkdownConverter(markdownify.MarkdownConverter):
    """
    定制 Markdown 转换器：
    - 带 data-vision-desc 属性的 span 标签 → 视觉描述引用块
    - 普通 img 标签（未替换）→ [图片]
    """
    def convert_span(self, el: Tag, text: str, parent_tags: set | None = None, **kwargs) -> str:
        desc = el.get("data-vision-desc")
        if desc:
            return _desc_to_screenshot_block(desc)
        return text

    def convert_img(self, el: Tag, text: str, parent_tags: set | None = None, **kwargs) -> str:
        alt = el.get("alt") or ""
        return f"\n\n> **【图片】**：{alt or '[无描述]'}\n\n"


def _load_vision_desc(support_id: str, seq: int) -> str:
    """读取 img_{seq}.desc.txt，不存在或为空返回空字符串"""
    desc_path = settings.KBD_CACHE_DIR / support_id / f"img_{seq}.desc.txt"
    if desc_path.exists():
        return desc_path.read_text(encoding="utf-8").strip()
    return ""


def _build_image_seq_map(support_id: str, content_html: str) -> dict[str, dict]:
    """
    按图片在 content HTML 中的出现顺序，建立 {绝对URL: {"seq": int, "desc": str}} 映射。
    全 section 统一编号（跨 section），从 0 开始递增。
    """
    soup = BeautifulSoup(content_html, "lxml")
    img_map: dict[str, dict] = {}
    seq = 0
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        abs_url = urljoin(settings.SANGFOR_API_BASE, src)
        if abs_url not in img_map:
            desc = _load_vision_desc(support_id, seq)
            img_map[abs_url] = {"seq": seq, "desc": desc}
            seq += 1
    return img_map


def _html_to_md(html: str, image_map: dict[str, dict]) -> str:
    """
    将 section 内容 HTML 转为 Markdown（用于 content_md）：
    - img 标签 → 视觉描述引用块（先替换为自定义 span）
    - 过滤多余空白
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "lxml")

    # 替换 img 为携带 vision_desc 的 span（展开为完整视觉描述块）
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        abs_src = urljoin(settings.SANGFOR_API_BASE, src)
        entry = image_map.get(abs_src)
        desc = entry["desc"] if entry else ""
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


def _html_to_md_with_placeholder(html: str, image_map: dict[str, dict]) -> str:
    """
    将 section 内容 HTML 转为 Markdown（用于章节字段存储）：
    - img 标签 → ![img:N] 占位符（N 为全局图片序号）
    - 不展开视觉描述，视觉描述单独存入 images_json
    - 过滤多余空白
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "lxml")

    # 替换 img 为 ![img:N] 占位符文本节点
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        abs_src = urljoin(settings.SANGFOR_API_BASE, src)
        entry = image_map.get(abs_src)
        if entry is not None:
            placeholder = soup.new_string(f" ![img:{entry['seq']}] ")
        else:
            placeholder = soup.new_string(" [图片] ")
        img.replace_with(placeholder)

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
    return not soup.find("img")


def convert_kbd(support_id: str) -> str | None:
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

    # 必填验证（使用 _SECTIONS 定义，避免重复计算）
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


def convert_kbd_with_meta(support_id: str) -> dict[str, Any] | None:
    """
    转换案例，同时返回元数据（供 importer.py 使用）。

    Returns:
        {
          "support_id": str,
          "title": str,
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

    content_md = convert_kbd(support_id)
    if content_md is None:
        return None

    from .fetcher import _extract_metadata
    return {
        "support_id": support_id,
        "title": title,
        "content_md": content_md,
        "metadata": _extract_metadata(rows),
    }


# Markdown 章节标题 → KbdIngestRequest 字段名映射
_MD_TITLE_TO_FIELD: dict[str, str] = {
    "问题描述":         "problem_description",
    "告警信息":         "alert_info",
    "有效排查步骤":     "steps_text",
    "根因":             "root_cause",
    "解决方案":         "solution",
    "操作影响范围":     "operational_impact",
    "是否是临时解决方案": "is_temporary",
    "建议与总结":       "recommendations",
    # "排查内容" 不映射到独立字段，但会并入 steps_text（见下方注释）
}


def convert_kbd_structured(support_id: str) -> dict[str, Any] | None:
    """
    转换案例，返回结构化字段字典（供 importer.py 调用 /api/kb/kbd/ingest 使用）。

    字段名与 KbdIngestRequest 对齐：
      - problem_description  ← 问题描述（含 ![img:N] 占位符）
      - alert_info           ← 告警信息（含 ![img:N] 占位符）
      - steps_text           ← 有效排查步骤（+ 排查内容，含 ![img:N] 占位符）
      - root_cause           ← 根因（含 ![img:N] 占位符）
      - solution             ← 解决方案（含 ![img:N] 占位符）
      - operational_impact   ← 操作影响范围（含 ![img:N] 占位符）
      - is_temporary         ← 是否是临时解决方案
      - recommendations      ← 建议与总结
      - steps_json           ← [] （空，待 admin 填充）
      - images_json          ← [{"seq": N, "section": field_name, "desc": "..."}]
      - content_md           ← 全部章节聚合 Markdown（含完整视觉描述块，供 LLM 注入）

    图片处理策略（方案 B）：
      章节字段中，图片位置以 ![img:N] 占位符标记（N 为跨章节全局序号）。
      视觉描述独立存储在 images_json 中，保证 admin 编辑章节后视觉信息不丢失。
      content_md 仍包含完整视觉描述块，供 LLM 上下文注入。

    "排查内容" section 说明：
      此字段非 8 大标准章节，内容并入 steps_text 末尾（以 "---" 分隔）。

    Returns:
        dict 或 None（缺少必填 section 时）
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

    # 建立图片序号→{seq, desc}映射（跨 section 统一编号，从 0 开始）
    image_map = _build_image_seq_map(support_id, content_html)

    # 解析 9 个 section 的 HTML
    sections = _parse_sections(content_html)

    # 必填验证
    missing = [
        md_title
        for _, md_title, required in _SECTIONS
        if required and _is_empty_content(sections.get(md_title, ""))
    ]
    if missing:
        _write_abnormal(support_id, title, missing)
        return None

    # ── 两路并行转换 ─────────────────────────────────────────────────────────
    # 路径 A：章节字段（含 ![img:N] 占位符，不含视觉描述文本）
    # 路径 B：content_md（含完整视觉描述块，供 LLM 注入）
    section_mds_placeholder: dict[str, str] = {}   # 路径 A
    section_mds_full: dict[str, str] = {}          # 路径 B

    for _, md_title, _ in _SECTIONS:
        section_html = sections.get(md_title, "")
        if _is_empty_content(section_html):
            section_mds_placeholder[md_title] = ""
            section_mds_full[md_title] = ""
        else:
            section_mds_placeholder[md_title] = _html_to_md_with_placeholder(
                section_html, image_map
            ).strip()
            section_mds_full[md_title] = _html_to_md(section_html, image_map).strip()

    # ── 构建 8 大章节字段（含占位符） ───────────────────────────────────────
    structured_fields: dict[str, str] = {
        field: "" for field in _MD_TITLE_TO_FIELD.values()
    }
    for md_title, field in _MD_TITLE_TO_FIELD.items():
        structured_fields[field] = section_mds_placeholder.get(md_title, "")

    # "排查内容" 并入 steps_text（若有内容）
    chacha_html = sections.get("排查内容", "")
    if not _is_empty_content(chacha_html):
        chacha_placeholder = _html_to_md_with_placeholder(chacha_html, image_map).strip()
        chacha_full = _html_to_md(chacha_html, image_map).strip()
        if chacha_placeholder:
            existing = structured_fields["steps_text"]
            structured_fields["steps_text"] = (
                existing + "\n\n---\n\n" + chacha_placeholder if existing else chacha_placeholder
            )
        if chacha_full:
            section_mds_full["排查内容"] = chacha_full

    # ── 构建 images_json（图片视觉描述结构化列表） ──────────────────────────
    # 按 seq 排序，记录每张图属于哪个章节字段
    # 通过扫描章节占位符文本来确定归属
    images_json: list[dict[str, Any]] = []
    # 构建 seq → section field 的反向映射
    # ── 构建 images_json（图片视觉描述结构化列表） ──────────────────────────
    # 通过扫描章节占位符文本确定每张图的归属章节字段
    seq_to_section: dict[int, str] = {}
    for md_title, field_name in _MD_TITLE_TO_FIELD.items():
        placeholder_text = section_mds_placeholder.get(md_title, "")
        for m in re.finditer(r'!\[img:(\d+)\]', placeholder_text):
            seq_to_section[int(m.group(1))] = field_name
    # 排查内容归属到 steps_text
    chacha_ph = section_mds_placeholder.get("排查内容", "")
    for m in re.finditer(r'!\[img:(\d+)\]', chacha_ph):
        seq_to_section[int(m.group(1))] = "steps_text"

    images_json: list[dict[str, Any]] = []
    for entry in image_map.values():
        images_json.append({
            "seq": entry["seq"],
            "section": seq_to_section.get(entry["seq"], "unknown"),
            "desc": entry["desc"],
        })
    images_json.sort(key=lambda x: x["seq"])

    # ── 聚合 content_md（含完整视觉描述，供 LLM 注入） ──────────────────────
    content_md_parts: list[str] = []
    for _, md_title, _ in _SECTIONS:
        md_text = section_mds_full.get(md_title, "")
        if md_text:
            content_md_parts.append(f"## {md_title}\n\n{md_text}")
    content_md = "\n\n".join(content_md_parts)

    if not content_md:
        logger.warning("案例 %s 所有 section 转换后均为空", support_id)
        return None

    from .fetcher import _extract_metadata
    return {
        "support_id": support_id,
        "title": title,
        "metadata": _extract_metadata(rows),
        # 8 大章节字段（含 ![img:N] 占位符，不含视觉描述文本）
        **structured_fields,
        # 结构化工具步骤（空列表，需 admin 后续填充）
        "steps_json": [],
        # 图片视觉描述（结构化，独立存储）
        "images_json": images_json,
        # 聚合渲染（含完整视觉描述块，供 LLM 上下文注入）
        "content_md": content_md,
    }
