"""
QFK 后端信号结构定义
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

# ─── 有效容器枚举（qfk_system 专用）────────────────────────────────────────────
VALID_CONTAINERS = {"asv-con", "vn-con", "vn-agent", "vs-cp-manager"}

# ─── 有效匹配模式 ──────────────────────────────────────────────────────────────
VALID_MATCH_MODES = {"or", "and", "not"}


class BackendSignal(BaseModel):
    """
    HCI 排障标准化后端信号数据模型

    共有字段：
    - instruction: 关键信号说明
    - host: 主机（变量池获取，特殊值 "cluster" 表示遍历集群）
    - vm: 虚拟机（变量池获取，可为空）
    - keyword: 关键字（必填）
    - timeout: 超时时间（默认 10 秒）
    - expected: 期望结果（默认 true）
    - match_mode: 匹配模式（默认 "or"）

    特有字段（按 namespace）：
    - qfk_log: file, end
    - qfk_system: command, container
    - qfk_service: service, action
    - qfk_vm/network/storage/hardware/platform: command
    """

    # ─── 共有字段 ─────────────────────────────────────────────────────────────
    instruction: str | None = Field(default=None, description="关键信号说明")
    host: str | None = Field(default=None, description="主机（变量池获取，特殊值 'cluster' 表示遍历集群）")
    vm: str | None = Field(default=None, description="虚拟机（变量池获取，可为空）")
    keyword: list[str] = Field(default_factory=list, description="关键字（必填）")
    timeout: int = Field(default=10, ge=1, le=300, description="超时时间（秒），默认 10")
    expected: bool = Field(default=True, description="期望结果：True=期望出现，False=期望不出现")
    match_mode: str = Field(default="or", description="匹配模式：or(任一)/and(全部)/not(均不出现)")

    # ─── 命名空间 ─────────────────────────────────────────────────────────────
    namespace: str = Field(..., description="acli 子命令空间名：log/service/system/vm/network/storage/hardware/platform")

    # ─── 特有字段 ─────────────────────────────────────────────────────────────
    # qfk_log
    file: str | None = Field(default=None, description="日志文件名（qfk_log 必填）")
    end: str | None = Field(default=None, description="结束时间（qfk_log 选填）")

    # qfk_system
    command: str | None = Field(default=None, description="执行命令（qfk_system/vm/network/storage/hardware/platform 必填）")
    container: str = Field(default="asv-con", description="容器类型（qfk_system 选填，默认 asv-con）")

    # qfk_service
    service: str | None = Field(default=None, description="服务名称（qfk_service 必填）")
    action: str = Field(default="status", description="动作（qfk_service 选填，默认 status）")

    # ─── 兼容旧字段（向后兼容）─────────────────────────────────────────────────
    target: dict[str, Any] | None = Field(default=None, description="旧版 target 字段（兼容）")
    keywords: list[str] | None = Field(default=None, description="旧版 keywords 字段（兼容）")
    sub_command: str | None = Field(default=None, description="旧版 sub_command 字段（兼容）")
    description: str | None = Field(default=None, description="旧版 description 字段（兼容）")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackendSignal:
        """从字典构建并校验，支持新旧字段兼容"""
        data = data.copy()

        # 兼容：keyword / keywords
        if "keyword" not in data and "keywords" in data:
            kw = data["keywords"]
            data["keyword"] = [kw] if isinstance(kw, str) else list(kw or [])

        # 兼容：instruction / description
        if "instruction" not in data and "description" in data:
            data["instruction"] = data["description"]

        # 兼容：sub_command -> command
        if "command" not in data and "sub_command" in data:
            data["command"] = data["sub_command"]

        # 兼容：target.host -> host
        target = data.get("target")
        if isinstance(target, dict):
            if "host" not in data and target.get("host"):
                data["host"] = target["host"]
            if "vm" not in data and target.get("vm"):
                data["vm"] = target["vm"]
            if "file" not in data and target.get("resource"):
                data["file"] = target["resource"]
            if "end" not in data and target.get("time_window"):
                data["end"] = target["time_window"]

        # 兼容：旧版 signal_type -> namespace
        if "namespace" not in data and "signal_type" in data:
            data["namespace"] = _signal_type_to_namespace(data["signal_type"])

        # 验证 match_mode
        if data.get("match_mode") not in VALID_MATCH_MODES:
            data["match_mode"] = "or"

        # 验证 container
        if data.get("container") and data["container"] not in VALID_CONTAINERS:
            data["container"] = "asv-con"

        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> BackendSignal:
        """从 JSON 字符串反序列化并校验"""
        data = json.loads(json_str)
        return cls.from_dict(data)

    def is_cluster_mode(self) -> bool:
        """判断是否为集群模式"""
        return self.host == "cluster"


# ─── 旧枚举值 -> namespace 的映射（向后兼容）────────────────────────────────────
_LEGACY_TYPE_MAP: dict[str, str] = {
    "log_keyword": "log",
    "service_status": "service",
    "vm_state": "vm",
    "network_check": "network",
    "storage_state": "storage",
    "hardware_state": "hardware",
    "platform_state": "platform",
    "system_metric": "system",
}


def _signal_type_to_namespace(signal_type: str) -> str:
    """将旧的 signal_type 枚举值转换为 namespace 名。"""
    return _LEGACY_TYPE_MAP.get(signal_type, signal_type)
