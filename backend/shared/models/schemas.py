"""
Shared Pydantic Schemas
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from enum import Enum

class CaseStatus(str, Enum):
    """工单状态"""
    CREATED = "created"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"

class MessageRole(str, Enum):
    """消息角色"""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    COMMAND = "command"

class CaseCreate(BaseModel):
    """创建工单请求"""
    client_id: str = Field(..., description="客户端ID")
    title: str = Field(..., max_length=200, description="工单标题")
    description: Optional[str] = Field(None, description="工单描述")

class CaseResponse(BaseModel):
    """工单响应"""
    case_id: str
    client_id: str
    status: CaseStatus
    title: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime]
    trace_id: Optional[str]
    
    class Config:
        from_attributes = True

class MessageCreate(BaseModel):
    """创建消息请求"""
    case_id: str
    role: MessageRole
    content: str
    metadata: Optional[dict] = None

class MessageResponse(BaseModel):
    """消息响应"""
    message_id: str
    conversation_id: str
    role: MessageRole
    content: str
    metadata: Optional[dict]
    created_at: datetime
    trace_id: Optional[str]
    
    class Config:
        from_attributes = True

class WebSocketMessage(BaseModel):
    """WebSocket消息格式"""
    type: str
    case_id: str
    content: str
    is_complete: bool = False
    metadata: Optional[dict] = None
