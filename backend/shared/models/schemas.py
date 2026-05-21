"""
Shared Pydantic Schemas
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

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
    assistant_type: str | None = Field(None, description="AI助手类型，默认htp-agent")


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
    assistant_type: str | None = "htp-agent"
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
    assistant_type: str | None = Field(None, description="AI助手类型，用于动态切换助手")

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


# ──────────────────────────────────────────────
# KB 服务契约模型（F-3）
# ──────────────────────────────────────────────

from typing import Literal  # noqa: E402


class KBIngestPayload(BaseModel):
    """KB 文档导入请求（case-service → kb-service）"""

    title: str = Field(..., description="文档标题")
    content_md: str = Field(..., description="Markdown 格式正文")
    source_id: str | None = Field(None, description="来源 ID，如工单 ID")
    source_type: Literal["kb", "sop", "realtime"] = Field(
        "realtime", description="数据来源类型"
    )
    yaml_meta: dict = Field(default_factory=dict, description="附加元数据（YAML 格式解析后）")


class KBSearchResponse(BaseModel):
    """KB 语义搜索响应"""

    chunks: list[dict] = Field(default_factory=list, description="命中文档片段列表")
    total: int = Field(0, description="命中总数")
    query_time_ms: float = Field(0.0, description="查询耗时（ms）")


class KBSOPMatchResponse(BaseModel):
    """KB SOP 精确匹配响应"""

    matched: bool = Field(..., description="是否命中")
    title: str | None = Field(None, description="SOP 标题")
    content: str | None = Field(None, description="SOP 正文")
    node_id: str | None = Field(None, description="SOP 节点 ID")


# ──────────────────────────────────────────────
# Scheduler 服务契约模型（G-1）
# ──────────────────────────────────────────────


class PodAllocationResponse(BaseModel):
    """Pod 分配响应（scheduler-service → api-gateway/case-service）"""

    allocated: bool = Field(..., description="是否成功分配")
    pod_name: str | None = Field(None, description="分配的 Pod 名称")
    pod_ip: str | None = Field(None, description="分配的 Pod IP")
    assistant_type: str | None = Field(None, description="AI 助手类型（htp-agent/ops-agent/pai-agent）")
    case_id: str | None = Field(None, description="绑定的工单 ID")
    error: str | None = Field(None, description="失败原因（allocated=false 时填充）")


class PodReleaseResponse(BaseModel):
    """Pod 释放响应（scheduler-service → api-gateway/case-service）"""

    released: bool = Field(..., description="是否成功释放")
    pod_name: str | None = Field(None, description="已释放的 Pod 名称")
    error: str | None = Field(None, description="失败原因（released=false 时填充）")


class PoolStatusResponse(BaseModel):
    """Pod 池状态响应（scheduler-service health/metrics 使用）"""

    assistant_type: str = Field(..., description="AI 助手类型")
    idle: int = Field(0, description="空闲 Pod 数")
    active: int = Field(0, description="活跃（已分配）Pod 数")
    total: int = Field(0, description="总 Pod 数（idle + active）")

    @property
    def is_exhausted(self) -> bool:
        """池是否耗尽（无空闲且有活跃 Pod，可能存在资源泄漏）"""
        return self.idle == 0 and self.active > 0


# ──────────────────────────────────────────────
# Environment 服务契约模型（Custom-UI 数据采集）
# ──────────────────────────────────────────────


class EnvType(StrEnum):
    """环境数据类型枚举"""

    CLUSTER = "cluster"      # 集群基本信息
    HOST = "host"            # 主机配置列表
    VM = "vm"                # 虚拟机列表
    NETWORK = "network"      # 网络拓扑
    ALERT = "alert"          # 告警列表（用于 S0 Prompt）
    TASK = "task"            # 任务状态列表（用于 S0 Prompt）


class EnvironmentCreate(BaseModel):
    """创建环境数据请求"""

    case_id: str = Field(..., description="关联工单 ID")
    env_type: EnvType = Field(..., description="环境数据类型")
    env_data: dict = Field(..., description="环境数据 JSONB 内容")
    collected_at: datetime | None = Field(None, description="数据采集时间")


class EnvironmentUpsert(BaseModel):
    """upsert 环境数据请求（case_id/env_type 由 path 参数指定）"""

    env_data: dict = Field(..., description="环境数据 JSONB 内容")
    collected_at: datetime | None = Field(None, description="数据采集时间（可选，默认当前时间）")


class EnvironmentResponse(BaseModel):
    """环境数据响应"""

    environment_id: UUID
    case_id: str
    env_type: EnvType  # 使用枚举类型，确保类型安全
    env_data: dict
    collected_at: datetime | None
    created_at: datetime
    updated_at: datetime
    trace_id: str | None

    class Config:
        from_attributes = True


class EnvironmentListResponse(BaseModel):
    """工单环境数据列表响应"""

    items: list[EnvironmentResponse] = []
    total: int = 0


class EnvironmentContextResponse(BaseModel):
    """S0 阶段 Prompt 构建所需的环境上下文响应"""

    env_info: dict = Field(default_factory=dict, description="环境基本信息")
    alert_logs: list[dict] = Field(default_factory=list, description="告警日志列表")
    task_logs: list[dict] = Field(default_factory=list, description="任务日志列表")
