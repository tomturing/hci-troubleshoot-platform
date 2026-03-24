"""
工具调用审计日志服务——写 tool_audit_log 表

设计约束：
  1. write() 在 ReactExecutor 的 finally 块中调用
  2. 内部所有异常必须 try/except 捕获，不可向上抛出（防止审计失败掩盖工具执行结果）
  3. result 字段截断到 2000 字符（防止大响应撑爆 DB）
  4. 审计记录只增不删（无 DELETE 接口）
"""

import logging
from datetime import datetime

from shared.models.audit import ToolAuditLog
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# result 字段最大字符数（防止超大响应占满数据库）
RESULT_MAX_CHARS = 2000


class AuditService:
    """工具调用审计日志服务（强制不可绕过）"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def write(
        self,
        audit_id: str,
        session_id: str,
        tool_name: str,
        tool_args: dict,
        risk_level: int,
        policy: str,
        result,
        error: str | None,
        started_at: datetime,
        completed_at: datetime,
        duration_ms: int,
        authorized_by: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        """
        写入工具调用审计记录。

        此方法由 ReactExecutor.finally 块调用，内部所有异常必须捕获并记录，
        不可向上抛出（否则会掩盖工具执行的真实结果）。
        """
        try:
            # result 截断到 RESULT_MAX_CHARS 字符，防止超大响应
            result_data: dict | None = None
            if result is not None:
                raw_str = str(result)
                result_data = {"data": raw_str[:RESULT_MAX_CHARS]}

            log = ToolAuditLog(
                id=audit_id,
                session_id=session_id,
                tool_name=tool_name,
                tool_args=tool_args,
                risk_level=risk_level,
                policy=policy,
                result=result_data,
                error=error,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration_ms,
                authorized_by=authorized_by,
                trace_id=trace_id,
            )
            self.db.add(log)
            await self.db.commit()
        except Exception as e:
            # 审计写入失败：记录日志但不阻断调用方（ReactExecutor finally 块）
            logger.error(
                f"审计日志写入失败 [session={session_id} tool={tool_name}]: {e}",
                exc_info=True,
            )
