"""
data-pipeline/raw_to_sop/converter.py — Raw Graph JSON → SOP Markdown 转化器

设计原则（第一性原理）：
  - 目标产物是标准 Markdown 文档，由 sop_parser 解析生成 tree_json，
    不直接生成 tree_json（避免绕过校验层，保证 validation_issues 的完整性）。
  - 转化为"扁平化"树结构：所有 branches 直接挂载在根节点下（H1 → H2），
    flow 的路由条件下沉为每个叶节点的"前置检查"段落内容，
    不产生中间路由层（降低 Agent ReAct 遍历的歧义）。
  - 纯规则转化，无 LLM 调用，无网络依赖，无数据库依赖。

核心映射契约（与 sop_template_rules.yaml 对齐）：
  - `# {meta.node_name}`         → 根节点（H1）
  - `## {branch.branch_name}`    → 叶节点（H2，直接挂根）
  - `#### 前置检查`               → prerequisite_items（routing_signals + flow entry conditions）
  - `#### 判断方法`               → diagnosis.acli_methods（checks 系列命令聚合）
    - `acli判断方法：`            → acli_methods 标签触发（sop_parser 关键约束）
  - `#### 解决方案`               → solution（solution_steps 聚合）
    - `快速恢复方案：`            → quick_recovery 标签触发
    - `彻底解决方案：`            → thorough_fix 标签触发

三阶段处理流程：
  Phase 1 (RawGraphAnalyzer)    — 图结构分析，建立索引
  Phase 2 (PrerequisiteBuilder) — 前置条件聚合（routing_signals + flow entry when）
  Phase 3 (MarkdownSynthesizer) — Markdown 文档生成

External Format Reference（内存ECC故障.json 样本）：
  top-level: meta / flow / branches / excluded_cases / flow_visual
  flow[i]:   step_id / action / command_example / branches[{when, goto_branch_id}] / if_no_match_next
  branch:    branch_id / branch_name / routing_signals / prerequisites /
             checks[{step_id, action, command_example, expected_result, ...}] /
             solution_steps[{action, command_example, expected_result, ...}] /
             temporary_workaround / permanent_fix / root_causes
"""
from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from typing import Any


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1: 图结构分析器
# ──────────────────────────────────────────────────────────────────────────────


class RawGraphAnalyzer:
    """分析 Raw Graph JSON，建立 branch 索引和 flow 路由映射。"""

    def __init__(self, raw_data: dict[str, Any]) -> None:
        self.meta: dict[str, Any] = raw_data.get("meta", {})
        self.flow: list[dict[str, Any]] = raw_data.get("flow", [])
        self.branches: list[dict[str, Any]] = raw_data.get("branches", [])
        self.flow_visual: str = raw_data.get("flow_visual", "")

        # branch_id → branch_data 快速查找
        self.branch_map: dict[str, dict[str, Any]] = {
            b["branch_id"]: b
            for b in self.branches
            if b.get("branch_id")
        }

        # branch_id → [when_condition, ...] (来自 flow 步骤中的跳转条件)
        self.flow_entry_conditions: dict[str, list[str]] = self._build_flow_entry_conditions()

    def _build_flow_entry_conditions(self) -> dict[str, list[str]]:
        """从 flow 步骤中提取每个 branch 的精确进入条件（when 字段）。"""
        conditions: dict[str, list[str]] = {}
        for step in self.flow:
            for branch_ref in step.get("branches", []):
                bid = branch_ref.get("goto_branch_id", "")
                when = (branch_ref.get("when") or "").strip()
                if bid and when:
                    conditions.setdefault(bid, []).append(when)
        return conditions

    @property
    def node_name(self) -> str:
        return self.meta.get("node_name", "未知故障场景")

    @property
    def node_uid(self) -> str:
        return self.meta.get("node_uid", "")

    @property
    def source_case_count(self) -> int:
        return self.meta.get("source_case_count", 0)


# ──────────────────────────────────────────────────────────────────────────────
# Phase 2: 前置条件聚合器
# ──────────────────────────────────────────────────────────────────────────────


class PrerequisiteBuilder:
    """为每个 branch 聚合前置检查条件。

    来源优先级（从精确到宽泛）：
      1. flow entry when 条件（最精确的语义路由条件，来自 flow 分析）
      2. branch.routing_signals（触发该分支的信号词汇集）
      3. branch.prerequisites（手动声明的前置条件）
    """

    def __init__(self, analyzer: RawGraphAnalyzer) -> None:
        self._analyzer = analyzer

    def build(self, branch: dict[str, Any]) -> list[str]:
        """返回去重后的前置条件文本列表（用于生成 `- ` 列表项）。"""
        bid = branch.get("branch_id", "")
        seen: set[str] = set()
        conditions: list[str] = []

        def _add(text: str) -> None:
            cleaned = text.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                conditions.append(cleaned)

        # 1. flow entry when 条件（精确路由语义）
        for when in self._analyzer.flow_entry_conditions.get(bid, []):
            _add(when)

        # 2. routing_signals（触发信号词）
        for sig in branch.get("routing_signals", []):
            _add(sig)

        # 3. prerequisites（手动声明）
        for prereq in branch.get("prerequisites", []):
            _add(prereq)

        return conditions


# ──────────────────────────────────────────────────────────────────────────────
# Phase 3: Markdown 合成器
# ──────────────────────────────────────────────────────────────────────────────


class MarkdownSynthesizer:
    """将 Raw Graph 的 branch 数据合成为标准 SOP Markdown 文档。

    生成的 Markdown 严格符合 sop_parser 的解析规则：
      - 段落标题使用 sop_template_rules.yaml 中的标准话术
      - acli_methods 必须以 `acli判断方法：` 标签行开头
      - quick_recovery 和 thorough_fix 分别以对应标签行开头
      - 前置检查以 `- ` 列表项格式书写
    """

    def __init__(
        self,
        analyzer: RawGraphAnalyzer,
        prereq_builder: PrerequisiteBuilder,
    ) -> None:
        self._analyzer = analyzer
        self._prereq_builder = prereq_builder

    # ── 内部辅助方法 ──────────────────────────────────────────────────────────

    def _clean_text(self, text: str) -> str:
        """清理文本：去除多余空白和特殊控制字符。"""
        return re.sub(r"\s+", " ", text or "").strip()

    def _format_command(self, cmd: str) -> str:
        """将命令包装为 Markdown 代码块。"""
        cmd = cmd.strip()
        if not cmd:
            return ""
        return f"```bash\n{cmd}\n```"

    def _render_checks_as_acli_methods(self, checks: list[dict[str, Any]]) -> list[str]:
        """将 branch.checks 列表渲染为 acli_methods 内容行。

        每个 check 生成：
          - 说明行（action 文本）
          - 命令代码块（command_example，若存在）
          - 预期输出说明（expected_result，若存在）

        注意：sop_parser 中，代码块内容会被合并为一条 acli_methods 列表项，
        说明文字也会被加入 acli_methods（因为 acli判断方法：标签已激活该 field）。
        """
        lines: list[str] = []
        for idx, check in enumerate(checks, 1):
            action = self._clean_text(check.get("action", ""))
            cmd = (check.get("command_example") or "").strip()
            expected = self._clean_text(check.get("expected_result", ""))

            if action:
                # 序号 + 动作说明
                lines.append(f"{idx}. {action}")
            if cmd:
                lines.append(self._format_command(cmd))
            if expected:
                lines.append(f"   **预期输出**：{expected}")

        return lines

    def _render_solution_steps(
        self,
        steps: list[dict[str, Any]],
        label_prefix: str = "",
    ) -> list[str]:
        """将 solution_steps 列表渲染为解决方案内容行。"""
        lines: list[str] = []
        for idx, step in enumerate(steps, 1):
            action = self._clean_text(step.get("action", ""))
            cmd = (step.get("command_example") or "").strip()
            expected = self._clean_text(step.get("expected_result", ""))

            if action:
                lines.append(f"{idx}. {action}")
            if cmd:
                lines.append(self._format_command(cmd))
            if expected:
                lines.append(f"   **预期结果**：{expected}")

        return lines

    # ── 单个 branch → Markdown 段落 ──────────────────────────────────────────

    def render_branch(self, branch: dict[str, Any]) -> str:
        """将单个 branch 渲染为完整的 H2 级别 SOP 章节。

        生成结构（严格对齐 sop_template_rules.yaml 标准话术）：
          ## {branch_name}

          <!-- source_cases: N -->

          #### 前置检查

          - {condition_1}
          - {condition_2}

          #### 判断方法

          acli判断方法：
          {checks rendered}

          #### 解决方案

          快速恢复方案：
          {quick_recovery_steps}

          彻底解决方案：
          {thorough_fix_steps}
        """
        parts: list[str] = []

        # H2 标题
        branch_name = self._clean_text(branch.get("branch_name", "未知故障分支"))
        parts.append(f"## {branch_name}")

        # 溯源注释（保留置信度元数据，HTML 注释不影响解析）
        source_count = branch.get("source_case_count", 0)
        if source_count:
            parts.append(f"<!-- source_cases: {source_count} -->")

        # ── 前置检查（prerequisite_items）──────────────────────────────────
        prereqs = self._prereq_builder.build(branch)
        if prereqs:
            parts.append("\n#### 前置检查\n")
            for p in prereqs:
                parts.append(f"- {p}")

        # ── 判断方法（diagnosis.acli_methods）─────────────────────────────
        checks = branch.get("checks", [])
        # 过滤出命令行可执行的 checks 优先放入 acli_methods
        acli_checks = [c for c in checks if c.get("command_line_executable", False)]
        page_checks = [c for c in checks if not c.get("command_line_executable", False)]

        parts.append("\n#### 判断方法\n")

        # 页面判断方法（可选）
        if page_checks:
            parts.append("页面判断方法：")
            for idx, check in enumerate(page_checks, 1):
                action = self._clean_text(check.get("action", ""))
                if action:
                    parts.append(f"{idx}. {action}")

        # acli 判断方法（必填，sop_parser 严格要求此标签）
        if acli_checks:
            parts.append("\nacli判断方法：")
            acli_lines = self._render_checks_as_acli_methods(acli_checks)
            parts.extend(acli_lines)
        elif checks:
            # 若无显式可执行命令，将所有 checks 放入 acli_methods
            # （sop_parser 要求 acli_methods 必填，兜底策略）
            parts.append("\nacli判断方法：")
            acli_lines = self._render_checks_as_acli_methods(checks)
            parts.extend(acli_lines)

        # 根因分析（可选，追加到判断方法段落内，sop_parser 会归入 acli_methods）
        root_causes: list[str] = branch.get("root_causes", [])
        if root_causes:
            parts.append("\n**根因分析**：")
            for cause in root_causes:
                parts.append(f"- {self._clean_text(cause)}")

        # ── 解决方案（solution）────────────────────────────────────────────
        parts.append("\n#### 解决方案\n")

        # 确定快速恢复来源
        # temporary_workaround / permanent_fix 在 Raw JSON 中均为 list[str]
        raw_temp = branch.get("temporary_workaround") or []
        raw_perm = branch.get("permanent_fix") or []
        # 兼容 str 类型（以防外部格式变化）
        if isinstance(raw_temp, str):
            temp_fix_items = [raw_temp] if raw_temp.strip() else []
        else:
            temp_fix_items = [s for s in raw_temp if isinstance(s, str) and s.strip()]
        if isinstance(raw_perm, str):
            perm_fix_items = [raw_perm] if raw_perm.strip() else []
        else:
            perm_fix_items = [s for s in raw_perm if isinstance(s, str) and s.strip()]

        solution_steps: list[dict[str, Any]] = branch.get("solution_steps", [])

        if temp_fix_items:
            parts.append("快速恢复方案：")
            for item in temp_fix_items:
                parts.append(f"- {self._clean_text(item)}")
        elif solution_steps:
            parts.append("快速恢复方案：")
            quick_lines = self._render_solution_steps(solution_steps)
            parts.extend(quick_lines)

        if perm_fix_items:
            parts.append("\n彻底解决方案：")
            for item in perm_fix_items:
                parts.append(f"- {self._clean_text(item)}")
        elif solution_steps:
            # 无 permanent_fix 时复用 solution_steps（sop_parser 会记录 warning 但不阻断）
            parts.append("\n彻底解决方案：")
            thorough_lines = self._render_solution_steps(solution_steps)
            parts.extend(thorough_lines)

        return "\n".join(parts)

    # ── 完整文档合成 ──────────────────────────────────────────────────────────

    def synthesize(self) -> str:
        """合成完整 SOP Markdown 文档。

        结构：
          # {node_name}

          <!-- meta: node_path=..., source_cases=N, generated_by=raw_to_sop -->

          ## {branch_A_name}
          ...
          ## {branch_B_name}
          ...
        """
        lines: list[str] = []

        # 根节点 H1（对应 SOPNode 根节点）
        lines.append(f"# {self._analyzer.node_name}")

        # 元数据注释（保留完整溯源信息，不影响解析）
        node_path = self._analyzer.meta.get("node_path", "")
        source_count = self._analyzer.source_case_count
        node_uid = self._analyzer.node_uid
        meta_comment_parts = []
        if node_path:
            meta_comment_parts.append(f"node_path={node_path}")
        if source_count:
            meta_comment_parts.append(f"source_cases={source_count}")
        if node_uid:
            meta_comment_parts.append(f"node_uid={node_uid}")
        meta_comment_parts.append("generated_by=raw_to_sop")
        lines.append(f"<!-- meta: {', '.join(meta_comment_parts)} -->")
        lines.append("")

        # 各 branch 章节（H2，扁平挂根）
        valid_branches = [b for b in self._analyzer.branches if b.get("branch_id")]
        if not valid_branches:
            raise ValueError(
                f"Raw JSON 中没有有效的 branch（含 branch_id 字段）：{self._analyzer.node_name}"
            )

        for branch in valid_branches:
            branch_md = self.render_branch(branch)
            lines.append(branch_md)
            lines.append("")  # 段落间空行

        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 公共 API
# ──────────────────────────────────────────────────────────────────────────────


def convert_raw_json_to_markdown(raw_data: dict[str, Any]) -> tuple[str, str, str]:
    """将 Raw Graph JSON dict 转化为 SOP Markdown 文档字符串。

    Args:
        raw_data: 外部 Raw Graph JSON 的 Python dict（已解析）

    Returns:
        (title, source_id, markdown_content)
        - title:      SOP 文档标题（= meta.node_name）
        - source_id:  幂等 key（= raw-{node_uid}，用于 API 去重）
        - markdown:   符合 sop_parser 规范的 Markdown 文档内容

    Raises:
        ValueError: 缺少必要字段或 branches 为空
    """
    analyzer = RawGraphAnalyzer(raw_data)

    if not analyzer.node_name or analyzer.node_name == "未知故障场景":
        raise ValueError("Raw JSON 缺少 meta.node_name 字段")

    if not analyzer.branches:
        raise ValueError(f"Raw JSON 缺少 branches 字段或为空：{analyzer.node_name}")

    prereq_builder = PrerequisiteBuilder(analyzer)
    synthesizer = MarkdownSynthesizer(analyzer, prereq_builder)

    markdown = synthesizer.synthesize()
    title = analyzer.node_name
    node_uid = analyzer.node_uid or analyzer.node_name
    source_id = f"raw-{re.sub(r'[^a-zA-Z0-9_-]', '_', node_uid)[:48]}"

    return title, source_id, markdown


def convert_raw_json_file(file_path: Path) -> tuple[str, str, str]:
    """从文件路径读取 Raw Graph JSON 并转化为 SOP Markdown。

    Args:
        file_path: .json 文件路径（支持中文文件名）

    Returns:
        (title, source_id, markdown_content)
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Raw JSON 文件不存在：{file_path}")

    raw_data: dict[str, Any] = json.loads(file_path.read_text(encoding="utf-8"))
    return convert_raw_json_to_markdown(raw_data)
