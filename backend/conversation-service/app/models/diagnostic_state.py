"""
诊断状态 Pydantic 模型

集中定义诊断会话和阶段转换的数据结构
"""

from pydantic import BaseModel, Field


# ─── 诊断阶段常量定义 ────────────────────────────────────────────────────────
# 避免在代码中硬编码字符串，统一使用常量
class DiagnosticStage:
    """诊断阶段常量（T-AGT-13：语义命名重构）

    新命名体系：
    - TRIAGE：分诊阶段（意图识别、故障分类）
    - INVESTIGATION：调查阶段（故障定位、假设生成、验证执行、根因确认）
    - REMEDIATION：修复阶段（解决方案制定）
    - CLOSURE：闭环阶段（验证闭环）

    数据库兼容：内部值仍为 S0/S1/S2/S3/S4/S5/S6，确保现有数据兼容。
    """

    # ─── 语义命名（推荐使用）──────────────────────────────────────────────
    TRIAGE = "S0"  # 分诊：意图识别
    INVESTIGATION = "S1"  # 调查：故障定位（泛指 S1-S4）
    REMEDIATION = "S5"  # 修复：解决方案
    CLOSURE = "S6"  # 闭环：验证闭环

    # ─── 细分阶段（Investigation 内部细分）──────────────────────────────────
    S1_LOCATION = "S1"  # 故障定位
    S2_HYPOTHESIS = "S2"  # 假设生成
    S3_VERIFICATION = "S3"  # 验证执行
    S4_ROOT_CAUSE = "S4"  # 根因确认

    # ─── 旧命名别名（兼容过渡期，已废弃）────────────────────────────────────
    S0_INTENT = "S0"  # 已废弃：使用 TRIAGE 替代
    S5_SOLUTION = "S5"  # 已废弃：使用 REMEDIATION 替代
    S0_FAILED = "S0_FAILED"  # 分诊失败
    TRIAGE_FAILED = "S0_FAILED"  # 语义别名

    # ─── 阶段分组 ─────────────────────────────────────────────────────────────
    TRIAGE_STAGES = {"S0", "S0_FAILED"}
    INVESTIGATION_STAGES = {"S1", "S2", "S3", "S4"}
    REMEDIATION_STAGES = {"S5"}
    CLOSURE_STAGES = {"S6"}


# 阶段标签映射（用于 UI 展示）
STAGE_LABELS: dict[str, str] = {
    DiagnosticStage.TRIAGE: "TRIAGE-分诊",
    DiagnosticStage.INVESTIGATION: "INVESTIGATION-调查",
    DiagnosticStage.S1_LOCATION: "S1-故障定位",
    DiagnosticStage.S2_HYPOTHESIS: "S2-假设生成",
    DiagnosticStage.S3_VERIFICATION: "S3-验证执行",
    DiagnosticStage.S4_ROOT_CAUSE: "S4-根因确认",
    DiagnosticStage.REMEDIATION: "REMEDIATION-修复",
    DiagnosticStage.CLOSURE: "CLOSURE-闭环",
    DiagnosticStage.TRIAGE_FAILED: "TRIAGE_FAILED-分诊失败",
    # 旧键兼容（已废弃）
    "S0": "TRIAGE-分诊",
    "S1": "S1-故障定位",
    "S2": "S2-假设生成",
    "S3": "S3-验证执行",
    "S4": "S4-根因确认",
    "S5": "REMEDIATION-修复",
    "S6": "CLOSURE-闭环",
    "S0_FAILED": "TRIAGE_FAILED-分诊失败",
}


class StageTransition(BaseModel):
    """诊断阶段转换记录"""

    from_stage: str = Field(..., description="转换前阶段，如 S0")
    to_stage: str = Field(..., description="转换后阶段，如 S1")
    triggered_by: str = Field(
        ...,
        description="触发原因：llm_output | tool_result | user_input | auto",
    )
    confidence: float = Field(default=1.0, description="转换置信度 0-1")


class DiagnosticSession(BaseModel):
    """诊断会话状态"""

    conversation_id: str = Field(..., description="对话 ID")
    case_id: str = Field(default="", description="工单 ID")
    current_stage: str = Field(default="S0", description="当前诊断阶段")
    stage_history: list[str] = Field(
        default_factory=list, description="阶段历史（按时间顺序）"
    )
    transitions: list[StageTransition] = Field(
        default_factory=list, description="阶段转换记录"
    )
    hypotheses: list[str] = Field(
        default_factory=list, description="当前假设列表"
    )
    confirmed_facts: list[str] = Field(
        default_factory=list, description="已确认的事实"
    )
    pending_questions: list[str] = Field(
        default_factory=list, description="待确认问题列表"
    )
    root_cause: str | None = Field(default=None, description="已确定的根因")
    solution: str | None = Field(default=None, description="已确定的解决方案")
    metadata: dict = Field(default_factory=dict, description="额外元数据")

    def advance_to(self, new_stage: str, triggered_by: str = "llm_output") -> StageTransition:
        """
        推进到新阶段，记录转换

        Args:
            new_stage: 目标阶段
            triggered_by: 触发原因

        Returns:
            新创建的 StageTransition 记录
        """
        transition = StageTransition(
            from_stage=self.current_stage,
            to_stage=new_stage,
            triggered_by=triggered_by,
        )
        self.transitions.append(transition)
        self.stage_history.append(self.current_stage)
        self.current_stage = new_stage
        return transition

    def add_hypothesis(self, hypothesis: str) -> None:
        """添加假设"""
        if hypothesis not in self.hypotheses:
            self.hypotheses.append(hypothesis)

    def confirm_fact(self, fact: str) -> None:
        """确认事实"""
        if fact not in self.confirmed_facts:
            self.confirmed_facts.append(fact)

    def add_pending_question(self, question: str) -> None:
        """添加待确认问题"""
        if question not in self.pending_questions:
            self.pending_questions.append(question)

    def clear_pending_questions(self) -> None:
        """清空待确认问题"""
        self.pending_questions.clear()

    def to_context_dict(self) -> dict:
        """转换为上下文字典（用于注入 System Prompt）"""
        return {
            "current_stage": self.current_stage,
            "stage_history": self.stage_history,
            "hypotheses": self.hypotheses,
            "confirmed_facts": self.confirmed_facts,
            "pending_questions": self.pending_questions,
            "root_cause": self.root_cause,
        }
