"""
共享数据模型统一导出模块
"""

from .audit import AuditLog, Authorization, ToolResult
from .base import TimestampMixin, TraceableMixin
from .conversation import Conversation
from .fact import ClaimEvidenceLink, Fact
from .information import EvidenceBundle, FactSource, InformationPacket, StaleDataGuard
from .kb import KBCategory, KBChunk, KBDocument, KBSopNode, KBSynonym
from .reliability import Claim, ClaimVerification, Hypothesis, ReasoningOutput
from .skill_definition import SkillDefinitionORM
from .system_prompt import SystemPrompt
from .user import User

__all__ = [
    "User",
    "Conversation",
    "SystemPrompt",
    "AuditLog",
    "Authorization",
    "ToolResult",
    "KBDocument",
    "KBChunk",
    "KBSopNode",
    "KBCategory",
    "KBSynonym",
    "SkillDefinitionORM",
    "TimestampMixin",
    "TraceableMixin",
    # 阶段二：轻量事实体系
    "FactSource",
    "InformationPacket",
    "StaleDataGuard",
    "EvidenceBundle",
    # 阶段三：推理约束与反幻觉
    "Hypothesis",
    "ReasoningOutput",
    "Claim",
    "ClaimVerification",
    # 阶段四：事实持久化与评估
    "Fact",
    "ClaimEvidenceLink",
]
