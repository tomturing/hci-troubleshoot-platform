"""
BackendSignal — QFK 后端排查信号（v2 扁平运行时模型）

字段命名与 acquirer_args 契约及 signals_json v2 完全一致：
- namespace    对应 acquirer 名称（log/service/system/vm/network/storage/hardware/platform）
- host         acli --host / --cluster
- command      acli <namespace> <command>
- file/path    qfk_log 的 -f / -p
- time_window  qfk_log 的 -t
- service/action qfk_service 的 <container> <name> <action>
- container    qfk_system 的 --container
- keyword      匹配关键字（list[str]）
- instruction  匹配说明
- match_mode   or/and/not
- expected     True=期望命中，False=期望不命中（取反语义）
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ─── 有效匹配模式 ──────────────────────────────────────────────────────────────
VALID_MATCH_MODES = {"or", "and", "not"}

# acli 主机作用域
VALID_CONTAINERS = (
    "asv",
    "dsv",
    "csv",
    "mpv",
    "drv",
    "fdv",
    "ssv",
    "msv",
    "osv",
    "csf",
    "csw",
    "gpuv",
)
VALID_SERVICE_CONTAINERS = VALID_CONTAINERS


class BackendSignal(BaseModel):
    """QFK 后端排查信号运行时模型（扁平 v2）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ─── 命名空间 ─────────────────────────────────────────────────────────────
    namespace: str = Field(
        ..., description="acli 子命令空间名：log/service/system/vm/network/storage/hardware/platform"
    )

    # ─── 主机作用域 ───────────────────────────────────────────────────────────
    host: str | None = Field(default=None, description="主机名；'cluster' 表示集群模式")
    vm: str | None = Field(default=None, description="虚拟机标识")
    timeout: int = Field(default=30, description="执行超时（秒）")
    container: str | None = Field(default=None, description="容器名（qfk_service/qfk_system 用）")
    cluster: bool = Field(default=False, description="是否集群模式")

    # ─── 特有字段 ─────────────────────────────────────────────────────────────
    command: str | None = Field(default=None, description="acli 子命令（vm/network/storage/hardware/platform/system）")
    file: str | None = Field(default=None, description="日志文件名（qfk_log 的 -f）")
    path: str | None = Field(default=None, description="日志路径（qfk_log 的 -p）")
    time_window: str | None = Field(default=None, description="时间窗（qfk_log 的 -t）")
    service: str | None = Field(default=None, description="服务名（qfk_service）")
    action: str | None = Field(default=None, description="服务操作（qfk_service，默认 status）")
    resource_keyword: str | None = Field(default=None, description="命令的只读资源过滤参数")

    # ─── 匹配配置 ─────────────────────────────────────────────────────────────
    keyword: list[str] = Field(default_factory=list, description="匹配关键字列表")
    match_mode: str = Field(default="or", description="关键字匹配模式：or/and/not")
    instruction: str | None = Field(default=None, description="匹配说明（人类可读）")
    expected: bool = Field(default=True, description="预期是否命中；False 表示取反语义")

    # ─── 校验 ─────────────────────────────────────────────────────────────────
    @model_validator(mode="after")
    def _validate(self) -> BackendSignal:
        if self.match_mode not in VALID_MATCH_MODES:
            raise ValueError(f"match_mode 必须是 {VALID_MATCH_MODES} 之一，收到: {self.match_mode}")
        return self

    # ─── 工具方法 ─────────────────────────────────────────────────────────────
    def is_cluster_mode(self) -> bool:
        """判断是否为集群模式"""
        return self.host == "cluster"

    @classmethod
    def from_dict(cls, data: Any) -> BackendSignal:
        """从字典/JSON 字符串构建并校验"""
        if isinstance(data, str):
            data = json.loads(data)
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> BackendSignal:
        """从 JSON 字符串构建并校验"""
        return cls.model_validate(json.loads(json_str))
