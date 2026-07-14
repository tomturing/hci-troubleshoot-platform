"""
Signal 异步 Job 管理器 — Asynchronous Request-Reply 模式实现

设计背景：
  EXTRACT_SIGNALS 阶段是长耗时任务（每次 LLM 抽取 ~30-60s）。之前的同步实现导致
  HTTP 请求长时间挂起、网关超时后失败。改为 Asynchronous Request-Reply 模式：
  - POST extract-signals 立即返回 202 + job_id，asyncio.create_task 后台执行
  - GET extract-signals/status?job_id=xxx 客户端轮询状态

  与 VISION 的 vision_job_manager 完全对齐（同一范式、同一套客户端轮询逻辑），
  保证「信号抽取 / 分类 / 识图」三种触发在 data-pipeline 与 admin-ui 两侧行为一致。

业界范式：
  Microsoft Azure Architecture Guide 的 Asynchronous Request-Reply Pattern：
  https://learn.microsoft.com/en-us/azure/architecture/patterns/async-request-reply

持久化：
  当前实现：进程内 dict（带 asyncio.Lock）。优点：零依赖、简单；
  缺点：kb-service 重启后 job 状态丢失（extract 幂等可重跑）。
  生产化演进路径：可替换为 redis 持久化（dev 环境已有 redis-0）。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

from shared.observability.logger import get_logger

logger = get_logger("kb-service-signal-job-manager")


class SignalJobManager:
    """Signal 异步 Job 管理器（进程内单例）。"""

    def __init__(self, max_concurrent: int = 10) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent

    async def submit(self, kbd_id: int, runner, trace_id: str | None = None) -> str:
        """提交 Job，返回 job_id。

        Args:
            kbd_id: kbd_entry.id
            runner: 异步执行函数（接收 kbd_id，返回 dict 结果）

        Returns:
            job_id（短 uuid）

        幂等去重：若同一 kbd_id 已有 pending/running 的 job，直接返回其
        job_id，避免重复点击「重新提交」并发起多个 job 重复烧 LLM 且相互覆盖 signals_json。
        """
        # 1. 清理过期 job（进程内 dict 只增不减 → 内存泄漏防御）
        self._prune_old()
        # 2. 去重：同一 kbd 已在跑则返回已有 job_id
        existing = await self.get_by_kbd_id(kbd_id)
        if existing is not None:
            logger.info(event="signal_job_dedup", job_id=existing["job_id"], kbd_id=kbd_id)
            return existing["job_id"]

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
                "result": None,
                "trace_id": trace_id,
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
            }
        # 后台执行（不阻塞 submit）
        asyncio.create_task(self._run(job_id, kbd_id, runner))
        logger.info(event="signal_job_submitted", job_id=job_id, kbd_id=kbd_id)
        return job_id

    def _prune_old(self, max_age_s: float = 3600.0) -> None:
        """清理已结束超过 max_age_s 的 job，防止内存无限增长。"""
        now = time.time()
        to_delete = [
            jid for jid, j in self._jobs.items()
            if j["status"] in ("done", "failed")
            and (now - (j.get("finished_at") or now)) > max_age_s
        ]
        for jid in to_delete:
            self._jobs.pop(jid, None)
        if to_delete:
            logger.info(event="signal_job_pruned", count=len(to_delete))

    async def _run(self, job_id: str, kbd_id: int, runner) -> None:
        """异步执行 Job（应用并发信号量，更新状态）。"""
        async with self._sem:
            await self._update(job_id, status="running", started_at=time.time())
            try:
                result = await runner(kbd_id)
                # 把 signals_count 映射到 total/done，使轮询进度更直观
                signals_count = (result or {}).get("signals_count", 0)
                failed_status = "done" if (result or {}).get("success", True) else "failed"
                await self._update(
                    job_id,
                    status=failed_status,
                    total=max(signals_count, 1),
                    done=signals_count,
                    failed=0,
                    result=result,
                    error=(result or {}).get("error") if failed_status == "failed" else None,
                    finished_at=time.time(),
                )
                logger.info(
                    event="signal_job_completed",
                    job_id=job_id, kbd_id=kbd_id,
                    status=self._jobs[job_id]["status"],
                    signals_count=signals_count,
                    trace_id=self._jobs[job_id].get("trace_id"),
                )
            except Exception as exc:
                await self._update(
                    job_id,
                    status="failed",
                    error=str(exc),
                    finished_at=time.time(),
                )
                logger.exception(
                    event="signal_job_failed",
                    job_id=job_id, kbd_id=kbd_id, error=exc,
                    trace_id=self._jobs[job_id].get("trace_id"),
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
_job_manager: SignalJobManager | None = None


def get_signal_job_manager() -> SignalJobManager:
    """获取 Job 管理器单例（首次调用时初始化）。"""
    global _job_manager
    if _job_manager is None:
        _job_manager = SignalJobManager(max_concurrent=10)
        logger.info(event="signal_job_manager_initialized", max_concurrent=10)
        # 多副本/多 worker 部署警告：进程内存储会导致 /status 轮询跨进程 404。
        workers = int(os.environ.get("WEB_CONCURRENCY", os.environ.get("KB_SERVICE_WORKERS", "1")))
        if workers > 1:
            logger.warning(
                event="signal_job_manager_multi_worker",
                workers=workers,
                message="SignalJobManager 为进程内存储：多 worker/多副本部署下 POST 与 GET /status "
                        "可能落到不同进程而 404。生产环境需演进到 redis/DB 持久化（dev 已有 redis-0）。",
            )
    return _job_manager
