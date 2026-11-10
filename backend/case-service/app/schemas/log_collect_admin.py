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

from pydantic import BaseModel, Field


class AdminPathSummary(BaseModel):
    """一条交付路径的计数（总览与 P1/P2 分组共用一套口径）。"""

    total: int = 0
    transferring: int = 0
    delivered: int = 0
    unverified: int = 0
    failed: int = 0


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
