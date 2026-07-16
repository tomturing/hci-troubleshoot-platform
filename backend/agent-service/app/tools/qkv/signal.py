"""
QKV 前端信号数据模型与类型定义
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class FrontendQueryType(StrEnum):
    """前端信号查询类型"""

    ALERT = "alert"    # 告警信息
    TASK = "task"      # 操作任务
    DIALOG = "dialog"  # 对话/弹框日志


class FrontendSignal(BaseModel):
    """
    前端信号模型（QKV 加载处理）
    """

    query: FrontendQueryType = Field(..., description="Q: 查什么，告警/任务/弹框")
    keyword: str = Field(..., description="K: 匹配关键字")
    is_failed: bool = Field(default=False, description="是否只查失败任务 (仅在 query 为 task 时生效)")
    limit: int = Field(default=100, description="最大返回数据量限制")
    produces: list[dict[str, str]] = Field(
        default_factory=list,
        description="产出变量规格：[{name: 'HOST', path: 'host'}, ...]，为空时 parser 走硬编码兜底",
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrontendSignal:
        """从字典构建并校验"""
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> FrontendSignal:
        """从 JSON 串反序列化并校验"""
        data = json.loads(json_str)
        return cls.from_dict(data)
