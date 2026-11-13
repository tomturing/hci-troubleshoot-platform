"""工单数据访问判定（跨服务单一判定点）。

背景（2026-11-13，Q2026111301229）：
日志收集允许用外部工单号补采，采集本身不校验归属；但桥日志回采（conversation-service）
与终端录像（api-gateway）此前只用 ``case.client_id == 当前身份`` 一条口径。于是同一个
人换了浏览器/清了 Cookie 后，"能采不能传"——采集成功、回采与回放必然 403，而工单本身
是日志入口自建的壳工单（零业务数据），没有任何越权风险。

统一口径（两条放行路径，其余一律拒绝）：
  1. ``owned``：工单归属当前身份（既有 BOLA 口径，不变）；
  2. ``task_granted``：工单是日志收集壳工单（``metadata.source == 'log_collect'``
     且零会话零消息），且当前身份在该工单下发起过采集任务。

为什么第二条必须限定"壳工单"：真实业务工单里有别人的聊天与证据，若仅凭"输入过工单号
并采集过"就放行，等于把 BOLA 从"工单号猜中即可读"降级为"工单号猜中 + 点一次采集"。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

SHELL_CASE_SOURCE = "log_collect"

_ACCESS_SQL = text(
    "SELECT c.client_id,"
    "       COALESCE(c.metadata ->> 'source', '') AS source,"
    "       (SELECT count(*) FROM conversation v WHERE v.case_id = c.case_id) AS conversations,"
    "       (SELECT count(*) FROM message m JOIN conversation v"
    "          ON v.conversation_id = m.conversation_id WHERE v.case_id = c.case_id) AS messages,"
    "       EXISTS (SELECT 1 FROM log_collect_task t"
    "               WHERE t.case_id = c.case_id AND t.created_by = :client_id) AS own_task"
    '  FROM "case" c WHERE c.case_id = :case_id'
)


async def resolve_data_access(session: Any, case_id: str, client_id: str) -> dict[str, Any]:
    """返回当前身份对某工单数据的访问判定。

    字段：``exists`` / ``owned`` / ``shell_case`` / ``task_granted`` / ``data_access``。
    调用方不应直接把结果原样返回给前端（``shell_case`` 等属于内部信号）。
    """
    result = await session.execute(_ACCESS_SQL, {"case_id": case_id, "client_id": client_id})
    row = result.mappings().first()
    if row is None:
        return {
            "exists": False,
            "owned": False,
            "shell_case": False,
            "task_granted": False,
            "data_access": False,
        }

    owned = row["client_id"] == client_id
    shell_case = row["source"] == SHELL_CASE_SOURCE and row["conversations"] == 0 and row["messages"] == 0
    task_granted = shell_case and bool(row["own_task"])
    return {
        "exists": True,
        "owned": owned,
        "shell_case": shell_case,
        "task_granted": task_granted,
        "data_access": owned or task_granted,
    }
