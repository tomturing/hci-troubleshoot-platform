"""
对话服务响应 Schema
"""

import uuid

from pydantic import BaseModel


class ConversationSessionResponse(BaseModel):
    """创建/获取对话的响应体，包含诊断阶段占位符。

    diagnostic_stage 当前固定为 "S0"（意图识别阶段），
    待 Task 07 完成阶段状态机迁移后由数据库真实值填充。
    """

    conversation_id: uuid.UUID
    case_id: str
    assistant_type: str = "openclaw"
    diagnostic_stage: str = "S0"
