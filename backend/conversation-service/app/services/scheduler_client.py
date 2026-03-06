"""
Scheduler Client - 与 scheduler-service 交互的 HTTP 客户端
"""

import asyncio

import httpx
from shared.utils.logger import get_logger

from app.config import settings

logger = get_logger("scheduler-client")


class SchedulerClient:
    """调用 scheduler-service 完成 Pod 分配和端点查询。"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def allocate_pod(
        self,
        case_id: str,
        assistant_type: str,
        case_title: str | None = None,
        case_description: str | None = None,
    ) -> bool:
        """请求调度器为 case 分配指定类型 Pod。

        case_title / case_description 会被注入到 ProductionClaw Pod 的环境变量，
        使得 LearningClaw 擁有工单上下文。
        """
        url = f"{self.base_url}/api/scheduler/pods/allocate"
        payload: dict = {"case_id": case_id, "assistant_type": assistant_type}
        if case_title:
            payload["case_title"] = case_title
        if case_description:
            payload["case_description"] = case_description
        try:
            async with httpx.AsyncClient(timeout=settings.SCHEDULER_ALLOCATE_TIMEOUT_SEC) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            logger.warning(
                event="scheduler_allocate_failed",
                message=f"Scheduler allocate failed: {resp.status_code}",
                case_id=case_id,
                assistant_type=assistant_type,
                response=resp.text,
            )
            return False
        except Exception as exc:
            logger.warning(
                event="scheduler_allocate_exception",
                message=f"Scheduler allocate exception: {exc}",
                case_id=case_id,
                assistant_type=assistant_type,
                error=str(exc),
            )
            return False

    async def wait_for_endpoint(self, case_id: str) -> str | None:
        """轮询 Pod 分配信息，等待 endpoint 就绪。"""
        url = f"{self.base_url}/api/scheduler/pods/{case_id}"
        timeout = max(settings.SCHEDULER_POD_READY_TIMEOUT_SEC, 1)
        interval = max(settings.SCHEDULER_POD_POLL_INTERVAL_SEC, 0.2)
        loops = max(int(timeout / interval), 1)

        async with httpx.AsyncClient(timeout=5.0) as client:
            for _ in range(loops):
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        endpoint = data.get("endpoint")
                        status = data.get("status")
                        if endpoint and status == "Running":
                            return endpoint
                except Exception:
                    pass
                await asyncio.sleep(interval)
        return None
