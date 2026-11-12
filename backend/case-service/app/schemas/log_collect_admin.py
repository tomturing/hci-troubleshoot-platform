"""日志收集「日志上传」管理端只读契约。

设计约束（2026-11-10 已确认）：
- 客户名不新增列：从 ``case.customer_id → customer.short_name/name`` 取，未关联时为 None；
  ``client_id`` 单独常驻返回，两者不可互相替代；
- 管理端全只读：本模块只有响应模型，没有任何写请求模型；
- 「查不到 ≠ 失败」：进度响应区分 ``no_record`` / ``unsupported`` / ``unavailable``，
  只有 ``done + target_path`` 才是强证据，绝不把瞬时观测写成失败结论。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from .log_collect_display import (
    LogCollectArtifactInfo,
    LogCollectDisplay,
    LogCollectNetworkInfo,
    LogCollectTransferInfo,
)


class AdminPathSummary(BaseModel):
    """一条交付路径的计数（总览与 P1/P2 分组共用一套口径）。

    ``by_conclusion`` 是唯一权威分桶：五桶之和必须等于 ``total``。
    旧字段（transferring/delivered/unverified/failed）保留给未升级前端，
    由同一批派生结果换算，不再各自写 SQL 条件——那正是 168/146 对不上账的根因。
    """

    total: int = 0
    transferring: int = 0
    delivered: int = 0
    unverified: int = 0
    failed: int = 0
    by_conclusion: dict[str, int] = Field(
        default_factory=lambda: {"pending": 0, "success": 0, "partial": 0, "failed": 0, "cancelled": 0}
    )

    @model_validator(mode="after")
    def _identity(self) -> AdminPathSummary:
        buckets = sum(self.by_conclusion.values())
        if buckets != self.total:
            raise ValueError(f"统计恒等式被破坏：by_conclusion 合计 {buckets} != total {self.total}")
        return self


class AdminLogCollectSummary(AdminPathSummary):
    p1: AdminPathSummary = Field(default_factory=AdminPathSummary)
    p2: AdminPathSummary = Field(default_factory=AdminPathSummary)


class AdminLogCollectTaskItem(BaseModel):
    task_id: UUID
    case_id: str
    client_id: str | None = None
    customer_name: str | None = None
    origin_node_ip: str | None = None
    upload_mode: str | None = None
    status: str
    progress: int = 0
    archive_size: int | None = None
    object_size: int | None = Field(default=None, description="平台侧对象体积（P2 合并后）")
    delivery_status: str
    relay_status: str
    relay_upload_id: str | None = None
    integrity_state: str
    archive_check: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    trace_id: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    delivery_finished_at: datetime | None = None
    # 派生字段：列表直接用 platform 自己的结论，界面不再用 status/包大小拼。
    display: LogCollectDisplay | None = None
    artifact: LogCollectArtifactInfo | None = None


class AdminLogCollectTaskListResponse(BaseModel):
    items: list[AdminLogCollectTaskItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    summary: AdminLogCollectSummary = Field(default_factory=AdminLogCollectSummary)


class DestinationProgressResponse(BaseModel):
    """日志服务器侧的真实上传进度（平台代理，前端不直连 5888）。"""

    task_id: UUID
    path: str = "unknown"
    state: str = "no_record"
    progress: int | None = None
    uploaded_size: int | None = None
    file_size: int | None = None
    received_chunks: int | None = None
    chunk_total: int | None = None
    target_path: str | None = None
    sha256: str | None = None
    finished_at: datetime | None = None
    checked_at: datetime
    reason: str | None = None


class AdminLogCollectTaskDetail(AdminLogCollectTaskItem):
    """单任务详情：列表字段 + 最新传输样本 + 网络探测结论。"""

    transfer: LogCollectTransferInfo | None = None
    network: LogCollectNetworkInfo | None = None
    archive_path: str | None = None
    archive_sha256: str | None = None
    acli_version: str | None = None
    arch: str | None = None
    integrity_state: str = "unverified"


class TransferMetricsResponse(BaseModel):
    """传输时间序列（按 seq 倒序，最新在前），用于画速率/ETA 曲线。"""

    task_id: UUID
    items: list[LogCollectTransferInfo] = Field(default_factory=list)
    latest: LogCollectTransferInfo | None = None


class NetworkProbeResponse(BaseModel):
    """下发前网络探测：每目标最新一条。"""

    task_id: UUID
    network: LogCollectNetworkInfo | None = None


class ProcessLogItem(BaseModel):
    """节点侧采集进程日志的元数据（内容另取，避免列表拖大包）。"""

    log_id: UUID
    host: str
    path: str
    size_bytes: int | None = None
    size_text: str | None = None
    sha256: str | None = None
    truncated: bool = False
    status: str = Field(default="ok", description="ok / empty / error")
    error: str | None = None
    collected_at: datetime | None = None


class ProcessLogListResponse(BaseModel):
    task_id: UUID
    items: list[ProcessLogItem] = Field(default_factory=list)


class ProcessLogContentResponse(BaseModel):
    """进程日志原文（只允许节点自己上报过的绝对路径）。"""

    log_id: UUID
    host: str
    path: str
    content: str
    truncated: bool = False
    size_bytes: int | None = None
    encoding: str = "utf-8"
    collected_at: datetime | None = None


class ArtifactManifestItem(BaseModel):
    """包内容探测结论：条目数、总字节、加密标记与判定原因。"""

    source: str
    archive_size: int | None = None
    archive_size_text: str | None = None
    archive_sha256: str | None = None
    entry_count: int | None = None
    entry_total_bytes: int | None = None
    entry_total_text: str | None = None
    has_encrypted: bool | None = None
    quality: str = "unknown"
    quality_label: str = "未知"
    reason: str | None = None
    reason_text: str | None = None
    entries: list[dict] | None = Field(default=None, description="最多返回前 200 条条目")
    entries_truncated: bool = False
    inspected_at: datetime | None = None


class ArtifactManifestResponse(BaseModel):
    task_id: UUID
    items: list[ArtifactManifestItem] = Field(default_factory=list)
