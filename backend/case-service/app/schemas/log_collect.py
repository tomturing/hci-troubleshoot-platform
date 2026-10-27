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
# 交付路径收敛为两条：acli_direct = P1（HCI 直传 5888），bridge_relay = P2（bridge 中转）。
# 原 platform_direct（平台直传）对应已废弃的 P2，不再接受。
UploadMode = Literal["acli_direct", "bridge_relay", "none"]

# 活跃状态：同一工单只允许一个（与部分唯一索引一致）
ACTIVE_STATUSES: frozenset[str] = frozenset(
    {"pending", "probing", "installing", "collecting", "packaging", "uploading"}
)
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

MAX_DATE_RANGE_DAYS = 30


class LogCollectTaskCreate(BaseModel):
    """创建任务请求。"""

    case_id: str = Field(
        pattern=r"^Q[0-9]{8}[0-9]{5}$",
        description="关联工单号（Q + 8 位日期 + 5 位序号），同时作为 acli -s",
    )
    origin_node_ip: str = Field(min_length=1, max_length=64, description="发起采集的 HCI 节点")
    modules: list[LogCollectModule] = Field(min_length=1, description="收集专项")
    date_from: date
    date_to: date
    ips: list[str] = Field(default_factory=lambda: ["all"], description='目标主机；["all"] 表示全部在线节点')
    extra_paths: list[str] = Field(default_factory=list, max_length=20)
    remote_url: str | None = Field(default=None, description="上传目标（仅 http/https 官方日志服务器）")
    baseline_report_hash: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "下发前锚定的节点报告指纹（`acli log_collect progress` 原文的 "
            "`长度-FNV32`）。同一工单重复 create 时插件会先返回上一次任务的报告，"
            "有该指纹才能判断报告是否已刷新成本次任务。"
        ),
    )
    trace_id: str | None = Field(default=None, max_length=64)
    # 探测事实（前端 probe 结果）：失败任务必须能自证运行环境，
    # 否则无法区分「acli 版本不支持 log_collect」与「参数/环境错误」。
    arch: str | None = Field(default=None, max_length=16, description="发起节点架构（probe 实测）")
    acli_version: str | None = Field(default=None, max_length=32, description="发起节点 acli 版本（probe 实测）")

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

    @field_validator("case_id")
    @classmethod
    def _validate_case_id_date(cls, value: str) -> str:
        """日期段必须真实存在（20261032 这类"格式合法但日期非法"的号要拒绝）。"""
        try:
            datetime.strptime(value[1:9], "%Y%m%d")
        except ValueError as exc:
            raise ValueError("工单号中的日期不合法，应为 Q + YYYYMMDD + 5 位序号") from exc
        return value


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
    origin_node_ip: str
    modules: list[str]

    @field_validator("origin_node_ip", mode="before")
    @classmethod
    def _coerce_inet(cls, value):
        """INET 列经 asyncpg 返回 IPv4Address/IPv6Address，响应契约是字符串。

        不转会导致 `ResponseValidationError`（500）：Pydantic 严格模式下
        `IPv4Address` 不是 str。这里统一在序列化前转字符串。
        """
        return str(value) if value is not None else value

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
    # 清理提示：平台不自动删除、不提供删除 API，仅以这些字段驱动告警展示
    cleanup_state: str = "none"
    cleanup_reason: str | None = None
    archive_deleted: bool = False
    # P2 段③ 代传状态（与主状态机解耦）
    relay_status: str = "none"
    relay_chunk_index: int | None = None
    relay_chunk_total: int | None = None
    relay_restarts: int = 0
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


# ── P2 段②：客户 PC → 平台 分块上传 ──────────────────────────────────────────
# 会话标识即 task_id（1 任务 : 1 包 : 1 会话），因此请求/响应均不含独立 upload_id 之外的字段。


class UploadInitRequest(BaseModel):
    """初始化上传会话。``expected_sha256`` 可由客户端在拉取完成后计算并声明。"""

    total_size: int = Field(gt=0, description="压缩包总字节数")
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class UploadInitResponse(BaseModel):
    upload_id: str
    upload_token: str = Field(description="数据面直连用的短期 token，绑定 task_id")
    chunk_size: int
    part_count: int
    total_size: int
    uploaded_parts: list[int]
    missing_parts: list[int]


class UploadStatusResponse(BaseModel):
    upload_id: str
    chunk_size: int
    part_count: int
    total_size: int
    uploaded_parts: list[int]
    missing_parts: list[int]


class UploadPartResponse(BaseModel):
    upload_id: str
    part_number: int
    size: int
    sha256: str
    uploaded_parts: int
    part_count: int


class UploadCompleteRequest(BaseModel):
    """``parts`` 省略时按 1..part_count 顺序合并；``sha256``/``size`` 用于最终校验。"""

    parts: list[int] | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size: int | None = Field(default=None, ge=1)


class UploadCompleteResponse(BaseModel):
    upload_id: str
    object_key: str
    size: int
    sha256: str
    part_count: int
