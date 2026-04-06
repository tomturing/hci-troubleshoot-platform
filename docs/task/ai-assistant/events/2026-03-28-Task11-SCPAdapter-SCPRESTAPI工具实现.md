---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 11
---

# Task 11：SCPAdapter——SCP REST API 工具实现（P1）

```
你是一名负责 hci-troubleshoot-platform SCP 平台接入的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
SCP（Service Control Platform）是深信服 HCI 的管理 REST API 网关，提供：
  - 告警列表查询（GET /janus/20180725/alarms）
  - 操作任务查询（GET /janus/20180725/tasks）
  - 云主机/虚拟机列表（GET /janus/20240725/servers）
  - 集群详情（GET /janus/20190725/clusters/{cluster_id}）

完整 OpenAPI 规范在：docs/reference/scp/openapi.yaml

认证方式：
  - EC2Auth（默认）或 TokenAuth（API Key in header: x-auth-token）
  - SCP 地址从环境变量读取：SCP_BASE_URL、SCP_API_KEY

SCPAdapter 是 ReactExecutor 的工具执行后端之一，实现上述 4 个"Tool"
（对应 Task 10 中 TOOL_REGISTRY 声明的 4 个工具函数）。

前置条件：Task 10（ReactExecutor 完成，TOOL_REGISTRY 已定义）

【任务目标】
1. 实现 backend/conversation-service/app/adapters/scp_adapter.py
2. 实现 4 个工具：get_active_alerts / get_failed_tasks / get_vm_list / get_cluster_detail
3. 实现连接超时和错误处理（SCP 不可用时的降级策略）
4. 集成到 ReactExecutor 的工具执行后端
5. 端到端验证：发送"查看告警"请求触发 get_active_alerts 工具调用

【涉及服务 / 文件范围】
允许新建/修改：
  - backend/conversation-service/app/adapters/scp_adapter.py（新建）
  - backend/conversation-service/app/adapters/__init__.py
  - deploy/env/platform.env（仅新增 SCP_BASE_URL / SCP_API_KEY 配置项注释）
只读参考：
  - docs/reference/scp/openapi.yaml（权威 API 规范）
  - backend/conversation-service/app/core/tool_registry.py（Task 10 产物）

【详细实现步骤】

Step 1：参考 OpenAPI 规范，实现 4 个工具

先阅读 docs/reference/scp/openapi.yaml 中以下路径的完整参数定义：
  - GET /janus/20180725/alarms（参数：fields, page_num, page_size, az_id）
  - GET /janus/20180725/tasks（参数：az_id, begin_time, end_time, fields, object_id）
  - GET /janus/20240725/servers（参数：fields, order_by, page_num）
  - GET /janus/20190725/clusters/{cluster_id}

```python
# backend/conversation-service/app/adapters/scp_adapter.py
"""SCP（深信服 HCI 管理平台）REST API 适配器"""
import logging
import os
from datetime import datetime, timedelta, timezone
import httpx

logger = logging.getLogger(__name__)

class SCPAdapter:
    """SCP REST API 工具执行适配器"""

    DEFAULT_TIMEOUT = 15.0    # 秒
    DEGRADED_RESPONSE = {"_degraded": True, "message": "SCP 暂时不可达，使用降级响应"}

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        self.headers = {
            "x-auth-token": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @classmethod
    def from_env(cls) -> "SCPAdapter":
        return cls(
            base_url=os.environ["SCP_BASE_URL"],
            api_key=os.environ["SCP_API_KEY"],
        )

    async def execute(self, tool_name: str, args: dict) -> dict:
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
        参数约定：page_size=limit, page_num=1
        响应结构：{"code": 0, "data": {"data": [...], "total": N}}
        """
        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.DEFAULT_TIMEOUT
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/janus/20180725/alarms",
                    params={"page_num": 1, "page_size": limit},
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
                            "level": a.get("level"),      # critical|major|minor
                            "status": a.get("status"),
                            "message": a.get("message"),
                            "created_at": a.get("created_at"),
                        }
                        for a in alarms[:limit]
                    ]
                }
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"SCP 告警查询超时/连接失败: {e}")
            return self.DEGRADED_RESPONSE

    async def get_failed_tasks(
        self,
        task_type: str | None = None,
        begin_time: str | None = None,
        limit: int = 10,
    ) -> dict:
        """
        查询失败任务/操作日志
        API: GET /janus/20180725/tasks
        """
        # 默认 begin_time = 24 小时内
        if not begin_time:
            begin_time = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).strftime("%Y-%m-%d %H:%M:%S")

        params = {
            "begin_time": begin_time,
            "page_size": limit,
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
                # 过滤失败任务
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
                    ]
                }
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"SCP 任务查询超时/连接失败: {e}")
            return self.DEGRADED_RESPONSE

    async def get_vm_list(
        self, name_filter: str | None = None, limit: int = 20
    ) -> dict:
        """查询虚拟机列表，可按名称过滤"""
        params = {"page_size": limit, "page_num": 1}
        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.DEFAULT_TIMEOUT
            ) as client:
                resp = await client.get(
                    f"{self.base_url}/janus/20240725/servers", params=params
                )
                resp.raise_for_status()
                data = resp.json()
                vms = data.get("data", {}).get("data", [])
                if name_filter:
                    vms = [v for v in vms if name_filter.lower() in (v.get("name") or "").lower()]
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
                    ]
                }
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"SCP VM 列表查询超时/连接失败: {e}")
            return self.DEGRADED_RESPONSE

    async def get_cluster_detail(self, cluster_id: str) -> dict:
        """查询集群详情"""
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
```

Step 2：集成到 ReactExecutor

在 ReactExecutor 初始化时注入 SCPAdapter：
```python
# 在 conversation_service.py 的服务初始化中
scp_adapter = SCPAdapter.from_env()
react_executor = ReactExecutor(
    glm_client=glm_client,
    tool_executor=scp_adapter,
    ...
)
```

Step 3：单元测试（使用 respx mock HTTP 调用）

```bash
uv add respx --dev    # HTTP mock 库

# tests/unit/test_scp_adapter.py
# - mock GET /alarms，验证返回格式
# - mock 连接超时，验证降级响应
# - mock name_filter 过滤逻辑
```

Step 4：端到端验证

```bash
# 在 conversation-service 中发送消息，触发告警查询
curl -X POST http://localhost:8002/api/v1/conversations/{session_id}/messages \
  -d '{"content": "帮我看一下当前有什么告警"}'
# 预期：AI 调用 get_active_alerts 工具，在 tool_audit_log 中有记录
# 如果 SCP 不可达，AI 应说明无法连接平台，而不是崩溃
```

【约束】
- SCP 不可达时，返回降级响应（不抛异常）
- 不缓存 SCP 响应（每次调用都实时查询，保证数据新鲜度）
- API Key 只从环境变量读取

【验收标准】
- [ ] uv run pytest tests/unit/test_scp_adapter.py -v 通过（含超时降级测试）
- [ ] SCP 可达时，get_active_alerts 返回正确格式的告警列表
- [ ] SCP 不可达时，返回 _degraded=True 的降级响应，不崩溃
- [ ] 每次工具调用在 tool_audit_log 表有记录
- [ ] make lint 无新增错误
```

---