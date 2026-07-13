"""
data-pipeline/kbd/converter.py — 文件缓存 → 结构化字段

功能：
  1. 从 cache/{support_id}/raw.json 读取原始 API 响应（rows 字段）
  2. 解析 rows.content HTML，提取全部 9 个 section 的内容
  3. 按图片在 HTML 中的出现顺序建立全局 seq 映射（跨 section 统一编号）
  4. 将章节内容语义化（仅取语义文本，丢弃装饰样式），图片位置以 ![img:N] 占位符标记
  5. 组装结构化字典（8 大章节字段 + images_json + 图片二进制），交由 importer 入库
  6. 必填字段（问题描述/有效排查步骤/解决方案）缺失 → 写 abnormal.json，返回 None

设计原则（Content–Presentation Separation）：
  - pipeline 只做语义提取，content_md 不在此生成，交由后端 rebuild_content_md 统一渲染
  - 图片视觉描述（desc）初始留空，由 VISION 阶段（reanalyze）填充到 images_json
  - 不再依赖本地 img_N.desc.txt 文件（该 legacy 机制已于 2026-07 彻底移除）
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .config import settings

logger = logging.getLogger("kbd.converter")

# ─── 五字段规整模板（+ 4 个辅助字段） ────────────────────────────────────────
# KBD 案例统一规整为五个标准字段（必填三 + 可选二）：
#   1. 问题描述（必填）
#   2. 告警信息
#   3. 有效排查步骤（必填）
#   4. 根因
#   5. 解决方案（必填）
#
# 另保留 4 个辅助字段（非必填，供 convert_kbd_structured 写入 kbd_entry 表）：
#   6. 操作影响范围      -> operational_impact
#   7. 是否是临时解决方案 -> is_temporary
#   8. 建议与总结        -> recommendations
#   9. 排查内容          -> 并入 steps_text
#
# 单一数据结构：(标准字段名, 是否必填, [别名列表])
# 别名覆盖原始 HTML input value 的常见变体：
#   - 问题描述：*问题描述 / 问题描述
#   - 告警信息：告警信息
#   - 有效排查步骤：有效排查步骤 / 处理过程（旧版命名）
#   - 根因：根因
#   - 解决方案：*解决方案 / 解决方案
#
# 后缀变体（em-dash U+2014、HTML entity &mdash;）由 _DASH_SUFFIX_RE 归一化处理
_FIELD_CONFIG: list[tuple[str, bool, list[str]]] = [
    # 五字段规整模板（必填三 + 可选二）
    ("问题描述",         True,  ["*问题描述", "问题描述"]),
    ("告警信息",         False, ["告警信息"]),
    ("有效排查步骤",     True,  ["有效排查步骤", "处理过程"]),
    ("根因",             False, ["根因"]),
    ("解决方案",         True,  ["*解决方案", "解决方案"]),
    # 辅助字段（非必填，供结构化入库使用）
    ("操作影响范围",     False, ["操作影响范围"]),
    ("是否是临时解决方案", False, ["是否是临时解决方案"]),
    ("建议与总结",       False, ["建议与总结"]),
    ("排查内容",         False, ["排查内容"]),
]

# 派生视图（保持向后兼容，元组格式：(主别名, 标准字段名, 是否必填)）
_SECTIONS: list[tuple[str, str, bool]] = [
    (aliases[0], field, required) for field, required, aliases in _FIELD_CONFIG
]

# 必填五字段
_MANDATORY_TITLES: frozenset[str] = frozenset(
    md_title for _, md_title, required in _SECTIONS if required
)

# 预编译正则：剥离末尾的 em-dash(U+2014) / en-dash(U+2013) / ASCII hyphen(U+002D)
# BeautifulSoup 已将 &mdash;&mdash; HTML entity 自动解码为 em-dash，无需单独处理
_DASH_SUFFIX_RE = re.compile(r'[\u2014\u2013\-]+$')

# 别名 -> 标准字段 的快速映射
_ALIAS_TO_FIELD: dict[str, str] = {
    alias: field for field, _, aliases in _FIELD_CONFIG for alias in aliases
}


# ─── VISION 描述（desc）来源 ─────────────────────────────────────────────────
# 已废弃：FULL_TEXT 截断已不需要。Vision LLM 后端直接存储完整 desc，由 frontend 按需渲染。
# 新架构：data-pipeline 不再写 images_json.desc，也不再从本地 img_N.desc.txt 读取；
#   images_json.desc 初始留空，由 VISION 阶段（reanalyze）填充。
# legacy 旧路径 convert_kbd / convert_kbd_with_meta / _load_vision_desc 已彻底移除（2026-07）。


# ─── HTML → Markdown 转换器 ──────────────────────────────────────────────────

def _build_image_seq_map(content_html: str) -> dict[str, dict]:
    """
    按图片在 content HTML 中的出现顺序，建立 {绝对URL: {"seq": int}} 映射。
    全 section 统一编号（跨 section），从 0 开始递增。

    注意：仅生成 seq（图片序号），不再读取本地 .desc.txt（legacy 已移除）。
    desc 初始留空，由 VISION 阶段（reanalyze）填充到 images_json。
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
            img_map[abs_url] = {"seq": seq}
            seq += 1
    return img_map


# ─── Section 解析 ────────────────────────────────────────────────────────────


def _match_field(api_val: str) -> str | None:
    """
    input value -> 标准五字段的三级匹配：

    1. 精确匹配：直接命中别名
    2. 归一化匹配：剥离末尾 em-dash/en-dash/hyphen 后缀后命中
       （覆盖 "*问题描述--" 这类带装饰线的旧版命名）
    3. 前缀匹配：兜底，覆盖未在别名表中的变体
    """
    # 1. 精确匹配
    if api_val in _ALIAS_TO_FIELD:
        return _ALIAS_TO_FIELD[api_val]
    # 2. 归一化后匹配
    normalized = _DASH_SUFFIX_RE.sub("", api_val).strip()
    if normalized in _ALIAS_TO_FIELD:
        return _ALIAS_TO_FIELD[normalized]
    # 3. 前缀匹配
    for alias, field in _ALIAS_TO_FIELD.items():
        if api_val.startswith(alias):
            return field
    return None


def _parse_sections(content_html: str) -> dict[str, str]:
    """
    从 rows.content HTML 解析五字段规整模板。

    兼容两种 HTML 结构：
      - Wrapped: <body><div class="case-tinymce-wrap">[mceNonEditable...]</div></body>
      - Flat:    <body>[mceNonEditable, <p>overflow</p>, mceNonEditable, ...]</body>

    内容捕获分三层：
      1. 容器内部最后一个直接子 div（标准内容）
      2. 容器后的 overflow 兄弟节点（直到下一个 mceNonEditable），
         覆盖 Flat 结构中游离的排障正文/截图
      3. 未匹配别名表的 mceNonEditable 容器（如 "判断标准"/"现象确认"等
         非标准命名）作为 overflow 归入上一个匹配的 section，
         保留旧算法的容错行为，避免内容丢失

    返回 {md_title: content_html_str}（未出现的 section key 不在结果中）
    """
    soup = BeautifulSoup(content_html, "lxml")
    mce_divs = soup.find_all("div", class_="mceNonEditable")

    result: dict[str, str] = {}
    last_matched_field: str | None = None

    for i, mce_div in enumerate(mce_divs):
        inp = mce_div.find("input")
        api_val = (inp.get("value") or "").strip() if inp else ""
        md_title = _match_field(api_val) if api_val else None

        if md_title:
            # 匹配成功：开启新 section
            content_divs = mce_div.find_all("div", recursive=False)
            parts: list[str] = [str(content_divs[-1])] if content_divs else []

            # 容器后的 overflow 兄弟节点（直到下一个 mceNonEditable）
            stop_at = mce_divs[i + 1] if i + 1 < len(mce_divs) else None
            sibling = mce_div.next_sibling
            while sibling is not None and sibling is not stop_at:
                if isinstance(sibling, Tag):
                    parts.append(str(sibling))
                sibling = sibling.next_sibling

            result[md_title] = "".join(parts)
            last_matched_field = md_title
        else:
            # 未匹配：作为 overflow 归入上一个匹配 section（保留旧算法容错行为）
            if last_matched_field is not None:
                content_divs = mce_div.find_all("div", recursive=False)
                if content_divs:
                    result[last_matched_field] = (
                        result.get(last_matched_field, "") + str(content_divs[-1])
                    )

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


# ─── 语义提取 + 图片封装（新原则：pipeline 只取语义文本，content_md 由后端统一渲染）──

# 图片扩展名 -> MIME 映射（用于 IMPORT 阶段 images base64 上传）
_IMAGE_MIME_MAP: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}


def _load_image_base64(support_id: str, seq: int) -> dict[str, str] | None:
    """读取本地 cache 图片文件，返回 base64 编码与 MIME 类型。

    用于 IMPORT 阶段将图片二进制随 ingest API 原子写入 kbd_image 表，
    消除 upload_images_to_db 孤儿脚本（pipeline 零直连 DB）。

    Returns:
        {"mime_type": str, "data_base64": str} 或 None（文件缺失）
    """
    kbd_dir = settings.KBD_CACHE_DIR / support_id
    for ext, mime in _IMAGE_MIME_MAP.items():
        img_path = kbd_dir / f"img_{seq}{ext}"
        if img_path.exists():
            data = img_path.read_bytes()
            return {
                "mime_type": mime,
                "data_base64": base64.b64encode(data).decode("ascii"),
            }
    return None


# 块级容器标签集合（_walk 递归遍历）
_BLOCK_TAGS: frozenset[str] = frozenset({
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    "blockquote", "main", "header", "footer", "figure", "details",
    "dl", "dd", "dt", "html", "body",
})
# 结构块标签集合（_inline_text 跳过，由 _walk 专门处理）
_STRUCTURE_TAGS: frozenset[str] = frozenset({"ul", "ol", "table", "pre"})


def _html_to_semantic_text(html: str, image_map: dict[str, dict]) -> str:
    """HTML -> 语义化统一文本（新原则：只取语义，丢装饰样式）。

    输出格式（跨案例高一致性）：
      - 段落/标题 -> 纯文本行，块间空行分隔
      - 列表（ul/ol）-> `- item`，嵌套层级用 2 空格缩进
      - 表格 -> `- 单元格1: 单元格2` 键值列表（单列则直接列项）
      - 图片 -> `![img:N]` 占位符（N 为 image_map 中的全局 seq）
      - 丢弃 script/style/input/a 及颜色/字体/对齐等装饰样式

    与 _html_to_md_with_placeholder 的区别：
      后者用 markdownify 忠实保留原文样式（旧路径/单文档导入用，有测试覆盖）；
      本函数为 pipeline 入库路径做语义归一化，content_md 交由后端
      rebuild_content_md 统一渲染，保证样式高一致。
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []

    def _abs(src: str) -> str:
        return urljoin(settings.SANGFOR_API_BASE, src)

    def _img_placeholder(img_tag: Tag) -> str:
        src = img_tag.get("src") or img_tag.get("data-src") or ""
        if not src or src.startswith("data:"):
            return ""
        entry = image_map.get(_abs(src))
        return f"![img:{entry['seq']}]" if entry is not None else ""

    def _inline_text(node: Tag) -> str:
        """提取节点行内文本 + 图片占位符（跳过结构块子节点）。"""
        parts: list[str] = []
        for sub in node.children:
            if isinstance(sub, str):
                t = sub.strip()
                if t:
                    parts.append(t)
            elif isinstance(sub, Tag):
                if sub.name == "img":
                    ph = _img_placeholder(sub)
                    if ph:
                        parts.append(ph)
                elif sub.name in ("script", "style") or sub.name in _STRUCTURE_TAGS:
                    continue
                else:
                    t = _inline_text(sub)
                    if t:
                        parts.append(t)
        return " ".join(parts)

    def _walk(node: Any, indent: int = 0) -> None:
        prefix = "  " * indent
        for child in node.children:
            if isinstance(child, str):
                t = child.strip()
                if t:
                    out.append(f"{prefix}{t}")
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name in ("script", "style", "input", "a", "br", "meta", "link"):
                continue
            if name == "img":
                ph = _img_placeholder(child)
                if ph:
                    out.append(f"{prefix}{ph}")
                continue
            if name in ("ul", "ol"):
                if out and out[-1] != "":
                    out.append("")
                _walk(child, indent)
                out.append("")
                continue
            if name == "li":
                sublists = child.find_all(["ul", "ol"], recursive=False)
                item_text = _inline_text(child)
                out.append(f"{prefix}- {item_text}".rstrip())
                for sl in sublists:
                    _walk(sl, indent + 1)
                continue
            if name == "table":
                if out and out[-1] != "":
                    out.append("")
                for tr in child.find_all("tr"):
                    cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                    cells = [c for c in cells if c]
                    if not cells:
                        continue
                    if len(cells) == 1:
                        out.append(f"{prefix}- {cells[0]}")
                    else:
                        out.append(f"{prefix}- {cells[0]}: {' '.join(cells[1:])}")
                out.append("")
                continue
            if name == "pre":
                # 代码块：保留换行，每行加缩进（不压平）
                if out and out[-1] != "":
                    out.append("")
                for line in child.get_text().splitlines():
                    out.append(f"{prefix}{line}")
                out.append("")
                continue
            if name in _BLOCK_TAGS:
                has_block = child.find(_BLOCK_TAGS | _STRUCTURE_TAGS)
                if has_block:
                    _walk(child, indent)
                else:
                    text = _inline_text(child)
                    if text:
                        out.append(f"{prefix}{text}")
                continue
            # 其他行内标签：取行内文本
            text = _inline_text(child)
            if text:
                out.append(f"{prefix}{text}")

    _walk(soup)
    result = "\n".join(out)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


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

    # 建立图片序号→{seq}映射（跨 section 统一编号，从 0 开始；desc 由 VISION 阶段填充）
    image_map = _build_image_seq_map(content_html)

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

    # ── 语义提取（新原则：只取语义文本，丢装饰样式） ───────────────────────
    # 章节字段含 ![img:N] 占位符；content_md 不在此生成，交由后端
    # rebuild_content_md 统一渲染（样式高一致）。
    section_texts: dict[str, str] = {}

    for _, md_title, _ in _SECTIONS:
        section_html = sections.get(md_title, "")
        if _is_empty_content(section_html):
            section_texts[md_title] = ""
        else:
            section_texts[md_title] = _html_to_semantic_text(section_html, image_map).strip()

    # ── 构建 8 大章节字段（含占位符） ───────────────────────────────────────
    structured_fields: dict[str, str] = {
        field: "" for field in _MD_TITLE_TO_FIELD.values()
    }
    for md_title, field in _MD_TITLE_TO_FIELD.items():
        structured_fields[field] = section_texts.get(md_title, "")

    # "排查内容" 并入 steps_text（若有内容）
    chacha_html = sections.get("排查内容", "")
    if not _is_empty_content(chacha_html):
        chacha_text = _html_to_semantic_text(chacha_html, image_map).strip()
        if chacha_text:
            existing = structured_fields["steps_text"]
            structured_fields["steps_text"] = (
                existing + "\n\n---\n\n" + chacha_text if existing else chacha_text
            )
            section_texts["排查内容"] = chacha_text

    # ── 构建 images_json + images（图片封装） ──────────────────────────────
    # 通过扫描章节语义文本中的占位符确定每张图的归属章节字段
    seq_to_section: dict[int, str] = {}
    for md_title, field_name in _MD_TITLE_TO_FIELD.items():
        for m in re.finditer(r'!\[img:(\d+)\]', section_texts.get(md_title, "")):
            seq_to_section[int(m.group(1))] = field_name
    # 排查内容归属到 steps_text
    for m in re.finditer(r'!\[img:(\d+)\]', section_texts.get("排查内容", "")):
        seq_to_section[int(m.group(1))] = "steps_text"

    images_json: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []  # 图片二进制（base64），随 ingest 原子写入 kbd_image
    for entry in image_map.values():
        seq = entry["seq"]
        section = seq_to_section.get(seq, "steps_text")
        # desc 初始为空，由 VISION 阶段（reanalyze）填充
        images_json.append({"seq": seq, "section": section, "desc": ""})
        img_b64 = _load_image_base64(support_id, seq)
        if img_b64 is None:
            logger.warning("案例 %s 图片 img_%d 文件缺失，跳过该图入库", support_id, seq)
            continue
        images.append({
            "seq": seq,
            "section": section,
            "mime_type": img_b64["mime_type"],
            "data_base64": img_b64["data_base64"],
        })
    images_json.sort(key=lambda x: x["seq"])
    images.sort(key=lambda x: x["seq"])

    # 章节字段全空校验（避免入库空记录）
    if not any(structured_fields.values()):
        logger.warning("案例 %s 所有 section 转换后均为空", support_id)
        return None

    from .fetcher import _extract_metadata
    return {
        "support_id": support_id,
        "title": title,
        "metadata": _extract_metadata(rows),
        # 8 大章节字段（含 ![img:N] 占位符，语义归一化文本）
        **structured_fields,
        # 结构化工具步骤（空列表，需 admin 后续填充）
        "steps_json": [],
        # 图片视觉描述（结构化，desc 初始空，VISION 阶段填充）
        "images_json": images_json,
        # 图片二进制（base64），IMPORT 阶段原子写入 kbd_image 表
        "images": images,
        # content_md 不传：由后端 rebuild_content_md 统一渲染（样式高一致）
        "content_md": None,
    }
