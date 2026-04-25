"""
SSH 终端会话相关模型
Task 37: SSH 代理与终端交互后端能力
Task 42: 终端操作录制功能
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AuthType(StrEnum):
    """认证方式"""

    PASSWORD = "password"
    KEY = "key"


class TerminalSessionCreate(BaseModel):
    """创建终端会话请求"""

    host: str = Field(..., description="SSH 主机地址")
    port: int = Field(default=22, ge=1, le=65535, description="SSH 端口")
    username: str = Field(..., min_length=1, max_length=64, description="用户名")
    auth_type: AuthType = Field(default=AuthType.PASSWORD, description="认证方式")
    password: str | None = Field(default=None, description="密码（认证方式为 password 时必填）")
    private_key: str | None = Field(default=None, description="私钥内容（认证方式为 key 时必填）")
    passphrase: str | None = Field(default=None, description="私钥密码（可选）")

    # 可选的上下文信息
    client_id: str | None = Field(default=None, description="客户端 ID")
    case_id: str | None = Field(default=None, description="关联工单 ID")

    def model_post_init(self, __context: Any) -> None:
        """验证认证信息"""
        if self.auth_type == AuthType.PASSWORD and not self.password:
            raise ValueError("密码认证方式需要提供 password 字段")
        if self.auth_type == AuthType.KEY and not self.private_key:
            raise ValueError("密钥认证方式需要提供 private_key 字段")


class TerminalSessionResponse(BaseModel):
    """创建终端会话响应"""

    session_id: str = Field(..., description="会话 ID")
    host: str = Field(..., description="SSH 主机地址（脱敏）")
    port: int = Field(..., description="SSH 端口")
    username: str = Field(..., description="用户名")
    status: str = Field(default="connected", description="连接状态")
    message: str | None = Field(default=None, description="连接消息")


class TerminalSessionClose(BaseModel):
    """关闭终端会话响应"""

    session_id: str = Field(..., description="会话 ID")
    status: str = Field(default="closed", description="关闭状态")
    message: str = Field(default="会话已关闭", description="关闭消息")


class TerminalSessionStatus(StrEnum):
    """终端会话状态"""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class TerminalSessionInfo(BaseModel):
    """终端会话信息（存储在 Redis）"""

    session_id: str
    host: str  # 脱敏后的 host
    port: int
    username: str
    client_id: str | None = None
    case_id: str | None = None
    status: TerminalSessionStatus = TerminalSessionStatus.CONNECTED
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_activity_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    trace_id: str | None = None


# WebSocket 消息协议
class WSMessageType(StrEnum):
    """WebSocket 消息类型"""

    # 客户端 -> 服务端
    STDIN = "stdin"
    RESIZE = "resize"
    PING = "ping"

    # 服务端 -> 客户端
    STDOUT = "stdout"
    STDERR = "stderr"
    STATUS = "status"
    PONG = "pong"
    ERROR = "error"


class TerminalWSMessage(BaseModel):
    """终端 WebSocket 消息"""

    type: WSMessageType
    data: str | None = None
    cols: int | None = None
    rows: int | None = None
    state: str | None = None  # connected, disconnected, error
    message: str | None = None


class TerminalStatusMessage(BaseModel):
    """终端状态消息"""

    type: WSMessageType = WSMessageType.STATUS
    state: TerminalSessionStatus
    message: str | None = None


# ============================================================
# 终端操作录制相关模型 (Task 42)
# ============================================================

class OperationDirection(StrEnum):
    """操作方向"""
    INPUT = "input"
    OUTPUT = "output"


class TerminalOperationCreate(BaseModel):
    """创建终端操作记录请求"""

    case_id: str = Field(..., description="工单 ID")
    conversation_id: str | None = Field(default=None, description="会话 ID（可选）")
    session_id: str | None = Field(default=None, description="SSH session ID（可选）")
    seq_number: int = Field(..., ge=1, description="操作序号")
    direction: OperationDirection = Field(..., description="操作方向")
    command: str | None = Field(default=None, description="命令文本（仅 input 类型）")
    content: str = Field(..., description="原始内容（含 ANSI 码）")
    content_clean: str | None = Field(default=None, description="纯文本内容（剔除 ANSI）")
    exit_code: int | None = Field(default=None, description="退出码（仅 output 类型）")
    diagnostic_stage: str | None = Field(default=None, description="诊断阶段 S0-S6")


class TerminalOperationResponse(BaseModel):
    """终端操作记录响应"""

    id: int = Field(..., description="记录 ID")
    case_id: str = Field(..., description="工单 ID")
    seq_number: int = Field(..., description="操作序号")
    direction: OperationDirection = Field(..., description="操作方向")
    command: str | None = Field(default=None, description="命令文本")
    content: str = Field(..., description="原始内容")
    exit_code: int | None = Field(default=None, description="退出码")
    diagnostic_stage: str | None = Field(default=None, description="诊断阶段")
    created_at: str = Field(..., description="操作时间")


class TerminalOperationListResponse(BaseModel):
    """终端操作记录列表响应"""

    total: int = Field(..., description="总记录数")
    operations: list[TerminalOperationResponse] = Field(default_factory=list, description="操作列表")


class TerminalOperationQuery(BaseModel):
    """终端操作查询参数"""

    case_id: str = Field(..., description="工单 ID")
    stage: str | None = Field(default=None, description="诊断阶段过滤")
    search: str | None = Field(default=None, description="关键词搜索")
    direction: OperationDirection | None = Field(default=None, description="方向过滤")
    order: str = Field(default="asc", description="排序方向 asc/desc")
    limit: int = Field(default=100, ge=1, le=1000, description="返回数量限制")
    offset: int = Field(default=0, ge=0, description="偏移量")
