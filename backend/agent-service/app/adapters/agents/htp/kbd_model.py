"""
KBD 数据模型（知识库文档 Knowledge Base Document）

设计约定：
  - KBDStep.expected_pattern 支持三种格式：
      __REGEX__:<regex>      正则匹配（全文搜索）
      __CONTAINS__:<text>    包含文本（不区分大小写）
      <自然语言>             留给 LLM 判断，作为 judge prompt 的参考依据
  - tool_args_template 中的 {{vm_name}}、{{host_id}} 等占位符在执行时
    由 env_context（S0 阶段采集的环境上下文）替换
"""

from __future__ import annotations

from dataclasses import dataclass

# 期望模式前缀常量，供判断逻辑识别
PATTERN_REGEX_PREFIX = "__REGEX__:"
PATTERN_CONTAINS_PREFIX = "__CONTAINS__:"


@dataclass
class KBDStep:
    """KBD 中单个诊断步骤的定义。

    tool_name 是归一化 key：同一分类下不同 KBD 若使用相同工具，
    其 tool_name 相同，便于 CDD 算法计算步骤覆盖频率。
    """

    tool_name: str  # 工具名称（对应 tool_registry 中的 ToolDefinition.name）
    tool_args_template: dict  # 参数模板（含 {{占位符}}，执行时由 env_context 填充）
    expected_pattern: str  # 期望输出特征（__REGEX__:/ __CONTAINS__:/ 自然语言）


@dataclass
class KBD:
    """知识库文档（Knowledge Base Document）。

    由 kb_client.search_cases_with_steps() 从知识库检索获得。
    KBD 差异诊断引擎对 candidates: list[KBD] 执行差异分析。

    字段与文档章节对应关系（8 大标准章节）：
      核心字段（4 个，诊断与呈现必需）：
        problem_description → 问题描述
        steps               → 有效排查步骤
        root_cause          → 根因
        solution            → 解决方案
      补充字段（4 个，来自 KB 文档，默认为空）：
        alert_info          → 告警信息
        operational_impact  → 操作影响范围
        is_temporary        → 是否是临时解决方案
        recommendations     → 建议与总结
    """

    # ── 基础标识 ──────────────────────────────────────────────
    id: str  # KBD 唯一 ID
    name: str  # KBD 名称（简短描述）
    category_id: str  # 所属故障分类编码，如 "虚拟机-003"

    # ── 核心内容（4 大章节，必填）────────────────────────────
    problem_description: str  # 问题描述：描述该 KBD 对应的故障现象
    steps: list[KBDStep]  # 有效排查步骤
    root_cause: str  # 根因
    solution: str  # 解决方案

    # ── 补充内容（4 个章节，可选）────────────────────────────
    alert_info: str = ""  # 告警信息（原始告警文本）
    operational_impact: str = ""  # 操作影响范围
    is_temporary: str = ""  # 是否是临时解决方案
    recommendations: str = ""  # 建议与总结

    # ── 检索元数据 ────────────────────────────────────────────
    similarity: float = 0.0  # 与用户查询的相似度评分（来自 KB 检索）

    @property
    def step_tool_names(self) -> set[str]:
        """返回本 KBD 所有步骤的 tool_name 集合（用于频率统计）。"""
        return {s.tool_name for s in self.steps}

    def get_step(self, tool_name: str) -> KBDStep | None:
        """按 tool_name 获取步骤定义（不存在时返回 None）。"""
        for step in self.steps:
            if step.tool_name == tool_name:
                return step
        return None

    def get_expected_pattern(self, tool_name: str) -> str | None:
        """返回指定工具对应的期望输出模式（用于 judge 步骤）。"""
        step = self.get_step(tool_name)
        return step.expected_pattern if step else None


def kbd_from_dict(d: dict) -> KBD:
    """从 KB API 返回的 dict 构建 KBD 对象（工厂函数）。

    KB 响应格式（对应 8 大标准章节）：
      {
        "id": "kbd-001",
        "name": "...",
        "category_id": "虚拟机-003",
        "similarity": 0.87,
        // 核心字段
        "problem_description": "...",
        "root_cause": "...",
        "solution": "...",
        "steps": [
          {"tool_name": "acli_vm_config", "tool_args_template": {...}, "expected_pattern": "..."},
          ...
        ],
        // 补充字段（可缺省）
        "alert_info": "...",
        "operational_impact": "...",
        "is_temporary": "...",
        "recommendations": "..."
      }
    """
    steps = [
        KBDStep(
            tool_name=s["tool_name"],
            tool_args_template=s.get("tool_args_template", {}),
            expected_pattern=s.get("expected_pattern", ""),
        )
        for s in d.get("steps", [])
    ]
    return KBD(
        id=d["id"],
        name=d.get("name", ""),
        category_id=d.get("category_id", ""),
        problem_description=d.get("problem_description", ""),
        steps=steps,
        root_cause=d.get("root_cause", ""),
        solution=d.get("solution", ""),
        alert_info=d.get("alert_info", ""),
        operational_impact=d.get("operational_impact", ""),
        is_temporary=d.get("is_temporary", ""),
        recommendations=d.get("recommendations", ""),
        similarity=float(d.get("similarity", 0.0)),
    )
