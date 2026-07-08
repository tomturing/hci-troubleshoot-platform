"""
QFK 后端信号结构定义
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class BackendSignalType(StrEnum):
    """
    后端信号类型：定义了 QFK 执行时调用的底层指令与匹配策略
    """

    LOG_KEYWORD = "log_keyword"          # 日志关键字匹配
    SERVICE_STATUS = "service_status"    # 服务运行状态匹配
    VM_STATE = "vm_state"                # 虚拟机状态匹配
    NETWORK_CHECK = "network_check"      # 网络检查
    STORAGE_STATE = "storage_state"      # 存储状态匹配
    HARDWARE_STATE = "hardware_state"    # 硬件状态匹配
    PLATFORM_STATE = "platform_state"    # 平台状态匹配
    SYSTEM_METRIC = "system_metric"      # 系统指标匹配


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
    """

    signal_type: BackendSignalType = Field(..., description="信号类型，对应具体排障场景的处理方法")
    target: BackendSignalTarget | None = Field(default=None, description="定位目标参数描述")
    keywords: list[str] = Field(default_factory=list, description="K: 期望匹配对比的关键字列表")
    match_mode: str = Field(default="any", description="关键字对比匹配模式：any(或) / all(与)")
    expected: bool = Field(default=True, description="期望结果：True=期望出现，False=期望不出现")
    description: str | None = Field(default=None, description="对此排查步骤后端信号的文字表述说明")
    container: str | None = Field(default=None, description="对于 service_status 专属的容器类型 (asv/anet/host)")
    sub_command: str | None = Field(default=None, description="专属 vm/network/storage 等的 acli 子命名空间操作串")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackendSignal:
        """从字典构建并校验"""
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> BackendSignal:
        """从 JSON 字符串反序列化并校验"""
        data = json.loads(json_str)
        return cls.from_dict(data)
