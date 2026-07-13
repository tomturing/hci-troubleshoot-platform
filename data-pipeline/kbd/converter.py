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


# ─── FULL_TEXT 截断策略 ───────────────────────────────────────────────────────

# 截断行数限制（入库展示用）
_MAX_FULL_TEXT_LINES = 10

# 截断保留策略：
# - 日志/终端截图（TYPE: 日志截图/终端截图）：保留最后 N 行（重要信息在末尾）
# - 其他截图（告警/任务/配置等）：保留前 N 行（重要信息在前面）
_TRUNCATE_KEEP_LAST_TYPES = frozenset(["日志截图", "终端截图"])


def _truncate_full_text_for_display(desc: str) -> str:
    """
    对 desc.txt 内容进行 FULL_TEXT 截断处理（仅用于入库展示）。

    截断策略：
    - 日志截图/终端截图：保留最后 10 行
    - 其他截图：保留前 10 行

    原始 desc.txt 文件保持完整（不做修改），此函数仅处理入库展示版本。

    Args:
        desc: 原始 desc.txt 内容

    Returns:
        截断后的 desc 内容（用于 images_json 入库）
    """
    if not desc or "FULL_TEXT:" not in desc:
        return desc

    lines = desc.split("\n")

    # 解析 TYPE
    screenshot_type = ""
    for line in lines:
        if line.startswith("TYPE:"):
            screenshot_type = line.split(":", 1)[1].strip()
            break

    # 找到 FULL_TEXT 区域
    ft_start = -1
    ft_end = -1
    for i, line in enumerate(lines):
        if line.startswith("FULL_TEXT:"):
            ft_start = i + 1  # 跳过 FULL_TEXT: 行本身
        elif ft_start != -1 and (line.startswith("DESCRIPTION:") or line.startswith("═══")):
            ft_end = i
            break

    if ft_start == -1:
        return desc

    # 收集 bullet 行
    bullet_lines = []
    for i in range(ft_start, ft_end if ft_end != -1 else len(lines)):
        line = lines[i]
        if line.startswith("- "):
            bullet_lines.append(line)

    # 不需要截断
    if len(bullet_lines) <= _MAX_FULL_TEXT_LINES:
        return desc

    # 截断处理
    keep_last = screenshot_type in _TRUNCATE_KEEP_LAST_TYPES
    if keep_last:
        # 保留最后 N 行
        kept_bullets = bullet_lines[-_MAX_FULL_TEXT_LINES:]
        truncation_note = f"> （截断：原 {len(bullet_lines)} 行，保留最后 {_MAX_FULL_TEXT_LINES} 行）"
    else:
        # 保留前 N 行
        kept_bullets = bullet_lines[:_MAX_FULL_TEXT_LINES]
        truncation_note = f"> （截断：原 {len(bullet_lines)} 行，保留前 {_MAX_FULL_TEXT_LINES} 行）"

    # 重建 desc
    result_lines = []
    for line in lines:
        if line.startswith("FULL_TEXT:"):
            result_lines.append(line)
            for bullet in kept_bullets:
                result_lines.append(bullet)
            result_lines.append(truncation_note)
            # 跳过原始 bullet 行
            continue
        if ft_start != -1 and ft_end != -1:
            # 检查是否在原始 FULL_TEXT 区域的 bullet 行
            idx = lines.index(line)
            if ft_start <= idx < ft_end and line.startswith("- "):
                continue
        result_lines.append(line)

    return "\n".join(result_lines)


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
        escape_asterisks=False,
        escape_underscores=False,
        escape_misc=False,
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
        escape_asterisks=False,
        escape_underscores=False,
        escape_misc=False,
    ).convert(str(soup))

    # 规范化多余空行
    md = re.sub(r'\n{3,}', '\n\n', md)
    return md.strip()


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
        # 入库展示时对 FULL_TEXT 进行截断处理（原始 desc.txt 保持完整）
        truncated_desc = _truncate_full_text_for_display(entry["desc"])
        # 章节外图片默认归属 steps_text（排查内容/有效排查步骤）
        section = seq_to_section.get(entry["seq"], "steps_text")
        images_json.append({
            "seq": entry["seq"],
            "section": section,
            "desc": truncated_desc,
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
