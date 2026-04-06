---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 12
---

# Task 12：人工确认机制——Redis 等待 + SSE 通知（P1）

```
你是一名负责 hci-troubleshoot-platform 人工确认机制的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
ReAct 执行器在遇到 risk_level >= 2 的工具时，需要暂停并等待用户确认。
实现方案：
  1. ReactExecutor 将待确认工具调用信息推送到用户的 SSE 流（confirm_request 事件）
  2. 前端收到 confirm_request 显示确认弹窗
  3. 用户点击确认/取消，触发 POST /confirm
  4. ReactExecutor 通过 Redis BRPOP 阻塞等待确认结果（超时 120 秒）
  5. 超时则自动取消操作

Redis Key 设计：
  confirm:{session_id}  → LIST，ReactExecutor BRPOP，前端 POST 触发 LPUSH

前置条件：Task 10（ReactExecutor 完成）

【任务目标】
1. 实现 ConfirmService（Redis BRPOP 等待逻辑）
2. 新增 POST /api/v1/conversations/{session_id}/confirm 接口
3. 在 SSE 推送中新增 confirm_request 事件类型
4. 集成到 ReactExecutor（替换 Task 10 中的 confirm_service 占位符）
5. 验证：触发 risk_level=2 工具后，SSE 推送 confirm_request，POST /confirm 后继续执行

【涉及服务 / 文件范围】
允许新建/修改：
  - backend/conversation-service/app/services/confirm_service.py（新建）
  - backend/conversation-service/app/api/conversations.py（新增 /confirm 路由）
只读参考：
  - docs/architecture/各层最优设计.md § Layer 1（Redis Key 设计）
  - backend/conversation-service/app/（现有 SSE 实现，了解 SSE event 格式）

【详细实现步骤】

Step 1：实现 ConfirmService

```python
# backend/conversation-service/app/services/confirm_service.py
"""人工确认服务：通过 Redis 实现 ReAct 执行器的阻塞等待"""
import json
import logging
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

CONFIRM_TIMEOUT = 120    # 等待用户确认的超时秒数
REDIS_KEY_PREFIX = "confirm:"

class ConfirmService:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def request_confirm(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict,
        risk_level: int,
    ) -> bool:
        """
        请求用户确认。阻塞等待直到用户响应或超时。
        返回 True = 用户确认，False = 用户取消或超时
        """
        key = f"{REDIS_KEY_PREFIX}{session_id}"

        # 清空可能残留的旧确认结果
        await self.redis.delete(key)

        # 推送 SSE confirm_request 事件（由调用方的 sse_emitter 处理）
        # 这里只负责等待

        logger.info(
            f"等待用户确认 [session={session_id}] 工具={tool_name}，超时={CONFIRM_TIMEOUT}s"
        )

        # BRPOP 阻塞等待，超时返回 None
        result = await self.redis.brpop(key, timeout=CONFIRM_TIMEOUT)

        if result is None:
            logger.warning(f"确认超时 [session={session_id}]")
            return False

        _, value = result
        try:
            data = json.loads(value)
            confirmed = data.get("confirmed", False)
            logger.info(f"用户确认结果 [session={session_id}]: confirmed={confirmed}")
            return confirmed
        except Exception:
            return False

    async def submit_confirm(
        self, session_id: str, confirmed: bool, authorized_by: str
    ) -> None:
        """
        接收并提交用户确认结果（由 POST /confirm 路由调用）
        """
        key = f"{REDIS_KEY_PREFIX}{session_id}"
        value = json.dumps({"confirmed": confirmed, "authorized_by": authorized_by})
        await self.redis.lpush(key, value)
        # 设置过期（防止遗留数据）
        await self.redis.expire(key, 300)
```

Step 2：新增 /confirm 路由

在 conversations.py 路由中新增：

```python
class ConfirmRequest(BaseModel):
    confirmed: bool
    authorized_by: str    # 当前用户 ID

@router.post("/{session_id}/confirm", status_code=200)
async def submit_confirm(
    session_id: str,
    req: ConfirmRequest,
    confirm_service: ConfirmService = Depends(get_confirm_service),
):
    """接收用户的工具调用确认结果"""
    await confirm_service.submit_confirm(
        session_id=session_id,
        confirmed=req.confirmed,
        authorized_by=req.authorized_by,
    )
    return {"status": "ok"}
```

Step 3：SSE confirm_request 事件格式定义

在 SSE 推送模块中，新增 confirm_request 事件类型：
```json
{
  "type": "confirm_request",
  "tool_name": "service_restart",
  "tool_args": {"service_name": "exporter", "host_id": "node-01"},
  "risk_level": 2,
  "risk_description": "重启 exporter 服务将导致监控数据短暂中断（约 30 秒）",
  "timeout_seconds": 120
}
```

Step 4：测试

```bash
# 模拟触发一个 risk_level=2 的工具调用
# 1. 发送消息（需要在 TOOL_REGISTRY 中临时添加一个 risk_level=2 的测试工具）
# 2. 监听 SSE，确认 confirm_request 事件推出
# 3. 调用 POST /confirm，确认 ReactExecutor 继续执行

uv run pytest backend/conversation-service/tests/test_confirm_service.py -v
```

【约束】
- 确认超时（120s）后，工具调用自动取消，不可强制执行
- Redis 不可用时，所有高风险工具 fallback 为 block（安全优先）

【验收标准】
- [ ] POST /api/v1/conversations/{session_id}/confirm 接口存在且返回 200
- [ ] Redis BRPOP 超时后，request_confirm 返回 False
- [ ] SSE 流中出现 confirm_request 事件类型
- [ ] 用户确认后，ReactExecutor 继续执行工具
- [ ] Redis 不可用时，高风险工具 fallback 为 block
- [ ] uv run pytest backend/conversation-service/tests/test_confirm_service.py -v 通过
- [ ] make lint 无新增错误
```

---