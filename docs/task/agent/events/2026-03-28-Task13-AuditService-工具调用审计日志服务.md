---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 13
---

# Task 13：AuditService——工具调用审计日志服务（P1）

```
你是一名负责 hci-troubleshoot-platform 工具调用审计日志的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
Task 10（ReactExecutor）中每次工具调用都执行：
  await self.audit.write(...)

但 audit_service 的实现从未被定义（只有注入占位）。
这是一个安全生产要求：所有工具调用——无论成功还是失败——都必须写入 tool_audit_log 表。
该表在 Task 07（迁移 003）中已创建，包含如下关键字段：
  - session_id、tool_name、tool_args（JSONB）、risk_level
  - policy（auto/notify/confirm/block）
  - authorized_by（risk_level>=2 时记录确认用户）
  - result（JSONB，执行结果摘要）
  - error（文本，执行异常信息）
  - started_at、completed_at、duration_ms
  - trace_id（W3C traceparent）

审计服务有两个关键约束：
  1. 写入操作在 ReactExecutor 的 finally 块中调用，不可因审计失败阻断工具执行
  2. 审计记录不可删除（只读 API，无 DELETE 路由）

前置条件：Task 07（tool_audit_log 表已建立）、Task 10（ReactExecutor 已定义注入接口）

【任务目标】
1. 实现 AuditService（写 tool_audit_log）
2. 实现审计日志查询 API（GET /api/v1/audit-logs，供管理后台展示）
3. 将 AuditService 集成到 ReactExecutor（替换 Task 10 中的 audit_service 占位符）
4. 验证：执行一次工具调用后，tool_audit_log 表有对应记录

【涉及服务 / 文件范围】
允许新建/修改：
  - backend/conversation-service/app/services/audit_service.py（新建）
  - backend/conversation-service/app/api/audit.py（新建：查询路由）
  - backend/conversation-service/app/services/conversation_service.py（注入 AuditService）
只读参考：
  - backend/shared/models/（Task 07 建立的 ToolAuditLog ORM 模型）
  - backend/conversation-service/app/core/react_executor.py（Task 10 产物，找到 audit_service 注入点）
禁止：
  - 添加 DELETE /audit-logs 路由（审计记录不可删除）
  - 审计写入失败时抛出异常阻断工具执行（必须 except Exception: logger.error 降级）

【详细实现步骤】

Step 1：实现 AuditService

```python
# backend/conversation-service/app/services/audit_service.py
"""工具调用审计日志服务：写 tool_audit_log 表，强制不可绕过"""
import logging
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from backend.shared.models.audit import ToolAuditLog   # Task 07 建立的 ORM 模型

logger = logging.getLogger(__name__)

class AuditService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def write(
        self,
        id: str,
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
            log = ToolAuditLog(
                id=id,
                session_id=session_id,
                tool_name=tool_name,
                tool_args=tool_args,
                risk_level=risk_level,
                policy=policy,
                result={"data": str(result)[:2000]} if result else None,   # 截断大结果
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
            # 审计写入失败：记录但不阻断调用方（ReactExecutor finally 块）
            logger.error(
                f"审计日志写入失败 [session={session_id} tool={tool_name}]: {e}",
                exc_info=True
            )
```

Step 2：添加审计查询路由（只读）

```python
# backend/conversation-service/app/api/audit.py
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from ..dependencies import get_db

router = APIRouter(prefix="/api/v1/audit-logs", tags=["audit"])

@router.get("")
async def list_audit_logs(
    session_id: str | None = Query(None, description="按会话 ID 过滤"),
    tool_name: str | None = Query(None, description="按工具名称过滤"),
    risk_level: int | None = Query(None, description="按风险等级过滤"),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """查询工具调用审计日志（只读，无删除接口）"""
    ...
    # 返回分页结果，含总数
```

Step 3：将 AuditService 集成到 conversation_service.py

```python
# 在服务初始化中（conversation_service.py）
from .audit_service import AuditService

audit_service = AuditService(db=db_session)
react_executor = ReactExecutor(
    ...
    audit_service=audit_service,    # 替换 Task 10 中的占位符
    ...
)
```

Step 4：验证

```bash
# 触发一次工具调用（如 get_active_alerts）
curl -X POST http://localhost:8002/api/v1/conversations/{session_id}/messages \
  -d '{"content": "查看当前有哪些告警"}'

# 查询审计日志，确认记录已写入
curl http://localhost:8002/api/v1/audit-logs?tool_name=get_active_alerts
# 预期：返回包含本次工具调用的记录，含 duration_ms、result 字段

# 数据库直查
docker compose -f deploy/docker/docker-compose.yml exec postgres \
  psql -U hci_user -d hci_db \
  -c "SELECT tool_name, risk_level, duration_ms, error FROM tool_audit_log ORDER BY started_at DESC LIMIT 5"
```

【约束】
- AuditService.write() 内部所有异常必须 try/except 捕获，不可向上抛出
- 不添加 DELETE /audit-logs 路由（审计记录只增不删）
- result 字段截断到 2000 字符（防止大响应撑爆 DB）
- 所有代码注释使用中文

【验收标准】
- [ ] 执行工具调用后，tool_audit_log 表有对应记录
- [ ] GET /api/v1/audit-logs 返回分页列表
- [ ] AuditService.write() 在 DB 连接中断时不抛异常（仅打 error 日志）
- [ ] 无 DELETE /audit-logs 路由
- [ ] uv run pytest backend/conversation-service/tests/test_audit_service.py -v 通过
- [ ] make lint 无新增错误
```

---

# 35_任务编排_P4_工具扩展与数据管道

> **阶段**：Phase 4 — acli 工具扩展 + 前端确认 UI + 历史工单数据管道（Phase 3 完成后开始）  
> **目标**：扩展诊断工具覆盖率、改善用户交互、启动知识蒸馏流程  
> **并行条件**：T14（acli 工具扩展）与 T15（前端 UI）可并行 | T16（工单数据管道）独立，可任意时间并行 | T17（知识反馈）最后执行  
> **前置依赖**：Task 10（ReactExecutor）、Task 12（人工确认机制）、Task 04（knowledge_atoms 表）  
> **创建日期**：2026-03-22  
> **关联文档**：
> - [docs/architecture/完整技术方案.md](../architecture/完整技术方案.md) § 七、Phase 4
> - [docs/architecture/知识库重建设计方案.md](../architecture/知识库重建设计方案.md) § 四、Case Library
> - http://acli.sangfor.com.cn:6888/commandList（acli 命令参考，需连接内网）

---