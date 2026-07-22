"""
QFK 后端信号结构定义
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ─── 有效容器枚举（qfk_system 专用）────────────────────────────────────────────
VALID_CONTAINERS = {"asv-con", "vn-con", "vn-agent", "vs-cp-manager"}

# ─── 有效服务容器（旧版语义，service handler 校验）──────────────────────────────
VALID_SERVICE_CONTAINERS = {"asv", "vn", "vn-agent", "vs"}

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

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    # ─── 共有字段 ─────────────────────────────────────────────────────────────
    instruction: str | None = Field(default=None, description="关键信号说明")
    host: str | None = Field(default=None, description="主机（变量池获取，特殊值 'cluster' 表示遍历集群）")
    vm: str | None = Field(default=None, description="虚拟机（变量池获取，可为空）")
    keyword: list[str] = Field(default_factory=list, description="关键字（必填）")
    timeout: int | None = Field(default=None, ge=1, le=300, description="超时时间（秒），默认 None 表示使用全局默认")
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
    container: str | None = Field(default=None, description="容器类型（qfk_system 选填）")

    # qfk_service
    service: str | None = Field(default=None, description="服务名称（qfk_service 必填）")
    action: str = Field(default="status", description="动作（qfk_service 选填，默认 status）")

    # ─── 兼容旧字段（向后兼容）─────────────────────────────────────────────────
    target: BackendSignalTarget | None = Field(default=None, description="旧版 target 字段（兼容）")
    sub_command: str | None = Field(default=None, description="旧版 sub_command 字段（兼容）")
    description: str | None = Field(default=None, description="旧版 description 字段（兼容）")

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_fields(cls, data: Any) -> Any:
        """对 dict 输入做兼容转换：target dict→BackendSignalTarget、keywords→keyword、sub_command→command、description→instruction、signal_type→namespace。"""
        if not isinstance(data, dict):
            return data
        data = dict(data)

        # target: dict → BackendSignalTarget
        if "target" in data and isinstance(data["target"], dict):
            allowed = {"scope", "resource", "path", "time_window"}
            data["target"] = BackendSignalTarget(
                **{k: v for k, v in data["target"].items() if k in allowed}
            )

        # keywords / keyword 互转
        if "keyword" not in data and "keywords" in data:
            kw = data["keywords"]
            data["keyword"] = [kw] if isinstance(kw, str) else list(kw or [])

        # description → instruction
        if "instruction" not in data and "description" in data:
            data["instruction"] = data["description"]

        # sub_command → command
        if "command" not in data and "sub_command" in data:
            data["command"] = data["sub_command"]

        # signal_type → namespace（同时保留 signal_type 用于旧代码访问）
        if "namespace" not in data and "signal_type" in data:
            ns = _signal_type_to_namespace(data["signal_type"])
            data["namespace"] = ns
            data["signal_type"] = ns

        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackendSignal:
        """从字典构建并校验，支持新旧字段兼容"""
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> BackendSignal:
        """从 JSON 字符串反序列化并校验"""
        data = json.loads(json_str)
        return cls.model_validate(data)

    def is_cluster_mode(self) -> bool:
        """判断是否为集群模式"""
        return self.host == "cluster"

    # 兼容旧字段访问（让 sig.keywords、sig.signal_type 等旧代码继续工作）
    @property
    def keywords_compat(self) -> list[str]:
        """旧版 keywords 字段（兼容）：返回 keyword 列表"""
        return self.keyword

    @property
    def signal_type_compat(self) -> str | None:
        """旧版 signal_type 字段（兼容）：返回 namespace"""
        return self.namespace

    # 提供给旧测试代码使用：sig.keywords == ["X"]
    @property
    def keywords(self) -> list[str]:
        """旧版 keywords 字段（兼容）：优先返回显式 keywords，其次 keyword"""
        if self.__dict__.get("keywords"):
            return list(self.__dict__["keywords"])
        return list(self.keyword)

    @keywords.setter
    def keywords(self, value: list[str] | None) -> None:
        """旧版 keywords 字段（兼容 setter）"""
        self.__dict__["keywords"] = list(value) if value else None

    @property
    def signal_type(self) -> str | None:
        """旧版 signal_type 字段（兼容）：返回 namespace"""
        if self.__dict__.get("signal_type"):
            return self.__dict__["signal_type"]
        return self.namespace

    @signal_type.setter
    def signal_type(self, value: str | None) -> None:
        """旧版 signal_type 字段（兼容 setter）"""
        self.__dict__["signal_type"] = value


class BackendSignalTarget(BaseModel):
    """
    QFK 信号目标（旧版 target 模型，向后兼容）

    历史语义：
    - scope: 主机/IP 范围（变量池获取）
    - resource: 目标资源（日志文件名/服务名等）
    - path: 路径
    - time_window: 时间窗口（结束时间）

    新版字段已平铺到 BackendSignal 顶层（host/vm/file/end/scope/resource/path/time_window），
    保留此类用于：
    1. 旧版 signals_json 数据反序列化（target 字段作为整体传入）
    2. 旧版测试代码引用

    新代码应直接使用 BackendSignal 的顶层字段。
    """

    scope: str | None = Field(default=None, description="主机/IP 范围（变量池获取）")
    resource: str | None = Field(default=None, description="目标资源（日志文件名/服务名等）")
    path: str | None = Field(default=None, description="路径")
    time_window: str | None = Field(default=None, description="时间窗口（结束时间）")

    def to_backend_signal_fields(self) -> dict[str, Any]:
        """转换为 BackendSignal 顶层字段"""
        out: dict[str, Any] = {}
        if self.scope:
            out["host"] = self.scope
        if self.resource:
            out["file"] = self.resource
        if self.time_window:
            out["end"] = self.time_window
        return out


def _coerce_target(value: Any) -> BackendSignalTarget | None:
    """接受 dict 或 BackendSignalTarget，统一返回 BackendSignalTarget 实例。"""
    if value is None:
        return None
    if isinstance(value, BackendSignalTarget):
        return value
    if isinstance(value, dict):
        # 过滤未声明的字段
        allowed = {"scope", "resource", "path", "time_window"}
        return BackendSignalTarget(**{k: v for k, v in value.items() if k in allowed})
    return None


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
