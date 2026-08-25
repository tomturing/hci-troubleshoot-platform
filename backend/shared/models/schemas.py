"""
Shared Pydantic Schemas
（安全加固版 2026-08-19/20：metadata 白名单校验、WS 协议字段修正）

设计原则：只收紧明确的攻击面（metadata 注入、畸形标识符），对存量
业务字段保持宽松兼容，避免破坏性变更。
"""

import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 宽松但拒绝注入字符的标识符格式（case_id 实际格式为服务生成的短字符串）
_SAFE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_CLIENT_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 消息 metadata 注入防护（安全审计 2026-08-19）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 系统内部字段黑名单：这些键由服务端/agent 事件流写入，用户请求中出现
# 即视为注入攻击。注意 test_run_id 不在此列——sim-ssh 仿真链路依赖它
# （conversation_service.send_message_stream_only 的 execution_mode=="sim-ssh"
# 分支），属于合法用户字段。
METADATA_FORBIDDEN_KEYS = frozenset({
    "bash_command", "acli_args", "ssh_host", "ssh_credentials",
    "ssh_password", "ssh_private_key", "passphrase",
    "api_key", "token", "password", "secret", "credentials",
    "pod_endpoint", "internal_token",
})

# execution_mode 合法值：safe-only 为默认保守模式，sim-ssh 为仿真
# SSH 排障链路（SimulationContextClient 会做二次校验，见
# conversation_service.py 中 test_run_id 归属验证）
METADATA_EXECUTION_MODES = frozenset({"safe-only", "sim-ssh"})

# metadata 序列化后最大长度与嵌套深度（防超大 JSONB 写入）
METADATA_MAX_JSON_BYTES = 16 * 1024
METADATA_MAX_DEPTH = 4


def _validate_metadata_value(value: object, depth: int = 0) -> None:
    """递归校验 metadata 值：嵌套深度、危险键。"""
    if depth > METADATA_MAX_DEPTH:
        raise ValueError(f"metadata 嵌套深度超过 {METADATA_MAX_DEPTH} 层")
    if isinstance(value, dict):
        injected = set(value.keys()) & METADATA_FORBIDDEN_KEYS
        if injected:
            raise ValueError(f"metadata 包含禁止字段: {sorted(injected)}（仅系统内部可设置）")
        for v in value.values():
            _validate_metadata_value(v, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_metadata_value(item, depth + 1)


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

    USER_COMMAND = "user_command"  # 用户主动输入命令关闭
    TIMEOUT = "timeout"  # 超时自动关闭
    ABANDON = "abandon"  # 用户放弃/断开连接
    ADMIN_CLOSE = "admin_close"  # 管理员强制关闭


class MessageRole(StrEnum):
    """消息角色"""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    COMMAND = "command"
    # ReAct 工具调用轮次角色（用于跨轮次持久化 ReAct 历史，见 OpenAI Function Calling 规范）
    TOOL_CALL = "tool_call"    # AI 发起的工具调用请求（含 tool_calls JSON）
    TOOL_RESULT = "tool_result"  # 工具执行结果（通过 tool_call_id 关联对应的 tool_call 消息）


class CaseCreate(BaseModel):
    """创建工单请求"""

    client_id: str = Field(..., pattern=_CLIENT_ID_PATTERN, description="客户端ID")
    title: str = Field(..., min_length=1, max_length=200, description="工单标题")
    description: str | None = Field(None, max_length=10000, description="工单描述")
    assistant_type: str | None = Field(None, pattern=r"^[a-z-]{3,20}$", description="AI助手类型")

    @field_validator("title", "description")
    @classmethod
    def sanitize_text_fields(cls, v: str | None) -> str | None:
        """移除 null 字节与控制字符（防截断/协议注入）"""
        if v:
            v = v.replace("\x00", "")
            v = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", v)
        return v


class CaseCloseRequest(BaseModel):
    """关闭工单请求"""

    close_reason: CloseReason | None = Field(None, description="关闭原因：user_command/timeout/abandon/admin_close")


class CaseUpdate(BaseModel):
    """更新工单请求"""

    title: str | None = Field(None, max_length=200, description="工单标题")
    description: str | None = Field(None, max_length=10000, description="工单描述")
    status: CaseStatus | None = Field(None, description="工单状态")
    priority: str | None = Field(None, max_length=20, description="优先级")
    category: str | None = Field(None, max_length=100, description="分类")
    assistant_type: str | None = Field(None, pattern=r"^[a-z-]{3,20}$", description="AI助手类型")


class CaseResponse(BaseModel):
    """工单响应"""

    case_id: str
    client_id: str
    status: CaseStatus
    title: str
    description: str | None
    assistant_type: str | None = "htp-agent"
    priority: str | None = "medium"
    category: str | None = None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    trace_id: str | None
    close_reason: str | None = None

    model_config = ConfigDict(from_attributes=True)


class MessageCreate(BaseModel):
    """创建消息请求

    metadata 保持 dict 类型（下游 send_message_stream_only 等以
    dict.get() 消费），通过 field_validator 做黑名单/白名单/深度校验。
    """

    case_id: str = Field(..., pattern=_SAFE_ID_PATTERN, description="工单ID")
    role: MessageRole
    content: str = Field(..., min_length=1, max_length=50000, description="消息内容（最大 50KB）")
    metadata: dict | None = None
    assistant_type: str | None = Field(None, pattern=r"^[a-z-]{3,20}$", description="AI 助手类型")
    auto_execute: bool | None = Field(None, description="是否开启工具自动执行")

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, v: str) -> str:
        """移除 null 字节与控制字符（在路由层协议过滤之前先保证字节安全）"""
        v = v.replace("\x00", "")
        return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", v)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("metadata 必须为对象")

        _validate_metadata_value(v)

        # execution_mode 白名单：合法业务值为 safe-only / sim-ssh
        exec_mode = v.get("execution_mode")
        if exec_mode is not None and exec_mode not in METADATA_EXECUTION_MODES:
            raise ValueError(f"execution_mode 仅允许 {sorted(METADATA_EXECUTION_MODES)}")

        auto_mode = v.get("auto_execute_mode")
        if auto_mode is not None and auto_mode not in METADATA_EXECUTION_MODES:
            raise ValueError(f"auto_execute_mode 仅允许 {sorted(METADATA_EXECUTION_MODES)}")

        # 序列化长度限制（防超大 JSONB 拖垮存储与下游 AI 上下文）
        try:
            serialized = json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            raise ValueError(f"metadata 无法序列化: {e}") from e
        if len(serialized.encode()) > METADATA_MAX_JSON_BYTES:
            raise ValueError(f"metadata 序列化后超过 {METADATA_MAX_JSON_BYTES} 字节")

        return v


class MessageResponse(BaseModel):
    """消息响应"""

    message_id: UUID
    conversation_id: UUID
    role: MessageRole
    content: str
    metadata: dict | None = Field(default=None, validation_alias="metadata_")
    created_at: datetime
    trace_id: str | None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


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
    """WebSocket消息格式

    协议修正（安全审计 2026-08-19）：原网关实现读取 message["conversation_id"]
    但旧 schema 只定义了 case_id，导致 Pydantic 校验后的对象无法提供
    conversation_id（两个不同实体被混淆）。现以 conversation_id 为必填
    主字段，case_id 保留为可选兼容字段。
    """

    type: str = Field(..., pattern=r"^[a-z_]{2,30}$", description="消息类型")
    conversation_id: str = Field(..., min_length=8, max_length=64, description="目标会话ID")
    content: str = Field(..., max_length=50000, description="消息内容")
    is_complete: bool = False
    case_id: str | None = Field(None, pattern=_SAFE_ID_PATTERN, description="工单ID（兼容字段）")
    metadata: dict | None = None

    @field_validator("content")
    @classmethod
    def sanitize_content(cls, v: str) -> str:
        v = v.replace("\x00", "")
        return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", v)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: dict | None) -> dict | None:
        return MessageCreate.validate_metadata(v)


# ──────────────────────────────────────────────
# KB 服务契约模型（F-3）
# ──────────────────────────────────────────────

class KBIngestPayload(BaseModel):
    """KB 文档导入请求（case-service -> kb-service）"""

    title: str = Field(..., min_length=1, max_length=500, description="文档标题")
    content_md: str = Field(..., max_length=200000, description="Markdown 格式正文")
    source_id: str | None = Field(None, pattern=_SAFE_ID_PATTERN, description="来源 ID，如工单 ID")
    source_type: Literal["kb", "sop", "realtime"] = Field("realtime", description="数据来源类型")
    yaml_meta: dict = Field(default_factory=dict, description="附加元数据（YAML 格式解析后）")


class KBSearchResponse(BaseModel):
    """KB 语义搜索响应"""

    chunks: list[dict] = Field(default_factory=list, description="命中文档片段列表")
    total: int = Field(0, description="命中总数")
    query_time_ms: float = Field(0.0, description="查询耗时（ms）")


# ──────────────────────────────────────────────
# Scheduler 服务契约模型（G-1）
# ──────────────────────────────────────────────

class PodAllocationResponse(BaseModel):
    """Pod 分配响应（scheduler-service -> api-gateway/case-service）"""

    allocated: bool = Field(..., description="是否成功分配")
    pod_name: str | None = Field(None, description="分配的 Pod 名称")
    pod_ip: str | None = Field(None, description="分配的 Pod IP")
    assistant_type: str | None = Field(None, description="AI 助手类型（htp-agent/ops-agent/pai-agent）")
    case_id: str | None = Field(None, description="绑定的工单 ID")
    error: str | None = Field(None, max_length=500, description="失败原因（allocated=false 时填充）")


class PodReleaseResponse(BaseModel):
    """Pod 释放响应（scheduler-service -> api-gateway/case-service）"""

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

    CLUSTER = "cluster"  # 集群基本信息
    HOST = "host"  # 主机配置列表
    VM = "vm"  # 虚拟机列表
    NETWORK = "network"  # 网络拓扑
    ALERT = "alert"  # 告警日志列表（用于 S0 Prompt）
    TASK = "task"  # 任务状态列表（用于 S0 Prompt）


class EnvironmentCreate(BaseModel):
    """创建环境数据请求"""

    case_id: str = Field(..., pattern=_SAFE_ID_PATTERN, description="关联工单 ID")
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

    model_config = ConfigDict(from_attributes=True)


class EnvironmentListResponse(BaseModel):
    """工单环境数据列表响应"""

    items: list[EnvironmentResponse] = []
    total: int = 0


class EnvironmentContextResponse(BaseModel):
    """S0 阶段 Prompt 构建所需的环境上下文响应"""

    env_info: dict = Field(default_factory=dict, description="环境基本信息")
    alert_logs: list[dict] = Field(default_factory=list, description="告警日志列表")
    task_logs: list[dict] = Field(default_factory=list, description="任务状态列表")
