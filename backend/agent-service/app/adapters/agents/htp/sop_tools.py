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

from typing import Any

from shared.clients import KBClient
from shared.observability.logger import get_logger

from app.memory.variable_pool import sop_request_variable

# 从公共模块导入（各模块的职责归属）
from app.tools.sop.client import ConversationSopClient
from app.tools.sop.nav import get_sop_node, sop_advance

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

    def __init__(
        self,
        *,
        sop_document_id: int,
        conversation_id: str,
        kb_client: KBClient,
        conversation_sop_client: ConversationSopClient,
        default_executor: Any,  # 原始 ToolExecutor（用于执行诊断工具）
        completed_steps: list[str] | None = None,  # T-AGT-23: 已完成节点列表（幂等性检查）
    ):
        self._sop_document_id = sop_document_id
        self._conversation_id = conversation_id
        self._kb_client = kb_client
        self._conversation_sop_client = conversation_sop_client
        self._default_executor = default_executor
        self._completed_steps = completed_steps or []  # T-AGT-23

    async def execute(self, tool_name: str, args: dict[str, Any]) -> Any:
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

        # SOP 导航工具：使用注入的上下文执行
        if tool_name == "get_sop_node":
            return await get_sop_node(
                node_id=args.get("node_id", "n-1"),
                sop_document_id=self._sop_document_id,
                kb_client=self._kb_client,
            )

        if tool_name == "sop_advance":
            return await sop_advance(
                target_node_id=args.get("target_node_id", ""),
                reasoning=args.get("reasoning", ""),
                conversation_id=self._conversation_id,
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
                conversation_id=self._conversation_id,
                sop_document_id=self._sop_document_id,
                kb_client=self._kb_client,
                conversation_sop_client=self._conversation_sop_client,
                tool_executor=self._default_executor,  # DC-02: 传入执行器用于 strategy=tool/user_confirm
            )

        # 其他工具（SCP/acli 诊断工具）：委托给默认执行器
        return await self._default_executor.execute(tool_name, args)
