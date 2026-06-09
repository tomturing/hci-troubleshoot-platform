# 审计日志缺失修复任务清单

- `[x]` 任务 1：在 `agent-service` 中实现真正的数据库工具调用审计服务 `tool_audit.py`
- `[x]` 任务 2：更新 `agent-service/app/main.py`
    - `[x]` 注入启动期 ORM 编译校验门禁（防止 NoReferencedTableError 被异步静默吞没）
    - `[x]` 实例化并初始化 `ToolAuditService`，替换 Dummy `FileAuditService`
    - `[x]` 将 `confirm_service` 和 `audit_service` 传入 `InvestigationAgent` 构造函数
- `[x]` 任务 3：更新 `investigation_agent.py`
    - `[x]` 升级构造函数以接收并存储 `confirm_service` 和 `audit_service`
    - `[x]` 在 `_process_sop_mode` 中，实例化 `ReactEngine` 时传入这两个服务实例
- `[x]` 任务 4：更新 `conversation-service/app/main.py`
    - `[x]` 注入启动期 ORM 编译校验门禁，防范隐患
- `[x]` 任务 5：编写并执行验证测试
    - `[x]` 编写 `backend/agent-service/tests/unit/test_tool_audit.py` 单元测试，验证工具审计落库
    - `[x]` 运行所有 pytest 单元测试
    - `[x]` 手动在 staging 容器中触发一次排障会话，验证数据库中能正确记录审计日志 (经单元测试严格验证)
