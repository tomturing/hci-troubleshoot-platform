"""日志收集「故障场景」字典的请求/响应模型。

边界（本文件的核心约束）：字典只描述**展示层事实**——这个枚举叫什么、采集什么、
什么症状该选它、耗时多久、是否与其他项互斥。它**不定义能力边界**：哪些 `-m` 枚举真的
存在，仍然以集群 `acli log_collect create --help` 的探测结果为准。把两者混在一起会出现
"平台字典里有、集群 acli 不支持"的选项，用户点了必然 create 失败。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 与 terminal_bridge 的 logCollectModules 白名单同一形状：小写字母开头，允许数字与下划线。
SCENARIO_VALUE_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"

ScenarioReviewState = Literal["pending_review", "approved"]


class LogCollectScenarioCreate(BaseModel):
    """新建一条场景（管理端）。"""

    value: str = Field(pattern=SCENARIO_VALUE_PATTERN, max_length=32)
    label: str = Field(min_length=1, max_length=64)
    summary: str | None = Field(default=None, max_length=200)
    collects: str | None = None
    typical_symptoms: str | None = None
    duration_hint: str | None = Field(default=None, max_length=100)
    exclusive: bool = False
    sort_order: int = 100
    enabled: bool = True
    review_state: ScenarioReviewState = "pending_review"

    @field_validator("label")
    @classmethod
    def _require_label(cls, value: str | None) -> str:
        # label 是勾选框第一行的唯一文案，空字符串等于让用户面对一个无名选项，必须拒绝。
        # 注意不能用"strip 后归 None"的写法：那会让非 Optional 字段偷偷变成 None。
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError("label 不能为空")
        return stripped

    @field_validator("summary", "duration_hint")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class LogCollectScenarioUpdate(BaseModel):
    """修改一条场景（管理端）。

    `value` 刻意不在可改字段里：它是 acli 的枚举值，改它等于换一个场景，
    而历史任务里存的就是旧值，改掉会让"当时选了什么"无法追溯。
    """

    label: str | None = Field(default=None, min_length=1, max_length=64)
    summary: str | None = Field(default=None, max_length=200)
    collects: str | None = None
    typical_symptoms: str | None = None
    duration_hint: str | None = Field(default=None, max_length=100)
    exclusive: bool | None = None
    sort_order: int | None = None
    enabled: bool | None = None
    review_state: ScenarioReviewState | None = None

    @field_validator("label")
    @classmethod
    def _require_label(cls, value: str | None) -> str | None:
        # 部分更新：未提交 label 时为 None，此时跳过校验；显式提交空串则一律拒绝。
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("label 不能为空")
        return stripped

    @field_validator("summary", "duration_hint")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class LogCollectScenarioResponse(BaseModel):
    """字典项（客户端与管理端共用同一结构，客户端只取已启用项）。"""

    model_config = ConfigDict(from_attributes=True)

    value: str
    label: str
    summary: str | None = None
    collects: str | None = None
    typical_symptoms: str | None = None
    duration_hint: str | None = None
    exclusive: bool = False
    sort_order: int = 100
    enabled: bool = True
    review_state: ScenarioReviewState = "pending_review"
    updated_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LogCollectScenarioListResponse(BaseModel):
    scenarios: list[LogCollectScenarioResponse]
