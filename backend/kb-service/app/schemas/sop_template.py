from __future__ import annotations

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
    """前置检查条目，区分过滤型和调序型。

    过滤型（filter）：条件判断类，不满足则跳过对应子节点，不执行检查。
    调序型（sequence）：告警类，存在漏报风险，不满足也会进行检查（仅调整优先级）。
    """

    type: Literal["filter", "sequence"] = Field(
        default="filter",
        description="过滤型（filter）/ 调序型（sequence）",
    )
    condition: str = Field(..., min_length=1, description="检查条件文本")

    @field_validator("condition")
    @classmethod
    def strip_condition(cls, v: str) -> str:
        return v.strip()


# ──────────────────────────────────────────────────────────────────────────────
# 叶节点内部结构 1/2：判断方法段落
# ──────────────────────────────────────────────────────────────────────────────


class DiagnosisDetail(BaseModel):
    """叶节点内部：判断方法段落的内容。

    page_methods（页面判断方法）：可选，新模板中标注"可选"。
    acli_methods（acli 命令行判断方法）：必填，至少 1 项。

    source_heading 保存原始标题文本（如"排查方法"），供话术审计使用。
    """

    # 判断段落内部前置检查（执行具体判断前需满足的条件，可选）
    prerequisites: list[str] = Field(
        default_factory=list,
        description="判断前的前置检查（可选）",
    )
    # 页面判断方法：可选（新模板显式标注"可选"）
    page_methods: list[str] = Field(
        default_factory=list,
        description="页面判断方法（可选）",
    )
    # acli 判断方法：必填（至少 1 项）
    acli_methods: list[str] = Field(
        ...,
        min_length=1,
        description="acli 命令行判断方法（必填，至少 1 项）",
    )
    description: str | None = Field(None, description="判断说明（可选）")
    root_cause: str | None = Field(None, description="问题根因（可选）")
    notes: str | None = Field(None, description="注意事项（可选）")
    # 溯源字段：记录 Markdown 原始标题文本（归一化前的原文），仅用于审计
    source_heading: str | None = Field(
        None,
        description="原始标题文本（如'排查方法'），仅用于审计溯源，不参与业务逻辑",
    )

    @field_validator("acli_methods")
    @classmethod
    def acli_methods_not_empty(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s.strip()]
        if not cleaned:
            raise ValueError("acli 判断方法至少需要 1 项非空内容")
        return cleaned

    @field_validator("page_methods", "prerequisites", mode="before")
    @classmethod
    def strip_list_items(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s.strip()]


# ──────────────────────────────────────────────────────────────────────────────
# 叶节点内部结构 2/2：解决方案段落
# ──────────────────────────────────────────────────────────────────────────────


class SolutionDetail(BaseModel):
    """叶节点内部：解决方案段落的内容。

    quick_recovery（快速恢复方案）：必填，至少 1 项。
    thorough_fix（彻底解决方案）：必填，至少 1 项。

    source_heading 保存原始标题文本（如"处理方法"），供话术审计使用。
    """

    quick_recovery: list[str] = Field(
        ...,
        min_length=1,
        description="快速恢复方案（必填，至少 1 项）",
    )
    thorough_fix: list[str] = Field(
        ...,
        min_length=1,
        description="彻底解决方案（必填，至少 1 项）",
    )
    source_heading: str | None = Field(
        None,
        description="原始标题文本（如'处理方法'），仅用于审计溯源，不参与业务逻辑",
    )

    @field_validator("quick_recovery", "thorough_fix")
    @classmethod
    def strip_and_validate(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s.strip()]
        if not cleaned:
            raise ValueError("解决方案列表不得为空")
        return cleaned


# ──────────────────────────────────────────────────────────────────────────────
# 核心：统一决策树节点（中间节点和叶节点共用同一类型）
# ──────────────────────────────────────────────────────────────────────────────


class SOPNode(BaseModel):
    """多叉决策树节点 — 中间节点（路由）与叶节点（案例）共用。

    判断规则：
      is_leaf    = (children == [])  → 必须有 diagnosis + solution
      is_routing = (children 非空)   → 不需要 diagnosis/solution

    宽松模式：模型层不抛异常，残缺节点可正常构建。
    完整性检查由 SOPValidationResult 负责。
    """

    node_id: str = Field(
        default="", description="自动生成的节点 ID（如 n-1-2-3），空串表示待生成"
    )
    name: str = Field(..., min_length=1, description="节点名称（来自文档标题文本）")
    level: int = Field(
        default=1, ge=1, description="来自文档 Heading 级别（1=H1, 2=H2...），仅元数据"
    )

    # 变量声明（任意层级均可有，批量注入或即时获取）
    variables: list[VariableDeclaration] = Field(
        default_factory=list,
        description="本节点的变量声明列表（表格解析结果）",
    )

    # 前置检查条目（路由/过滤逻辑，区分过滤型和调序型）
    prerequisite_items: list[PrerequisiteItem] = Field(
        default_factory=list,
        description="本节点的前置检查条目列表",
    )

    # 叶节点专属（children=[] 时必填）
    diagnosis: DiagnosisDetail | None = Field(None, description="判断方法（叶节点必填）")
    solution: SolutionDetail | None = Field(None, description="解决方案（叶节点必填）")

    # 中间节点专属（非空时为路由节点）
    children: list[SOPNode] = Field(
        default_factory=list, description="子节点列表（多叉分支）"
    )

    @model_validator(mode="after")
    def _noop_validator(self) -> SOPNode:
        """占位：模型层保持宽松，完整性校验由 SOPValidationResult 负责。"""
        return self

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def is_routing(self) -> bool:
        return bool(self.children)

    def collect_leaves(self) -> list[SOPNode]:
        """递归收集所有叶节点。"""
        if self.is_leaf:
            return [self]
        result: list[SOPNode] = []
        for child in self.children:
            result.extend(child.collect_leaves())
        return result

    def find_node(self, node_id: str) -> SOPNode | None:
        """按 node_id 递归查找节点。"""
        if self.node_id == node_id:
            return self
        for child in self.children:
            found = child.find_node(node_id)
            if found:
                return found
        return None


# Pydantic v2 需要显式触发 model_rebuild 解析自引用
SOPNode.model_rebuild()


# ──────────────────────────────────────────────────────────────────────────────
# 校验结果类型（与存储层解耦，供 upload/approve 接口使用）
# ──────────────────────────────────────────────────────────────────────────────


class ValidationIssue(BaseModel):
    """单条校验问题。"""

    level: str = Field(..., description="error（阻断）/ warning（非阻断）")
    location: str = Field(..., description="节点路径（如 '服务组件异常 > Redis OOM'）")
    line_number: int | None = Field(
        None, description="触发该问题的 Markdown 源行号（1-based，None 表示无法定位）"
    )
    message: str = Field(..., description="问题描述")


class SOPValidationResult(BaseModel):
    """SOP 多叉决策树整体校验结果。"""

    is_valid: bool = Field(..., description="是否通过必填项校验（无 error 级别问题）")
    errors: list[ValidationIssue] = Field(
        default_factory=list, description="错误列表（阻断入库）"
    )
    warnings: list[ValidationIssue] = Field(
        default_factory=list, description="警告列表（允许入库）"
    )
    tree: SOPNode | None = Field(
        None, description="解析成功的决策树根节点（有 error 时为 None）"
    )
