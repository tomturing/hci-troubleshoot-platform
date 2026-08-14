"""
DiagnosticItem Client - 诊断结论服务客户端
负责调用 conversation-service 进行诊断条目的 CRUD 操作

主要接口：
  - create_item: 创建单个诊断条目
  - batch_create_items: 批量创建诊断条目（S2 假设列表）
  - update_status: 更新条目状态
  - archive_all: 归档会话所有条目
  - get_items: 查询诊断条目

设计依据：
  - docs/task/agent/events/2026-05-26-SOP执行引擎-M1数据库与M2导航工具化.md T-AGT-19
"""

import uuid
from typing import Any

import httpx

from shared.observability.logger import get_logger
from shared.utils.internal_http import InternalHTTPClient

logger = get_logger("diagnostic-item-client")

# 超时配置（诊断条目操作通常是快速数据库操作）
_REQUEST_TIMEOUT = 5.0


class DiagnosticItemClient(InternalHTTPClient):
    """
    诊断结论服务 HTTP 客户端（G-3：继承 InternalHTTPClient，统一认证头管理）

    持有长连接连接池，避免每次请求创建新 AsyncClient。
    调用方应在服务关闭时调用 await client.aclose()。
    """

    def __init__(self, conversation_service_url: str, internal_token: str):
        import os

        # 优先使用传入的 internal_token，注入环境变量供基类读取
        os.environ.setdefault("INTERNAL_API_TOKEN", internal_token)
        if internal_token:
            os.environ["INTERNAL_API_TOKEN"] = internal_token
        super().__init__(base_url=conversation_service_url.rstrip("/"), timeout=_REQUEST_TIMEOUT)
        self._api_prefix = "/api/conversations"

    async def create_item(
        self,
        conversation_id: uuid.UUID,
        stage: str,
        type: str,
        seq: int = 1,
        content: dict[str, Any] | None = None,
        probability: float | None = None,
        status: str = "pending",
    ) -> dict | None:
        """
        创建单个诊断条目

        Args:
            conversation_id: 会话 ID
            stage: 阶段标识（S2/S3/S4/S5）
            type: 类型（hypothesis/verification_step/root_cause/solution）
            seq: 序号（从1开始）
            content: 结构化内容
            probability: 假设概率（仅 hypothesis）
            status: 状态（默认 pending）

        Returns:
            创建结果字典，包含 {ok, id, message}，失败时返回 None
        """
        try:
            resp = await self.post(
                f"{self._api_prefix}/{conversation_id}/diagnostic-items",
                json={
                    "stage": stage,
                    "type": type,
                    "seq": seq,
                    "content": content or {},
                    "probability": probability,
                    "status": status,
                },
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                event="diagnostic_item_create_http_error",
                message=f"Diagnostic item create returned HTTP {exc.response.status_code}",
                conversation_id=str(conversation_id),
                stage=stage,
                type=type,
                status_code=exc.response.status_code,
            )
            return None
        except httpx.RequestError as exc:
            logger.warning(
                event="diagnostic_item_create_unavailable",
                message=f"Conversation service unreachable: {exc}",
                conversation_id=str(conversation_id),
            )
            return None

    async def batch_create_items(
        self,
        conversation_id: uuid.UUID,
        stage: str,
        type: str,
        items_data: list[dict[str, Any]],
    ) -> dict | None:
        """
        批量创建诊断条目（S2 假设列表场景）

        Args:
            conversation_id: 会话 ID
            stage: 阶段标识
            type: 类型
            items_data: 条目数据列表，每个元素包含 {content, probability?, status?}

        Returns:
            创建结果字典，包含 {ok, ids, count, message}，失败时返回 None
        """
        try:
            resp = await self.post(
                f"{self._api_prefix}/{conversation_id}/diagnostic-items",
                json={
                    "stage": stage,
                    "type": type,
                    "items": items_data,
                },
            )
            resp.raise_for_status()
            result = resp.json()
            logger.info(
                event="diagnostic_item_batch_created",
                conversation_id=str(conversation_id),
                count=result.get("count", 0),
            )
            return result
        except httpx.HTTPStatusError as exc:
            logger.warning(
                event="diagnostic_item_batch_create_http_error",
                message=f"Batch create returned HTTP {exc.response.status_code}",
                conversation_id=str(conversation_id),
                count=len(items_data),
                status_code=exc.response.status_code,
            )
            return None
        except httpx.RequestError as exc:
            logger.warning(
                event="diagnostic_item_batch_create_unavailable",
                message=f"Conversation service unreachable: {exc}",
                conversation_id=str(conversation_id),
            )
            return None

    async def update_status(
        self,
        conversation_id: uuid.UUID,
        item_id: uuid.UUID,
        new_status: str,
        content_update: dict[str, Any] | None = None,
    ) -> dict | None:
        """
        更新诊断条目状态

        Args:
            conversation_id: 会话 ID
            item_id: 条目 ID
            new_status: 新状态（in_progress/confirmed/rejected/skipped）
            content_update: 内容更新（可选）

        Returns:
            更新结果字典，失败时返回 None
        """
        try:
            resp = await self.put(
                f"{self._api_prefix}/{conversation_id}/diagnostic-items/{item_id}/status",
                json={
                    "status": new_status,
                    "content_update": content_update,
                },
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                event="diagnostic_item_update_http_error",
                message=f"Status update returned HTTP {exc.response.status_code}",
                item_id=str(item_id),
                status_code=exc.response.status_code,
            )
            return None
        except httpx.RequestError as exc:
            logger.warning(
                event="diagnostic_item_update_unavailable",
                message=f"Conversation service unreachable: {exc}",
                item_id=str(item_id),
            )
            return None

    async def archive_all(
        self,
        conversation_id: uuid.UUID,
    ) -> dict | None:
        """
        归档会话的所有诊断条目（S6 用户选 B 重进 S1）

        Args:
            conversation_id: 会话 ID

        Returns:
            归档结果字典，包含 {ok, count, message}，失败时返回 None
        """
        try:
            resp = await self.put(
                f"{self._api_prefix}/{conversation_id}/diagnostic-items/archive",
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                event="diagnostic_item_archive_http_error",
                message=f"Archive returned HTTP {exc.response.status_code}",
                conversation_id=str(conversation_id),
                status_code=exc.response.status_code,
            )
            return None
        except httpx.RequestError as exc:
            logger.warning(
                event="diagnostic_item_archive_unavailable",
                message=f"Conversation service unreachable: {exc}",
                conversation_id=str(conversation_id),
            )
            return None

    async def get_items(
        self,
        conversation_id: uuid.UUID,
        stage: str | None = None,
        type: str | None = None,
        status: str | None = None,
    ) -> dict | None:
        """
        查询诊断条目

        Args:
            conversation_id: 会话 ID
            stage: 阶段过滤（可选）
            type: 类型过滤（可选）
            status: 状态过滤（可选）

        Returns:
            条目列表字典，包含 {items, total}，失败时返回 None
        """
        try:
            params = {}
            if stage:
                params["stage"] = stage
            if type:
                params["type"] = type
            if status:
                params["status"] = status

            resp = await self.get(
                f"{self._api_prefix}/{conversation_id}/diagnostic-items",
                params=params,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                event="diagnostic_item_get_http_error",
                message=f"Get items returned HTTP {exc.response.status_code}",
                conversation_id=str(conversation_id),
                status_code=exc.response.status_code,
            )
            return None
        except httpx.RequestError as exc:
            logger.warning(
                event="diagnostic_item_get_unavailable",
                message=f"Conversation service unreachable: {exc}",
                conversation_id=str(conversation_id),
            )
            return None
