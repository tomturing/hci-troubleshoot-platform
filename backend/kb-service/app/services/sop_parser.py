"""
KB Service — SOP 多叉决策树 Markdown 解析器

解析策略：叶优先（"找头尾"）
  1. 扫描所有 Markdown 标题（不截断层级，支持任意深度）
  2. 关键词分类：diagnosis/solution 段落 vs 普通节点标题
  3. 按相对层级差构建树（根节点由顶层标题自动识别）
  4. diagnosis/solution 段落挂到当前最近祖先节点的对应字段
  5. 扫描完成后校验叶节点完整性，收集 ValidationIssue
  6. 返回 SOPValidationResult（error → tree=None，warning → tree 仍返回）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.schemas.sop_template import (
    DiagnosisDetail,
    SolutionDetail,
    SOPNode,
    SOPValidationResult,
    ValidationIssue,
)

# ──────────────────────────────────────────────────────────────────────────────
# 变量名启发式规则（来自 §12.7.3）
# ──────────────────────────────────────────────────────────────────────────────

STRATEGY_HINTS: dict[str, str] = {
    # 环境上下文类变量（自动从 case/conversation 获取）
    r".*_ip$|^node_ip$|^cluster_ip$|^host_ip$|^server_ip$": "env_context",
    r"^cluster_name$|^node_name$|^host_name$|^server_name$": "env_context",
    # 工具获取类变量（需要调用诊断工具）
    r"^vm_name$|^vm_id$": "tool:get_vm_list",
    r"^disk_id$|^disk_name$": "tool:acli_storage_disk_list",
    r"^nic_name$|^nic_id$": "tool:acli_network_nic_list",
    r"^volume_id$|^volume_name$": "tool:acli_storage_volume_list",
}

# 变量章节标题关键词（等效列表）
VARIABLE_SECTION_KEYWORDS: frozenset[str] = frozenset(
    ["变量", "变量定义", "参数", "参数定义", "环境变量"]
)

# ──────────────────────────────────────────────────────────────────────────────
# 关键词等效表（解析器层唯一来源，模型层不重复）
# ──────────────────────────────────────────────────────────────────────────────

DIAGNOSIS_KEYWORDS: frozenset[str] = frozenset(
    ["判断方法", "判断依据", "排查方法", "排查步骤", "识别方法", "确认方法", "诊断方法"]
)
SOLUTION_KEYWORDS: frozenset[str] = frozenset(
    ["解决方案", "解决方法", "处理方法", "处理步骤", "修复方法", "修复步骤", "解决步骤"]
)

# 标准话术（source_heading 与此不同时生成 warning）
STANDARD_DIAGNOSIS_HEADING = "判断方法"
STANDARD_SOLUTION_HEADING = "解决方案"

# diagnosis 段落内容：标签文本 → 列表字段名
_DIAGNOSIS_LIST_MAP: dict[str, str] = {
    "页面判断方法": "page_methods",
    "页面操作步骤": "page_methods",
    "acli判断方法": "acli_methods",
    "acli命令行": "acli_methods",
    "命令行判断": "acli_methods",
    "前置检查": "prerequisites",
}

# diagnosis 段落内容：标签文本 → 文本字段名
_DIAGNOSIS_TEXT_MAP: dict[str, str] = {
    "判断说明": "description",
    "问题根因": "root_cause",
    "原因": "root_cause",
    "注意事项": "notes",
}

# solution 段落内容：标签文本 → 列表字段名（无文本字段）
_SOLUTION_LIST_MAP: dict[str, str] = {
    "快速恢复方案": "quick_recovery",
    "快速恢复": "quick_recovery",
    "临时处理": "quick_recovery",
    "彻底解决方案": "thorough_fix",
    "彻底修复": "thorough_fix",
    "永久修复": "thorough_fix",
    "根本解决": "thorough_fix",
}

# 列表项正则（支持 - / * / 1. / 1、 格式）
_LIST_ITEM_RE = re.compile(r"^(?:[-*]|\d+[.、])\s+(.+)$")

# 标签行正则：以"字段名："或"字段名:"开头
_LABEL_RE = re.compile(r"^(.+?)[：:]\s*(.*)$")


# ──────────────────────────────────────────────────────────────────────────────
# 内部数据结构
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class _SectionEntry:
    """解析过程中的中间表示：一个标题及其正文内容行"""

    level: int  # 标题层级（# 的数量）
    text: str  # 标题文本（去除 # 前缀后）
    section_type: Literal["node", "diagnosis", "solution"]  # 分类结果
    content: list[str] = field(default_factory=list)  # 正文行（已 strip）


# ──────────────────────────────────────────────────────────────────────────────
# 公共 API：关键词分类
# ──────────────────────────────────────────────────────────────────────────────


# 不识别为 diagnosis/solution 的后缀词（中间节点标题通常以这些词结尾）
_STRUCTURAL_SUFFIXES: frozenset[str] = frozenset(
    ["概述", "汇总", "总览", "简介", "概要", "目录"]
)


def classify_heading(text: str) -> Literal["diagnosis", "solution", "node"]:
    """判断标题文本的语义类型

    匹配规则：标题包含 diagnosis/solution 关键词，且不以结构性后缀词结尾。
    例：
      "判断方法"          → diagnosis（精确等于关键词）
      "Redis OOM 判断方法" → diagnosis（包含关键词，无结构后缀）
      "判断方法概述"       → node（包含关键词但以"概述"结尾，为中间节点标题）
    """
    for suffix in _STRUCTURAL_SUFFIXES:
        if text.endswith(suffix):
            return "node"
    for kw in DIAGNOSIS_KEYWORDS:
        if kw in text:
            return "diagnosis"
    for kw in SOLUTION_KEYWORDS:
        if kw in text:
            return "solution"
    return "node"


# ──────────────────────────────────────────────────────────────────────────────
# 内部：文档分段解析
# ──────────────────────────────────────────────────────────────────────────────


def _parse_into_sections(content_md: str) -> list[_SectionEntry]:
    """将 Markdown 文本解析为 _SectionEntry 列表

    关键逻辑：
    - 一旦进入 diagnosis/solution 段落（遇到相应关键词标题），
      其下更深层标题转为标签行内容（"标题文本："格式），
      直到遇到同层或更浅层标题为止。
    - 这样可以支持 docx 中用子标题代替文本标签的写法。
    """
    sections: list[_SectionEntry] = []

    # 当前段落状态
    cur_level: int = 0
    cur_text: str = ""
    cur_type: Literal["node", "diagnosis", "solution"] = "node"
    cur_content: list[str] = []

    # 当前 diagnosis/solution 段落的起始层级（None = 不在该模式）
    ds_depth: int | None = None

    for line in content_md.splitlines():
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
                    )
                )

            cur_level = h_level
            cur_text = h_text
            cur_type = h_type
            cur_content = []

            # 进入 diagnosis/solution 模式
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
            )
        )

    return sections


# ──────────────────────────────────────────────────────────────────────────────
# 内部：解析内容行为字段
# ──────────────────────────────────────────────────────────────────────────────


def _parse_content_lines(
    content: list[str],
    list_field_map: dict[str, str],
    text_field_map: dict[str, str],
    default_list_field: str,
) -> tuple[dict[str, list[str]], dict[str, str | None]]:
    """通用内容行解析器

    按标签切换当前字段，无标签的列表项进 default_list_field。

    Args:
        content: 正文行列表（已 strip）
        list_field_map: 标签文本 → 列表字段名
        text_field_map: 标签文本 → 文本字段名（str | None 型）
        default_list_field: 无标签列表项的默认目标字段名

    Returns:
        (list_result, text_result) 二元组
    """
    # 初始化列表结果（所有已知列表字段）
    list_result: dict[str, list[str]] = {}
    for fname in set(list_field_map.values()):
        list_result[fname] = []
    if default_list_field not in list_result:
        list_result[default_list_field] = []

    # 初始化文本结果
    text_result: dict[str, str | None] = {
        fname: None for fname in set(text_field_map.values())
    }

    cur_list_field = default_list_field
    cur_text_field: str | None = None

    for line in content:
        # ① 尝试匹配标签行（标签：内容 格式）
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
                cur_list_field = default_list_field  # 回到默认列表字段
                if rest:
                    text_result[cur_text_field] = rest
                continue

        # ② 尝试匹配列表项
        list_m = _LIST_ITEM_RE.match(line)
        if list_m:
            item = list_m.group(1).strip()
            if item:
                list_result[cur_list_field].append(item)
            continue

        # ③ 普通文本行
        if line:
            if cur_text_field:
                prev = text_result.get(cur_text_field)
                text_result[cur_text_field] = f"{prev}\n{line}" if prev else line
            else:
                # 无文本字段上下文时，追加到当前列表字段
                list_result[cur_list_field].append(line)

    return list_result, text_result


def _build_diagnosis_detail(
    content: list[str],
    source_heading: str,
    location: str,
    issues: list[ValidationIssue],
) -> DiagnosisDetail | None:
    """从内容行构建 DiagnosisDetail

    若内容为空或无有效页面判断方法，则记 warning 并返回 None。
    """
    if not content:
        issues.append(
            ValidationIssue(
                level="warning",
                location=location,
                message=f"「{source_heading}」段落无任何内容，diagnosis 字段未设置",
            )
        )
        return None

    list_r, text_r = _parse_content_lines(
        content, _DIAGNOSIS_LIST_MAP, _DIAGNOSIS_TEXT_MAP, "page_methods"
    )

    page_methods = list_r.get("page_methods", [])
    if not page_methods:
        issues.append(
            ValidationIssue(
                level="warning",
                location=location,
                message=f"「{source_heading}」段落未找到页面判断方法，diagnosis 字段未设置",
            )
        )
        return None

    return DiagnosisDetail(
        prerequisites=list_r.get("prerequisites", []),
        page_methods=page_methods,
        acli_methods=list_r.get("acli_methods", []),
        description=text_r.get("description"),
        root_cause=text_r.get("root_cause"),
        notes=text_r.get("notes"),
        source_heading=source_heading,
    )


def _build_solution_detail(
    content: list[str],
    source_heading: str,
    location: str,
    issues: list[ValidationIssue],
) -> SolutionDetail | None:
    """从内容行构建 SolutionDetail

    若内容为空则记 warning 并返回 None；
    只有 quick_recovery 时复用为 thorough_fix（记 warning）；反之亦然。
    """
    if not content:
        issues.append(
            ValidationIssue(
                level="warning",
                location=location,
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
                level="warning",
                location=location,
                message=f"「{source_heading}」段落无有效内容，solution 字段未设置",
            )
        )
        return None

    # 单方向缺失时互相补充，记 warning
    if quick_recovery and not thorough_fix:
        issues.append(
            ValidationIssue(
                level="warning",
                location=location,
                message=f"「{source_heading}」段落缺少彻底解决方案，已复用快速恢复方案内容",
            )
        )
        thorough_fix = list(quick_recovery)
    elif thorough_fix and not quick_recovery:
        issues.append(
            ValidationIssue(
                level="warning",
                location=location,
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
# 内部：构建树
# ──────────────────────────────────────────────────────────────────────────────


def _get_location(stack: list[tuple[int, SOPNode]]) -> str:
    """从栈生成节点路径字符串，用于 ValidationIssue.location"""
    return " > ".join(node.name for _, node in stack)


def _build_tree(
    sections: list[_SectionEntry],
) -> tuple[SOPNode | None, list[ValidationIssue]]:
    """将 _SectionEntry 列表构建为 SOPNode 树

    使用栈追踪层级上下文，根节点由第一个 node 类型段落确定。

    Returns:
        (root_node, issues) 二元组；root_node 可能为 None（全空）
    """
    issues: list[ValidationIssue] = []
    root: SOPNode | None = None

    # 栈：[(level, node), ...]，追踪当前节点上下文
    stack: list[tuple[int, SOPNode]] = []

    for section in sections:
        if section.section_type == "node":
            # 弹出所有层级 >= 当前层级的节点（确保父子关系正确）
            while stack and stack[-1][0] >= section.level:
                stack.pop()

            node = SOPNode(name=section.text, level=section.level)

            if not stack:
                # 无父节点：成为根节点（或多根时追加到已有根的子节点）
                if root is None:
                    root = node
                else:
                    issues.append(
                        ValidationIssue(
                            level="warning",
                            location=section.text,
                            message=(
                                f"发现多个顶层节点「{section.text}」（层级 {section.level}），"
                                "已追加到第一根节点的子节点列表"
                            ),
                        )
                    )
                    root.children.append(node)
            else:
                stack[-1][1].children.append(node)

            stack.append((section.level, node))

        elif section.section_type in ("diagnosis", "solution"):
            if not stack:
                issues.append(
                    ValidationIssue(
                        level="warning",
                        location=section.text,
                        message=f"「{section.text}」段落出现在文档最前，无父节点，已跳过",
                    )
                )
                continue

            owner = stack[-1][1]
            location = _get_location(stack)

            if section.section_type == "diagnosis":
                owner.diagnosis = _build_diagnosis_detail(
                    section.content, section.text, location, issues
                )
                # 话术规范检查
                if section.text != STANDARD_DIAGNOSIS_HEADING:
                    issues.append(
                        ValidationIssue(
                            level="warning",
                            location=location,
                            message=f"话术不规范：「{section.text}」建议改为「{STANDARD_DIAGNOSIS_HEADING}」",
                        )
                    )
            else:
                owner.solution = _build_solution_detail(
                    section.content, section.text, location, issues
                )
                # 话术规范检查
                if section.text != STANDARD_SOLUTION_HEADING:
                    issues.append(
                        ValidationIssue(
                            level="warning",
                            location=location,
                            message=f"话术不规范：「{section.text}」建议改为「{STANDARD_SOLUTION_HEADING}」",
                        )
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
    """递归校验叶节点：必须同时具有 diagnosis 和 solution"""
    location = " > ".join(path)

    if node.is_leaf:
        if node.diagnosis is None:
            issues.append(
                ValidationIssue(
                    level="error",
                    location=location,
                    message="叶节点缺少判断方法段落（等效关键词：判断方法/排查方法 等）",
                )
            )
        if node.solution is None:
            issues.append(
                ValidationIssue(
                    level="error",
                    location=location,
                    message="叶节点缺少解决方案段落（等效关键词：解决方案/处理方法 等）",
                )
            )
    else:
        for child in node.children:
            _validate_leaves(child, path + [child.name], issues)


# ──────────────────────────────────────────────────────────────────────────────
# 内部：分配 node_id
# ──────────────────────────────────────────────────────────────────────────────


def _assign_node_ids(node: SOPNode, path: list[int]) -> None:
    """递归分配 node_id，格式 n-1-2-3（根节点为 n-1）"""
    node.node_id = "n-" + "-".join(str(i) for i in path)
    for idx, child in enumerate(node.children, start=1):
        _assign_node_ids(child, path + [idx])


# ──────────────────────────────────────────────────────────────────────────────
# 公共 API：主解析入口
# ──────────────────────────────────────────────────────────────────────────────


def parse_sop_markdown(content_md: str) -> SOPValidationResult:
    """解析 Markdown 文本为 SOPNode 决策树，并返回校验结果

    解析流程：
      1. _parse_into_sections → 标题分段（diagnosis/solution 段落内子标题合并为内容）
      2. _build_tree          → 栈驱动，按相对层级差构建树 + 填充 diagnosis/solution
      3. _validate_leaves     → 叶节点完整性校验（缺 diagnosis 或 solution → error）
      4. _assign_node_ids     → 为所有节点分配 n-x-x 格式 ID

    Returns:
        SOPValidationResult:
          - is_valid=True  → errors 为空，tree 非 None（warnings 可存在）
          - is_valid=False → errors 非空，tree=None（error 级别问题阻断入库）
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
                    message="文档无法识别任何标题，请确认 Markdown 格式正确",
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
                    message="无法识别根节点，请确认文档包含标题（# 标题格式）",
                )
            ],
        )

    # 叶节点完整性校验（缺 diagnosis 或 solution → error）
    _validate_leaves(root, [root.name], build_issues)

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
    """根据变量名推断获取策略

    Args:
        var_name: 变量名（如 node_ip, vm_name）

    Returns:
        dict: {"acquisition_strategy": str, "acquisition_tool": str | None}
    """
    for pattern, strategy in STRATEGY_HINTS.items():
        if re.match(pattern, var_name):
            if strategy.startswith("tool:"):
                return {"acquisition_strategy": "tool", "acquisition_tool": strategy[5:]}
            return {"acquisition_strategy": strategy, "acquisition_tool": None}
    # 默认策略：用户手动输入
    return {"acquisition_strategy": "user_input", "acquisition_tool": None}


def _parse_variable_section(content_md: str) -> dict[str, dict]:
    """解析 ## 变量 章节，提取声明变量

    章节格式：
    ## 变量
    - node_ip：节点 IP 地址，从环境上下文获取
    - vm_name：虚拟机名称，需要调用工具获取

    Args:
        content_md: SOP Markdown 内容

    Returns:
        dict: {变量名: {display_name, description, acquisition_strategy, ...}}
    """
    declared_vars: dict[str, dict] = {}

    # 找到变量章节（支持等效关键词）
    var_section_start = -1
    var_section_end = len(content_md)
    lines = content_md.splitlines()

    for i, line in enumerate(lines):
        heading_match = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading_match:
            heading_text = heading_match.group(1).strip()
            # 检查是否是变量章节标题
            for kw in VARIABLE_SECTION_KEYWORDS:
                if kw in heading_text:
                    var_section_start = i
                    break
            # 如果已进入变量章节，遇到新标题则结束
            if var_section_start >= 0 and i > var_section_start and heading_text not in VARIABLE_SECTION_KEYWORDS:
                var_section_end = i
                break

    if var_section_start < 0:
        return declared_vars  # 无变量章节

    # 解析变量章节内容（列表项格式）
    var_section_lines = lines[var_section_start + 1:var_section_end]

    for line in var_section_lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 匹配列表项：- var_name：描述 或 * var_name：描述
        list_match = re.match(r"^(?:[-*])\s+([a-z][a-z0-9_]*)[：:]\s*(.*)$", stripped)
        if list_match:
            var_name = list_match.group(1)
            rest = list_match.group(2).strip()

            # 尝试从描述中提取策略（如"从环境上下文获取"）
            strategy_override = None
            strategy_hint_patterns = [
                (r".*环境上下文.*|.*自动获取.*|.*从 case.*", "env_context"),
                (r".*工具.*获取.*|.*调用.*工具.*", "tool"),
                (r".*用户.*输入.*|.*手动.*填写.*", "user_input"),
                (r".*用户.*确认.*|.*确认.*选择.*", "user_confirm"),
            ]
            for pat, strat in strategy_hint_patterns:
                if re.search(pat, rest):
                    strategy_override = strat
                    break

            declared_vars[var_name] = {
                "display_name": var_name,
                "description": rest,
                "acquisition_strategy": strategy_override or _infer_strategy(var_name)["acquisition_strategy"],
                "acquisition_tool": _infer_strategy(var_name)["acquisition_tool"] if strategy_override == "tool" else None,
                "type": "string",
                "required": True,
            }

    return declared_vars


def _extract_vars_from_text(text: str) -> set[str]:
    """从文本中提取 {placeholder} 格式的变量名

    Args:
        text: 文本内容

    Returns:
        set: 变量名集合
    """
    # 匹配 {var_name} 格式，变量名规则：小写字母开头，允许小写字母、数字、下划线
    return set(re.findall(r"\{([a-z][a-z0-9_]*)\}", text))


def _extract_vars_from_tree(node: SOPNode) -> set[str]:
    """从决策树节点中递归提取变量名

    扫描范围：
    - node.name（节点名称）
    - node.prerequisites（前置条件）
    - node.diagnosis（判断方法，包括 page_methods/acli_methods）
    - node.solution（解决方案）

    Args:
        node: SOPNode 树节点

    Returns:
        set: 变量名集合
    """
    vars_set: set[str] = set()

    # 节点名称
    vars_set |= _extract_vars_from_text(node.name)

    # 前置条件
    for prereq in node.prerequisites:
        vars_set |= _extract_vars_from_text(prereq)

    # diagnosis 字段
    if node.diagnosis:
        vars_set |= _extract_vars_from_text(node.diagnosis.description or "")
        vars_set |= _extract_vars_from_text(node.diagnosis.root_cause or "")
        vars_set |= _extract_vars_from_text(node.diagnosis.notes or "")
        for method in node.diagnosis.page_methods:
            vars_set |= _extract_vars_from_text(method)
        for method in node.diagnosis.acli_methods:
            vars_set |= _extract_vars_from_text(method)

    # solution 字段
    if node.solution:
        for step in node.solution.quick_recovery:
            vars_set |= _extract_vars_from_text(step)
        for step in node.solution.thorough_fix:
            vars_set |= _extract_vars_from_text(step)

    # 递归子节点
    for child in node.children:
        vars_set |= _extract_vars_from_tree(child)

    return vars_set


def extract_sop_variables(
    content_md: str,
    tree: SOPNode | None = None,
) -> tuple[list[dict], list[str], list[str]]:
    """提取 SOP 变量定义并进行双向校验

    功能：
    1. 解析 ## 变量 章节获取 declared_vars（变量名 → 手动描述）
    2. 扫描全文 + tree_json 中的 {placeholder}
    3. 双向 diff：
       - undeclared = used_vars - declared_vars → Error（用但未声明）
       - orphan = declared_vars - used_vars → Warning（声明但未用）
    4. 构建 SopVariableDefinition 列表（含启发式策略推断）

    Args:
        content_md: SOP Markdown 正文
        tree: 解析后的 SOPNode 决策树（可选）

    Returns:
        tuple: (variable_defs, undeclared_errors, orphan_warnings)
            - variable_defs: 变量定义列表
            - undeclared_errors: 未声明但使用的变量名列表
            - orphan_warnings: 声明但未使用的变量名列表
    """
    # 1. 解析 ## 变量 章节获取声明变量
    declared_vars = _parse_variable_section(content_md)

    # 2. 扫描全文 + tree_json 中的 {placeholder}
    used_vars = _extract_vars_from_text(content_md)
    if tree:
        used_vars |= _extract_vars_from_tree(tree)

    # 3. 双向 diff
    undeclared = sorted(used_vars - set(declared_vars.keys()))  # Error：用但未声明
    orphan = sorted(set(declared_vars.keys()) - used_vars)  # Warning：声明但未用

    # 4. 构建 SopVariableDefinition 列表（仅包含实际使用的变量）
    variable_defs: list[dict] = []
    for var_name in sorted(used_vars):
        declared = declared_vars.get(var_name, {})
        # 如果未声明，使用推断的策略
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
            "auto_generated": var_name not in declared_vars,  # 标记是否为自动推断
        })

    return variable_defs, undeclared, orphan


# ──────────────────────────────────────────────────────────────────────────────
# 变量三路合并（T-AGT-26）
# ──────────────────────────────────────────────────────────────────────────────


def merge_variable_schema(
    old_schema: list[dict],
    new_schema: list[dict],
) -> tuple[list[dict], list[str]]:
    """三路合并变量 Schema（重新导入后维护人工编辑）

    合并规则：
    1. 两版都有 → 保留 old 的 description、acquisition_strategy、acquisition_tool、
                   acquisition_prompt、validation_pattern（人工知识），更新 auto_generated=False
    2. 仅新版有 → 直接追加，auto_generated=True
    3. 仅旧版有 → 标记 deprecated=True，保留在列表中

    Args:
        old_schema: 旧版 variable_schema（可能含人工编辑）
        new_schema: 新版 variable_schema（自动解析结果）

    Returns:
        tuple: (merged_schema, deprecated_names)
            - merged_schema: 合并后的 variable_schema
            - deprecated_names: 标记 deprecated 的变量名列表（用于告警）
    """
    old_by_name = {v["name"]: v for v in old_schema}
    new_by_name = {v["name"]: v for v in new_schema}
    deprecated: list[str] = []
    merged: list[dict] = []

    # 人工编辑字段列表（三路合并时保留）
    HUMAN_FIELDS = [
        "description",
        "acquisition_strategy",
        "acquisition_tool",
        "acquisition_prompt",
        "validation_pattern",
        "default_value",
        "display_name",
    ]

    # 新版变量（保留旧版人工字段）
    for name, new_var in new_by_name.items():
        if name in old_by_name:
            old_var = old_by_name[name]
            merged_var = {**new_var}
            # 保留人工编辑字段（优先使用旧版值）
            for human_field in HUMAN_FIELDS:
                old_value = old_var.get(human_field)
                if old_value is not None and old_value != "":
                    merged_var[human_field] = old_value
            # 清除 deprecated 标记（变量重新出现）
            merged_var.pop("deprecated", None)
            # 标记为已确认（非自动生成）
            merged_var["auto_generated"] = False
            merged.append(merged_var)
        else:
            # 新版新增变量：标记为自动生成
            merged.append({**new_var, "auto_generated": True})

    # 旧版独有（标记 deprecated）
    for name, old_var in old_by_name.items():
        if name not in new_by_name:
            # 清除 auto_generated（deprecated 变量不再参与自动流程）
            deprecated_var = {**old_var, "deprecated": True}
            deprecated_var.pop("auto_generated", None)
            merged.append(deprecated_var)
            deprecated.append(name)

    return merged, deprecated
