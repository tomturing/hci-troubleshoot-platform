"""
排障Agent结构化推理与反幻觉数据模型 (T3-2)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Hypothesis(BaseModel):
    """根因假设模型"""

    hypothesis_id: str = Field(description="假设的唯一ID，如 hyp-1")
    statement: str = Field(description="假设的具体表述描述")
    confidence: float = Field(default=0.5, description="置信度评分，0.0 到 1.0 之间")
    supporting_fact_ids: list[str] = Field(default_factory=list, description="支持该假设的已知事实 ID 列表")
    contradicting_fact_ids: list[str] = Field(default_factory=list, description="反对该假设的已知事实 ID 列表")
    verification_plan: list[str] = Field(default_factory=list, description="后续的验证操作步骤列表")


class ReasoningOutput(BaseModel):
    """结构化推理输出模型（用于 S2 假设生成 / S3 诊断推理阶段）"""

    summary: str = Field(description="当前状态及进展概述")
    hypotheses: list[Hypothesis] = Field(default_factory=list, description="当前生成或更新的根因假设列表")
    evidence_needed: list[str] = Field(default_factory=list, description="仍缺失的、需要补充采集的事实字段")
    tool_requests: list[dict[str, Any]] = Field(
        default_factory=list, description="建议调用的工具列表，格式符合 function calling"
    )
    unsupported_claims: list[str] = Field(default_factory=list, description="目前无直接事实支持的断言或猜测列表")
    user_questions: list[str] = Field(default_factory=list, description="需要向用户澄清的问题列表")
    next_state: str = Field(description="推荐的下一个诊断阶段状态")


class Claim(BaseModel):
    """结论/断言校验项"""

    claim_id: str = Field(description="校验项的唯一ID，如 claim-1")
    claim_text: str = Field(description="要校验的断言或结论文本")
    status: str = Field(
        description="校验状态：supported(已证实) | contradicted(已证伪) | insufficient_evidence(证据不足)"
    )
    supporting_fact_ids: list[str] = Field(default_factory=list, description="支持该结论的事实 ID 列表")
    contradicting_fact_ids: list[str] = Field(default_factory=list, description="推翻或反对该结论的事实 ID 列表")
    required_next_action: str | None = Field(default=None, description="当证据不足时，下一步需要的验证动作建议")


class ClaimVerification(BaseModel):
    """结构化校验输出模型（用于 S4 验证确认阶段）"""

    claims: list[Claim] = Field(default_factory=list, description="所有需要交叉校验的诊断结论列表")
