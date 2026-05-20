"""
SCP（深信服 HCI 管理平台）REST API 适配器

SCP 是深信服 HCI 的管理 REST API 网关，提供：
  - 告警列表查询（GET /janus/20180725/alarms）
  - 操作任务查询（GET /janus/20180725/tasks）
  - 云主机/虚拟机列表（GET /janus/20240725/servers）
  - 集群详情（GET /janus/20190725/clusters/{cluster_id}）

认证方式：TokenAuth（x-auth-token Header）
SCP 不可达时返回降级响应，不抛异常（保证 ReactExecutor 工具调用的健壮性）
"""

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SCPAdapter:
    """SCP REST API 工具执行适配器，供 ReactExecutor 注入使用"""

    # 连接超时 10s，读超时 15s（SCP API 响应可能较慢）
    DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0)

    # SCP 不可达时的降级响应标识
    DEGRADED_RESPONSE: dict[str, Any] = {
        "_degraded": True,
        "message": "SCP 暂时不可达，请稍后重试或手动查询 HCI 管理平台",
    }

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "x-auth-token": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @classmethod
    def from_env(cls) -> "SCPAdapter":
        """从环境变量创建实例"""
        return cls(
            base_url=os.environ["SCP_BASE_URL"],
            api_key=os.environ["SCP_API_KEY"],
        )

    async def execute(self, tool_name: str, args: dict) -> Any:
        """统一工具执行入口，供 ReactExecutor 调用"""
        dispatch = {
            "get_active_alerts": self.get_active_alerts,
            "get_failed_tasks": self.get_failed_tasks,
            "get_vm_list": self.get_vm_list,
            "get_cluster_detail": self.get_cluster_detail,
        }
        handler = dispatch.get(tool_name)
        if not handler:
            return {"error": f"SCPAdapter 未实现工具: {tool_name}"}
        return await handler(**args)

    async def get_active_alerts(self, limit: int = 10) -> dict:
        """
        查询活跃告警列表

        API: GET /janus/20180725/alarms
        响应结构：{"code": 0, "data": {"data": [...], "total": N}}
        """
        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.DEFAULT_TIMEOUT
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/janus/20180725/alarms",
                    params={"page_num": 1, "page_size": min(limit, 50)},
                )
                resp.raise_for_status()
                data = resp.json()
                alarms = data.get("data", {}).get("data", [])
                return {
                    "total": len(alarms),
                    "alarms": [
                        {
                            "id": a.get("id"),
                            "name": a.get("name"),
                            "level": a.get("level"),       # critical|major|minor
                            "status": a.get("status"),
                            "message": a.get("message"),
                            "created_at": a.get("created_at"),
                        }
                        for a in alarms[:limit]
                    ],
                }
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"SCP 告警查询超时/连接失败: {e}")
            return self.DEGRADED_RESPONSE
        except httpx.HTTPStatusError as e:
            logger.warning(f"SCP 告警查询 HTTP 错误 [{e.response.status_code}]: {e}")
            return {"error": f"SCP 返回错误: {e.response.status_code}"}

    async def get_failed_tasks(
        self,
        task_type: str | None = None,
        begin_time: str | None = None,
        limit: int = 10,
    ) -> dict:
        """
        查询失败操作任务

        API: GET /janus/20180725/tasks
        默认查询最近 24 小时内的失败任务
        """
        # 默认 begin_time = 24 小时内
        if not begin_time:
            begin_time = (
                datetime.now(UTC) - timedelta(hours=24)
            ).strftime("%Y-%m-%d %H:%M:%S")

        params: dict[str, Any] = {
            "begin_time": begin_time,
            "page_size": min(limit * 2, 50),   # 多取一些，因为要在客户端过滤失败状态
            "page_num": 1,
        }
        if task_type:
            params["fields"] = task_type

        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.DEFAULT_TIMEOUT
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/janus/20180725/tasks",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                tasks = data.get("data", {}).get("data", [])
                # 客户端过滤：仅保留失败状态
                failed = [t for t in tasks if t.get("status") in ("failed", "error")]
                return {
                    "total_failed": len(failed),
                    "tasks": [
                        {
                            "id": t.get("id"),
                            "name": t.get("name"),
                            "description": t.get("description"),
                            "status": t.get("status"),
                            "error_message": t.get("error_message"),
                            "created_at": t.get("created_at"),
                            "object_name": t.get("object_name"),
                        }
                        for t in failed[:limit]
                    ],
                }
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"SCP 任务查询超时/连接失败: {e}")
            return self.DEGRADED_RESPONSE
        except httpx.HTTPStatusError as e:
            logger.warning(f"SCP 任务查询 HTTP 错误 [{e.response.status_code}]: {e}")
            return {"error": f"SCP 返回错误: {e.response.status_code}"}

    async def get_vm_list(
        self,
        name_filter: str | None = None,
        limit: int = 20,
    ) -> dict:
        """
        查询虚拟机列表，可按名称过滤（客户端模糊匹配）

        API: GET /janus/20240725/servers
        """
        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.DEFAULT_TIMEOUT
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/janus/20240725/servers",
                    params={"page_size": min(limit, 50), "page_num": 1},
                )
                resp.raise_for_status()
                data = resp.json()
                vms = data.get("data", {}).get("data", [])
                # 客户端名称过滤（模糊匹配）
                if name_filter:
                    vms = [
                        v for v in vms
                        if name_filter.lower() in (v.get("name") or "").lower()
                    ]
                return {
                    "total": len(vms),
                    "vms": [
                        {
                            "id": v.get("id"),
                            "name": v.get("name"),
                            "status": v.get("status"),
                            "host_name": v.get("host_name"),
                            "cluster_name": v.get("cluster_name"),
                        }
                        for v in vms[:limit]
                    ],
                }
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"SCP VM 列表查询超时/连接失败: {e}")
            return self.DEGRADED_RESPONSE
        except httpx.HTTPStatusError as e:
            logger.warning(f"SCP VM 列表查询 HTTP 错误 [{e.response.status_code}]: {e}")
            return {"error": f"SCP 返回错误: {e.response.status_code}"}

    async def get_cluster_detail(self, cluster_id: str) -> dict:
        """
        查询集群详细信息

        API: GET /janus/20190725/clusters/{cluster_id}
        """
        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.DEFAULT_TIMEOUT
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/janus/20190725/clusters/{cluster_id}"
                )
                resp.raise_for_status()
                data = resp.json()
                cluster = data.get("data", {})
                return {
                    "id": cluster.get("id"),
                    "name": cluster.get("name"),
                    "arch_type": cluster.get("arch_type"),
                    "authorize_mode": cluster.get("authorize_mode"),
                    "az_id": cluster.get("az_id"),
                    "node_count": cluster.get("node_count"),
                }
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"SCP 集群详情查询超时/连接失败: {e}")
            return self.DEGRADED_RESPONSE
        except httpx.HTTPStatusError as e:
            logger.warning(f"SCP 集群详情查询 HTTP 错误 [{e.response.status_code}]: {e}")
            return {"error": f"SCP 返回错误: {e.response.status_code}"}
