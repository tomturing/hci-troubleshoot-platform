"""
Conversation Service Models（v6.2 更新）
"""

from shared.models.conversation import Conversation
from shared.models.system_prompt import SystemPrompt

from .authorization import Authorization
from .diagnostic_item import DiagnosticItem
from .message import Message, MessageRole
from .skill_definition import SkillDefinition
from .sop_execution import STATUS_ABORTED, STATUS_ACTIVE, STATUS_COMPLETED, STATUS_INTERRUPTED, SopExecution
from .tool_definition import ToolDefinition
from .tool_result import ToolResult

__all__ = [
    "Conversation",
    "DiagnosticItem",
    "Message",
    "MessageRole",
    "SopExecution",
    "STATUS_ACTIVE",
    "STATUS_COMPLETED",
    "STATUS_INTERRUPTED",
    "STATUS_ABORTED",
    "SystemPrompt",
    "ToolDefinition",
    "ToolResult",
    "SkillDefinition",
    "Authorization",
]
