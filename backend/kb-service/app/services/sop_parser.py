"""
KB Service — SOP 多叉决策树 Markdown 解析器（v2）

v2 改动摘要：
  - 所有 ValidationIssue 携带 line_number（Markdown 源行号，1-based）
  - 新增段落类型：variables（变量声明表格）、prerequisites（前置检查列表）
  - classify_heading 识别 4 种段落类型（从 YAML 配置加载关键词）
  - W-7/W-8 升级为 error（多根节点、无父节点段落）
  - W-9/W-10 升级为 error（话术不规范）
  - page_methods 改为可选；acli_methods 缺失报 error
  - 新增前置检查条目数 vs 子节点数匹配校验（warning）

解析流程：
  1. _parse_into_sections → 标题分段，追踪行号，识别 5 种段落类型
  2. _build_tree          → 栈驱动，按相对层级差构建树
  3. _validate_leaves     → 叶节点完整性校验
  4. _validate_prerequisite_count → 前置检查条目数与子节点数校验
  5. _assign_node_ids     → 为所有节点分配 n-x-x 格式 ID
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.config.template_config_loader import (
    get_binary_outcome_patterns,
    get_keywords,
    get_prerequisite_type_keywords,
    get_standard_heading,
    get_validation_level,
)
from app.schemas.sop_template import (
    DiagnosisDetail,
    PrerequisiteItem,
    SolutionDetail,
    SOPNode,
    SOPValidationResult,
    ValidationIssue,
    VariableDeclaration,
)

# ──────────────────────────────────────────────────────────────────────────────
# 变量名启发式规则（来自 §12.7.3）
# ──────────────────────────────────────────────────────────────────────────────

STRATEGY_HINTS: dict[str, str] = {
    r".*_ip$|^node_ip$|^cluster_ip$|^host_ip$|^server_ip$": "env_context",
    r"^cluster_name$|^node_name$|^host_name$|^server_name$": "env_context",
    r"^vm_name$|^vm_id$": "tool:get_vm_list",
    r"^disk_id$|^disk_name$": "tool:acli_storage_disk_list",
    r"^nic_name$|^nic_id$": "tool:acli_network_nic_list",
    r"^volume_id$|^volume_name$": "tool:acli_storage_volume_list",
}

# 变量章节标题关键词（旧版兼容）
VARIABLE_SECTION_KEYWORDS: frozenset[str] = frozenset(
    ["变量", "变量定义", "参数", "参数定义", "环境变量"]
)

# ──────────────────────────────────────────────────────────────────────────────
# 内容行解析辅助正则
# ──────────────────────────────────────────────────────────────────────────────

# 列表项正则（支持 - / * / 1. / 1、 格式）
_LIST_ITEM_RE = re.compile(r"^(?:[-*]|\d+[.、])\s+(.+)$")

# 标签行正则：以"字段名："或"字段名:"开头
_LABEL_RE = re.compile(r"^(.+?)[：:]\s*(.*)$")

# Markdown 表格行正则
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$")
_TABLE_SEP_RE = re.compile(r"^\|[\s\-|:]+\|$")

# 前置检查数字序号行（1、条件文本）
_PREREQ_NUMBERED_RE = re.compile(r"^\d+[、.]\s*(.+)$")

# ──────────────────────────────────────────────────────────────────────────────
# diagnosis 段落字段映射
# ──────────────────────────────────────────────────────────────────────────────

_DIAGNOSIS_LIST_MAP: dict[str, str] = {
    "页面判断方法": "page_methods",
    "页面操作步骤": "page_methods",
    "acli判断方法": "acli_methods",
    "acli命令行": "acli_methods",
    "命令行判断": "acli_methods",
    "前置检查": "prerequisites",
}

_DIAGNOSIS_TEXT_MAP: dict[str, str] = {
    "判断说明": "description",
    "问题根因": "root_cause",
    "原因": "root_cause",
    "注意事项": "notes",
}

_SOLUTION_LIST_MAP: dict[str, str] = {
    "快速恢复方案": "quick_recovery",
    "快速恢复": "quick_recovery",
    "临时处理": "quick_recovery",
    "彻底解决方案": "thorough_fix",
    "彻底修复": "thorough_fix",
    "永久修复": "thorough_fix",
    "根本解决": "thorough_fix",
}

# 不识别为 diagnosis/solution 的后缀词
_STRUCTURAL_SUFFIXES: frozenset[str] = frozenset(
    ["概述", "汇总", "总览", "简介", "概要", "目录"]
)


# ──────────────────────────────────────────────────────────────────────────────
# 内部数据结构
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class _SectionEntry:
    """解析过程中的中间表示：一个标题及其正文内容行"""

    level: int
    text: str
    section_type: Literal["node", "diagnosis", "solution", "variables", "prerequisites"]
    content: list[str] = field(default_factory=list)
    line_number: int = 0  # 标题行的 Markdown 源行号（1-based）


# ──────────────────────────────────────────────────────────────────────────────
# 公共 API：关键词分类
# ──────────────────────────────────────────────────────────────────────────────


def classify_heading(
    text: str,
) -> Literal["diagnosis", "solution", "variables", "prerequisites", "node"]:
    """判断标题文本的语义类型（从 YAML 配置加载关键词）。

    匹配顺序：结构性后缀 → diagnosis → solution → variables → prerequisites → node

    例：
      "判断方法"          → diagnosis
      "前置检查"          → prerequisites
      "变量声明"          → variables
      "判断方法概述"       → node（以结构性后缀词结尾）
    """
    for suffix in _STRUCTURAL_SUFFIXES:
        if text.endswith(suffix):
            return "node"
    for kw in get_keywords("diagnosis"):
        if kw in text:
            return "diagnosis"
    for kw in get_keywords("solution"):
        if kw in text:
            return "solution"
    for kw in get_keywords("variables"):
        if kw in text:
            return "variables"
    for kw in get_keywords("prerequisites"):
        if kw in text:
            return "prerequisites"
    return "node"


# ──────────────────────────────────────────────────────────────────────────────
# 内部：文档分段解析（追踪行号）
# ──────────────────────────────────────────────────────────────────────────────


def _parse_into_sections(content_md: str) -> list[_SectionEntry]:
    """将 Markdown 文本解析为 _SectionEntry 列表，并记录标题行号。

    关键逻辑：
    - diagnosis/solution 段落下更深层标题转为标签行内容，直到遇到同层或更浅层标题
    - variables/prerequisites 段落内容直接收集（不进入标签行模式）
    - 每个 _SectionEntry 记录 line_number（标题所在行，1-based）
    """
    sections: list[_SectionEntry] = []

    cur_level: int = 0
    cur_text: str = ""
    cur_type: Literal["node", "diagnosis", "solution", "variables", "prerequisites"] = "node"
    cur_content: list[str] = []
    cur_line_number: int = 0

    # 当前 diagnosis/solution 段落的起始层级（None = 不在该模式）
    ds_depth: int | None = None

    for line_no, line in enumerate(content_md.splitlines(), start=1):
        heading_m = re.match(r"^(#{1,10})\s+(.+)$", line)
        if heading_m:
            h_level = len(heading_m.group(1))
            h_text = heading_m.group(2).strip()
            h_type = classify_heading(h_text)

            if ds_depth is not None:
                if h_level > ds_depth:
                    # 子标题：视为标签行，转换为"标题文本："格式加入当前内容
                    cur_content.append(f"{h_text}：")
                    continue
                # 同层或更浅层标题：退出 diagnosis/solution 模式
                ds_depth = None

            # 保存上一段落
            if cur_text:
                sections.append(
                    _SectionEntry(
                        level=cur_level,
                        text=cur_text,
                        section_type=cur_type,
                        content=list(cur_content),
                        line_number=cur_line_number,
                    )
                )

            cur_level = h_level
            cur_text = h_text
            cur_type = h_type
            cur_content = []
            cur_line_number = line_no

            # 仅 diagnosis/solution 进入子标题吸收模式
            if h_type in ("diagnosis", "solution"):
                ds_depth = h_level
        else:
            stripped = line.strip()
            if stripped:
                cur_content.append(stripped)

    # 处理末尾段落
    if cur_text:
        sections.append(
            _SectionEntry(
                level=cur_level,
                text=cur_text,
                section_type=cur_type,
                content=list(cur_content),
                line_number=cur_line_number,
            )
        )

    return sections


# ──────────────────────────────────────────────────────────────────────────────
# 内部：通用内容行解析器
# ──────────────────────────────────────────────────────────────────────────────


def _parse_content_lines(
    content: list[str],
    list_field_map: dict[str, str],
    text_field_map: dict[str, str],
    default_list_field: str,
) -> tuple[dict[str, list[str]], dict[str, str | None]]:
    """通用内容行解析器（标签行 + 列表项 + 纯文本）。"""
    list_result: dict[str, list[str]] = {}
    for fname in set(list_field_map.values()):
        list_result[fname] = []
    if default_list_field not in list_result:
        list_result[default_list_field] = []

    text_result: dict[str, str | None] = {
        fname: None for fname in set(text_field_map.values())
    }

    cur_list_field = default_list_field
    cur_text_field: str | None = None

    for line in content:
        label_m = _LABEL_RE.match(line)
        if label_m:
            label = label_m.group(1).strip()
            rest = (label_m.group(2) or "").strip()

            if label in list_field_map:
                cur_list_field = list_field_map[label]
                cur_text_field = None
                if rest:
                    list_result[cur_list_field].append(rest)
                continue

            if label in text_field_map:
                cur_text_field = text_field_map[label]
                cur_list_field = default_list_field
                if rest:
                    text_result[cur_text_field] = rest
                continue

        list_m = _LIST_ITEM_RE.match(line)
        if list_m:
            item = list_m.group(1).strip()
            if item:
                list_result[cur_list_field].append(item)
            continue

        if line:
            if cur_text_field:
                prev = text_result.get(cur_text_field)
                text_result[cur_text_field] = f"{prev}\n{line}" if prev else line
            else:
                list_result[cur_list_field].append(line)

    return list_result, text_result


# ──────────────────────────────────────────────────────────────────────────────
# 内部：构建 DiagnosisDetail
# ──────────────────────────────────────────────────────────────────────────────


def _build_diagnosis_detail(
    content: list[str],
    source_heading: str,
    location: str,
    line_number: int | None,
    issues: list[ValidationIssue],
) -> DiagnosisDetail | None:
    """从内容行构建 DiagnosisDetail。

    acli_methods 为必填（缺失 → 使用配置中 diagnosis_missing_acli_methods 级别）。
    page_methods 为可选（缺失 → warning）。
    """
    if not content:
        issues.append(
            ValidationIssue(
                level=get_validation_level("diagnosis_empty_content"),
                location=location,
                line_number=line_number,
                message=f"「{source_heading}」段落无任何内容，diagnosis 字段未设置",
            )
        )
        return None

    list_r, text_r = _parse_content_lines(
        content, _DIAGNOSIS_LIST_MAP, _DIAGNOSIS_TEXT_MAP, "page_methods"
    )

    acli_methods = list_r.get("acli_methods", [])
    page_methods = list_r.get("page_methods", [])

    # acli_methods 必填
    if not acli_methods:
        issues.append(
            ValidationIssue(
                level=get_validation_level("diagnosis_missing_acli_methods"),
                location=location,
                line_number=line_number,
                message=f"「{source_heading}」段落缺少 acli 判断方法（需添加「acli判断方法：」标签）",
            )
        )
        return None

    # page_methods 可选，缺失仅 warning
    if not page_methods:
        issues.append(
            ValidationIssue(
                level=get_validation_level("diagnosis_missing_page_methods"),
                location=location,
                line_number=line_number,
                message=f"「{source_heading}」段落未填写页面判断方法（可选，建议补充）",
            )
        )

    return DiagnosisDetail(
        prerequisites=list_r.get("prerequisites", []),
        page_methods=page_methods,
        acli_methods=acli_methods,
        description=text_r.get("description"),
        root_cause=text_r.get("root_cause"),
        notes=text_r.get("notes"),
        source_heading=source_heading,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 内部：构建 SolutionDetail
# ──────────────────────────────────────────────────────────────────────────────


def _build_solution_detail(
    content: list[str],
    source_heading: str,
    location: str,
    line_number: int | None,
    issues: list[ValidationIssue],
) -> SolutionDetail | None:
    """从内容行构建 SolutionDetail（新增 line_number 参数）。"""
    if not content:
        issues.append(
            ValidationIssue(
                level=get_validation_level("solution_empty_content"),
                location=location,
                line_number=line_number,
                message=f"「{source_heading}」段落无任何内容，solution 字段未设置",
            )
        )
        return None

    list_r, _ = _parse_content_lines(content, _SOLUTION_LIST_MAP, {}, "quick_recovery")

    quick_recovery = list_r.get("quick_recovery", [])
    thorough_fix = list_r.get("thorough_fix", [])

    if not quick_recovery and not thorough_fix:
        issues.append(
            ValidationIssue(
                level=get_validation_level("solution_missing_valid_content"),
                location=location,
                line_number=line_number,
                message=f"「{source_heading}」段落无有效内容，solution 字段未设置",
            )
        )
        return None

    if quick_recovery and not thorough_fix:
        issues.append(
            ValidationIssue(
                level=get_validation_level("solution_missing_thorough_fix"),
                location=location,
                line_number=line_number,
                message=f"「{source_heading}」段落缺少彻底解决方案，已复用快速恢复方案内容",
            )
        )
        thorough_fix = list(quick_recovery)
    elif thorough_fix and not quick_recovery:
        issues.append(
            ValidationIssue(
                level=get_validation_level("solution_missing_quick_recovery"),
                location=location,
                line_number=line_number,
                message=f"「{source_heading}」段落缺少快速恢复方案，已复用彻底解决方案内容",
            )
        )
        quick_recovery = list(thorough_fix)

    return SolutionDetail(
        quick_recovery=quick_recovery,
        thorough_fix=thorough_fix,
        source_heading=source_heading,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 内部：解析变量声明表格
# ──────────────────────────────────────────────────────────────────────────────


def _parse_variable_table(
    content: list[str],
    location: str,
    line_number: int | None,
    issues: list[ValidationIssue],
) -> list[VariableDeclaration]:
    """从内容行解析 Markdown 变量声明表格。

    表格格式（列顺序：变量名 | 类型 | 来源 | 说明）：
    | 变量名   | 类型   | 来源         | 说明 |
    |---------|--------|-------------|------|
    | vm_name | string | user_confirm | ...  |
    """
    variables: list[VariableDeclaration] = []
    header_cols: list[str] = []

    for line in content:
        # 跳过分隔行（|---|---|...）
        if _TABLE_SEP_RE.match(line):
            continue
        row_m = _TABLE_ROW_RE.match(line)
        if not row_m:
            continue
        cols = [c.strip() for c in row_m.group(1).split("|")]

        # 识别表头行
        if not header_cols:
            header_lower = [c.lower() for c in cols]
            if any(c in header_lower for c in ["变量名", "name"]):
                header_cols = header_lower
            continue

        # 数据行：按表头位置取字段
        def _col(name: str, *aliases: str, header_cols=header_cols, cols=cols) -> str:
            for alias in (name, *aliases):
                if alias in header_cols:
                    idx = header_cols.index(alias)
                    return cols[idx] if idx < len(cols) else ""
            return ""

        var_name = _col("变量名", "name")
        var_type = _col("类型", "type") or "string"
        var_source = _col("来源", "source")
        var_desc = _col("说明", "description") or None

        if not var_name or not var_source:
            continue

        variables.append(
            VariableDeclaration(
                name=var_name,
                type=var_type,
                source=var_source,
                description=var_desc,
            )
        )

    if not variables:
        issues.append(
            ValidationIssue(
                level="warning",
                location=location,
                line_number=line_number,
                message="「变量声明」段落未解析到任何变量行（表格格式：| 变量名 | 类型 | 来源 | 说明 |）",
            )
        )

    return variables


# ──────────────────────────────────────────────────────────────────────────────
# 内部：解析前置检查列表
# ──────────────────────────────────────────────────────────────────────────────


def _detect_prerequisite_type(text: str) -> Literal["filter", "sequence"]:
    """根据文本内容识别前置检查类型（从配置加载关键词）。"""
    type_kw = get_prerequisite_type_keywords()
    for kw in type_kw.get("sequence", []):
        if kw in text:
            return "sequence"
    # 默认过滤型
    return "filter"


def _parse_prerequisites_content(
    content: list[str],
    location: str,
    line_number: int | None,
    issues: list[ValidationIssue],
) -> list[PrerequisiteItem]:
    """从内容行解析前置检查列表。

    支持编号列表（1、...）和普通列表（- ...）两种格式。
    """
    items: list[PrerequisiteItem] = []

    for line in content:
        numbered_m = _PREREQ_NUMBERED_RE.match(line)
        list_m = _LIST_ITEM_RE.match(line)

        condition_text = ""
        if numbered_m:
            condition_text = numbered_m.group(1).strip()
        elif list_m:
            condition_text = list_m.group(1).strip()
        elif line.strip():
            condition_text = line.strip()

        if condition_text:
            items.append(
                PrerequisiteItem(
                    type=_detect_prerequisite_type(condition_text),
                    description=condition_text,
                )
            )

    if not items:
        issues.append(
            ValidationIssue(
                level="warning",
                location=location,
                line_number=line_number,
                message="「前置检查」段落未解析到任何条目",
            )
        )

    return items


# ──────────────────────────────────────────────────────────────────────────────
# 内部：构建树
# ──────────────────────────────────────────────────────────────────────────────


def _build_tree(
    sections: list[_SectionEntry],
) -> tuple[SOPNode | None, list[ValidationIssue]]:
    """将 _SectionEntry 列表构建为 SOPNode 树。

    Error 级别（从 YAML 配置加载）：
    - multi_root：文档存在多个顶层节点
    - orphan_ds_section：diagnosis/solution/variables/prerequisites 无父节点
    - non_standard_*_heading：话术不规范
    """
    issues: list[ValidationIssue] = []
    root: SOPNode | None = None

    # 栈：[(level, node), ...]
    stack: list[tuple[int, SOPNode]] = []

    for section in sections:
        if section.section_type == "node":
            while stack and stack[-1][0] >= section.level:
                stack.pop()

            node = SOPNode(id="_", title=section.text, level=section.level, line_number=section.line_number)

            if not stack:
                if root is None:
                    root = node
                else:
                    issues.append(
                        ValidationIssue(
                            level=get_validation_level("multi_root"),
                            location=section.text,
                            line_number=section.line_number,
                            message=(
                                f"发现多个顶层节点「{section.text}」（层级 {section.level}），"
                                "文档应只有一个根节点（H1 标题）"
                            ),
                        )
                    )
                    root.children.append(node)
            else:
                stack[-1][1].children.append(node)

            stack.append((section.level, node))

        elif section.section_type in ("diagnosis", "solution", "variables", "prerequisites"):
            if not stack:
                issues.append(
                    ValidationIssue(
                        level=get_validation_level("orphan_ds_section"),
                        location=section.text,
                        line_number=section.line_number,
                        message=f"「{section.text}」段落出现在文档最前，无父节点",
                    )
                )
                continue

            owner = stack[-1][1]
            location = " > ".join(n.title for _, n in stack)

            if section.section_type == "diagnosis":
                std = get_standard_heading("diagnosis")
                if section.text != std:
                    issues.append(
                        ValidationIssue(
                            level=get_validation_level("non_standard_diagnosis_heading"),
                            location=location,
                            line_number=section.line_number,
                            message=f"话术不规范：「{section.text}」应改为「{std}」",
                        )
                    )
                owner.diagnosis = _build_diagnosis_detail(
                    section.content, section.text, location, section.line_number, issues
                )

            elif section.section_type == "solution":
                std = get_standard_heading("solution")
                if section.text != std:
                    issues.append(
                        ValidationIssue(
                            level=get_validation_level("non_standard_solution_heading"),
                            location=location,
                            line_number=section.line_number,
                            message=f"话术不规范：「{section.text}」应改为「{std}」",
                        )
                    )
                owner.solution = _build_solution_detail(
                    section.content, section.text, location, section.line_number, issues
                )

            elif section.section_type == "variables":
                std = get_standard_heading("variables")
                if section.text != std:
                    issues.append(
                        ValidationIssue(
                            level=get_validation_level("non_standard_variables_heading"),
                            location=location,
                            line_number=section.line_number,
                            message=f"话术不规范：「{section.text}」应改为「{std}」",
                        )
                    )
                owner.variables = _parse_variable_table(
                    section.content, location, section.line_number, issues
                )

            elif section.section_type == "prerequisites":
                std = get_standard_heading("prerequisites")
                if section.text != std:
                    issues.append(
                        ValidationIssue(
                            level=get_validation_level("non_standard_prerequisites_heading"),
                            location=location,
                            line_number=section.line_number,
                            message=f"话术不规范：「{section.text}」应改为「{std}」",
                        )
                    )
                owner.prerequisite_items = _parse_prerequisites_content(
                    section.content, location, section.line_number, issues
                )

    return root, issues


# ──────────────────────────────────────────────────────────────────────────────
# 内部：叶节点完整性校验
# ──────────────────────────────────────────────────────────────────────────────


def _validate_leaves(
    node: SOPNode,
    path: list[str],
    issues: list[ValidationIssue],
) -> None:
    """递归校验叶节点：必须同时具有 diagnosis 和 solution。"""
    location = " > ".join(path)

    if node.is_leaf:
        if node.diagnosis is None:
            issues.append(
                ValidationIssue(
                    level=get_validation_level("leaf_missing_diagnosis"),
                    location=location,
                    message="叶节点缺少判断方法段落（标准话术：判断方法）",
                )
            )
        if node.solution is None:
            issues.append(
                ValidationIssue(
                    level=get_validation_level("leaf_missing_solution"),
                    location=location,
                    message="叶节点缺少解决方案段落（标准话术：解决方案）",
                )
            )
    else:
        for child in node.children:
            _validate_leaves(child, path + [child.title], issues)


# ──────────────────────────────────────────────────────────────────────────────
# 内部：前置检查条目数 vs 子节点数校验
# ──────────────────────────────────────────────────────────────────────────────


def _validate_prerequisite_count(
    node: SOPNode,
    path: list[str],
    issues: list[ValidationIssue],
) -> None:
    """递归校验中间节点的前置检查条目数与子节点数是否匹配。

    判断逻辑：
    - 若前置检查包含二元结果关键词（是/否、有/无 等），期望子节点 = 2
    - 期望数与实际不符时，按配置级别报告
    """
    if node.is_routing and node.prerequisite_items:
        binary_patterns = get_binary_outcome_patterns()
        binary_count = 0
        for item in node.prerequisite_items:
            for pattern in binary_patterns:
                if pattern in item.description:
                    binary_count += 1
                    break

        if binary_count > 0:
            expected = 2 * binary_count
            actual = len(node.children)
            if actual != expected:
                issues.append(
                    ValidationIssue(
                        level=get_validation_level("prerequisite_count_mismatch"),
                        location=" > ".join(path),
                        message=(
                            f"前置检查包含 {binary_count} 个二元判断条件"
                            f"（预期 {expected} 个子节点），实际有 {actual} 个子节点"
                        ),
                    )
                )

    for child in node.children:
        _validate_prerequisite_count(child, path + [child.title], issues)


# ──────────────────────────────────────────────────────────────────────────────
# 内部：分配 node_id
# ──────────────────────────────────────────────────────────────────────────────


def _assign_node_ids(node: SOPNode, path: list[int]) -> None:
    """递归分配 id，格式 n-1-2-3（根节点为 n-1）。"""
    node.id = "n-" + "-".join(str(i) for i in path)
    for idx, child in enumerate(node.children, start=1):
        _assign_node_ids(child, path + [idx])


# ──────────────────────────────────────────────────────────────────────────────
# 公共 API：主解析入口
# ──────────────────────────────────────────────────────────────────────────────


def parse_sop_markdown(content_md: str) -> SOPValidationResult:
    """解析 Markdown 文本为 SOPNode 决策树，并返回校验结果。

    流程：
      1. _parse_into_sections → 标题分段 + 行号追踪
      2. _build_tree          → 构建树 + 填充各类段落数据
      3. _validate_leaves     → 叶节点完整性校验
      4. _validate_prerequisite_count → 前置检查条目数与子节点数校验
      5. _assign_node_ids     → 分配 n-x-x 格式 ID
    """
    if not content_md or not content_md.strip():
        return SOPValidationResult(
            is_valid=False,
            errors=[
                ValidationIssue(
                    level="error", location="文档", message="文档内容为空，无法解析"
                )
            ],
        )

    sections = _parse_into_sections(content_md)

    if not sections:
        return SOPValidationResult(
            is_valid=False,
            errors=[
                ValidationIssue(
                    level="error",
                    location="文档",
                    message="文档无法识别任何标题，请确认 Markdown 格式正确（使用 # 标题格式）",
                )
            ],
        )

    root, build_issues = _build_tree(sections)

    if root is None:
        return SOPValidationResult(
            is_valid=False,
            errors=[
                ValidationIssue(
                    level="error",
                    location="文档",
                    message="无法识别根节点，请确认文档包含至少一个 H1 标题（# 标题）",
                )
            ],
        )

    # 叶节点完整性校验
    _validate_leaves(root, [root.title], build_issues)

    # 前置检查条目数与子节点数匹配校验
    _validate_prerequisite_count(root, [root.title], build_issues)

    # 分配 node_id
    _assign_node_ids(root, [1])

    errors = [i for i in build_issues if i.level == "error"]
    warnings = [i for i in build_issues if i.level == "warning"]

    return SOPValidationResult(
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        tree=root if not errors else None,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 变量提取与双向校验（T-AGT-24）
# ──────────────────────────────────────────────────────────────────────────────


def _infer_strategy(var_name: str) -> dict:
    """根据变量名推断获取策略。"""
    for pattern, strategy in STRATEGY_HINTS.items():
        if re.match(pattern, var_name):
            if strategy.startswith("tool:"):
                return {"acquisition_strategy": "tool", "acquisition_tool": strategy[5:]}
            return {"acquisition_strategy": strategy, "acquisition_tool": None}
    return {"acquisition_strategy": "user_input", "acquisition_tool": None}


def _parse_variable_section(content_md: str) -> dict[str, dict]:
    """解析变量章节，提取声明变量（兼容新版表格格式和旧版列表格式）。

    新版表格格式：
    ## 变量声明
    | 变量名 | 类型 | 来源 | 说明 |
    |--------|------|------|------|
    | vm_name | string | user_confirm | ... |

    旧版列表格式（向后兼容）：
    ## 变量
    - node_ip：节点 IP 地址，从环境上下文获取
    """
    declared_vars: dict[str, dict] = {}

    var_section_start = -1
    var_section_end_line: int = -1
    lines = content_md.splitlines()

    all_var_kw = get_keywords("variables") | VARIABLE_SECTION_KEYWORDS

    for i, line in enumerate(lines):
        heading_match = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading_match:
            heading_text = heading_match.group(1).strip()
            if var_section_start < 0:
                for kw in all_var_kw:
                    if kw in heading_text:
                        var_section_start = i
                        break
            elif i > var_section_start:
                var_section_end_line = i
                break

    if var_section_start < 0:
        return declared_vars

    end = var_section_end_line if var_section_end_line > 0 else len(lines)
    section_lines = lines[var_section_start + 1 : end]

    # 尝试识别表格格式
    header_cols: list[str] = []
    for line in section_lines:
        if _TABLE_SEP_RE.match(line.strip()):
            continue
        row_m = _TABLE_ROW_RE.match(line.strip())
        if row_m:
            cols = [c.strip() for c in row_m.group(1).split("|")]
            if not header_cols:
                header_lower = [c.lower() for c in cols]
                if any(c in header_lower for c in ["变量名", "name"]):
                    header_cols = header_lower
                continue
            if header_cols:
                def _col(name: str, *aliases: str, header_cols=header_cols, cols=cols) -> str:
                    for alias in (name, *aliases):
                        if alias in header_cols:
                            idx = header_cols.index(alias)
                            return cols[idx] if idx < len(cols) else ""
                    return ""
                var_name = _col("变量名", "name")
                var_source = _col("来源", "source")
                var_type = _col("类型", "type") or "string"
                var_desc = _col("说明", "description") or ""
                if var_name and var_source:
                    if var_source.startswith("tool:"):
                        strategy = "tool"
                        tool = var_source[5:]
                    elif var_source.startswith("env:"):
                        strategy = "env_context"
                        tool = None
                    elif var_source == "user_confirm":
                        strategy = "user_confirm"
                        tool = None
                    else:
                        strategy = "user_input"
                        tool = None
                    declared_vars[var_name] = {
                        "display_name": var_name,
                        "description": var_desc,
                        "type": var_type,
                        "acquisition_strategy": strategy,
                        "acquisition_tool": tool,
                        "required": True,
                    }
            continue

        # 旧版列表格式（fallback）
        stripped = line.strip()
        list_match = re.match(r"^(?:[-*])\s+([a-z][a-z0-9_]*)[：:]\s*(.*)$", stripped)
        if list_match:
            var_name = list_match.group(1)
            rest = list_match.group(2).strip()
            inferred = _infer_strategy(var_name)
            declared_vars[var_name] = {
                "display_name": var_name,
                "description": rest,
                "acquisition_strategy": inferred["acquisition_strategy"],
                "acquisition_tool": inferred["acquisition_tool"],
                "type": "string",
                "required": True,
            }

    return declared_vars


def _extract_vars_from_text(text: str) -> set[str]:
    """从文本中提取 {placeholder} 格式的变量名。"""
    return set(re.findall(r"\{([a-z][a-z0-9_]*)\}", text))


def _extract_vars_from_tree(node: SOPNode) -> set[str]:
    """从决策树节点中递归提取变量名。"""
    vars_set: set[str] = set()
    vars_set |= _extract_vars_from_text(node.title)
    for item in node.prerequisite_items:
        vars_set |= _extract_vars_from_text(item.description)
    if node.diagnosis:
        vars_set |= _extract_vars_from_text(node.diagnosis.description or "")
        vars_set |= _extract_vars_from_text(node.diagnosis.root_cause or "")
        vars_set |= _extract_vars_from_text(node.diagnosis.notes or "")
        for method in node.diagnosis.page_methods:
            vars_set |= _extract_vars_from_text(method)
        for method in node.diagnosis.acli_methods:
            vars_set |= _extract_vars_from_text(method)
    if node.solution:
        for step in node.solution.quick_recovery:
            vars_set |= _extract_vars_from_text(step)
        for step in node.solution.thorough_fix:
            vars_set |= _extract_vars_from_text(step)
    for child in node.children:
        vars_set |= _extract_vars_from_tree(child)
    return vars_set


def extract_sop_variables(
    content_md: str,
    tree: SOPNode | None = None,
) -> tuple[list[dict], list[str], list[str]]:
    """提取 SOP 变量定义并进行双向校验。

    Returns:
        (variable_defs, undeclared_errors, orphan_warnings)
    """
    declared_vars = _parse_variable_section(content_md)
    used_vars = _extract_vars_from_text(content_md)
    if tree:
        used_vars |= _extract_vars_from_tree(tree)

    undeclared = sorted(used_vars - set(declared_vars.keys()))
    orphan = sorted(set(declared_vars.keys()) - used_vars)

    variable_defs: list[dict] = []
    for var_name in sorted(used_vars):
        declared = declared_vars.get(var_name, {})
        inferred = _infer_strategy(var_name)
        strategy_info = {
            "acquisition_strategy": declared.get("acquisition_strategy") or inferred["acquisition_strategy"],
            "acquisition_tool": declared.get("acquisition_tool") or inferred["acquisition_tool"],
        }
        variable_defs.append({
            "name": var_name,
            "display_name": declared.get("display_name", var_name),
            "description": declared.get("description", ""),
            "type": declared.get("type", "string"),
            "required": True,
            **strategy_info,
            "validation_pattern": declared.get("validation_pattern"),
            "default_value": declared.get("default_value"),
            "auto_generated": var_name not in declared_vars,
        })

    return variable_defs, undeclared, orphan


# ──────────────────────────────────────────────────────────────────────────────
# 变量三路合并（T-AGT-26）
# ──────────────────────────────────────────────────────────────────────────────


def merge_variable_schema(
    old_schema: list[dict],
    new_schema: list[dict],
) -> tuple[list[dict], list[str]]:
    """三路合并变量 Schema（重新导入后维护人工编辑）。"""
    old_by_name = {v["name"]: v for v in old_schema}
    new_by_name = {v["name"]: v for v in new_schema}
    deprecated: list[str] = []
    merged: list[dict] = []

    HUMAN_FIELDS = [
        "description", "acquisition_strategy", "acquisition_tool",
        "acquisition_prompt", "validation_pattern", "default_value", "display_name",
    ]

    for name, new_var in new_by_name.items():
        if name in old_by_name:
            old_var = old_by_name[name]
            merged_var = {**new_var}
            for human_field in HUMAN_FIELDS:
                old_value = old_var.get(human_field)
                if old_value is not None and old_value != "":
                    merged_var[human_field] = old_value
            merged_var.pop("deprecated", None)
            merged_var["auto_generated"] = False
            merged.append(merged_var)
        else:
            merged.append({**new_var, "auto_generated": True})

    for name, old_var in old_by_name.items():
        if name not in new_by_name:
            deprecated_var = {**old_var, "deprecated": True}
            deprecated_var.pop("auto_generated", None)
            merged.append(deprecated_var)
            deprecated.append(name)

    return merged, deprecated
