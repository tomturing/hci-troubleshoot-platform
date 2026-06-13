"""
SopExecution Repository - SOP 执行状态数据访问层

提供 SOP 执行实例的 CRUD 和状态推进方法：
  - get_active_by_conversation: 查询活跃的执行实例（用于中断恢复）
  - create: 创建新的执行实例（S1 阶段命中 SOP 时）
  - advance: 推进到下一节点（sop_advance 工具调用）
  - complete: 标记执行完成（到达叶节点时）
  - interrupt: 标记中断等待变量
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.sop_execution import (
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_INTERRUPTED,
    SopExecution,
)


class SopExecutionRepository:
    """SOP 执行状态数据访问层"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_active_by_conversation(self, conversation_id: uuid.UUID) -> SopExecution | None:
        """查询活跃的执行实例（用于中断恢复）

        Args:
            conversation_id: 会话 ID

        Returns:
            SopExecution 实例，若不存在或状态非 active 则返回 None
        """
        result = await self.session.execute(
            select(SopExecution)
            .where(SopExecution.conversation_id == conversation_id)
            .where(SopExecution.status == STATUS_ACTIVE)
        )
        return result.scalar_one_or_none()

    async def get_by_conversation(self, conversation_id: uuid.UUID) -> SopExecution | None:
        """查询任意状态的执行实例

        Args:
            conversation_id: 会话 ID

        Returns:
            SopExecution 实例，不存在时返回 None
        """
        result = await self.session.execute(select(SopExecution).where(SopExecution.conversation_id == conversation_id))
        return result.scalar_one_or_none()

    async def create(
        self,
        conversation_id: uuid.UUID,
        sop_document_id: int,
        current_node_id: str,
        trace_id: str | None = None,
        initial_variables: dict[str, Any] | None = None,
        initial_variable_sources: dict[str, str] | None = None,
    ) -> SopExecution:
        """创建新的 SOP 执行实例（S1 阶段命中 SOP 时）

        Args:
            conversation_id: 会话 ID（唯一约束，一个会话只能有一个执行实例）
            sop_document_id: SOP 文档 ID
            current_node_id: 当前节点 ID（通常是根节点）
            trace_id: 请求 trace ID
            initial_variables: 初始环境注入的变量（可选）
            initial_variable_sources: 初始变量来源标记（可选）

        Returns:
            创建的 SopExecution 实例
        """
        context_vars = {}
        if initial_variables:
            for var_name, var_value in initial_variables.items():
                context_vars[var_name] = {
                    "value": var_value,
                    "source": (initial_variable_sources or {}).get(var_name, "env_context"),
                    "resolved_at": datetime.now(UTC).isoformat(),
                }

        execution = SopExecution(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            sop_document_id=sop_document_id,
            current_node_id=current_node_id,
            status=STATUS_ACTIVE,
            context_variables=context_vars,
            completed_steps=[],
            pending_variable_name=None,
            execution_log=[
                {
                    "type": "node_entered",
                    "node_id": current_node_id,
                    "entered_at": datetime.now(UTC).isoformat(),
                    "reasoning": "SOP 执行开始，进入根节点",
                }
            ],
            trace_id=trace_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.session.add(execution)
        await self.session.flush()
        await self.session.refresh(execution)
        return execution

    async def advance(
        self,
        conversation_id: uuid.UUID,
        target_node_id: str,
        reasoning: str,
        node_type: str | None = None,
        variables_extracted: dict[str, Any] | None = None,
        existing_execution: "SopExecution | None" = None,  # DC-03: 避免重复查询
    ) -> SopExecution | None:
        """推进到下一节点（sop_advance 工具调用）

        操作：
          1. 更新 current_node_id 为目标节点
          2. 追加 execution_log 条目（node_entered）
          3. 追加 completed_steps（前一节点标记完成）
          4. 若叶节点（solution）则更新 status=completed
          5. 若有 variables_extracted 则更新 context_variables

        Args:
            conversation_id: 会话 ID
            target_node_id: 目标节点 ID
            reasoning: LLM 推进理由（写入 execution_log）
            node_type: 目标节点类型（diagnosis/solution/branch），用于判断是否叶节点
            variables_extracted: 变量池更新（可选）

        Returns:
            更新后的 SopExecution 实例，不存在时返回 None
        """
        # 查询当前执行实例（DC-03: 若已有实例则跳过查询）
        execution = existing_execution or await self.get_active_by_conversation(conversation_id)
        if execution is None:
            return None

        # 记录前一节点
        prev_node_id = execution.current_node_id

        # 追加 completed_steps
        completed_steps = list(execution.completed_steps or [])
        if prev_node_id not in completed_steps:
            completed_steps.append(prev_node_id)

        # 追加 execution_log
        execution_log = list(execution.execution_log or [])
        execution_log.append(
            {
                "type": "node_entered",
                "node_id": target_node_id,
                "entered_at": datetime.now(UTC).isoformat(),
                "reasoning": reasoning,
            }
        )

        # 更新 context_variables（T-AGT-27: source 标记为 tool_result）
        context_variables = dict(execution.context_variables or {})
        if variables_extracted:
            for var_name, var_value in variables_extracted.items():
                context_variables[var_name] = {
                    "value": var_value,
                    "source": "tool_result",
                    "resolved_at": datetime.now(UTC).isoformat(),
                    "resolved_by_tool": "sop_advance",
                }

        # 判断是否叶节点（solution）
        is_leaf = node_type == "solution"
        new_status = STATUS_COMPLETED if is_leaf else STATUS_ACTIVE

        # 执行更新
        await self.session.execute(
            update(SopExecution)
            .where(SopExecution.conversation_id == conversation_id)
            .values(
                current_node_id=target_node_id,
                status=new_status,
                completed_steps=completed_steps,
                execution_log=execution_log,
                context_variables=context_variables,
                updated_at=datetime.now(UTC),
            )
        )
        await self.session.flush()

        # 返回更新后的实例
        return await self.get_by_conversation(conversation_id)

    async def complete(self, conversation_id: uuid.UUID) -> SopExecution | None:
        """标记执行完成（到达叶节点时）

        Args:
            conversation_id: 会话 ID

        Returns:
            更新后的 SopExecution 实例
        """
        await self.session.execute(
            update(SopExecution)
            .where(SopExecution.conversation_id == conversation_id)
            .where(SopExecution.status == STATUS_ACTIVE)
            .values(
                status=STATUS_COMPLETED,
                updated_at=datetime.now(UTC),
            )
        )
        await self.session.flush()
        return await self.get_by_conversation(conversation_id)

    async def interrupt(
        self,
        conversation_id: uuid.UUID,
        pending_variable_name: str,
    ) -> SopExecution | None:
        """标记中断等待变量（sop_request_variable 工具阻塞时）

        Args:
            conversation_id: 会话 ID
            pending_variable_name: 待填变量名

        Returns:
            更新后的 SopExecution 实例
        """
        await self.session.execute(
            update(SopExecution)
            .where(SopExecution.conversation_id == conversation_id)
            .where(SopExecution.status == STATUS_ACTIVE)
            .values(
                status=STATUS_INTERRUPTED,
                pending_variable_name=pending_variable_name,
                updated_at=datetime.now(UTC),
            )
        )
        await self.session.flush()
        return await self.get_by_conversation(conversation_id)

    async def resume(self, conversation_id: uuid.UUID) -> SopExecution | None:
        """恢复执行（变量填充后）

        Args:
            conversation_id: 会话 ID

        Returns:
            更新后的 SopExecution 实例
        """
        await self.session.execute(
            update(SopExecution)
            .where(SopExecution.conversation_id == conversation_id)
            .where(SopExecution.status == STATUS_INTERRUPTED)
            .values(
                status=STATUS_ACTIVE,
                pending_variable_name=None,
                updated_at=datetime.now(UTC),
            )
        )
        await self.session.flush()
        return await self.get_by_conversation(conversation_id)

    async def set_variable(
        self,
        conversation_id: uuid.UUID,
        variable_name: str,
        value: Any,
        source: str = "user_input",
    ) -> SopExecution | None:
        """写入变量值并恢复执行状态（T-AGT-25）。

        操作：
          1. 写入 context_variables[variable_name] = {value, source, resolved_at}
          2. 清空 pending_variable_name
          3. 恢复 status=active（如果之前是 interrupted）

        Args:
            conversation_id: 会话 ID
            variable_name: 变量名
            value: 变量值
            source: 值来源（user_input/user_confirm/tool_result/env_context）

        Returns:
            更新后的 SopExecution 实例
        """
        execution = await self.get_by_conversation(conversation_id)
        if execution is None:
            return None

        # 获取当前 context_variables
        context_variables = dict(execution.context_variables or {})
        context_variables[variable_name] = {
            "value": value,
            "source": source,
            "resolved_at": datetime.now(UTC).isoformat(),
        }

        # 确定新状态：如果之前是 interrupted，恢复为 active
        new_status = STATUS_ACTIVE if execution.status == STATUS_INTERRUPTED else execution.status
        new_pending = None if execution.pending_variable_name == variable_name else execution.pending_variable_name

        await self.session.execute(
            update(SopExecution)
            .where(SopExecution.conversation_id == conversation_id)
            .values(
                status=new_status,
                context_variables=context_variables,
                pending_variable_name=new_pending,
                updated_at=datetime.now(UTC),
            )
        )
        await self.session.flush()
        return await self.get_by_conversation(conversation_id)
