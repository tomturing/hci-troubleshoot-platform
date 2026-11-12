"""日志收集展示相关的响应模型（派生字段，不对应任何任务表列）。

契约来源：``contracts/log_collect_display.json``；判定来源：
``app/domain/log_collect_display.py``。本文件只负责把派生结果固化成
可被 OpenAPI 描述的结构，不做任何语义判断。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..domain.log_collect_display import derive


class LogCollectCondition(BaseModel):
    """并存的观察事实。多值，界面按顺序展示为标签，不参与统计分母。"""

    key: str = Field(description="条件键，如 artifact_missing / handoff_pending")
    label: str = Field(description="中文文案（契约生成）")


class LogCollectDisplay(BaseModel):
    """三维展示结论：环节 + 结论 + 观察事实。"""

    phase: str = Field(description="当前环节：queued/collecting/fetching/uploading/relaying/settled")
    phase_label: str
    conclusion: str = Field(description="业务结论：pending/success/partial/failed/cancelled")
    conclusion_label: str
    conclusion_tone: str = Field(description="展示色板：success/warning/error/info")
    conditions: list[LogCollectCondition] = Field(default_factory=list)
    progress_percent: int | None = Field(
        default=None,
        description=(
            "合成进度（采集 0-60、交付 60-90、终结 100）。null 表示无法得知，界面必须显示 —，不得显示 0 或 100。"
        ),
    )
    upload_mode_label: str | None = None


class LogCollectArtifactInfo(BaseModel):
    """产物证据：回答「这次到底有没有拿到可用日志」。"""

    quality: str = Field(description="ok / suspect / invalid / unknown")
    quality_label: str
    reason: str = Field(description="判定原因键，如 missing / too_small / metadata_only")
    reason_text: str
    size: int | None = None
    size_text: str = Field(description="统一体积口径（1024 进制），缺失为 —")
    entry_count: int | None = None
    entry_total_bytes: int | None = None
    entry_total_text: str | None = None
    source: str = Field(description="证据来源：manifest（内容探测）/ recorded（历史结论）/ size_inference（体积推断）")


class LogCollectTransferInfo(BaseModel):
    """传输样本：阶段、速率、ETA、停滞判定。"""

    stage: str | None = None
    stage_label: str | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None
    percent: float | None = None
    elapsed_ms: int | None = None
    rate_bps: float | None = None
    rate_text: str | None = None
    eta_sec: int | None = None
    stalled: bool = False
    stalled_seconds: int | None = None
    stall_threshold_seconds: int = 60
    updated_at: datetime | None = None
    source: str | None = None


class LogCollectNetworkTarget(BaseModel):
    """单个探测目标的网络质量。"""

    target: str
    reachable: bool | None = None
    grade: str
    grade_label: str
    rtt_avg_ms: float | None = None
    jitter_ms: float | None = None
    loss_rate: float | None = None
    http_code: int | None = None
    up_bps: float | None = None
    down_bps: float | None = None
    up_text: str | None = None
    down_text: str | None = None
    advice: str | None = None
    checked_at: datetime | None = None


class LogCollectNetworkInfo(BaseModel):
    """网络探测汇总：按当前交付路径选相关目标定级。"""

    grade: str = Field(description="good / fair / poor / unknown")
    grade_label: str
    relevant_target: str | None = Field(default=None, description="当前交付路径关心的目标：log_server / platform_data")
    targets: list[LogCollectNetworkTarget] = Field(default_factory=list)
    checked_at: datetime | None = None


class LogCollectDisplayPayload(BaseModel):
    """任务响应附加的派生四件套。"""

    display: LogCollectDisplay
    artifact: LogCollectArtifactInfo
    transfer: LogCollectTransferInfo | None = None
    network: LogCollectNetworkInfo | None = None


def build_display_payload(
    task: Any,
    *,
    manifest: Mapping[str, Any] | None = None,
    metrics: Mapping[str, Any] | None = None,
    probes: Iterable[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> LogCollectDisplayPayload:
    """把 ORM 行/字典行转成响应模型（判定仍在 domain 层）。"""
    return LogCollectDisplayPayload.model_validate(
        derive(task, manifest=manifest, metrics=metrics, probes=probes, now=now)
    )
