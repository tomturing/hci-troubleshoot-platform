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
from shared.observability.redaction import redact_observation_value

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
        status: str | None = None,
        input_hash: str | None = None,
        authorization_id: str | None = None,
        idempotency_key: str | None = None,
        case_id: str | None = None,
        retry_count: int | None = None,
        exec_id: str | None = None,
        artifact_id: str | None = None,
        output_sha256: str | None = None,
        error_type: str | None = None,
        bridge_trace_id: str | None = None,
    ) -> None:
        """异步地将工具执行审计日志写入或更新到数据库

        Args:
            audit_id: 审计流水号 (UUID 字符串，对应 ToolResult.id)
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
            status: 工具执行状态 (proposed/executing/committed/failed/cancelled等)
            input_hash: 参数哈希值
            authorization_id: 关联授权记录 ID
            idempotency_key: 幂等防重键
            case_id: 关联工单号
        """
        if not trace_id:
            from shared.observability.otel import get_current_trace_id

            trace_id = get_current_trace_id()

        if not cls._session_factory:
            logger.warning("ToolAuditService 未初始化，跳过审计写入")
            return

        try:
            # 校验并解析 UUID，支持派生
            conv_uuid = uuid.UUID(str(session_id)) if not isinstance(session_id, uuid.UUID) else session_id
        except ValueError:
            conv_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, str(session_id))
            logger.info(
                event="tool_audit_uuid_derived",
                message=f"已对非标准 UUID 格式会话ID派生定位: {session_id} -> {conv_uuid}",
            )

        # Bridge 原始输出只保存在受控 Artifact；审计表保存脱敏输入、关联键、hash 和执行摘要。
        safe_tool_args = redact_observation_value(tool_args)
        result_artifact_id = artifact_id or getattr(result, "artifact_id", None)
        output_str = "" if result_artifact_id else (str(result) if result is not None else "")
        if len(output_str) > RESULT_MAX_CHARS:
            output_str = output_str[:RESULT_MAX_CHARS] + "... [已截断]"
        result_metadata = {
            "artifact_id": result_artifact_id,
            "exec_id": getattr(result, "exec_id", None),
            "trace_id": getattr(result, "trace_id", None),
            "exit_code": getattr(result, "exit_code", None),
            "duration_ms": getattr(result, "duration_ms", None),
            "stdout_bytes": getattr(result, "stdout_bytes", None),
            "stderr_bytes": getattr(result, "stderr_bytes", None),
            "stdout_sha256": getattr(result, "stdout_sha256", None),
            "stderr_sha256": getattr(result, "stderr_sha256", None),
            "stdout_truncated": getattr(result, "stdout_truncated", None),
            "stderr_truncated": getattr(result, "stderr_truncated", None),
            "error_type": getattr(result, "error_type", None),
        }
        result_metadata = {key: value for key, value in result_metadata.items() if value is not None}
        output_json = {"data": output_str, **result_metadata} if result is not None else None
        exec_id = exec_id or getattr(result, "exec_id", None) or audit_id
        artifact_id = result_artifact_id
        output_sha256 = output_sha256 or getattr(result, "stdout_sha256", None)
        error_type = error_type or getattr(result, "error_type", None)
        bridge_trace_id = bridge_trace_id or getattr(result, "trace_id", None)

        # 自动推断 tool_type
        tool_type = "scp"
        if tool_name:
            if tool_name.startswith("acli_") or tool_name == "bash_exec":
                tool_type = "acli"
            elif tool_name in ("get_sop_node", "sop_advance", "sop_request_variable"):
                tool_type = "sop"

        try:
            async with cls._session_factory() as session:
                from sqlalchemy import select

                # 基于 exec_id (audit_id) 查询现有记录，实现增量更新状态
                stmt = select(ToolResult).where(ToolResult.id == audit_id)
                res = await session.execute(stmt)
                log = res.scalar_one_or_none()

                if log is None:
                    # 记录不存在，执行 INSERT
                    log = ToolResult(
                        id=audit_id or str(uuid.uuid4()),
                        conversation_id=conv_uuid,
                        tool_name=tool_name,
                        tool_type=tool_type,
                        step_no=step_no,
                        risk_level=risk_level if risk_level is not None else 1,
                        policy=policy,
                        authorized_by=authorized_by,
                        input_json=safe_tool_args or {},
                        output_json=output_json,
                        error=error,
                        started_at=(started_at.astimezone(UTC) if started_at.tzinfo else started_at)
                        if started_at
                        else datetime.now(UTC),
                        completed_at=(completed_at.astimezone(UTC) if completed_at.tzinfo else completed_at)
                        if completed_at
                        else None,
                        duration_ms=duration_ms,
                        trace_id=trace_id,
                        status=status or "committed",
                        input_hash=input_hash,
                        authorization_id=authorization_id,
                        idempotency_key=idempotency_key,
                        case_id=case_id,
                        retry_count=retry_count if retry_count is not None else 0,
                        exec_id=exec_id,
                        artifact_id=uuid.UUID(artifact_id) if artifact_id else None,
                        output_sha256=output_sha256,
                        error_type=error_type,
                        bridge_trace_id=bridge_trace_id,
                    )
                    session.add(log)
                else:
                    # 记录存在，执行 UPDATE，仅更新传入的非空字段
                    if tool_name:
                        log.tool_name = tool_name
                        log.tool_type = tool_type
                    if step_no is not None:
                        log.step_no = step_no
                    if risk_level is not None:
                        log.risk_level = risk_level
                    if policy:
                        log.policy = policy
                    if authorized_by:
                        log.authorized_by = authorized_by
                    if tool_args is not None:
                        log.input_json = safe_tool_args
                    if output_json is not None:
                        log.output_json = output_json
                    if error is not None:
                        log.error = error
                    if started_at:
                        log.started_at = started_at.astimezone(UTC) if started_at.tzinfo else started_at
                    if completed_at:
                        log.completed_at = completed_at.astimezone(UTC) if completed_at.tzinfo else completed_at
                    if duration_ms is not None:
                        log.duration_ms = duration_ms
                    if trace_id:
                        log.trace_id = trace_id
                    if status:
                        log.status = status
                    if input_hash:
                        log.input_hash = input_hash
                    if authorization_id:
                        log.authorization_id = authorization_id
                    if idempotency_key:
                        log.idempotency_key = idempotency_key
                    if case_id:
                        log.case_id = case_id
                    if retry_count is not None:
                        log.retry_count = retry_count
                    if exec_id:
                        log.exec_id = exec_id
                    if artifact_id:
                        log.artifact_id = uuid.UUID(artifact_id)
                    if output_sha256:
                        log.output_sha256 = output_sha256
                    if error_type:
                        log.error_type = error_type
                    if bridge_trace_id:
                        log.bridge_trace_id = bridge_trace_id

                await session.commit()
            logger.info(
                event="agent_tool_audit_success",
                message=f"已成功捕获并记录/更新工具审计: tool={tool_name}, status={status or (log.status if log else 'unknown')}, duration={duration_ms}ms",
                conversation_id=str(conv_uuid),
            )
        except Exception as e:
            # 审计日志落库失败绝不能阻断主流转，静默捕获并记录日志
            logger.error(
                event="agent_tool_audit_error",
                message=f"工具审计写入/更新失败（已自行隔离）: {e}",
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
            status=kwargs.get("status"),
            input_hash=kwargs.get("input_hash"),
            authorization_id=kwargs.get("authorization_id"),
            idempotency_key=kwargs.get("idempotency_key"),
            case_id=kwargs.get("case_id"),
            retry_count=kwargs.get("retry_count"),
            exec_id=kwargs.get("exec_id") or audit_id,
            artifact_id=kwargs.get("artifact_id"),
            output_sha256=kwargs.get("output_sha256"),
            error_type=kwargs.get("error_type"),
            bridge_trace_id=kwargs.get("bridge_trace_id"),
        )
