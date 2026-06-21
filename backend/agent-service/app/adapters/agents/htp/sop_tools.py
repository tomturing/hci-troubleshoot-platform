"""
SOP 工具执行协调层（HTP Agent 专用）

本模块是 HTP Agent 的 SOP 工具执行协调器。
核心功能 SopToolExecutor 负责将 ReactEngine 的工具调用分发到：
  - SOP 导航工具 (app.tools.sop.nav): get_sop_node, sop_advance
  - 变量池引擎  (app.memory.variable_pool): sop_request_variable
  - 默认执行器   (ToolExecutor): SCP / acli 诊断工具

向后兼容导出：
  - ConversationSopClient  → app.tools.sop.client
  - get_sop_node           → app.tools.sop.nav
  - VariableRequestResult  → app.memory.variable_pool
  - sop_request_variable   → app.memory.variable_pool

架构说明（见 docs/solution/agent/agent设计.md §12.6）：
  SOP 命中后，InvestigationAgent 创建 SopToolExecutor 并传递给 ReactEngine，
  ReactEngine 在执行 SOP 工具调用时使用此执行器注入上下文，
  实现上下文可控的决策树遍历（避免一次性注入完整 SOP 文档）。
"""

from __future__ import annotations

import uuid
from typing import Any

from shared.clients import KBClient
from shared.observability.logger import get_logger

from app.memory.variable_pool import sop_request_variable

# 从公共模块导入（各模块的职责归属）
from app.tools.sop.client import ConversationSopClient
from app.tools.sop.nav import (
    find_missing_guarded_variables_for_node_window,
    find_node_in_tree,
    get_sop_node,
    sop_advance,
)

logger = get_logger("htp.sop-tools")


# ─────────────────────────────────────────────────────────────────────────────
# T-AGT-22: SopToolExecutor 工具执行器
# ─────────────────────────────────────────────────────────────────────────────


class SopToolExecutor:
    """SOP 导航工具执行器（T-AGT-22 + T-AGT-23）。

    专为 ReactEngine 设计的工具执行器，用于执行 SOP 导航工具
    （get_sop_node、sop_advance），注入必要的上下文。

    T-AGT-23 新增：
      - completed_steps: 已完成节点列表（幂等性检查）
      - 写操作工具在 completed_steps 中节点被跳过执行

    使用场景：
      SOP 命中后，InvestigationAgent 创建此执行器并传递给 ReactEngine，
      ReactEngine 在执行 SOP 工具时使用此执行器而非默认的 ToolExecutor。
    """

    # 写操作工具列表（risk_level >= 2）
    WRITE_OPERATION_TOOLS = {
        "acli_service_restart",
        "acli_network_nic_up",
        "acli_netdoctor",
    }
    SOP_NAVIGATION_TOOLS = {"get_sop_node", "sop_advance", "sop_request_variable"}

    def __init__(
        self,
        *,
        sop_document_id: int,
        conversation_id: str,
        kb_client: KBClient,
        conversation_sop_client: ConversationSopClient,
        default_executor: Any,  # 原始 ToolExecutor（用于执行诊断工具）
        skill_runner: Any | None = None,
        completed_steps: list[str] | None = None,  # T-AGT-23: 已完成节点列表（幂等性检查）
    ):
        self._sop_document_id = sop_document_id
        self._conversation_id = conversation_id
        self._kb_client = kb_client
        self._conversation_sop_client = conversation_sop_client
        self._default_executor = default_executor
        self._skill_runner = skill_runner
        self._completed_steps = completed_steps or []  # T-AGT-23

    async def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        conversation_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """执行工具调用。

        SOP 工具使用本执行器的上下文注入执行，
        其他工具委托给默认执行器（SCP/acli 诊断工具）。

        T-AGT-23 新增幂等性检查：
          - 若 tool_name 是写操作工具且当前节点在 completed_steps 中，
            返回跳过执行消息而非实际执行。

        Args:
            tool_name: 工具名称
            args: 工具参数（由 LLM 传入）

        Returns:
            工具执行结果（字典格式）
        """
        # T-AGT-23: 幂等性检查 - 写操作工具在 SOP 恢复模式下跳过重复执行
        if tool_name in self.WRITE_OPERATION_TOOLS and self._completed_steps:
            logger.info(
                event="write_tool_idempotency_skip",
                tool_name=tool_name,
                completed_steps=self._completed_steps,
                conversation_id=self._conversation_id,
                message="SOP 恢复模式：跳过写操作工具，避免重复执行",
            )
            return {
                "skipped": True,
                "reason": (f"SOP 恢复模式：工具 {tool_name} 已在先前节点中执行，跳过重复执行以保证幂等性"),
                "completed_steps_count": len(self._completed_steps),
            }

        effective_conversation_id = conversation_id or self._conversation_id

        if tool_name == "get_sop_node":
            context_variables = {}
            try:
                execution = await self._conversation_sop_client.get_execution(uuid.UUID(effective_conversation_id))
                if execution:
                    context_variables = execution.get("context_variables") or {}
            except Exception as exc:
                logger.warning(
                    event="sop_get_sop_node_execution_load_failed",
                    conversation_id=effective_conversation_id,
                    error=str(exc),
                )
            return await get_sop_node(
                node_id=args.get("node_id", "n-1"),
                sop_document_id=self._sop_document_id,
                kb_client=self._kb_client,
                context_variables=context_variables,
            )

        if tool_name == "sop_advance":
            return await sop_advance(
                target_node_id=args.get("target_node_id", ""),
                reasoning=args.get("reasoning", ""),
                conversation_id=effective_conversation_id,
                sop_document_id=self._sop_document_id,
                kb_client=self._kb_client,
                conversation_sop_client=self._conversation_sop_client,
                node_type=args.get("node_type"),
                variables_extracted=args.get("variables_extracted"),
                completed_steps=self._completed_steps,  # T-AGT-23
            )

        if tool_name == "sop_request_variable":
            return await sop_request_variable(
                variable_name=args.get("variable_name", ""),
                reason=args.get("reason"),
                conversation_id=effective_conversation_id,
                sop_document_id=self._sop_document_id,
                kb_client=self._kb_client,
                conversation_sop_client=self._conversation_sop_client,
                tool_executor=self._default_executor,  # DC-02: 传入执行器用于 strategy=tool/user_confirm
                skill_runner=self._skill_runner,
            )

        variable_gate_result = await self._check_variable_source_gate(
            tool_name=tool_name,
            conversation_id=effective_conversation_id,
        )
        if variable_gate_result is not None:
            return variable_gate_result

        # 其他工具（SCP/acli 诊断工具）：委托给默认执行器，传递 conversation_id
        return await self._default_executor.execute(
            tool_name, args, conversation_id=effective_conversation_id, **kwargs
        )

    async def _check_variable_source_gate(
        self,
        *,
        tool_name: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """在真实诊断工具执行前强制检查 SOP 变量来源契约。

        SOP 作者在 Markdown「变量声明」中定义的 user_input/user_confirm/env_* 来源，
        是控制面契约。LLM 不能先调用 bash_exec/acli_exec 自行替代这些来源。
        """
        if tool_name in self.SOP_NAVIGATION_TOOLS:
            return None

        try:
            execution = await self._conversation_sop_client.get_execution(uuid.UUID(conversation_id))
        except Exception as exc:
            logger.warning(
                event="sop_variable_gate_execution_load_failed",
                tool_name=tool_name,
                conversation_id=conversation_id,
                error=str(exc),
            )
            return None

        if not execution:
            return None

        current_node_id = execution.get("current_node_id") or "n-1"
        context_variables = execution.get("context_variables") or {}

        try:
            tree_data = await self._kb_client.get_sop_tree(self._sop_document_id)
            tree_json = (tree_data or {}).get("tree") if isinstance(tree_data, dict) else None
            sop_doc = await self._kb_client.get_sop_document(self._sop_document_id)
        except Exception as exc:
            logger.warning(
                event="sop_variable_gate_sop_load_failed",
                tool_name=tool_name,
                conversation_id=conversation_id,
                sop_document_id=self._sop_document_id,
                error=str(exc),
            )
            return None

        if not isinstance(tree_json, dict):
            return None

        variable_schema = []
        if isinstance(sop_doc, dict) and isinstance(sop_doc.get("variable_schema"), list):
            variable_schema = sop_doc.get("variable_schema") or []

        current_node = find_node_in_tree(tree_json, current_node_id)
        if current_node is None:
            return None

        missing_variables = find_missing_guarded_variables_for_node_window(
            current_node=current_node,
            variable_schema=variable_schema,
            context_variables=context_variables,
        )
        if not missing_variables:
            return None

        first_missing = missing_variables[0]
        logger.info(
            event="sop_variable_gate_blocked_tool_call",
            tool_name=tool_name,
            conversation_id=conversation_id,
            sop_document_id=self._sop_document_id,
            current_node_id=current_node_id,
            missing_variables=[v.get("name") for v in missing_variables],
        )
        reason = first_missing.get("description") or (
            f"SOP 当前节点 {current_node_id} 需要变量 {first_missing.get('name')}"
        )
        return {
            "ok": False,
            "error": "sop_variable_gate_blocked",
            "message": (
                f"SOP 当前节点依赖变量 {first_missing.get('name')}，"
                "且该变量尚未按声明来源获取。请先调用 sop_request_variable。"
            ),
            "tool_name": tool_name,
            "current_node_id": current_node_id,
            "missing_variables": missing_variables,
            "next_tool_call": {
                "tool_name": "sop_request_variable",
                "args": {
                    "variable_name": first_missing.get("name"),
                    "reason": reason,
                },
            },
        }
