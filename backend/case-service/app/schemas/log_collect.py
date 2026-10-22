"""日志收集任务的请求/响应模型。

校验口径与 terminal_bridge 保持一致（bridge 会二次校验）：插件本身不防注入，
平台层先拦住非法工单号、专项、日期跨度、路径与上传地址。
"""

from __future__ import annotations

import ipaddress
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LogCollectStatus = Literal[
    "pending",
    "probing",
    "installing",
    "collecting",
    "packaging",
    "uploading",
    "completed",
    "failed",
    "cancelled",
]
LogCollectModule = Literal[
    "other",
    "system",
    "slowdisk",
    "network",
    "storage",
    "vm_start",
    "vm_suspend",
    "host_offline",
    "performance",
    "core",
]
UploadMode = Literal["acli_direct", "platform_direct", "bridge_relay", "none"]

# 活跃状态：同一工单只允许一个（与部分唯一索引一致）
ACTIVE_STATUSES: frozenset[str] = frozenset(
    {"pending", "probing", "installing", "collecting", "packaging", "uploading"}
)
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

MAX_DATE_RANGE_DAYS = 30


class LogCollectTaskCreate(BaseModel):
    """创建任务请求。"""

    case_id: str = Field(min_length=1, max_length=20, description="关联工单")
    ticket_no: str = Field(
        pattern=r"^[A-Za-z0-9_-]{1,32}$",
        description="ICare 单号，作为 acli log_collect -s，并成为产物名",
    )
    origin_node_ip: str = Field(min_length=1, max_length=64, description="发起采集的 HCI 节点")
    modules: list[LogCollectModule] = Field(min_length=1, description="收集专项")
    date_from: date
    date_to: date
    ips: list[str] = Field(default_factory=lambda: ["all"], description='目标主机；["all"] 表示全部在线节点')
    extra_paths: list[str] = Field(default_factory=list, max_length=20)
    remote_url: str | None = Field(default=None, description="上传目标（仅 http/https 官方日志服务器）")
    trace_id: str | None = Field(default=None, max_length=64)

    @field_validator("origin_node_ip")
    @classmethod
    def _validate_origin_ip(cls, value: str) -> str:
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(f"非法节点 IP：{value}") from exc
        return value

    @field_validator("modules")
    @classmethod
    def _dedupe_modules(cls, value: list[str]) -> list[str]:
        # 保序去重：acli -m 会按顺序拼接，重复项没有意义。
        return list(dict.fromkeys(value))

    @field_validator("ips")
    @classmethod
    def _validate_ips(cls, value: list[str]) -> list[str]:
        if not value:
            return ["all"]
        normalized: list[str] = []
        for item in value:
            item = item.strip()
            if item == "all":
                return ["all"]
            try:
                ipaddress.IPv4Address(item)
            except ValueError as exc:
                raise ValueError(f"非法主机 IP：{item}") from exc
            normalized.append(item)
        return normalized

    @field_validator("extra_paths")
    @classmethod
    def _validate_extra_paths(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.startswith("/") or ".." in item:
                raise ValueError(f"额外收集路径必须是绝对路径且不允许 ..：{item}")
            if any(ch in item for ch in " \t\r\n'\"`;|&$"):
                raise ValueError(f"额外收集路径包含非法字符：{item}")
        return value

    @field_validator("remote_url")
    @classmethod
    def _validate_remote_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("上传地址必须以 http:// 或 https:// 开头")
        if any(ch in value for ch in " \t\r\n'\"`;|&$"):
            raise ValueError("上传地址包含非法字符")
        return value

    @model_validator(mode="after")
    def _validate_date_range(self) -> LogCollectTaskCreate:
        if self.date_to < self.date_from:
            raise ValueError("结束日期不能早于开始日期")
        if (self.date_to - self.date_from).days + 1 > MAX_DATE_RANGE_DAYS:
            raise ValueError(f"收集范围不能超过 {MAX_DATE_RANGE_DAYS} 天")
        return self


class LogCollectTaskPatch(BaseModel):
    """更新任务状态与制品元数据。

    进度语义（重要）：
    - ``acli log_collect progress`` 的权威源在 HCI 的 ``cluster_progress.json``，
      只要知道 ``origin_node_ip`` 就能随时重建，因此 **平台不保存实时进度**；
    - ``progress`` / ``detail`` 只是**快照**，用于任务列表兜底展示，允许为空；
    - 推荐只在**状态跃迁与终态**时调用本接口（每任务 ≤6 次），避免每 5 秒轮询写库
      造成写放大，以及"平台进度与 HCI 不一致"的双源冲突。
    """

    status: LogCollectStatus | None = None
    progress: int | None = Field(default=None, ge=0, le=100, description="进度快照，非权威值")
    arch: str | None = Field(default=None, max_length=16)
    acli_version: str | None = Field(default=None, max_length=32)
    upload_mode: UploadMode | None = None
    archive_path: str | None = None
    archive_size: int | None = Field(default=None, ge=0)
    archive_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    detail: dict | None = None
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = None

    @model_validator(mode="after")
    def _require_any_field(self) -> LogCollectTaskPatch:
        if not self.model_dump(exclude_none=True):
            raise ValueError("至少要提供一个待更新字段")
        return self


class LogCollectTaskResponse(BaseModel):
    """任务响应。"""

    model_config = ConfigDict(from_attributes=True)

    task_id: UUID
    case_id: str
    ticket_no: str
    origin_node_ip: str
    modules: list[str]
    date_from: date
    date_to: date
    ips: list[str]
    extra_paths: list[str] | None = None
    remote_url: str | None = None
    status: str
    progress: int
    arch: str | None = None
    acli_version: str | None = None
    upload_mode: str | None = None
    archive_path: str | None = None
    archive_size: int | None = None
    archive_sha256: str | None = None
    detail: dict = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    created_by: str
    trace_id: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class LogCollectTaskListResponse(BaseModel):
    total: int
    items: list[LogCollectTaskResponse]
