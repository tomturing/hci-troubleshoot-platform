"""日志收集任务数据访问层。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.log_collect import LogCollectTask
from ..schemas.log_collect import ACTIVE_STATUSES


class LogCollectTaskRepository:
    """任务表 CRUD。

    事务边界由 DatabaseManager 的 session 生成器统一提交，本层只 flush。
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, task: LogCollectTask) -> LogCollectTask:
        self.session.add(task)
        await self.session.flush()
        await self.session.refresh(task)
        return task

    async def get_by_id(self, task_id) -> LogCollectTask | None:
        result = await self.session.execute(select(LogCollectTask).where(LogCollectTask.task_id == task_id))
        return result.scalar_one_or_none()

    async def get_active_by_case(self, case_id: str) -> LogCollectTask | None:
        """返回该工单的活跃任务（同一工单最多一个，见部分唯一索引）。"""
        result = await self.session.execute(
            select(LogCollectTask)
            .where(LogCollectTask.case_id == case_id)
            .where(LogCollectTask.status.in_(ACTIVE_STATUSES))
            .order_by(LogCollectTask.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_by_case(
        self,
        case_id: str,
        *,
        created_by: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[LogCollectTask], int]:
        """按工单查询任务。

        ``created_by`` 非空时只返回该身份创建的任务：同一工单号可能被多个客户端
        复用（日志收集入口允许跨客户端指定），任务详情按创建者隔离。
        """
        filters = [LogCollectTask.case_id == case_id]
        if created_by is not None:
            filters.append(LogCollectTask.created_by == created_by)

        total_result = await self.session.execute(select(func.count()).select_from(LogCollectTask).where(*filters))
        total = int(total_result.scalar_one() or 0)
        result = await self.session.execute(
            select(LogCollectTask)
            .where(*filters)
            .order_by(LogCollectTask.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all()), total

    async def update_upload_progress(self, task_id, progress: dict[str, Any]) -> None:
        """把分片上传进度原子合并进 ``detail.upload_progress``。

        为什么用一条原生 SQL 而不是"读出 task → 改 detail → flush"：

        - 分片上传是两个 case-service 副本轮流接的，同一任务的不同分片会并发落库，
          读-改-写必然互相覆盖，最后 detail 里只剩某一片的进度；
        - ``||`` 是库内原子合并，只动一个 key，不会碰 detail 里其它字段
          （例如 ``data_correction`` 审计）。

        只写进度、不参与交付判定：进度是给人看的可观测性数据，
        任何情况下都不该反过来影响上传本身。
        """
        await self.session.execute(
            text(
                "UPDATE log_collect_task "
                "SET detail = coalesce(detail, '{}'::jsonb) "
                "|| jsonb_build_object('upload_progress', cast(:progress AS jsonb)) "
                "WHERE task_id = :task_id"
            ),
            {"task_id": str(task_id), "progress": json.dumps(progress, ensure_ascii=False)},
        )

    async def flush(self) -> None:
        await self.session.flush()

    async def commit(self) -> None:
        """显式提交当前事务。

        默认事务边界由请求收尾统一提交，本层只 flush；但 complete 是例外：
        合并是分钟级纯文件 IO，必须先结束只读事务（30s idle 超时会杀连接），
        落库后又必须立即放行行锁给代传 trigger（否则自死锁）。这两处
        不能等到请求收尾，只能显式提交。
        """
        await self.session.commit()

    async def refresh(self, task: LogCollectTask) -> None:
        await self.session.refresh(task)
