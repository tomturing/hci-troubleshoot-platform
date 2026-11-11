"""日志收集「故障场景」字典的读写服务。

为什么单独一层而不是把 SQL 塞进路由：这张表的读取方有两个（客户面板、管理端），
写入方只有一个（管理端），而"客户端只允许看到已启用项"是**安全与体验双重约束**
（停用的场景一旦出现在客户面前，就等于承诺了一个平台已下线的能力）。把过滤规则
写在服务层，两个入口就不会各写一份而先后漂移。
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..schemas.log_collect_scenario import (
    LogCollectScenarioCreate,
    LogCollectScenarioResponse,
    LogCollectScenarioUpdate,
)
from .log_collect_spc_catalog import M_WHITELIST

logger = logging.getLogger(__name__)

# 列清单集中一处：RETURNING / SELECT 共用，避免加字段时漏掉某一处导致响应缺列。
_SCENARIO_COLUMNS = (
    "value, label, summary, collects, typical_symptoms, duration_hint, "
    "exclusive, sort_order, enabled"
)

# 可更新列白名单：模型字段名会直接拼进 SET 子句，绝不能依赖"pydantic 只送合法字段"这种外部假设。
_UPDATABLE_COLUMNS = frozenset(
    {
        "label",
        "summary",
        "collects",
        "typical_symptoms",
        "duration_hint",
        "exclusive",
        "sort_order",
        "enabled",
    }
)


class LogCollectScenarioNotFoundError(Exception):
    """字典项不存在：路由层转 404，不暴露"表里有也没有"的额外信息。"""


class LogCollectScenarioService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_scenarios(self, *, include_disabled: bool) -> list[LogCollectScenarioResponse]:
        """按运营配置的顺序返回字典项。

        `include_disabled=False` 是客户端的唯一入口口径：停用项不出现，
        但历史任务结果态仍按 value 回显（结果态只用 label，不依赖 enabled）。
        """
        where_clause = "" if include_disabled else "WHERE enabled"
        result = await self.session.execute(
            text(
                f"SELECT {_SCENARIO_COLUMNS} FROM log_collect_scenario "  # noqa: S608 - 静态列名与固定子句
                f"{where_clause} ORDER BY sort_order, value"
            )
        )
        return [LogCollectScenarioResponse(**row) for row in result.mappings()]

    async def create_scenario(
        self, payload: LogCollectScenarioCreate
    ) -> LogCollectScenarioResponse:
        """补录字典项；枚举必须是 acli -m 白名单内的真实能力。

        白名单校验是硬约束：平台字典只是展示层文案，造一个 acli 不支持的枚举
        会让客户看到永远无法采集的选项，且旧的场景表没有删除接口、无法清理。
        """
        if payload.value not in M_WHITELIST:
            raise ValueError(f"不是 acli 支持的场景枚举：{payload.value}")
        result = await self.session.execute(
            text(
                f"""
                INSERT INTO log_collect_scenario
                    (value, label, summary, collects, typical_symptoms, duration_hint,
                     exclusive, sort_order, enabled)
                VALUES
                    (:value, :label, :summary, :collects, :typical_symptoms, :duration_hint,
                     :exclusive, :sort_order, :enabled)
                RETURNING {_SCENARIO_COLUMNS}
                """
            ),
            {
                "value": payload.value,
                "label": payload.label,
                "summary": payload.summary,
                "collects": payload.collects,
                "typical_symptoms": payload.typical_symptoms,
                "duration_hint": payload.duration_hint,
                "exclusive": payload.exclusive,
                "sort_order": payload.sort_order,
                "enabled": payload.enabled,
            },
        )
        row = result.mappings().one()
        return LogCollectScenarioResponse(**row)

    async def update_scenario(
        self,
        value: str,
        payload: LogCollectScenarioUpdate,
    ) -> LogCollectScenarioResponse:
        """按需更新字段（只写调用方显式给出的列）。

        用 `model_dump(exclude_unset=True)` 而不是"全量覆盖"：只改一个开关时，
        全量覆盖会把没提交的说明文本清空——对运营是数据丢失，对客户是文案突然消失。
        """
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise ValueError("没有需要更新的字段")
        illegal = set(changes) - _UPDATABLE_COLUMNS
        if illegal:
            # 理论上不可达（模型里没有这些字段），留着是为了不让"新增字段忘了登记"变成 SQL 注入面。
            raise ValueError(f"不允许更新的字段：{sorted(illegal)}")

        set_clause = ", ".join(f"{column} = :{column}" for column in changes)
        result = await self.session.execute(
            text(
                f"""
                UPDATE log_collect_scenario
                SET {set_clause}
                WHERE value = :value
                RETURNING {_SCENARIO_COLUMNS}
                """
            ),
            {**changes, "value": value},
        )
        row = result.mappings().first()
        if row is None:
            raise LogCollectScenarioNotFoundError(value)
        return LogCollectScenarioResponse(**row)
