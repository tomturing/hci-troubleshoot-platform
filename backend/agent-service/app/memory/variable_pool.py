"""
变量池 — Agent 工作记忆的变量 JIT 获取引擎（T-AGT-25）

理论基础（见 docs/solution/agent/agent记忆设计.md §3, §5）：
  变量池是 SOP 执行上下文的工作记忆层。
  Schema 层（variable_schema JSONB）是程序性记忆，记录变量定义和获取策略；
  值层（context_variables JSONB）是工作记忆，存储当前执行状态中已获取的变量值。
  每轮 LLM 调用前，context_variables 被渲染注入上下文窗口（MemGPT 外化工作记忆模式）。

变量赋值策略（acquisition_strategy 字段）：
  - env_injection  : 环境上下文批量注入（初始化阶段，不走 JIT 流程）
  - sop_default    : SOP 文档预设默认值（初始化阶段直接读 variable_schema.default_value，JIT 路径也支持）
  - user_input     : 阻塞等待用户填写
  - user_confirm   : 调用工具获取候选列表后让用户确认
  - tool_call      : 自动调用指定工具获取值（DC-02），失败时降级为 user_input
  - llm_inference  : LLM 从对话推断得出（通过 sop_advance.variables_extracted 被动写入）
  - agent_pass     : Agent 初始化或编程方式写入（不走 JIT 流程）

导出：
  - VariableRequestResult: 需要用户交互时的返回结果
  - sop_request_variable: JIT 变量获取函数（LLM 可调用工具）
"""

from __future__ import annotations

import uuid
from typing import Any

from shared.clients import KBClient
from shared.observability.logger import get_logger

logger = get_logger("memory.variable-pool")


class VariableRequestResult:
    """变量请求结果（用于标识需要阻塞等待用户输入）。

    当 sop_request_variable 需要用户输入时返回此类型，
    ReactEngine 或 InvestigationAgent 捕获此结果后 yield AgentInteractiveRequest。

    Attributes:
        needs_input: 是否需要用户输入（True 时阻塞等待）
        variable_name: 变量名
        variable_schema: 变量 Schema 定义（含 display_name、description、validation_pattern）
        current_value: 当前值（若已存在）
        message: 消息（用于 LLM 或用户）
        kind: 交互类型（variable_input / variable_confirm）
        options: 候选选项列表（user_confirm 类型时使用）
    """

    def __init__(
        self,
        *,
        needs_input: bool = False,
        variable_name: str = "",
        variable_schema: dict | None = None,
        current_value: Any = None,
        message: str = "",
        kind: str = "variable_input",
        options: list[dict] | None = None,
    ):
        self.needs_input = needs_input
        self.variable_name = variable_name
        self.variable_schema = variable_schema or {}
        self.current_value = current_value
        self.message = message
        self.kind = kind
        self.options = options or []


async def sop_request_variable(
    variable_name: str,
    reason: str | None = None,
    *,
    conversation_id: str,
    sop_document_id: int,
    kb_client: KBClient,
    conversation_sop_client: Any | None = None,  # ConversationSopClient，避免循环导入用 Any
    tool_executor: Any | None = None,  # DC-02: 用于 strategy="tool_call" 自动调用工具获取变量值
) -> VariableRequestResult | dict[str, Any]:
    """请求获取 SOP 变量值（JIT 懒加载，T-AGT-25）。

    此函数是变量池的核心入口，实现工作记忆层的 JIT 变量获取。
    LLM 通过 tool_call 调用此工具，引擎根据 acquisition_strategy 决定获取路径。

    流程：
      1. 检查 context_variables 中是否已有值（缓存命中直接返回）
      2. 获取 SOP 文档的 variable_schema，找到变量定义
      3. 根据 acquisition_strategy 决定获取方式：
         - sop_default  : 读取 variable_schema.default_value，立即返回，无 I/O
         - tool_call   : 调用指定工具自动获取值（DC-02），失败降级为 user_input
         - user_confirm: 调用工具获取候选列表，展示给用户确认
         - user_input  : 返回 VariableRequestResult(needs_input=True) 阻塞等待
         - env_injection: 报错（此类变量应在初始化阶段注入，不走 JIT）

    Args:
        variable_name: 变量名（如 vm_name、node_ip）
        reason: 为什么需要此变量（用于向用户解释）
        conversation_id: 会话 ID（由上下文注入）
        sop_document_id: SOP 文档 ID（由上下文注入）
        kb_client: KB 服务客户端（用于获取 variable_schema）
        conversation_sop_client: Conversation SOP API 客户端（用于获取执行状态）
        tool_executor: 工具执行器（用于 tool_call / user_confirm 策略）

    Returns:
        VariableRequestResult(needs_input=True): 需要用户输入，ReactEngine 应阻塞等待
        dict(ok=True, value=...)               : 已有值或已获取到值，直接返回给 LLM
        dict(error="...")                       : 错误信息
    """
    logger.info(
        event="sop_request_variable_start",
        conversation_id=conversation_id,
        sop_document_id=sop_document_id,
        variable_name=variable_name,
        reason=reason,
    )

    # 1. 获取 SOP 执行状态（检查 context_variables）
    if conversation_sop_client is None:
        return {"error": "ConversationSopClient 未注入，无法获取执行状态"}

    execution = await conversation_sop_client.get_execution(uuid.UUID(conversation_id))
    if execution is None:
        return {"error": "SOP 执行实例不存在"}

    context_variables = execution.get("context_variables", {})
    pending_variable = execution.get("pending_variable_name")

    # 缓存命中：已有值直接返回
    if variable_name in context_variables:
        existing_value = context_variables[variable_name]
        value = existing_value.get("value") if isinstance(existing_value, dict) else existing_value

        if value is not None and value != "":
            logger.info(
                event="sop_request_variable_cached",
                conversation_id=conversation_id,
                variable_name=variable_name,
                cached_value=value,
            )
            return {
                "ok": True,
                "value": value,
                "source": "cached",
                "message": f"变量 {variable_name} 已有值：{value}",
            }

    # 防止并发变量请求（同一时刻只允许一个变量等待用户输入）
    if pending_variable and pending_variable != variable_name:
        return {
            "error": f"已有变量 {pending_variable} 正在等待用户输入，请先完成该变量填写后再请求 {variable_name}",
        }

    # 2. 获取 SOP 文档的 variable_schema
    sop_doc = await kb_client.get_sop_document(sop_document_id)
    if sop_doc is None:
        return {"error": f"SOP 文档 {sop_document_id} 不存在"}

    variable_schema_list = sop_doc.get("variable_schema", [])
    if not variable_schema_list:
        # variable_schema 未定义，允许 LLM 自行处理，降级为自由输入
        logger.warning(
            event="sop_request_variable_schema_missing",
            sop_document_id=sop_document_id,
            variable_name=variable_name,
        )
        return VariableRequestResult(
            needs_input=True,
            variable_name=variable_name,
            variable_schema={
                "name": variable_name,
                "display_name": variable_name,
                "description": reason or f"请提供变量 {variable_name} 的值",
                "type": "string",
                "required": True,
            },
            message=f"变量 {variable_name} 需要用户提供值",
            kind="variable_input",
        )

    # 查找变量定义
    var_def = None
    for v in variable_schema_list:
        if v.get("name") == variable_name:
            var_def = v
            break

    if var_def is None:
        # 变量未在 schema 中定义，降级为自由输入
        logger.warning(
            event="sop_request_variable_not_defined",
            sop_document_id=sop_document_id,
            variable_name=variable_name,
        )
        return VariableRequestResult(
            needs_input=True,
            variable_name=variable_name,
            variable_schema={
                "name": variable_name,
                "display_name": variable_name,
                "description": reason or f"请提供变量 {variable_name} 的值",
                "type": "string",
                "required": True,
            },
            message=f"变量 {variable_name} 未在 SOP Schema 中定义，需要用户提供值",
            kind="variable_input",
        )

    # 3. 根据 acquisition_strategy 决定获取方式
    strategy = var_def.get("acquisition_strategy", "user_input")
    acquisition_tool = var_def.get("acquisition_tool")

    logger.info(
        event="sop_request_variable_strategy",
        variable_name=variable_name,
        strategy=strategy,
        acquisition_tool=acquisition_tool,
    )

    if strategy == "env_injection":
        # env_injection 类变量应在初始化阶段批量注入，不走 JIT 流程
        return {
            "error": (
                f"变量 {variable_name} 类型为 env_injection，"
                "应在 SOP 初始化阶段从环境上下文批量注入，无需调用此工具"
            ),
        }

    if strategy == "sop_default":
        # sop_default 类变量：直接读 variable_schema.default_value，无需用户输入或工具调用
        default_val = var_def.get("default_value")
        if default_val is None:
            return {
                "error": (
                    f"变量 {variable_name} 的 acquisition_strategy 为 sop_default，"
                    "但 variable_schema 未定义 default_value"
                ),
            }
        value = str(default_val)
        logger.info(
            event="sop_request_variable_default_resolved",
            variable_name=variable_name,
            value=value,
        )
        return {"ok": True, "value": value, "source": "sop_default"}

    # 辅助：调用 interrupt API 并返回 VariableRequestResult（阻塞等待用户输入）
    async def _request_user_input(
        var_schema: dict,
        kind: str = "variable_input",
        options: list[dict] | None = None,
        msg: str = "",
    ) -> VariableRequestResult:
        """标记执行中断，等待用户提供变量值"""
        if conversation_sop_client:
            try:
                await conversation_sop_client.interrupt(
                    conversation_id=uuid.UUID(conversation_id),
                    pending_variable_name=variable_name,
                )
                logger.info(
                    event="sop_request_variable_interrupt_set",
                    conversation_id=conversation_id,
                    variable_name=variable_name,
                )
            except Exception as exc:
                logger.warning(
                    event="sop_request_variable_interrupt_failed",
                    conversation_id=conversation_id,
                    variable_name=variable_name,
                    error=str(exc),
                )
        return VariableRequestResult(
            needs_input=True,
            variable_name=variable_name,
            variable_schema=var_schema,
            message=msg or f"变量 {variable_name} 需要用户提供值",
            kind=kind,
            options=options or [],
        )

    if strategy == "tool_call" and acquisition_tool:
        # DC-02: tool_call 策略：调用指定工具自动获取变量值
        if tool_executor is not None:
            try:
                tool_result = await tool_executor.execute(acquisition_tool, {})
                # 尝试从结果中提取单一值
                acquired_value = None
                if isinstance(tool_result, dict):
                    acquired_value = tool_result.get("value") or tool_result.get(variable_name)
                elif tool_result is not None and not isinstance(tool_result, (list, dict)):
                    acquired_value = tool_result
                if acquired_value is not None:
                    logger.info(
                        event="sop_request_variable_tool_acquired",
                        variable_name=variable_name,
                        acquisition_tool=acquisition_tool,
                    )
                    return {"ok": True, "value": acquired_value, "source": "tool_call"}
            except Exception as exc:
                logger.warning(
                    event="sop_request_variable_tool_failed",
                    variable_name=variable_name,
                    acquisition_tool=acquisition_tool,
                    error=str(exc),
                )
        else:
            logger.warning(
                event="sop_request_variable_tool_no_executor",
                variable_name=variable_name,
                acquisition_tool=acquisition_tool,
            )
        # 降级：工具执行失败或无执行器，请用户手动输入
        return await _request_user_input(
            var_schema=var_def,
            kind="variable_input",
            msg=f"变量 {variable_name} 自动获取失败，请手动输入",
        )

    if strategy == "user_confirm":
        # user_confirm 策略：先调用工具获取候选值，再展示给用户确认
        options: list[dict] = []
        if acquisition_tool and tool_executor is not None:
            try:
                candidates_result = await tool_executor.execute(acquisition_tool, {})
                if isinstance(candidates_result, list):
                    options = [
                        {"label": str(item), "value": item} for item in candidates_result
                    ]
                elif isinstance(candidates_result, dict) and "items" in candidates_result:
                    options = [
                        {"label": str(item), "value": item}
                        for item in candidates_result["items"]
                    ]
                logger.info(
                    event="sop_request_variable_confirm_candidates",
                    variable_name=variable_name,
                    acquisition_tool=acquisition_tool,
                    candidate_count=len(options),
                )
            except Exception as exc:
                logger.warning(
                    event="sop_request_variable_confirm_fetch_failed",
                    variable_name=variable_name,
                    acquisition_tool=acquisition_tool,
                    error=str(exc),
                )
        # 展示候选值（可能为空）让用户确认
        return await _request_user_input(
            var_schema=var_def,
            kind="variable_confirm",
            options=options,
            msg=f"变量 {variable_name} 需要用户确认",
        )

    # 兼容旧策略名 "tool"（向后兼容，等同于 tool_call）
    if strategy == "tool" and acquisition_tool:
        if tool_executor is not None:
            try:
                tool_result = await tool_executor.execute(acquisition_tool, {})
                acquired_value = None
                if isinstance(tool_result, dict):
                    acquired_value = tool_result.get("value") or tool_result.get(variable_name)
                elif tool_result is not None and not isinstance(tool_result, (list, dict)):
                    acquired_value = tool_result
                if acquired_value is not None:
                    return {"ok": True, "value": acquired_value, "source": "tool_call"}
            except Exception as exc:
                logger.warning(
                    event="sop_request_variable_tool_compat_failed",
                    variable_name=variable_name,
                    acquisition_tool=acquisition_tool,
                    error=str(exc),
                )
        return await _request_user_input(
            var_schema=var_def,
            kind="variable_input",
            msg=f"变量 {variable_name} 自动获取失败，请手动输入",
        )

    # 兼容旧策略名 "env_context"（向后兼容，等同于 env_injection）
    if strategy == "env_context":
        return {
            "error": (
                f"变量 {variable_name} 类型为 env_context/env_injection，"
                "应在 SOP 初始化阶段从环境上下文批量注入，无需调用此工具"
            ),
        }

    # 默认：user_input 策略
    return await _request_user_input(
        var_schema=var_def,
        kind="variable_input",
        msg=f"变量 {variable_name} 需要用户提供值",
    )
