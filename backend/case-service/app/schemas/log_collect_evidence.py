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

Stage = Literal["waiting", "fetching", "uploading", "relaying"]
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
    # bridge 上报的是每次 TCP 拨测的 RTT 毫秒（失败为 -1），不是对象数组。
    # 契约跟随生产者：旧版把 int 当 dict 校验，导致 network-probe 全部 422。
    samples: list[int] = Field(default_factory=list, max_length=64)
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
    # 采集轮次：init=下发后初始化轮，terminal=成功终态，terminal_failed=失败/取消终态。
    # 缺省 terminal，兼容旧桥不带该字段的上报。
    phase: Literal["init", "terminal", "terminal_failed"] = "terminal"


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
    # 本次任务要求采集的目标节点（创建时的快照，不是当前节点列表）。
    # 只用于把「目标集合」与产物条目 sp_<ip>.tgz 对账：acli 会在缺台时仍报 100%，
    # 不看这个集合就会出现「包质量 ok、实际少一半节点」的假成功（Q2026112300134）。
    expected_hosts: list[str] | None = Field(default=None, max_length=512)
    evidence: dict[str, Any] = Field(default_factory=dict)


class FailureSnapshotIn(BaseModel):
    """失败现场快照：节点上关键路径的存在性与磁盘事实。

    这份数据只回答「失败时节点长什么样」，不参与状态判定；由 bridge 在任务
    失败终态时上报，平台写入 detail.failure_snapshot（服务端 single-writer）。
    """

    reason: str = Field(default="", max_length=128)
    host: str = Field(default="", max_length=64)
    files: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    disk: dict[str, Any] | None = None
    collected_at: str | None = Field(default=None, max_length=64)


class EvidenceErrorIn(BaseModel):
    """证据上报失败的显式陈述（bridge → 平台）。

    没有这条事实时，平台只能观察到「没收到清单」，无法区分「还没跑完」
    与「真的传不进去」，于是历史上只能靠 ``quality=unknown`` 猜成失败
    （Q2026110302166 误报）。这里只收事实：哪一类证据、什么错误、HTTP 码。
    """

    kind: Literal["artifact-manifest", "transfer-metric", "network-probe", "process-logs"]
    error: str = Field(max_length=512)
    http_code: int | None = Field(default=None, ge=0, le=599)


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
