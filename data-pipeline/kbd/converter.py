"""
data-pipeline/kbd/converter.py — 文件缓存 → 结构化字段

功能：
  1. 从 cache/{support_id}/raw.json 读取原始 API 响应（rows 字段）
  2. 解析 rows.content HTML，提取全部 9 个 section 的内容
  3. 按图片在 HTML 中的出现顺序建立全局 seq 映射（跨 section 统一编号）
  4. 将章节内容语义化（仅取语义文本，丢弃装饰样式），图片位置以 ![img:N] 占位符标记
  5. 组装结构化字典（8 大章节字段 + Evidence 上下文 + 图片二进制），交由 importer 入库
  6. 必填字段（问题描述/有效排查步骤/解决方案）缺失 → 写 abnormal.json，返回 None

设计原则（Content–Presentation Separation）：
  - pipeline 只做语义提取，content_md 不在此生成，交由后端 rebuild_content_md 统一渲染
  - 图片前后文在 IMPORT 阶段从章节语义文本中确定性提取并持久化，VISION 不再反向解析 content_md
  - 图片视觉描述（desc）初始留空，由 VISION 阶段（reanalyze）填充到 images_json
  - 不再依赖本地 img_N.desc.txt 文件（该 legacy 机制已于 2026-07 彻底移除）
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from prometheus_client import Counter

from .config import settings
from .observability import get_trace_id

logger = logging.getLogger("kbd.converter")

_SEMANTIC_CONVERSIONS = Counter(
    "kbd_semantic_conversion_total",
    "KBD HTML 语义转换次数",
    ("status",),
)
_SEMANTIC_INTEGRITY_FAILURES = Counter(
    "kbd_semantic_integrity_failure_total",
    "KBD HTML 语义转换完整性失败次数",
    ("kind",),
)

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
    ("问题描述", True, ["*问题描述", "问题描述"]),
    ("告警信息", False, ["告警信息"]),
    ("有效排查步骤", True, ["有效排查步骤", "处理过程"]),
    ("根因", False, ["根因"]),
    ("解决方案", True, ["*解决方案", "解决方案"]),
    # 辅助字段（非必填，供结构化入库使用）
    ("操作影响范围", False, ["操作影响范围"]),
    ("是否是临时解决方案", False, ["是否是临时解决方案"]),
    ("建议与总结", False, ["建议与总结"]),
    ("排查内容", False, ["排查内容"]),
]

# 派生视图（保持向后兼容，元组格式：(主别名, 标准字段名, 是否必填)）
_SECTIONS: list[tuple[str, str, bool]] = [(aliases[0], field, required) for field, required, aliases in _FIELD_CONFIG]

# 必填五字段
_MANDATORY_TITLES: frozenset[str] = frozenset(md_title for _, md_title, required in _SECTIONS if required)

# 预编译正则：剥离末尾的 em-dash(U+2014) / en-dash(U+2013) / ASCII hyphen(U+002D)
# BeautifulSoup 已将 &mdash;&mdash; HTML entity 自动解码为 em-dash，无需单独处理
_DASH_SUFFIX_RE = re.compile(r"[\u2014\u2013\-]+$")

# 别名 -> 标准字段 的快速映射
_ALIAS_TO_FIELD: dict[str, str] = {alias: field for field, _, aliases in _FIELD_CONFIG for alias in aliases}

_IMAGE_PLACEHOLDER_RE = re.compile(r"!\[img:(\d+)\]")
_IMAGE_CONTEXT_BEFORE_CHARS = 800
_IMAGE_CONTEXT_AFTER_CHARS = 300


# ─── VISION 描述（desc）来源 ─────────────────────────────────────────────────
# 已废弃：FULL_TEXT 截断已不需要。Vision LLM 后端直接存储完整 desc，由 frontend 按需渲染。
# 新架构：data-pipeline 不再写 images_json.desc，也不再从本地 img_N.desc.txt 读取；
#   images_json.desc 初始留空，由 VISION 阶段（reanalyze）填充。
# legacy 旧路径 convert_kbd / convert_kbd_with_meta / _load_vision_desc 已彻底移除（2026-07）。


def _normalize_image_context(text: str) -> str:
    """清理截图上下文中的其他图片占位符和展示性空白。"""

    without_placeholders = _IMAGE_PLACEHOLDER_RE.sub(" ", text)
    return re.sub(r"\s+", " ", without_placeholders).strip()


def _extract_image_context(section_text: str, seq: int) -> tuple[str, str]:
    """从章节语义文本中提取指定截图的前后文。

    截图序号在 IMPORT 阶段仍与章节文本中的 ``![img:N]`` 同源，此时提取不会受到
    后续 ``rebuild_content_md/sync_sections_from_content_md`` 往返渲染的影响。相同图片
    在同一章节重复出现时采用首次出现位置，保证结果确定且可复现。
    """

    marker = f"![img:{seq}]"
    pos = section_text.find(marker)
    if pos < 0:
        return "", ""

    before = section_text[max(0, pos - _IMAGE_CONTEXT_BEFORE_CHARS) : pos]
    after_start = pos + len(marker)
    after = section_text[after_start : after_start + _IMAGE_CONTEXT_AFTER_CHARS]
    return _normalize_image_context(before), _normalize_image_context(after)


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
            # 保留所有直接子 div（修复：旧逻辑仅取最后一个 div，导致子步骤/命令/预期结果/判断依据等被丢弃）
            parts: list[str] = [str(d) for d in content_divs]

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
                    result[last_matched_field] = result.get(last_matched_field, "") + "".join(
                        str(d) for d in content_divs
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
        support_id,
        missing,
    )


def _write_integrity_failure(
    support_id: str,
    source_sha256: str,
    section_reports: dict[str, SemanticIntegrityReport],
) -> None:
    """原子写入转换完整性失败证据，供异常扫描和人工复核使用。"""

    target = settings.KBD_CACHE_DIR / support_id / "integrity.json"
    temporary = target.with_suffix(".json.tmp")
    payload = {
        "support_id": support_id,
        "source_sha256": source_sha256,
        "trace_id": get_trace_id() or "",
        "sections": {name: report.as_dict() for name, report in section_reports.items()},
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


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
    "问题描述": "problem_description",
    "告警信息": "alert_info",
    "有效排查步骤": "steps_text",
    "根因": "root_cause",
    "解决方案": "solution",
    "操作影响范围": "operational_impact",
    "是否是临时解决方案": "is_temporary",
    "建议与总结": "recommendations",
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


# 块级容器必须按子节点原始顺序递归，不能先抽取行内内容再补结构块。
_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "blockquote",
        "main",
        "header",
        "footer",
        "figure",
        "figcaption",
        "details",
        "summary",
        "dl",
        "dd",
        "dt",
        "html",
        "body",
    }
)
_STRUCTURE_TAGS: frozenset[str] = frozenset({"ul", "ol", "table", "pre"})
_IGNORED_TAGS: frozenset[str] = frozenset({"script", "style", "input", "meta", "link"})


@dataclass
class SemanticIntegrityReport:
    """记录源 HTML 与语义输出之间不可丢失的结构事实。"""

    source_pre_hashes: list[str] = dataclass_field(default_factory=list)
    rendered_pre_hashes: list[str] = dataclass_field(default_factory=list)
    source_image_seqs: list[int] = dataclass_field(default_factory=list)
    rendered_image_seqs: list[int] = dataclass_field(default_factory=list)
    source_ordered_items: list[int] = dataclass_field(default_factory=list)
    rendered_ordered_items: list[int] = dataclass_field(default_factory=list)
    source_structure_events: list[str] = dataclass_field(default_factory=list)
    rendered_structure_events: list[str] = dataclass_field(default_factory=list)
    errors: list[str] = dataclass_field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "valid": self.valid,
                "source_pre_count": len(self.source_pre_hashes),
                "rendered_pre_count": len(self.rendered_pre_hashes),
                "source_image_count": len(self.source_image_seqs),
                "rendered_image_count": len(self.rendered_image_seqs),
                "source_ordered_item_count": sum(self.source_ordered_items),
                "rendered_ordered_item_count": sum(self.rendered_ordered_items),
            }
        )
        return payload


@dataclass
class SemanticRenderResult:
    text: str
    integrity: SemanticIntegrityReport


class SemanticIntegrityError(ValueError):
    """源 HTML 的代码、图片或有序列表语义未被完整保留。"""

    def __init__(self, report: SemanticIntegrityReport):
        self.report = report
        super().__init__("；".join(report.errors))


def _normalize_code_payload(tag: Tag) -> str:
    return tag.get_text("", strip=False).replace("\r\n", "\n").replace("\r", "\n")


def _payload_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _code_language(tag: Tag) -> str:
    classes = [str(item) for item in (tag.get("class") or [])]
    code = tag.find("code", recursive=False)
    if code is not None:
        classes.extend(str(item) for item in (code.get("class") or []))
    for class_name in classes:
        match = re.fullmatch(r"(?:language|lang)-([A-Za-z0-9_+.-]+)", class_name)
        if match:
            return match.group(1)
    return "text"


def _semantic_render(html: str, image_map: dict[str, dict]) -> SemanticRenderResult:
    """按 DOM 顺序生成语义 Markdown，并返回可写库前验证的结构报告。"""

    if not html or not html.strip():
        return SemanticRenderResult("", SemanticIntegrityReport())

    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    report = SemanticIntegrityReport()

    def _abs(src: str) -> str:
        return urljoin(settings.SANGFOR_API_BASE, src)

    def _image_seq(img_tag: Tag) -> int | None:
        src = img_tag.get("src") or img_tag.get("data-src") or ""
        if not src or src.startswith("data:"):
            return None
        entry = image_map.get(_abs(src))
        return int(entry["seq"]) if entry is not None else None

    report.source_pre_hashes = [_payload_hash(_normalize_code_payload(tag)) for tag in soup.find_all("pre")]
    report.source_image_seqs = [seq for tag in soup.find_all("img") if (seq := _image_seq(tag)) is not None]
    report.source_ordered_items = [len(tag.find_all("li", recursive=False)) for tag in soup.find_all("ol")]
    for tag in soup.find_all(["ol", "pre", "img"]):
        if tag.name == "ol":
            report.source_structure_events.append(f"ol:{len(tag.find_all('li', recursive=False))}")
        elif tag.name == "pre":
            report.source_structure_events.append(f"pre:{_payload_hash(_normalize_code_payload(tag))}")
        else:
            seq = _image_seq(tag)
            if seq is not None:
                report.source_structure_events.append(f"img:{seq}")

    def _append(line: str = "") -> None:
        if line or not out or out[-1] != "":
            out.append(line.rstrip())

    def _inline_text(node: Tag) -> str:
        parts: list[str] = []
        for sub in node.children:
            if isinstance(sub, str):
                text = re.sub(r"\s+", " ", sub).strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(sub, Tag) or sub.name in _IGNORED_TAGS:
                continue
            if sub.name == "br":
                parts.append("\n")
                continue
            if sub.name == "img":
                seq = _image_seq(sub)
                if seq is not None:
                    parts.append(f"![img:{seq}]")
                    report.rendered_image_seqs.append(seq)
                    report.rendered_structure_events.append(f"img:{seq}")
                continue
            if sub.name in _STRUCTURE_TAGS or sub.name in _BLOCK_TAGS:
                continue
            text = _inline_text(sub)
            if text:
                parts.append(text)
        text = " ".join(parts)
        return re.sub(r" *\n *", "\n", text).strip()

    def _render_code(tag: Tag, prefix: str) -> None:
        payload = _normalize_code_payload(tag)
        longest = max((len(match.group(0)) for match in re.finditer(r"`+", payload)), default=0)
        fence = "`" * max(3, longest + 1)
        payload_digest = _payload_hash(payload)
        report.rendered_pre_hashes.append(payload_digest)
        report.rendered_structure_events.append(f"pre:{payload_digest}")
        _append()
        _append(f"{prefix}{fence}{_code_language(tag)}")
        for line in payload.split("\n"):
            _append(f"{prefix}{line}")
        _append(f"{prefix}{fence}")
        _append()
        # Support 偶尔把截图嵌入 pre；图片不是代码载荷，必须在围栏外保留锚点。
        for image in tag.find_all("img"):
            _render_image(image, prefix)

    def _render_table(tag: Tag, prefix: str) -> None:
        _append()
        for row in tag.find_all("tr"):
            cells = [re.sub(r"\s+", " ", cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
            cells = [cell for cell in cells if cell]
            if len(cells) == 1:
                _append(f"{prefix}- {cells[0]}")
            elif cells:
                _append(f"{prefix}- {cells[0]}: {' '.join(cells[1:])}")
        _append()

    def _render_image(tag: Tag, prefix: str) -> None:
        seq = _image_seq(tag)
        if seq is None:
            return
        report.rendered_image_seqs.append(seq)
        report.rendered_structure_events.append(f"img:{seq}")
        _append()
        _append(f"{prefix}![img:{seq}]")
        _append()

    def _render_container(node: Any, prefix: str = "") -> None:
        inline_parts: list[str] = []

        def _flush_inline() -> None:
            if not inline_parts:
                return
            text = " ".join(inline_parts).strip()
            inline_parts.clear()
            if text:
                for line in text.splitlines():
                    _append(f"{prefix}{line}")

        for child in node.children:
            if isinstance(child, str):
                text = re.sub(r"\s+", " ", child).strip()
                if text:
                    inline_parts.append(text)
                continue
            if not isinstance(child, Tag) or child.name in _IGNORED_TAGS:
                continue
            name = child.name
            if name == "br":
                _flush_inline()
            elif name == "img":
                _flush_inline()
                _render_image(child, prefix)
            elif name in ("ul", "ol"):
                _flush_inline()
                _render_list(child, prefix)
            elif name == "pre":
                _flush_inline()
                _render_code(child, prefix)
            elif name == "table":
                _flush_inline()
                _render_table(child, prefix)
            elif name in _BLOCK_TAGS:
                _flush_inline()
                if child.find(_BLOCK_TAGS | _STRUCTURE_TAGS | {"img"}):
                    _render_container(child, prefix)
                else:
                    text = _inline_text(child)
                    if text:
                        for line in text.splitlines():
                            _append(f"{prefix}{line}")
            else:
                text = _inline_text(child)
                if text:
                    inline_parts.append(text)
        _flush_inline()

    def _render_list_item(item: Tag, marker: str, indent: str) -> None:
        first_prefix = f"{indent}{marker}"
        continuation = f"{indent}{' ' * len(marker)}"
        first_content = True
        inline_parts: list[str] = []

        def _line_prefix() -> str:
            nonlocal first_content
            if first_content:
                first_content = False
                return first_prefix
            return continuation

        def _flush_inline() -> None:
            if not inline_parts:
                return
            text = " ".join(inline_parts).strip()
            inline_parts.clear()
            if not text:
                return
            for line in text.splitlines():
                _append(f"{_line_prefix()}{line}")

        for child in item.children:
            if isinstance(child, str):
                text = re.sub(r"\s+", " ", child).strip()
                if text:
                    inline_parts.append(text)
                continue
            if not isinstance(child, Tag) or child.name in _IGNORED_TAGS:
                continue
            name = child.name
            if name == "br":
                _flush_inline()
            elif name == "img":
                _flush_inline()
                _render_image(child, _line_prefix())
            elif name in ("ul", "ol"):
                _flush_inline()
                if first_content:
                    _append(first_prefix.rstrip())
                    first_content = False
                _render_list(child, continuation)
            elif name == "pre":
                _flush_inline()
                _render_code(child, _line_prefix())
            elif name == "table":
                _flush_inline()
                _render_table(child, _line_prefix())
            elif name in _BLOCK_TAGS:
                _flush_inline()
                if child.find(_BLOCK_TAGS | _STRUCTURE_TAGS | {"img"}):
                    before = len(out)
                    _render_container(child, continuation if not first_content else "")
                    if len(out) > before and first_content:
                        first_line = out[before].lstrip()
                        out[before] = f"{first_prefix}{first_line}"
                        first_content = False
                else:
                    text = _inline_text(child)
                    if text:
                        for line in text.splitlines():
                            _append(f"{_line_prefix()}{line}")
            else:
                text = _inline_text(child)
                if text:
                    inline_parts.append(text)
        _flush_inline()
        if first_content:
            _append(first_prefix.rstrip())

    def _render_list(tag: Tag, indent: str) -> None:
        _append()
        items = tag.find_all("li", recursive=False)
        ordered = tag.name == "ol"
        if ordered:
            report.rendered_ordered_items.append(len(items))
            report.rendered_structure_events.append(f"ol:{len(items)}")
        try:
            current = int(tag.get("start", 1))
        except (TypeError, ValueError):
            current = 1
        for item in items:
            if ordered:
                with contextlib.suppress(TypeError, ValueError):
                    current = int(item.get("value", current))
                marker = f"{current}. "
                current += 1
            else:
                marker = "- "
            _render_list_item(item, marker, indent)
        _append()

    _render_container(soup)
    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()

    checks = (
        ("code_block", report.source_pre_hashes, report.rendered_pre_hashes, "代码块内容或顺序不一致"),
        ("image_anchor", report.source_image_seqs, report.rendered_image_seqs, "图片锚点数量或顺序不一致"),
        ("ordered_list", report.source_ordered_items, report.rendered_ordered_items, "有序列表项数量或层级不一致"),
        (
            "structure_order",
            report.source_structure_events,
            report.rendered_structure_events,
            "代码块、图片锚点和有序列表的相对顺序不一致",
        ),
    )
    for kind, source, rendered, message in checks:
        if source != rendered:
            report.errors.append(message)
            _SEMANTIC_INTEGRITY_FAILURES.labels(kind=kind).inc()
    _SEMANTIC_CONVERSIONS.labels(status="success" if report.valid else "failed").inc()
    return SemanticRenderResult(result, report)


def _html_to_semantic_text(html: str, image_map: dict[str, dict]) -> str:
    """HTML 转语义 Markdown；结构不完整时拒绝返回可写库结果。"""

    result = _semantic_render(html, image_map)
    if not result.integrity.valid:
        raise SemanticIntegrityError(result.integrity)
    return result.text


def convert_kbd_structured(
    support_id: str,
    *,
    include_image_data: bool = True,
    strict_integrity: bool = True,
) -> dict[str, Any] | None:
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
      - signals_json          ← [] （空，待关键信号分级抽取阶段填充）
      - images_json          ← [{"seq": N, "section": field_name,
                                  "context_before": "...", "context_after": "...",
                                  "desc": "..."}]
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
        md_title for _, md_title, required in _SECTIONS if required and _is_empty_content(sections.get(md_title, ""))
    ]
    if missing:
        _write_abnormal(support_id, title, missing)
        return None

    # ── 语义提取（新原则：只取语义文本，丢装饰样式） ───────────────────────
    # 章节字段含 ![img:N] 占位符；content_md 不在此生成，交由后端
    # rebuild_content_md 统一渲染（样式高一致）。
    section_texts: dict[str, str] = {}
    integrity_reports: dict[str, SemanticIntegrityReport] = {}
    source_sha256 = _payload_hash(content_html)

    for _, md_title, _ in _SECTIONS:
        section_html = sections.get(md_title, "")
        if _is_empty_content(section_html):
            section_texts[md_title] = ""
        else:
            rendered = _semantic_render(section_html, image_map)
            section_texts[md_title] = rendered.text.strip()
            integrity_reports[md_title] = rendered.integrity

    failed_reports = {name: report for name, report in integrity_reports.items() if not report.valid}
    if failed_reports:
        _write_integrity_failure(support_id, source_sha256, failed_reports)
        logger.error(
            "案例 %s 语义转换完整性失败 trace_id=%s sections=%s errors=%s",
            support_id,
            get_trace_id() or "-",
            sorted(failed_reports),
            {name: report.errors for name, report in failed_reports.items()},
        )
        combined = SemanticIntegrityReport(
            errors=[f"{name}: {error}" for name, report in failed_reports.items() for error in report.errors]
        )
        if strict_integrity:
            raise SemanticIntegrityError(combined)

    # ── 构建 8 大章节字段（含占位符） ───────────────────────────────────────
    structured_fields: dict[str, str] = {field: "" for field in _MD_TITLE_TO_FIELD.values()}
    for md_title, field in _MD_TITLE_TO_FIELD.items():
        structured_fields[field] = section_texts.get(md_title, "")

    # "排查内容" 并入 steps_text（若有内容）
    chacha_html = sections.get("排查内容", "")
    if not _is_empty_content(chacha_html):
        chacha_text = section_texts.get("排查内容", "").strip()
        if chacha_text:
            existing = structured_fields["steps_text"]
            structured_fields["steps_text"] = existing + "\n\n---\n\n" + chacha_text if existing else chacha_text
            section_texts["排查内容"] = chacha_text

    # ── 构建 images_json + images（图片封装） ──────────────────────────────
    # 通过扫描章节语义文本中的占位符确定每张图的归属章节字段
    seq_to_section: dict[int, str] = {}
    seq_to_context: dict[int, tuple[str, str]] = {}
    for md_title, field_name in _MD_TITLE_TO_FIELD.items():
        section_text = section_texts.get(md_title, "")
        for m in _IMAGE_PLACEHOLDER_RE.finditer(section_text):
            seq = int(m.group(1))
            seq_to_section.setdefault(seq, field_name)
            seq_to_context.setdefault(seq, _extract_image_context(section_text, seq))
    # 排查内容归属到 steps_text
    troubleshooting_text = section_texts.get("排查内容", "")
    for m in _IMAGE_PLACEHOLDER_RE.finditer(troubleshooting_text):
        seq = int(m.group(1))
        seq_to_section.setdefault(seq, "steps_text")
        seq_to_context.setdefault(seq, _extract_image_context(troubleshooting_text, seq))

    images_json: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []  # 图片二进制（base64），随 ingest 原子写入 kbd_image
    for entry in image_map.values():
        seq = entry["seq"]
        section = seq_to_section.get(seq, "steps_text")
        context_before, context_after = seq_to_context.get(seq, ("", ""))
        # desc 初始为空，由 VISION 阶段（reanalyze）填充
        images_json.append(
            {
                "seq": seq,
                "section": section,
                "context_before": context_before,
                "context_after": context_after,
                "desc": "",
            }
        )
        if not include_image_data:
            continue
        img_b64 = _load_image_base64(support_id, seq)
        if img_b64 is None:
            logger.warning("案例 %s 图片 img_%d 文件缺失，跳过该图入库", support_id, seq)
            continue
        images.append(
            {
                "seq": seq,
                "section": section,
                "mime_type": img_b64["mime_type"],
                "data_base64": img_b64["data_base64"],
            }
        )
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
        # 关键信号（空列表，需 extract-signals 后续填充；steps_json 已彻底移除，见 ADR-1）
        "signals_json": [],
        # 图片视觉描述（结构化，独立存储）
        "images_json": images_json,
        # 图片二进制（base64），IMPORT 阶段原子写入 kbd_image 表
        "images": images,
        "conversion_integrity": {
            "source_sha256": source_sha256,
            "valid": not failed_reports,
            "sections": {name: report.as_dict() for name, report in integrity_reports.items()},
        },
        # content_md 不传：由后端 rebuild_content_md 统一渲染（样式高一致）
        "content_md": None,
    }
