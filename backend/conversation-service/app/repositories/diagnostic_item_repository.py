"""
DiagnosticItem Repository - 诊断结论子表数据访问层

提供 diagnostic_item 的 CRUD 方法：
  - create: 创建新的诊断条目（S2/S3/S4/S5 各阶段）
  - batch_create: 批量创建（S2 假设列表）
  - update_status: 更新状态（S3 验证中、S4 确认/排除）
  - archive_all: 归档所有条目（S6 用户选 B 重进 S1）
  - get_by_conversation: 查询会话的所有诊断条目

设计依据：
  - docs/task/agent/events/2026-05-26-SOP执行引擎-M1数据库与M2导航工具化.md T-AGT-19
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.diagnostic_item import (
    STATUS_ARCHIVED,
    STATUS_PENDING,
    DiagnosticItem,
)


class DiagnosticItemRepository:
    """诊断结论子表数据访问层"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        conversation_id: uuid.UUID,
        stage: str,
        type: str,
        seq: int = 1,
        content: dict[str, Any] | None = None,
        probability: float | None = None,
        status: str = STATUS_PENDING,
        trace_id: str | None = None,
    ) -> DiagnosticItem:
        """创建单个诊断条目

        Args:
            conversation_id: 会话 ID
            stage: 阶段标识（S2/S3/S4/S5）
            type: 类型（hypothesis/verification_step/root_cause/solution）
            seq: 同会话同类型内排序序号（从1开始）
            content: 结构化内容（按 type 格式不同）
            probability: 假设概率（仅 type=hypothesis）
            status: 状态（默认 pending）
            trace_id: 请求 trace ID

        Returns:
            创建的 DiagnosticItem 实例
        """
        item = DiagnosticItem(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            stage=stage,
            type=type,
            seq=seq,
            content=content or {},
            probability=probability,
            status=status,
            trace_id=trace_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.session.add(item)
        await self.session.flush()
        await self.session.refresh(item)
        return item

    async def batch_create(
        self,
        conversation_id: uuid.UUID,
        stage: str,
        type: str,
        items_data: list[dict[str, Any]],
        trace_id: str | None = None,
    ) -> list[DiagnosticItem]:
        """批量创建诊断条目（S2 假设列表）

        Args:
            conversation_id: 会话 ID
            stage: 阶段标识
            type: 类型
            items_data: 条目数据列表，每个元素包含：
                - content: 结构化内容
                - probability: 假设概率（可选）
                - status: 状态（可选，默认 pending）
            trace_id: 请求 trace ID

        Returns:
            创建的 DiagnosticItem 实例列表
        """
        created_items = []
        for idx, data in enumerate(items_data, start=1):
            item = await self.create(
                conversation_id=conversation_id,
                stage=stage,
                type=type,
                seq=idx,
                content=data.get("content", {}),
                probability=data.get("probability"),
                status=data.get("status", STATUS_PENDING),
                trace_id=trace_id,
            )
            created_items.append(item)
        return created_items

    async def update_status(
        self,
        item_id: uuid.UUID,
        new_status: str,
        content_update: dict[str, Any] | None = None,
    ) -> DiagnosticItem | None:
        """更新诊断条目状态

        Args:
            item_id: 条目 ID
            new_status: 新状态（in_progress/confirmed/rejected/skipped）
            content_update: 内容更新（可选）

        Returns:
            更新后的 DiagnosticItem 实例，不存在时返回 None
        """
        # 构建更新值
        values = {
            "status": new_status,
            "updated_at": datetime.now(UTC),
        }
        if content_update:
            # 合并内容更新：使用 PostgreSQL JSONB || 运算符实现部分更新
            # 语法：content = content || :content_update，仅覆盖传入的 key
            values["content"] = DiagnosticItem.content.op("||")(content_update)

        await self.session.execute(update(DiagnosticItem).where(DiagnosticItem.id == item_id).values(**values))
        await self.session.flush()

        # 返回更新后的实例
        result = await self.session.execute(select(DiagnosticItem).where(DiagnosticItem.id == item_id))
        return result.scalar_one_or_none()

    async def archive_all(
        self,
        conversation_id: uuid.UUID,
    ) -> int:
        """归档会话的所有诊断条目（S6 用户选 B 重进 S1）

        Args:
            conversation_id: 会话 ID

        Returns:
            更新的条目数量
        """
        result = await self.session.execute(
            update(DiagnosticItem)
            .where(DiagnosticItem.conversation_id == conversation_id)
            .where(DiagnosticItem.status != STATUS_ARCHIVED)
            .values(
                status=STATUS_ARCHIVED,
                updated_at=datetime.now(UTC),
            )
        )
        await self.session.flush()
        return result.rowcount

    async def get_by_conversation(
        self,
        conversation_id: uuid.UUID,
        stage: str | None = None,
        type: str | None = None,
        status: str | None = None,
    ) -> list[DiagnosticItem]:
        """查询会话的诊断条目（可按阶段/类型/状态过滤）

        Args:
            conversation_id: 会话 ID
            stage: 阶段过滤（可选）
            type: 类型过滤（可选）
            status: 状态过滤（可选）

        Returns:
            DiagnosticItem 实例列表（按 seq 排序）
        """
        query = select(DiagnosticItem).where(DiagnosticItem.conversation_id == conversation_id)

        if stage:
            query = query.where(DiagnosticItem.stage == stage)
        if type:
            query = query.where(DiagnosticItem.type == type)
        if status:
            query = query.where(DiagnosticItem.status == status)

        query = query.order_by(DiagnosticItem.type, DiagnosticItem.seq)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_hypotheses_by_status(
        self,
        conversation_id: uuid.UUID,
        status: str | None = None,
    ) -> list[DiagnosticItem]:
        """查询会话的假设条目（便捷方法）

        Args:
            conversation_id: 会话 ID
            status: 状态过滤（可选）

        Returns:
            hypothesis 类型的 DiagnosticItem 列表
        """
        return await self.get_by_conversation(
            conversation_id=conversation_id,
            type="hypothesis",
            status=status,
        )

    async def get_verification_steps(
        self,
        conversation_id: uuid.UUID,
    ) -> list[DiagnosticItem]:
        """查询会话的验证步骤条目（便捷方法）

        Args:
            conversation_id: 会话 ID

        Returns:
            verification_step 类型的 DiagnosticItem 列表
        """
        return await self.get_by_conversation(
            conversation_id=conversation_id,
            type="verification_step",
        )
