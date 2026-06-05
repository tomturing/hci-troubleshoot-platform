"""
工具调用审计服务 (下沉至 agent-service)

用于将 ReAct 执行器中的每一次工具执行信息（输入、输出、状态、时间等）异步持久化到 PostgreSQL 数据库的 tool_result 表中。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from shared.models.audit import ToolResult
from shared.observability.logger import get_logger

logger = get_logger("tool-audit-service")

# 限制输出结果的最大字符数，防止超大返回值撑爆数据库
RESULT_MAX_CHARS = 2000


class ToolAuditService:
    """真正的工具调用审计日志服务"""

    _session_factory: Any = None

    @classmethod
    def initialize(cls, session_factory: Any) -> None:
        """初始化注入数据库异步 session_factory"""
        cls._session_factory = session_factory
        logger.info("ToolAuditService 初始化成功")

    @classmethod
    async def write_tool_audit(
        cls,
        audit_id: str,
        session_id: str | uuid.UUID,
        tool_name: str,
        tool_args: dict,
        risk_level: int,
        policy: str,
        result: Any,
        error: str | None,
        started_at: datetime,
        completed_at: datetime,
        duration_ms: int,
        authorized_by: str | None = None,
        trace_id: str | None = None,
        step_no: int | None = None,
    ) -> None:
        """异步非阻塞地将工具执行审计日志写入数据库

        Args:
            audit_id: 审计流水号 (UUID 字符串)
            session_id: 会话 ID
            tool_name: 工具名称
            tool_args: 工具执行输入参数
            risk_level: 风险级别 (1=只读 2=写 3=高危)
            policy: 执行策略 (auto/notify/confirm/block)
            result: 工具执行结果
            error: 异常报错信息
            started_at: 开始执行时间
            completed_at: 结束执行时间
            duration_ms: 执行耗时 (毫秒)
            authorized_by: 确认授权人 (仅 policy=confirm 时有效)
            trace_id: 链路追踪 ID
            step_no: 诊断步骤序列号 (SOP 模式下为节点顺序)
        """
        if not cls._session_factory:
            logger.warning("ToolAuditService 未初始化，跳过审计写入")
            return

        try:
            # 校验并解析 UUID，支持派生
            conv_uuid = (
                uuid.UUID(str(session_id)) if not isinstance(session_id, uuid.UUID) else session_id
            )
        except ValueError:
            conv_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, str(session_id))
            logger.info(
                event="tool_audit_uuid_derived",
                message=f"已对非标准 UUID 格式会话ID派生定位: {session_id} -> {conv_uuid}",
            )

        # 格式化输出数据并截断
        output_str = str(result) if result is not None else ""
        if len(output_str) > RESULT_MAX_CHARS:
            output_str = output_str[:RESULT_MAX_CHARS] + "... [已截断]"
        output_json = {"data": output_str}

        # 自动推断 tool_type
        if tool_name.startswith("acli_") or tool_name == "bash_exec":
            tool_type = "acli"
        elif tool_name in ("get_sop_node", "sop_advance", "sop_request_variable"):
            tool_type = "sop"
        else:
            tool_type = "scp"

        try:
            async with cls._session_factory() as session:
                log = ToolResult(
                    id=audit_id or str(uuid.uuid4()),
                    conversation_id=conv_uuid,
                    tool_name=tool_name,
                    tool_type=tool_type,
                    step_no=step_no,
                    risk_level=risk_level,
                    policy=policy,
                    authorized_by=authorized_by,
                    input_json=tool_args or {},
                    output_json=output_json,
                    error=error,
                    started_at=started_at.astimezone(UTC) if started_at.tzinfo else started_at,
                    completed_at=completed_at.astimezone(UTC) if completed_at.tzinfo else completed_at,
                    duration_ms=duration_ms,
                    trace_id=trace_id,
                )
                session.add(log)
                await session.commit()
            logger.info(
                event="agent_tool_audit_success",
                message=f"已成功捕获并记录工具审计: tool={tool_name}, duration={duration_ms}ms",
                conversation_id=str(conv_uuid),
            )
        except Exception as e:
            # 审计日志落库失败绝不能阻断主流转，静默捕获并记录日志
            logger.error(
                event="agent_tool_audit_error",
                message=f"工具审计写入失败（已自行隔离）: {e}",
                conversation_id=str(conv_uuid),
                exc_info=True,
            )


class DbAuditService:
    """符合 ReactEngine AuditServiceProtocol 要求的数据库审计服务适配器"""

    async def write(self, audit_id: str, **kwargs) -> None:
        """审计记录写入代理方法"""
        await ToolAuditService.write_tool_audit(
            audit_id=audit_id,
            session_id=kwargs.get("session_id"),
            tool_name=kwargs.get("tool_name"),
            tool_args=kwargs.get("tool_args"),
            risk_level=kwargs.get("risk_level"),
            policy=kwargs.get("policy"),
            result=kwargs.get("result"),
            error=kwargs.get("error"),
            started_at=kwargs.get("started_at"),
            completed_at=kwargs.get("completed_at"),
            duration_ms=kwargs.get("duration_ms"),
            authorized_by=kwargs.get("authorized_by"),
            trace_id=kwargs.get("trace_id"),
            step_no=kwargs.get("step"),
        )
