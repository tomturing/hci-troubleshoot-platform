"""
KB Service — SOP 多叉决策树 Pydantic 校验模型（v2）

v2 改动摘要（相对 v1）：
  - 新增 VariableDeclaration：变量声明表格条目
  - 新增 PrerequisiteItem：前置检查条目（区分过滤型 / 调序型）
  - DiagnosisDetail.page_methods → 可选（新模板标注"可选"）
  - DiagnosisDetail.acli_methods → 必填（min_length=1）
  - SOPNode 新增 variables / prerequisite_items 字段
  - ValidationIssue 新增 line_number 字段（记录 Markdown 源行号）

核心数据结构：点（结果） - 边（检查方法）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  点（节点）= 已定位到的故障场景/类别/具体案例
  边（检查）= 进入该节点的前置检查条件（prerequisite_items）

节点类型（由结构自动区分，不需要独立类型字段）：
  中间节点（路由节点）：children 非空，prerequisite_items 是路由判断条件
  叶节点（案例节点）：children 为空，diagnosis + solution 均必填

层级说明（来自 Heading 级别，仅元数据，不约束树深度）：
  H1   场景名称 → 根节点
  H2   大类名称 → 中间节点（第一层路由，可选）
  H3   类别名称 → 中间节点（第二层路由，可选）
  H4+  详细案例 → 叶节点（案例节点，层级深度可变）

段落关键词识别策略（解析层职责，配置文件 sop_template_rules.yaml 唯一来源）：
  diagnosis 关键词   → 参见 keywords.diagnosis
  solution 关键词    → 参见 keywords.solution
  variables 关键词   → 参见 keywords.variables
  prerequisites 关键词 → 参见 keywords.prerequisites

校验规则（校验层职责，模型层宽松接受）：
  ① 叶节点（children=[]）必须有 diagnosis 且 diagnosis.acli_methods 非空 → 缺失记 error
  ② 叶节点必须有 solution 且 quick_recovery/thorough_fix 均非空 → 缺失记 error
  ③ 话术偏差（标题不符合标准词）→ error 阻断
  ④ 多个顶层节点 / 无父节点的段落 → error 阻断
  ⑤ 层级深度不固定：可以 H1→H2→H4（跳过 H3），或更深

校验结果：Pydantic 模型层不抛异常——残缺节点可以构建，issues 由
SOPValidationResult 收集后决定是否阻断入库（error 阻断，warning 放行）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ──────────────────────────────────────────────────────────────────────────────
# 变量声明（表格行）
# ──────────────────────────────────────────────────────────────────────────────


class VariableDeclaration(BaseModel):
    """变量声明表格中的单行条目。

    对应 Markdown 表格：
    | 变量名   | 类型   | 来源                         | 说明 |
    | vm_name | string | user_confirm                 | ... |
    """

    name: str = Field(..., min_length=1, description="变量名（英文标识符）")
    type: str = Field(..., min_length=1, description="类型（string / ip / int 等）")
    source: str = Field(
        ...,
        min_length=1,
        description=(
            "来源策略：user_confirm / user_input / "
            "env:<key>（如 env:ssh_context）/ "
            "tool:<name>（如 tool:acli_storage_disk_list）"
        ),
    )
    description: str | None = Field(None, description="变量说明（可选）")

    @field_validator("name", "type", "source")
    @classmethod
    def strip_value(cls, v: str) -> str:
        return v.strip()


# ──────────────────────────────────────────────────────────────────────────────
# 前置检查条目
# ──────────────────────────────────────────────────────────────────────────────


class PrerequisiteItem(BaseModel):
    """前置检查条目。

    区分两种类型：
    - 过滤型：用于筛选匹配的子节点（如 "存储类型为文件"）
    - 调序型：用于调整子节点优先级（如 "优先检查 XX"）
    """

    description: str = Field(..., min_length=1, description="前置检查描述")
    type: Literal["filter", "priority"] = Field("filter", description="类型：过滤型 / 调序型")
    target_node_hint: str | None = Field(None, description="目标节点提示（可选）")


# ──────────────────────────────────────────────────────────────────────────────
# 诊断详情
# ──────────────────────────────────────────────────────────────────────────────


class DiagnosisDetail(BaseModel):
    """诊断详情（叶节点）。"""

    acli_methods: list[str] = Field(
        ...,
        min_length=1,
        description="推荐的 acli 检查方法列表（至少 1 个）",
    )
    page_methods: list[str] | None = Field(None, description="页面检查方法（可选）")
    analysis_steps: list[str] = Field(default_factory=list, description="分析步骤")
    possible_causes: list[str] = Field(default_factory=list, description="可能原因")


# ──────────────────────────────────────────────────────────────────────────────
# 解决方案详情
# ──────────────────────────────────────────────────────────────────────────────


class SolutionDetail(BaseModel):
    """解决方案详情（叶节点）。"""

    quick_recovery: list[str] = Field(
        default_factory=list,
        description="快速恢复步骤（临时方案）",
    )
    thorough_fix: list[str] = Field(
        default_factory=list,
        description="彻底修复步骤（根治方案）",
    )


# ──────────────────────────────────────────────────────────────────────────────
# 校验问题
# ──────────────────────────────────────────────────────────────────────────────


class ValidationIssue(BaseModel):
    """校验问题（error/warning）。"""

    code: str = Field(..., description="问题代码（如 W-1, W-2）")
    level: Literal["error", "warning"] = Field(..., description="问题级别")
    message: str = Field(..., description="问题描述")
    location: str | None = Field(None, description="问题位置（如节点 ID）")
    line_number: int | None = Field(None, description="Markdown 源行号（1-based）")


# ──────────────────────────────────────────────────────────────────────────────
# SOP 节点
# ──────────────────────────────────────────────────────────────────────────────


class SOPNode(BaseModel):
    """SOP 多叉决策树节点。"""

    id: str = Field(..., description="节点 ID（如 n-1, n-1-1）")
    title: str = Field(..., min_length=1, description="节点标题")
    level: int = Field(..., ge=1, le=6, description="Heading 级别（1-6）")
    line_number: int = Field(..., ge=1, description="标题所在行号（1-based）")
    children: list[SOPNode] = Field(default_factory=list, description="子节点列表")
    prerequisite_items: list[PrerequisiteItem] = Field(
        default_factory=list,
        description="前置检查条目",
    )
    # 向后兼容字段：从 prerequisite_items.description 提取的字符串列表
    prerequisites: list[str] = Field(
        default_factory=list,
        description="前置检查描述列表（向后兼容，由 prerequisite_items 生成）",
    )
    variables: list[VariableDeclaration] = Field(
        default_factory=list,
        description="变量声明（仅根节点/路由节点）",
    )
    diagnosis: DiagnosisDetail | None = Field(None, description="诊断详情（叶节点）")
    solution: SolutionDetail | None = Field(None, description="解决方案（叶节点）")

    @model_validator(mode="after")
    def sync_prerequisites(self) -> SOPNode:
        """从 prerequisite_items 同步 prerequisites 字段（向后兼容）。"""
        if self.prerequisite_items and not self.prerequisites:
            self.prerequisites = [p.description for p in self.prerequisite_items]
        return self

    @model_validator(mode="after")
    def validate_leaf_node(self) -> SOPNode:
        """叶节点必须有诊断和解决方案。"""
        if not self.children:  # 叶节点
            if not self.diagnosis:
                pass  # 校验层收集 issue，模型层不阻断
            if not self.solution:
                pass  # 校验层收集 issue，模型层不阻断
        return self


# ──────────────────────────────────────────────────────────────────────────────
# 校验结果
# ──────────────────────────────────────────────────────────────────────────────


class SOPValidationResult(BaseModel):
    """SOP 校验结果。"""

    root_nodes: list[SOPNode] = Field(default_factory=list, description="根节点列表")
    issues: list[ValidationIssue] = Field(default_factory=list, description="校验问题列表")
    has_error: bool = Field(False, description="是否存在阻断性错误")

    @model_validator(mode="after")
    def check_errors(self) -> SOPValidationResult:
        """根据 issues 更新 has_error。"""
        self.has_error = any(i.level == "error" for i in self.issues)
        return self
