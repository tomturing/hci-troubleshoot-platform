"""
Shared Pydantic Schemas
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CaseStatus(StrEnum):
    """工单状态"""

    CREATED = "created"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class CloseReason(StrEnum):
    """工单关闭原因"""

    USER_COMMAND = "user_command"   # 用户主动输入命令关闭
    TIMEOUT = "timeout"             # 超时自动关闭
    ABANDON = "abandon"             # 用户放弃/断开连接
    ADMIN_CLOSE = "admin_close"     # 管理员强制关闭


class MessageRole(StrEnum):
    """消息角色"""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    COMMAND = "command"


class CaseCreate(BaseModel):
    """创建工单请求"""

    client_id: str = Field(..., description="客户端ID")
    title: str = Field(..., max_length=200, description="工单标题")
    description: str | None = Field(None, description="工单描述")
    assistant_type: str | None = Field(None, description="AI助手类型，默认openclaw")


class CaseCloseRequest(BaseModel):
    """关闭工单请求"""

    close_reason: CloseReason | None = Field(None, description="关闭原因：user_command/timeout/abandon/admin_close")


class CaseResponse(BaseModel):
    """工单响应"""

    case_id: str
    client_id: str
    status: CaseStatus
    title: str
    description: str | None
    assistant_type: str | None = "openclaw"
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    trace_id: str | None
    close_reason: str | None = None

    class Config:
        from_attributes = True


class MessageCreate(BaseModel):
    """创建消息请求"""

    case_id: str
    role: MessageRole
    content: str
    metadata: dict | None = None


from uuid import UUID


class MessageResponse(BaseModel):
    """消息响应"""

    message_id: UUID
    conversation_id: UUID
    role: MessageRole
    content: str
    metadata: dict | None = Field(default=None, validation_alias="metadata_")
    created_at: datetime
    trace_id: str | None

    class Config:
        from_attributes = True
        populate_by_name = True


class CaseListResponse(BaseModel):
    """工单分页列表响应（Admin）"""

    items: list[CaseResponse] = []
    total: int = 0
    skip: int = 0
    limit: int = 20


class CaseStatsResponse(BaseModel):
    """工单统计响应（Admin）"""

    total: int = 0
    by_status: dict[str, int] = {}


class ClientInfo(BaseModel):
    """客户端信息"""

    client_id: str
    case_count: int
    last_case_at: datetime | None = None


class ClientListResponse(BaseModel):
    """客户端列表响应（Admin）"""

    items: list[ClientInfo] = []
    total: int = 0


class WebSocketMessage(BaseModel):
    """WebSocket消息格式"""

    type: str
    case_id: str
    content: str
    is_complete: bool = False
    metadata: dict | None = None
