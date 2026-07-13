"""
Vision 异步 Job 管理器 — Asynchronous Request-Reply 模式实现

设计背景：
  VISION 阶段是长耗时任务（每图 ~60s）。之前的同步实现导致 HTTP 请求长时间挂起、
  300s 超时后失败，且多案例串行处理。改为 Asynchronous Request-Reply 模式：
  - POST reanalyze-images 立即返回 202 + job_id，asyncio.create_task 后台执行
  - GET reanalyze-images/status?job_id=xxx 客户端轮询状态

业界范式：
  Microsoft Azure Architecture Guide 的 Asynchronous Request-Reply Pattern：
  https://learn.microsoft.com/en-us/azure/architecture/patterns/async-request-reply

持久化：
  当前实现：进程内 dict（带 asyncio.Lock）。优点：零依赖、简单；
  缺点：kb-service 重启后 job 状态丢失（reanalyze 幂等可重跑）。
  生产化演进路径：可替换为 redis 持久化（dev 环境已有 redis-0）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger("kb-service-vision-job-manager")


class VisionJobManager:
    """Vision 异步 Job 管理器（进程内单例）。"""

    def __init__(self, max_concurrent: int = 10) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent

    async def submit(self, kbd_id: int, runner) -> str:
        """提交 Job，返回 job_id。

        Args:
            kbd_id: kbd_entry.id
            runner: 异步执行函数（接收 kbd_id，返回 dict 结果）

        Returns:
            job_id（短 uuid）
        """
        job_id = uuid.uuid4().hex[:12]
        async with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "kbd_id": kbd_id,
                "status": "pending",
                "total": 0,
                "done": 0,
                "failed": 0,
                "error": None,
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
            }
        # 后台执行（不阻塞 submit）
        asyncio.create_task(self._run(job_id, kbd_id, runner))
        logger.info(event="vision_job_submitted", job_id=job_id, kbd_id=kbd_id)
        return job_id

    async def _run(self, job_id: str, kbd_id: int, runner) -> None:
        """异步执行 Job（应用并发信号量，更新状态）。"""
        async with self._sem:
            await self._update(job_id, status="running", started_at=time.time())
            try:
                # 通过 runner 执行（默认 re-analyze 全部图片）
                result = await runner(kbd_id)
                await self._update(
                    job_id,
                    status="done" if result.get("success", True) else "failed",
                    total=result.get("total", 0),
                    done=result.get("done", 0),
                    failed=result.get("failed", 0),
                    finished_at=time.time(),
                )
                logger.info(
                    event="vision_job_completed",
                    job_id=job_id, kbd_id=kbd_id,
                    status=self._jobs[job_id]["status"],
                    done=result.get("done", 0), failed=result.get("failed", 0),
                )
            except Exception as exc:
                await self._update(
                    job_id,
                    status="failed",
                    error=str(exc),
                    finished_at=time.time(),
                )
                logger.error(
                    event="vision_job_failed",
                    job_id=job_id, kbd_id=kbd_id, error=str(exc),
                    exc_info=True,
                )

    async def _update(self, job_id: str, **fields: Any) -> None:
        """原子更新 Job 状态。"""
        async with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(fields)

    async def get_status(self, job_id: str) -> dict[str, Any] | None:
        """获取 Job 状态（不存在返回 None）。"""
        async with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    async def get_by_kbd_id(self, kbd_id: int) -> dict[str, Any] | None:
        """按 kbd_id 查找最近一个 Job（用于幂等去重）。"""
        async with self._lock:
            for job in reversed(list(self._jobs.values())):
                if job["kbd_id"] == kbd_id and job["status"] in ("pending", "running"):
                    return dict(job)
            return None

    @property
    def active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j["status"] in ("pending", "running"))


# 全局单例（在 app.main 生命周期内复用）
_job_manager: VisionJobManager | None = None


def get_job_manager() -> VisionJobManager:
    """获取 Job 管理器单例（首次调用时初始化）。"""
    global _job_manager
    if _job_manager is None:
        # _VISION_CONCURRENCY 默认 1，提高到 3 避免同 kbd 内串行长耗时
        # 真实并发控制在 vision_processor 内部 Semaphore
        _job_manager = VisionJobManager(max_concurrent=10)
        logger.info(event="vision_job_manager_initialized", max_concurrent=10)
    return _job_manager
