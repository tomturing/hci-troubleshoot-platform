"""
高危操作授权记录服务（T1-1）

提供 Authorization 表的写入与查询能力，配合 ConfirmService 在用户做出 approve/deny
决策时同步落库：每一次 risk_level>=2 工具的人工授权都会在数据库留下不可篡改的
auth_id / exec_id / actor / decision / tool_input_hash / expires_at 记录，
并通过 tool_result.authorization_id 与对应工具执行行关联。

设计原则：
  1. fail-safe：DB 不可用时仅 warning，不阻塞 ConfirmService 主流程（Redis 已确认）；
  2. 审计完整：approve 与 deny 均写入，便于后续追溯"是谁拒绝了什么操作"；
  3. 反查 input_hash：从 tool_result(exec_id) 读取 input_hash，
     避免依赖前端可能丢失/伪造的字段。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from shared.models.audit import Authorization, ToolResult
from shared.observability.logger import get_logger
from sqlalchemy import select

logger = get_logger("authorization-service")

# 授权决策默认有效期：与 ConfirmService 的 120s 等待保持一致
DEFAULT_AUTH_TTL_SECONDS = 120


class AuthorizationService:
    """Authorization 表的写入与关联服务。"""

    def __init__(self, session_factory: Any | None = None) -> None:
        self._session_factory = session_factory

    @property
    def enabled(self) -> bool:
        return self._session_factory is not None

    async def record_decision(
        self,
        *,
        exec_id: str,
        actor: str,
        decision: str,
        tool_input_hash: str | None = None,
        ttl_seconds: int = DEFAULT_AUTH_TTL_SECONDS,
    ) -> str | None:
        """写入一条授权记录，并将 authorization_id 回写到对应 tool_result 行。

        Args:
            exec_id: 工具执行 ID（与 tool_result.id 一致）
            actor: 决策人（用户标识 / client_id）
            decision: "approve" 或 "deny"
            tool_input_hash: 工具参数哈希；若未提供则从 tool_result 反查
            ttl_seconds: 授权决策的有效期秒数

        Returns:
            写入成功时返回 auth_id；DB 不可用 / 写入失败时返回 None。
        """
        if not self.enabled:
            return None
        if decision not in ("approve", "deny"):
            logger.warning(
                event="authorization_invalid_decision",
                message=f"非法的授权决策值: {decision}",
                exec_id=exec_id,
            )
            return None

        auth_id = str(uuid.uuid4())
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

        try:
            assert self._session_factory is not None
            async with self._session_factory() as session:
                # 1) 如未传入 hash，从 tool_result 反查（防伪造）
                if not tool_input_hash:
                    stmt = select(ToolResult).where(ToolResult.id == exec_id)
                    res = await session.execute(stmt)
                    tool_res = res.scalar_one_or_none()
                    if tool_res and tool_res.input_hash:
                        tool_input_hash = tool_res.input_hash

                # tool_input_hash 必填，回退为空哈希以满足 NOT NULL
                effective_hash = tool_input_hash or ("0" * 64)

                # 2) 写 Authorization
                row = Authorization(
                    auth_id=auth_id,
                    exec_id=exec_id,
                    actor=actor,
                    decision=decision,
                    tool_input_hash=effective_hash,
                    expires_at=expires_at,
                )
                session.add(row)
                await session.flush()

                # 3) 将 auth_id 关联到 tool_result（如果存在），形成 1:N 链路
                stmt2 = select(ToolResult).where(ToolResult.id == exec_id)
                res2 = await session.execute(stmt2)
                tool_res2 = res2.scalar_one_or_none()
                if tool_res2 is not None:
                    tool_res2.authorization_id = auth_id
                    if decision == "approve":
                        tool_res2.authorized_by = actor

                await session.commit()

            logger.info(
                event="authorization_recorded",
                message="授权决策已落库",
                auth_id=auth_id,
                exec_id=exec_id,
                actor=actor,
                decision=decision,
            )
            return auth_id
        except Exception as exc:
            logger.warning(
                event="authorization_record_failed",
                message=f"授权决策落库失败（不阻塞主流程）: {exc}",
                exec_id=exec_id,
                actor=actor,
                decision=decision,
            )
            return None
