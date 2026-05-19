"""
KB Service — SOP 多叉决策树 Pydantic 校验模型

核心数据结构：点（结果） - 边（检查方法）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  点（节点）= 已定位到的故障场景/类别/具体案例
  边（检查）= 进入该节点的前置检查条件（prerequisites）

节点类型（由结构自动区分，不需要独立类型字段）：
  中间节点（路由节点）：children 非空，prerequisites 是路由判断条件
  叶节点（案例节点）：children 为空，diagnosis + solution 均必填

树的遍历逻辑（AI Agent 执行时）：
  1. 从根节点（H1）出发
  2. 对每个子节点，评估其 prerequisites（边条件）
  3. 进入满足条件的子节点，递归执行
  4. 到达叶节点后：执行 diagnosis 详细判断，确认匹配后执行 solution

层级说明（来自 docx Heading 级别，仅元数据，不约束树深度）：
  H1   场景名称 → 根节点
  H2   大类名称 → 中间节点（第一层路由，可选）
  H3   类别名称 → 中间节点（第二层路由，可选）
  H4+  详细案例 → 叶节点（案例节点，层级深度可变）
  「判断方法」段落 → 叶节点内部 diagnosis 字段（任意层级，关键词匹配）
  「解决方案」段落 → 叶节点内部 solution 字段（任意层级，关键词匹配）

  注意：diagnosis/solution 两个段落不是独立树节点，而是叶节点的内部结构。
  它们的识别依据是**关键词语义匹配**，而非固定的标题层级（H5 只是典型情况）。

段落关键词识别策略（解析层职责，模型层仅记录契约）：
  diagnosis 等效关键词（任一匹配）：
    判断方法 / 判断依据 / 排查方法 / 排查步骤 / 识别方法 / 确认方法 / 诊断方法
  solution 等效关键词（任一匹配）：
    解决方案 / 解决方法 / 处理方法 / 处理步骤 / 修复方法 / 修复步骤 / 解决步骤
  配对约束：同一叶节点下 diagnosis 和 solution 必须同时出现
  话术归一：识别到的原始标题文本记录在 DiagnosisDetail.source_heading /
            SolutionDetail.source_heading，供差异审计和二次优化使用

约束规则（校验层职责，模型层宽松接受）：
  ① 叶节点（children=[]）应有 diagnosis 且 diagnosis.page_methods 非空 → 缺失记 error
  ② 叶节点应有 solution 且 quick_recovery/thorough_fix 均非空 → 缺失记 error
  ③ 中间节点（children 非空）不需要 diagnosis/solution → 有则记 warning
  ④ 层级深度不固定：可以 H1→H2→H4（跳过H3），或更深

  注意：Pydantic 模型层不抛异常——残缺节点可以构建，issues 由 SOPValidationResult
  收集后决定是否阻断入库（error 阻断，warning 放行）。

为什么没有独立的 SOPDecisionTree 根类型？
  ─────────────────────────────────────────
  根节点就是一个 SOPNode（name=场景名称，level=1，无父节点）。
  schema_version / generated_at / sop_document_id 等元数据
  属于存储层（sop_tree 表的列），不进入 Pydantic 数据模型。
  统一节点类型使遍历算法无需区分根 vs 非根，更简洁。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

# ──────────────────────────────────────────────────────────────────────────────
# 叶节点内部结构 1/2：判断方法段落
# ──────────────────────────────────────────────────────────────────────────────


class DiagnosisDetail(BaseModel):
    """叶节点内部：判断方法段落的内容

    对应 docx 中任意层级、以「判断方法」类关键词命名的标题段落。
    原始标题文本（如"排查方法"、"判断依据"）保存在 source_heading 字段中，
    用于话术归一差异审计。
    """

    prerequisites: list[str] = Field(
        default_factory=list,
        description="判断前的前置检查（在执行具体判断前需满足的条件，可选）",
    )
    page_methods: list[str] = Field(
        ...,
        min_length=1,
        description="页面判断方法（必填，至少 1 项）",
    )
    acli_methods: list[str] = Field(
        default_factory=list,
        description="acli 命令行判断方法（可选）",
    )
    description: str | None = Field(None, description="判断说明（可选）")
    root_cause: str | None = Field(None, description="问题根因（可选）")
    notes: str | None = Field(None, description="注意事项（可选）")
    # 溯源字段：docx 原始标题文本（归一化前），用于话术差异审计
    # 例：docx 写"排查方法" → source_heading="排查方法"，模型字段统一用 diagnosis
    source_heading: str | None = Field(
        None,
        description="原始标题文本（如'排查方法'），解析器归一化前的原文，仅用于审计溯源，不参与业务逻辑",
    )

    @field_validator("page_methods")
    @classmethod
    def page_methods_not_empty(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s.strip()]
        if not cleaned:
            raise ValueError("页面判断方法至少需要 1 项非空内容")
        return cleaned

    @field_validator("prerequisites", "acli_methods", mode="before")
    @classmethod
    def strip_list_items(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s.strip()]


# ──────────────────────────────────────────────────────────────────────────────
# 叶节点内部结构 2/2：解决方案段落
# ──────────────────────────────────────────────────────────────────────────────


class SolutionDetail(BaseModel):
    """叶节点内部：解决方案段落的内容

    对应 docx 中任意层级、以「解决方案」类关键词命名的标题段落。
    原始标题文本（如"处理方法"、"解决方法"）保存在 source_heading 字段中，
    用于话术归一差异审计。
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
    # 溯源字段：docx 原始标题文本（归一化前），用于话术差异审计
    # 例：docx 写"处理方法" → source_heading="处理方法"，模型字段统一用 solution
    source_heading: str | None = Field(
        None,
        description="原始标题文本（如'处理方法'），解析器归一化前的原文，仅用于审计溯源，不参与业务逻辑",
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
    """多叉决策树节点 — 中间节点（路由）与叶节点（案例）共用

    判断规则：
      is_leaf    = (children == [])  → 期望有 diagnosis + solution（缺失由校验层记 error）
      is_routing = (children 非空)   → 不需要 diagnosis/solution

    宽松模式：模型层不抛异常，残缺节点可正常构建。
    完整性检查由 SOPValidationResult 负责，结果存入 sop_tree.validation_issues。
    """

    node_id: str = Field(default="", description="自动生成的节点 ID（如 n-1-2-3），空串表示待生成")
    name: str = Field(..., min_length=1, description="节点名称（来自文档标题文本）")
    level: int = Field(default=1, ge=1, description="来自文档 Heading 级别（1=H1, 2=H2...），仅元数据")

    # 边：进入此节点的检查条件（所有层级统一，不区分 filter/sequence）
    prerequisites: list[str] = Field(
        default_factory=list,
        description="进入此节点前需要满足的前置检查条件（边的条件）",
    )

    # 叶节点专属（children=[] 时必填）
    diagnosis: DiagnosisDetail | None = Field(None, description="判断方法（叶节点必填）")
    solution: SolutionDetail | None = Field(None, description="解决方案（叶节点必填）")

    # 中间节点专属（非空时为路由节点）
    children: list[SOPNode] = Field(default_factory=list, description="子节点列表（多叉分支）")

    @field_validator("prerequisites", mode="before")
    @classmethod
    def strip_prerequisites(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s.strip()]

    @model_validator(mode="after")
    def warn_leaf_missing_fields(self) -> "SOPNode":
        """宽松校验：叶节点缺少 diagnosis 或 solution 时不抛异常，
        仅在 SOPValidationResult 层记录 error（此方法不做任何校验）。

        外部调用方（如单元测试）可通过 SOPValidationResult 获取完整校验结果。
        此 validator 占位存在，为未来开关式严格模式预留接口。
        """
        return self

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def is_routing(self) -> bool:
        return bool(self.children)

    def collect_leaves(self) -> list[SOPNode]:
        """递归收集所有叶节点"""
        if self.is_leaf:
            return [self]
        result: list[SOPNode] = []
        for child in self.children:
            result.extend(child.collect_leaves())
        return result

    def find_node(self, node_id: str) -> SOPNode | None:
        """按 node_id 递归查找节点"""
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
# 校验结果类型（与存储层解耦，供 upload 接口使用）
# ──────────────────────────────────────────────────────────────────────────────


class ValidationIssue(BaseModel):
    """单条校验问题"""

    level: str = Field(..., description="error（阻断）/ warning（非阻断）")
    location: str = Field(..., description="节点路径（如 '服务组件异常 > Redis OOM'）")
    message: str = Field(..., description="问题描述")


class SOPValidationResult(BaseModel):
    """SOP 多叉决策树整体校验结果"""

    is_valid: bool = Field(..., description="是否通过必填项校验（无 error 级别问题）")
    errors: list[ValidationIssue] = Field(default_factory=list, description="错误列表（阻断入库）")
    warnings: list[ValidationIssue] = Field(default_factory=list, description="警告列表（允许入库）")
    tree: SOPNode | None = Field(None, description="解析成功的决策树根节点（有 error 时为 None）")
