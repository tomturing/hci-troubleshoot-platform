"""
SOP 导航工具集 — 提供 SOP 决策树遍历能力

工具：
  - get_sop_node(node_id): 获取节点内容 + 子节点列表（T-AGT-20）
  - sop_advance(target_node_id, reasoning): 推进到子节点（T-AGT-21）

设计依据：
  - docs/solution/agent/agent设计.md §12.6 推荐方案③（导航工具化）
  - docs/task/agent/events/2026-05-26-SOP执行引擎-M1数据库与M2导航工具化.md

使用场景：
  SOP 命中后，ReactEngine 动态注入此工具，LLM 按需获取 SOP 节点内容，
  无需服务端预注入完整 SOP 文档，实现上下文可控的决策树遍历。
"""

from __future__ import annotations

import uuid
from typing import Any

from shared.clients import KBClient
from shared.observability.logger import get_logger

logger = get_logger("sop-tools")


async def get_sop_node(
    node_id: str,
    *,
    sop_document_id: int,
    kb_client: KBClient,
) -> dict[str, Any]:
    """
    获取 SOP 决策树节点内容。

    Args:
        node_id: 节点 ID，如 "n-1"、"n-3-2"（由 LLM 传入）
        sop_document_id: SOP 文档 ID（由 SOP 命中时注入，非 LLM 传入）
        kb_client: KB 服务客户端（由上下文注入）

    Returns:
        节点内容字典，格式：
        {
          "node_id": "n-3",
          "type": "branch",           // branch | diagnosis | solution
          "title": "存储 I/O 故障诊断",
          "content": "检查磁盘错误率...",
          "commands": ["acli host.disk.stat"],
          "children": [
            {"node_id": "n-3-1", "title": "磁盘错误率 < 5%"},
            {"node_id": "n-3-2", "title": "磁盘错误率 > 5%"}
          ]
        }

    Raises:
        返回错误信息字典（而非抛异常），格式：
        {"error": "节点 n-999 不存在", "node_id": "n-999"}
    """
    # 1. 获取完整决策树
    tree_json = await kb_client.get_sop_tree(sop_document_id)
    if tree_json is None:
        logger.warning(
            event="sop_tree_not_found",
            sop_document_id=sop_document_id,
            node_id=node_id,
        )
        return {
            "error": f"SOP 文档 {sop_document_id} 的决策树不存在或未发布",
            "sop_document_id": sop_document_id,
        }

    # 2. 递归查找目标节点（使用 SOPNode.find_node 方法）
    # tree_json 是 SOPNode.model_dump() 格式，需要模拟 find_node 行为
    target_node = _find_node_in_tree(tree_json, node_id)
    if target_node is None:
        logger.warning(
            event="sop_node_not_found",
            sop_document_id=sop_document_id,
            node_id=node_id,
        )
        return {
            "error": f"节点 {node_id} 不存在于 SOP 决策树中",
            "node_id": node_id,
        }

    # 3. 构建返回结果
    result = _build_node_response(target_node)

    logger.info(
        event="sop_node_retrieved",
        sop_document_id=sop_document_id,
        node_id=node_id,
        node_type=result.get("type"),
        children_count=len(result.get("children", [])),
    )

    return result


def _find_node_in_tree(tree_json: dict, node_id: str) -> dict | None:
    """递归查找节点（模拟 SOPNode.find_node 方法）。

    Args:
        tree_json: SOPNode.model_dump() 格式的 JSON
        node_id: 目标节点 ID

    Returns:
        找到的节点 dict，未找到返回 None
    """
    if tree_json.get("node_id") == node_id:
        return tree_json

    for child in tree_json.get("children", []):
        found = _find_node_in_tree(child, node_id)
        if found:
            return found

    return None


def _build_node_response(node: dict) -> dict[str, Any]:
    """构建节点响应（符合任务文档规格）。

    Args:
        node: SOPNode.model_dump() 格式的节点 dict

    Returns:
        工具返回格式：
        - branch（中间节点）：返回 title + prerequisites + children 概览
        - diagnosis：返回判断方法详情（叶节点 diagnosis 字段）
        - solution：返回解决方案详情（叶节点 solution 字段）
    """
    children = node.get("children", [])
    is_leaf = not children

    # 提取子节点概览（仅 node_id + title）
    children_summary = [
        {"node_id": child.get("node_id", ""), "title": child.get("name", "")}
        for child in children
    ]

    # 确定节点类型和内容
    if is_leaf:
        # 叶节点 ：优先返回诊断信息（后续可能需要 sop_advance 到 solution）
        diagnosis = node.get("diagnosis")
        solution = node.get("solution")

        if diagnosis:
            # 返回诊断内容
            return {
                "node_id": node.get("node_id", ""),
                "type": "diagnosis",
                "title": node.get("name", ""),
                "content": _format_diagnosis_content(diagnosis),
                "commands": diagnosis.get("acli_methods", []),
                "children": [],  # 叶节点无子节点
                "has_solution": solution is not None,
            }
        elif solution:
            # 无诊断但有解决方案（异常情况，但需处理）
            return {
                "node_id": node.get("node_id", ""),
                "type": "solution",
                "title": node.get("name", ""),
                "content": _format_solution_content(solution),
                "commands": [],
                "children": [],
            }
        else:
            # 叶节点但无 diagnosis/solution（异常）
            return {
                "node_id": node.get("node_id", ""),
                "type": "leaf",
                "title": node.get("name", ""),
                "content": "（此叶节点缺少诊断和解决方案内容）",
                "commands": [],
                "children": [],
            }
    else:
        # 中间节点（分支节点）
        prerequisites = node.get("prerequisites", [])
        content_parts = []
        if prerequisites:
            content_parts.append("【进入条件】")
            content_parts.extend(f"- {p}" for p in prerequisites)

        return {
            "node_id": node.get("node_id", ""),
            "type": "branch",
            "title": node.get("name", ""),
            "content": "\n".join(content_parts) if content_parts else "",
            "commands": [],
            "children": children_summary,
        }


def _format_diagnosis_content(diagnosis: dict) -> str:
    """格式化诊断内容为可读文本。"""
    parts = []

    # 前置检查
    prerequisites = diagnosis.get("prerequisites", [])
    if prerequisites:
        parts.append("【前置检查】")
        parts.extend(f"- {p}" for p in prerequisites)

    # 页面判断方法
    page_methods = diagnosis.get("page_methods", [])
    if page_methods:
        parts.append("【页面判断方法】")
        parts.extend(f"- {p}" for p in page_methods)

    # 说明
    description = diagnosis.get("description")
    if description:
        parts.append(f"【说明】{description}")

    # 根因
    root_cause = diagnosis.get("root_cause")
    if root_cause:
        parts.append(f"【根因】{root_cause}")

    # 注意事项
    notes = diagnosis.get("notes")
    if notes:
        parts.append(f"【注意事项】{notes}")

    return "\n".join(parts)


def _format_solution_content(solution: dict) -> str:
    """格式化解决方案内容为可读文本。"""
    parts = []

    # 快速恢复
    quick_recovery = solution.get("quick_recovery", [])
    if quick_recovery:
        parts.append("【快速恢复】")
        parts.extend(f"- {s}" for s in quick_recovery)

    # 彻底解决
    thorough_fix = solution.get("thorough_fix", [])
    if thorough_fix:
        parts.append("【彻底解决】")
        parts.extend(f"- {s}" for s in thorough_fix)

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# T-AGT-21: sop_advance 工具
# ─────────────────────────────────────────────────────────────────────────────


class ConversationSopClient:
    """Conversation Service SOP API 客户端（用于 SOP 执行状态管理）

    SOP 执行状态表在 conversation-service 管理，
    agent-service 需通过 HTTP API 调用 conversation-service 来管理执行状态。
    """

    def __init__(self, base_url: str, internal_token: str):
        self._base_url = base_url.rstrip("/")
        self._internal_token = internal_token

    async def create(
        self,
        conversation_id: uuid.UUID,
        sop_document_id: int,
        root_node_id: str = "n-1",
    ) -> dict[str, Any]:
        """创建 SOP 执行实例（T-AGT-22）。

        Args:
            conversation_id: 会话 ID
            sop_document_id: SOP 文档 ID
            root_node_id: 根节点 ID（默认 n-1）

        Returns:
            {
                "ok": true,
                "conversation_id": "...",
                "sop_document_id": 123,
                "current_node_id": "n-1",
                "status": "active",
                "message": "..."
            }
            或 {"error": "..."}
        """
        import httpx

        url = f"{self._base_url}/api/conversations/{conversation_id}/sop/create"
        headers = {
            "Authorization": f"Bearer {self._internal_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "sop_document_id": sop_document_id,
            "root_node_id": root_node_id,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 401:
                    return {"error": "内部服务 Token 无效"}
                if resp.status_code >= 500:
                    return {"error": f"conversation-service 错误: {resp.status_code}"}
                resp.raise_for_status()
                return resp.json()
        except httpx.RequestError as exc:
            logger.error(
                event="sop_create_request_error",
                conversation_id=str(conversation_id),
                sop_document_id=sop_document_id,
                error=str(exc),
            )
            return {"error": f"调用 conversation-service 失败: {exc}"}

    async def advance(
        self,
        conversation_id: uuid.UUID,
        target_node_id: str,
        reasoning: str,
        node_type: str | None = None,
        variables_extracted: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用 conversation-service SOP 推进 API。

        Args:
            conversation_id: 会话 ID
            target_node_id: 目标节点 ID
            reasoning: LLM 推进理由
            node_type: 目标节点类型（用于判断叶节点）
            variables_extracted: 变量池更新（可选）

        Returns:
            {"ok": true, "current_node_id": "...", "node_type": "...", "message": "..."}
            或 {"error": "..."}
        """
        import httpx

        url = f"{self._base_url}/api/conversations/{conversation_id}/sop/advance"
        headers = {
            "Authorization": f"Bearer {self._internal_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "target_node_id": target_node_id,
            "reasoning": reasoning,
            "node_type": node_type,
            "variables_extracted": variables_extracted,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 404:
                    return {"error": "SOP 执行实例不存在或已结束"}
                if resp.status_code >= 500:
                    return {"error": f"conversation-service 错误: {resp.status_code}"}
                resp.raise_for_status()
                return resp.json()
        except httpx.RequestError as exc:
            logger.error(
                event="sop_advance_request_error",
                conversation_id=str(conversation_id),
                error=str(exc),
            )
            return {"error": f"调用 conversation-service 失败: {exc}"}

    async def get_execution(self, conversation_id: uuid.UUID) -> dict[str, Any] | None:
        """获取 SOP 执行实例详情（用于恢复场景）。

        Args:
            conversation_id: 会话 ID

        Returns:
            执行实例详情字典，不存在时返回 None
        """
        import httpx

        url = f"{self._base_url}/api/conversations/{conversation_id}/sop/execution"
        headers = {
            "Authorization": f"Bearer {self._internal_token}",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json()
        except httpx.RequestError as exc:
            logger.error(
                event="sop_get_execution_error",
                conversation_id=str(conversation_id),
                error=str(exc),
            )
            return None

    async def interrupt(
        self,
        conversation_id: uuid.UUID,
        pending_variable_name: str,
    ) -> dict[str, Any]:
        """标记 SOP 执行中断等待变量（T-AGT-25）。

        Args:
            conversation_id: 会话 ID
            pending_variable_name: 待填变量名

        Returns:
            {"ok": true, "status": "interrupted"}
            或 {"error": "..."}
        """
        import httpx

        url = f"{self._base_url}/api/conversations/{conversation_id}/sop/interrupt"
        headers = {
            "Authorization": f"Bearer {self._internal_token}",
            "Content-Type": "application/json",
        }
        payload = {"pending_variable_name": pending_variable_name}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 404:
                    return {"error": "SOP 执行实例不存在或已结束"}
                if resp.status_code >= 500:
                    return {"error": f"conversation-service 错误: {resp.status_code}"}
                resp.raise_for_status()
                return resp.json()
        except httpx.RequestError as exc:
            logger.error(
                event="sop_interrupt_request_error",
                conversation_id=str(conversation_id),
                error=str(exc),
            )
            return {"error": f"调用 conversation-service 失败: {exc}"}


async def sop_advance(
    target_node_id: str,
    reasoning: str,
    *,
    conversation_id: str,
    sop_document_id: int,
    kb_client: KBClient,
    conversation_sop_client: ConversationSopClient | None = None,
    node_type: str | None = None,
    variables_extracted: dict[str, Any] | None = None,
    completed_steps: list[str] | None = None,  # T-AGT-23: 已完成节点列表（幂等性检查）
) -> dict[str, Any]:
    """推进 SOP 到指定子节点，记录推理路径（T-AGT-21 + T-AGT-23）。

    Args:
        target_node_id: 目标子节点 ID（由 LLM 决策后传入）
        reasoning: LLM 推进理由（写入 execution_log）
        conversation_id: 会话 ID（由上下文注入）
        sop_document_id: SOP 文档 ID（由 SOP 命中时注入）
        kb_client: KB 服务客户端（用于验证节点合法性）
        conversation_sop_client: Conversation SOP API 客户端（用于更新执行状态）
        node_type: 目标节点类型（可选，用于判断叶节点）
        variables_extracted: 变量池更新（可选，M3 变量池功能）
        completed_steps: 已完成节点列表（T-AGT-23，用于幂等性检查）
        conversation_sop_client: Conversation SOP API 客户端（用于更新执行状态）
        node_type: 目标节点类型（可选，用于判断叶节点）
        variables_extracted: 变量池更新（可选，M3 变量池功能）

    Returns:
        {
            "ok": true,
            "current_node_id": "n-3-2",
            "node_type": "diagnosis",
            "message": "已推进到：磁盘错误率 > 5% 分支",
            "is_leaf": false
        }
        或 {"error": "..."}
    """
    try:
        # T-AGT-23: 幂等性检查 - 目标节点已在 completed_steps 中时跳过推进
        if completed_steps and target_node_id in completed_steps:
            logger.info(
                event="sop_advance_idempotent_skip",
                conversation_id=conversation_id,
                sop_document_id=sop_document_id,
                target_node_id=target_node_id,
                completed_steps=completed_steps,
            )
            return {
                "ok": True,
                "current_node_id": target_node_id,
                "node_type": node_type or "already_completed",
                "message": f"节点 {target_node_id} 已完成，跳过重复推进",
                "is_leaf": True,
                "skipped": True,  # 标记为跳过
            }

        # 1. 获取 SOP 决策树，验证目标节点存在
        tree_data = await kb_client.get_sop_tree(sop_document_id)
        if tree_data is None:
            return {"error": f"SOP 文档 {sop_document_id} 不存在"}

        tree_json = tree_data.get("tree")
        if tree_json is None:
            return {"error": f"SOP 文档 {sop_document_id} 决策树未生成"}

        # 查找目标节点
        target_node = _find_node_in_tree(tree_json, target_node_id)
        if target_node is None:
            return {"error": f"目标节点 {target_node_id} 不存在于决策树中"}

        # 确定节点类型（用于判断是否叶节点）
        children = target_node.get("children", [])
        is_leaf_node = not children
        actual_node_type = node_type
        if actual_node_type is None:
            # 根据节点内容推断类型
            if is_leaf_node:
                if target_node.get("solution"):
                    actual_node_type = "solution"
                elif target_node.get("diagnosis"):
                    actual_node_type = "diagnosis"
                else:
                    actual_node_type = "leaf"
            else:
                actual_node_type = "branch"

        node_title = target_node.get("name", "")

        # 2. 调用 conversation-service 更新执行状态
        if conversation_sop_client is None:
            return {"error": "ConversationSopClient 未注入，无法推进 SOP 执行"}

        result = await conversation_sop_client.advance(
            conversation_id=uuid.UUID(conversation_id),
            target_node_id=target_node_id,
            reasoning=reasoning,
            node_type=actual_node_type,
            variables_extracted=variables_extracted,
        )

        if "error" in result:
            return result

        # 3. 构造返回消息
        logger.info(
            event="sop_advance_success",
            conversation_id=conversation_id,
            sop_document_id=sop_document_id,
            target_node_id=target_node_id,
            node_type=actual_node_type,
            is_leaf=is_leaf_node,
            reasoning=reasoning[:100],
        )

        return {
            "ok": True,
            "current_node_id": target_node_id,
            "node_type": actual_node_type,
            "message": f"已推进到：{node_title}",
            "is_leaf": is_leaf_node,
        }

    except Exception as exc:
        logger.error(
            event="sop_advance_error",
            conversation_id=conversation_id,
            sop_document_id=sop_document_id,
            target_node_id=target_node_id,
            error=str(exc),
        )
        return {"error": f"推进 SOP 失败: {exc}"}


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
                "reason": (
                    f"SOP 恢复模式：工具 {tool_name} 已在先前节点中执行，"
                    "跳过重复执行以保证幂等性"
                ),
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


# ─────────────────────────────────────────────────────────────────────────────
# T-AGT-25: sop_request_variable 工具（JIT 变量获取）
# ─────────────────────────────────────────────────────────────────────────────


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
    conversation_sop_client: ConversationSopClient | None = None,
    tool_executor: Any | None = None,  # DC-02: 用于 strategy="tool" 自动调用工具获取变量值
) -> VariableRequestResult | dict[str, Any]:
    """请求获取 SOP 变量值（JIT 懒加载，T-AGT-25）。

    流程：
      1. 检查 context_variables 中是否已有值，有则直接返回缓存值
      2. 获取 SOP 文档的 variable_schema，找到变量定义
      3. 根据 acquisition_strategy 决定获取方式：
         - user_input：返回 VariableRequestResult(needs_input=True)
         - user_confirm：返回 VariableRequestResult(needs_input=True, kind="variable_confirm")
         - tool：调用指定工具获取值（暂不实现，返回提示信息）
         - env_context：应直接从 env_context 取值，不应调用此工具

    Args:
        variable_name: 变量名（如 vm_name、node_ip）
        reason: 为什么需要此变量（用于向用户解释）
        conversation_id: 会话 ID（由上下文注入）
        sop_document_id: SOP 文档 ID（由上下文注入）
        kb_client: KB 服务客户端（用于获取 variable_schema）
        conversation_sop_client: Conversation SOP API 客户端（用于获取执行状态）

    Returns:
        VariableRequestResult(needs_input=True)：需要用户输入，ReactEngine 应阻塞等待
        dict(ok=True, value=...)：已有值或已获取到值，直接返回给 LLM
        dict(error="...")：错误信息
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

    # 检查是否已有值
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

    # 检查是否已有等待中的变量（防止并发请求）
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
        # variable_schema 未定义，允许 LLM 自行处理
        logger.warning(
            event="sop_request_variable_schema_missing",
            sop_document_id=sop_document_id,
            variable_name=variable_name,
        )
        # 返回需要输入，但无 validation_pattern
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
        # 变量未在 schema 中定义，允许自由输入
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

    if strategy == "env_context":
        # env_context 类变量应直接从环境上下文取值，不应调用此工具
        return {
            "error": f"变量 {variable_name} 类型为 env_context，应直接从环境上下文取值，无需调用此工具",
        }

    # 定义辅助函数：调用 interrupt API 并返回 VariableRequestResult
    async def _request_user_input(
        var_schema: dict,
        kind: str = "variable_input",
        options: list[dict] | None = None,
        msg: str = "",
    ) -> VariableRequestResult:
        """调用 interrupt API 设置 pending_variable_name，然后返回 VariableRequestResult"""
        # 调用 interrupt API（通过 ConversationSopClient）
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

    if strategy == "tool" and acquisition_tool:
        # DC-02: tool 类型：调用指定工具自动获取变量值
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
                    return {"ok": True, "value": acquired_value, "source": "tool"}
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
        # DC-02: user_confirm 类型：先调用工具获取候选值，再展示给用户确认
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

    # 默认：user_input 类型
    return await _request_user_input(
        var_schema=var_def,
        kind="variable_input",
        msg=f"变量 {variable_name} 需要用户提供值",
    )
