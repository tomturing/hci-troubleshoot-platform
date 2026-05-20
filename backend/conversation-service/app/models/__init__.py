"""
Conversation Service Models（v6.2 更新）
"""

from shared.models.conversation import Conversation

from .diagnostic_item import DiagnosticItem
from .diagnostic_state import DiagnosticSession, StageTransition
from .message import Message, MessageRole
from .system_prompt import SystemPrompt
from .tool_definition import ToolDefinition
from .tool_result import ToolResult

__all__ = [
    "Conversation",
    "DiagnosticItem",
    "DiagnosticSession",
    "Message",
    "MessageRole",
    "StageTransition",
    "SystemPrompt",
    "ToolDefinition",
    "ToolResult",
]
