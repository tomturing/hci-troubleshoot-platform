"""
QFK 后端信号结构定义
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


class BackendSignalTarget(BaseModel):
    """
    后端信号目标（Q: 查什么）的定位信息
    """

    scope: str | None = Field(default=None, description="查询范围限定，例如：主节点、备节点、所有节点或IP")
    resource: str | None = Field(default=None, description="具体资源名称，例如：vtpdaemon.log, redis, vm-101")
    path: str | None = Field(default=None, description="日志路径或文件所在目录，例如：/sf/log/today/")
    time_window: str | None = Field(default=None, description="时间范围，例如：今天，最近1小时，2026-07-01 10:00:00")


class BackendSignal(BaseModel):
    """
    HCI 排障标准化后端信号数据模型

    acquirer 采用 namespace 命名（qfk.log / qfk.service / qfk.system 等），
    namespace 字段为 acli 子命令空间名（log / service / vm / network / storage /
    hardware / platform / system），HandlerRegistry 据此路由到对应 Handler。
    """

    namespace: str = Field(..., description="acli 子命令空间名：log/service/vm/network/storage/hardware/platform/system")
    signal_type: str = Field(default="", description="信号类型描述（同 namespace，向后兼容 QFKResult 展示）")
    target: BackendSignalTarget | None = Field(default=None, description="定位目标参数描述")
    keywords: list[str] = Field(default_factory=list, description="K: 期望匹配对比的关键字列表")
    match_mode: str = Field(default="or", description="关键字组合匹配模式：or(任一) / and(全部) / not(均不出现)。not 取代旧 expected=False 的取反语义")
    expected: bool = Field(default=True, description="期望结果：True=期望出现，False=期望不出现")
    description: str | None = Field(default=None, description="对此排查步骤后端信号的文字表述说明")
    container: str | None = Field(default=None, description="对于 service 专属的容器类型 (asv/anet/host)")
    sub_command: str | None = Field(default=None, description="专属 vm/network/storage 等的 acli 子命名空间操作串")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackendSignal:
        """从字典构建并校验"""
        # 兼容：若传入 signal_type 但未传 namespace，则用 signal_type 推导
        if "namespace" not in data and "signal_type" in data:
            st = data["signal_type"]
            ns = _signal_type_to_namespace(st)
            data = {**data, "namespace": ns, "signal_type": ns}
        elif "namespace" in data and not data.get("signal_type"):
            data = {**data, "signal_type": data["namespace"]}
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> BackendSignal:
        """从 JSON 字符串反序列化并校验"""
        data = json.loads(json_str)
        return cls.from_dict(data)


# 旧枚举值 -> namespace 的映射（向后兼容旧 signals_json 数据）
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
