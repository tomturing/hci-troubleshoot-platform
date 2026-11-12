"""日志收集证据数据面的上报契约（bridge → case-service）。

所有字段都是「事实」：没有 status、没有 conclusion、没有 delivery_*。
结论字段由平台推导；带这些字段的上报会被忽略（Pydantic 默认丢额外字段，
``model_config.extra=forbid`` 会把老 bridge 直接打挂，因此不启用）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Stage = Literal["fetching", "uploading", "relaying"]
ProbeTarget = Literal["log_server", "platform_data"]
Source = Literal["node_zip", "platform_object"]


class TransferMetricIn(BaseModel):
    seq: int = Field(ge=0, description="单调递增序号，作为幂等键")
    stage: Stage
    bytes_done: int = Field(ge=0)
    bytes_total: int | None = Field(default=None, ge=0)
    percent: float | None = Field(default=None, ge=0, le=100)
    elapsed_ms: int | None = Field(default=None, ge=0)
    rate_bps: int | None = Field(default=None, ge=0)
    eta_sec: int | None = Field(default=None, ge=0)
    stalled: bool = False
    source: Literal["bridge", "platform"] = "bridge"


class NetworkProbeIn(BaseModel):
    target: ProbeTarget
    reachable: bool
    sample_count: int = Field(default=0, ge=0)
    loss_rate: float | None = Field(default=None, ge=0, le=1)
    rtt_min_ms: int | None = Field(default=None, ge=0)
    rtt_avg_ms: int | None = Field(default=None, ge=0)
    rtt_max_ms: int | None = Field(default=None, ge=0)
    jitter_ms: int | None = Field(default=None, ge=0)
    dns_ms: int | None = Field(default=None, ge=0)
    connect_ms: int | None = Field(default=None, ge=0)
    http_code: int | None = None
    up_bps: int | None = Field(default=None, ge=0)
    down_bps: int | None = Field(default=None, ge=0)
    advice: str | None = Field(default=None, max_length=512)
    samples: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    bridge_version: str | None = Field(default=None, max_length=32)


class ProcessLogIn(BaseModel):
    host: str = Field(max_length=64)
    path: str = Field(max_length=512, description="节点自己报告过的绝对路径")
    size: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # gzip 压缩后的原文，base64 编码；单文件最多 2 MiB、单任务最多 20 MiB。
    content_gz_b64: str | None = Field(default=None, max_length=3 * 1024 * 1024)
    truncated: bool = False
    status: Literal["ok", "empty", "error"] = "ok"
    error: str | None = Field(default=None, max_length=1024)
    collected_at: datetime | None = None


class ArtifactManifestIn(BaseModel):
    source: Source
    archive_path: str | None = Field(default=None, max_length=512)
    archive_size: int | None = Field(default=None, ge=0)
    archive_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    entry_count: int = Field(ge=0)
    entry_total_bytes: int = Field(ge=0)
    entries: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    has_encrypted: bool = False
    readable: bool = Field(default=True, description="中央目录是否读得出来")
    evidence: dict[str, Any] = Field(default_factory=dict)


class EvidenceAck(BaseModel):
    ok: bool = True
    task_id: UUID | None = None
    id: str | None = None
    duplicate: bool = False


class NetworkProbeAck(EvidenceAck):
    probe_id: UUID
    ticket: str = Field(description="测速端点专用短期票据，绑定 probe_id")
    grade: str
    grade_label: str


class BandwidthAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bytes: int
    probe_id: UUID
