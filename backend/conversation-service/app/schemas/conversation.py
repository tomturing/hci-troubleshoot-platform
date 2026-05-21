"""
对话服务响应 Schema
"""

import uuid

from pydantic import BaseModel


class ConversationSessionResponse(BaseModel):
    """创建/获取对话的响应体，包含诊断阶段信息。

    diagnostic_stage 从数据库实际值填充（迁移 0003 完成后）。
    """

    conversation_id: uuid.UUID
    case_id: str
    assistant_type: str = "htp-agent"
    diagnostic_stage: str = "S0"
    category_l1: str | None = None
    category_l2: str | None = None
    category_id: str | None = None

    class Config:
        from_attributes = True
