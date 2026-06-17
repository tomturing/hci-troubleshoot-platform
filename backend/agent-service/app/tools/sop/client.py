"""
ConversationSopClient — Conversation Service 的 SOP 执行状态 HTTP 客户端

负责 agent-service 与 conversation-service 之间 SOP 执行状态的 API 通信：
  - create()    : 创建 SOP 执行实例
  - advance()   : 推进 SOP 决策树节点
  - get_execution(): 获取执行实例详情（用于恢复场景）
  - interrupt() : 标记执行中断等待变量

SOP 执行状态（current_node_id、context_variables、status）存储于 conversation-service，
agent-service 通过此客户端进行读写，实现 Agent 工作记忆的持久化。
"""

from __future__ import annotations

import uuid
from typing import Any

from shared.observability.logger import get_logger

logger = get_logger("tools.sop.client")


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

    async def set_variable(
        self,
        conversation_id: uuid.UUID,
        variable_name: str,
        value: str,
        source: str = "jit_auto_acquire",
    ) -> dict[str, Any]:
        """JIT 自动获取变量后写回 context_variables（DC-07）。

        调用 sop/variable-response 端点，该端点已支持 ACTIVE 状态直写变量。
        """
        import httpx

        url = f"{self._base_url}/api/conversations/{conversation_id}/sop/variable-response"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._internal_token}",
        }
        payload = {
            "variable_name": variable_name,
            "value": str(value),
            "source": source,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code >= 500:
                    return {"error": f"conversation-service 错误: {resp.status_code}"}
                resp.raise_for_status()
                return resp.json()
        except httpx.RequestError as exc:
            logger.error(
                event="sop_set_variable_error",
                conversation_id=str(conversation_id),
                variable_name=variable_name,
                error=str(exc),
            )
            return {"error": f"调用 conversation-service 失败: {exc}"}
