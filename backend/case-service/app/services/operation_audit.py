"""操作审计（operation_audit）写入。

只回答一个问题：**谁在什么时候、用什么参数、对哪个资源做了什么**。

日志收集最需要它，因为"用户选的是 P1 还是 P2"此前没有任何请求级记录：
任务表存的是服务端归一化后的结果，bridge 事件里只有拼好的命令行，
事后无法证明"当时的选择"（2026-10-28 现场 Q2026102800331 审计 0 行，
只能从 `-u` 是否存在反推）。

约束：
1. 审计失败只记日志，绝不向上抛——不能让它掩盖业务结果；
2. details 只放可核对的短字段，超限时保留最小集合；
3. trace_id 与任务一致，才能把「请求 → 任务 → bridge 事件」串起来。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

_DETAILS_MAX_BYTES = 8192


def _shrink_details(details: dict[str, Any]) -> str:
    """序列化 details，超限时丢弃 probe 明细（保留"曾经有探测"这一事实）。"""
    payload = json.dumps(details, ensure_ascii=False, default=str)
    if len(payload.encode("utf-8")) <= _DETAILS_MAX_BYTES:
        return payload
    minimal = {key: value for key, value in details.items() if key != "probe"}
    minimal["probe"] = {"truncated": True}
    return json.dumps(minimal, ensure_ascii=False, default=str)


async def write_operation_audit(
    session_factory: Callable[[], Any],
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any],
    trace_id: str,
    result: str = "success",
) -> None:
    """写一条操作审计；任何异常都在内部消化。"""
    try:
        async with session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO operation_audit "
                    "(action, resource_type, resource_id, result, details, trace_id) "
                    "VALUES (:action, :resource_type, :resource_id, :result, "
                    "CAST(:details AS jsonb), :trace_id)"
                ),
                {
                    "action": action,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "result": result,
                    "details": _shrink_details(details),
                    "trace_id": trace_id or "unknown",
                },
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - 审计是旁路，不得影响主流程
        logger.warning(
            "operation_audit 写入失败 action=%s resource=%s error=%s",
            action,
            resource_id,
            exc,
        )
