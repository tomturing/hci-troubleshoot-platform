"""
Conversation Service Models（v6.2 更新）
"""

from shared.models.conversation import Conversation
from shared.models.system_prompt import SystemPrompt

from .diagnostic_item import DiagnosticItem
from .diagnostic_state import DiagnosticSession, StageTransition
from .message import Message, MessageRole
from .sop_execution import STATUS_ABORTED, STATUS_ACTIVE, STATUS_COMPLETED, STATUS_INTERRUPTED, SopExecution
from .tool_definition import ToolDefinition
from .tool_result import ToolResult

__all__ = [
    "Conversation",
    "DiagnosticItem",
    "DiagnosticSession",
    "Message",
    "MessageRole",
    "SopExecution",
    "STATUS_ACTIVE",
    "STATUS_COMPLETED",
    "STATUS_INTERRUPTED",
    "STATUS_ABORTED",
    "StageTransition",
    "SystemPrompt",
    "ToolDefinition",
    "ToolResult",
]
