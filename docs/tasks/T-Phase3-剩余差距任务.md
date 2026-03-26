# Phase 3 剩余差距任务编排

> 创建日期：2026-03-25  
> 目标分支：`docs/phase0-4-task-orchestration`  
> 背景：Phase 3 核心代码已全部就绪（ReactExecutor、GLMClient、SCP/acli 适配器、ConfirmDialog 等），
> 但尚有部署配置、前端体验、测试覆盖、代码清洁度四个方向的差距待补齐。
> 参考：[架构文档 05](../architecture/05_AI助手层设计.md) | [差距分析 08](../architecture/08_HCI平台效果差距分析与重构方案.md) | [实施规范 11](../architecture/11_完整技术方案.md)

---

## Task T31：解锁 Helm 部署配置，激活 ReactExecutor（P0）

```
你是一名负责 hci-troubleshoot-platform 部署配置层的 agent。

【仓库】
# 主代码库
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

# 环境配置仓库（独立仓库，同级目录）
cd ..
git clone <env-repo-url> hci-platform-env
cd hci-platform-env

【背景】
Phase 3 的核心代码已全部实现并合并：
- ReactExecutor（react_executor.py）：ReAct 循环 + 工具调度
- SCPAdapter（scp_adapter.py）：SCP 平台 HTTP 工具，需要 SCP_BASE_URL + SCP_API_KEY
- AcliAdapter（acli_adapter.py）：acli SSH 工具，需要 HCI_SSH_USER + HCI_SSH_KEY_PATH

但 Helm chart 的 conversation-service deployment.yaml 中 **完全缺失** 这些环境变量，
导致应用启动时 `settings.REACT_ENABLED = False`，ReactExecutor 从未被初始化。

相关代码位置：
- backend/conversation-service/app/config.py（Settings 类，REACT_ENABLED 默认 False）
- backend/conversation-service/app/main.py（第96行：if settings.REACT_ENABLED and ...）
- deploy/helm/hci-platform/templates/conversation-service/deployment.yaml

已有 Helm 注入模式参考：同一 deployment.yaml 中 DATABASE_URL 已通过 .Values 注入，
SCP_API_KEY 应通过 Kubernetes Secret 注入（和 postgresPassword 同一个 hci-secrets）。

【任务目标】
1. 在 deployment.yaml 的 env 段添加所有 Phase 3 所需环境变量（5 个），引用正确的 Values 路径
2. 在 deployment.yaml 的 Values 引用保持与现有风格一致（snake_case Values key + quote）
3. 在 hci-platform-env/environments/dev/values.yaml 中添加配套配置节（含占位注释）
4. SCP_API_KEY 和 HCI_SSH_PASSWORD 通过 Kubernetes Secret 注入（不明文写 values）
5. 验证：`helm template` 渲染正确，`conversation_service.agent_mode_available` 最终为 True

【涉及服务 / 文件范围】
- deploy/helm/hci-platform/templates/conversation-service/deployment.yaml
- deploy/helm/hci-platform/values.yaml（添加 scpBaseUrl、hciSsh section 的默认值）
- hci-platform-env/environments/dev/values.yaml（添加实际配置节）
- backend/conversation-service/app/config.py（只读，确认字段名）
- backend/conversation-service/app/main.py（只读，确认激活条件）
- docs/architecture/11_完整技术方案.md §11.3（只读，参考模板）

【详细实现步骤】

Step 1：对齐文档与当前配置
- 阅读 docs/architecture/11_完整技术方案.md §11.3（两步激活指南）
- 阅读 backend/conversation-service/app/config.py，确认：
  - REACT_ENABLED: bool（默认 False）
  - SCP_BASE_URL: str（默认 ""）
  - SCP_API_KEY: str（默认 ""）
- 阅读 backend/conversation-service/app/adapters/acli_adapter.py，确认：
  - HCI_SSH_USER（默认 "admin"）
  - HCI_SSH_KEY_PATH（默认 ""）
  - HCI_SSH_PASSWORD（可选，与 KEY 二选一）
- 阅读 deploy/helm/hci-platform/templates/conversation-service/deployment.yaml，
  确认现有 env block 结构（目前只有 SERVICE_NAME / SERVICE_PORT / DATABASE_URL）

Step 2：修改 Helm Chart deployment.yaml
在现有 env 段的 DATABASE_URL 之后追加：

```yaml
- name: REACT_ENABLED
  value: {{ .Values.conversationService.reactEnabled | default "false" | quote }}
- name: SCP_BASE_URL
  value: {{ .Values.scpBaseUrl | default "" | quote }}
- name: SCP_API_KEY
  valueFrom:
    secretKeyRef:
      name: hci-secrets
      key: scp-api-key
      optional: true          # dev 环境无 SCP 时允许 Pod 正常启动
- name: HCI_SSH_USER
  value: {{ .Values.hciSsh.user | default "admin" | quote }}
- name: HCI_SSH_KEY_PATH
  value: {{ .Values.hciSsh.keyPath | default "" | quote }}
- name: HCI_SSH_PASSWORD
  valueFrom:
    secretKeyRef:
      name: hci-secrets
      key: hci-ssh-password
      optional: true          # 使用密钥时此字段可为空
```

Step 3：修改 deploy/helm/hci-platform/values.yaml（全局默认值）
在 conversationService 节中新增：
```yaml
  reactEnabled: "false"   # 默认关闭，环境仓库覆盖启用
scpBaseUrl: ""            # SCP 平台 API base URL，格式：http://host:port
hciSsh:
  user: "admin"
  keyPath: ""             # SSH 私钥路径，与 hci-ssh-password 二选一
```

Step 4：修改 hci-platform-env/environments/dev/values.yaml
添加以下配置节（不提交真实密码，密码由 SealedSecret / Vault 管理）：
```yaml
# Phase 3 ReAct 配置
conversationService:
  reactEnabled: "true"    # dev 环境开启 Agent 模式

scpBaseUrl: "http://<SCP_IP>:8082"   # 替换为 dev 环境 SCP 实际地址

hciSsh:
  user: "admin"
  keyPath: "/etc/ssh/hci_rsa"   # 替换为 dev 环境实际密钥路径
```

【约束】
- SCP_API_KEY 和 HCI_SSH_PASSWORD 严禁明文写入任何 YAML 文件，必须走 SecretKeyRef
- optional: true 要加，保证没有 SCP 的纯对话场景 Pod 能正常启动（不 crash）
- 不修改其他服务的 Helm 模板
- 代码注释必须使用中文
- 不引入新 Helm dependency

【验收标准】
- [ ] `helm template deploy/helm/hci-platform -f hci-platform-env/environments/dev/values.yaml | grep -A 5 "REACT_ENABLED"` 输出 value: "true"
- [ ] `helm template ... | grep "scp-api-key"` 确认 secretKeyRef 引用存在
- [ ] `kubectl exec <conversation-pod> -- env | grep REACT_ENABLED` 输出 `REACT_ENABLED=true`（deploy 后验证）
- [ ] `make lint` 或等效命令通过（无 YAML 语法错误）
- [ ] conversation-service /health 端点返回 200，不因 optional secret 缺失而 CrashLoop
```

---

## Task T32：前端诊断阶段进度条（P1）

```
你是一名负责 hci-troubleshoot-platform 前端 customer 应用的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
后端 conversation-service 已实现诊断阶段状态机（S0→S6），并在对话流中持久化
`diagnostic_stage` 字段到数据库（conversation 表 diagnostic_stage 列）。

当前前端情况：
- chat.ts 的 SSE 事件循环已处理：error / confirm_request / tool_executing / thinking
- **缺失**：`stage_change` 事件分支（后端发出但前端静默忽略）
- **缺失**：展示 S0→S6 进度条的 DiagnosticProgress.vue 组件

后端 stage_change 事件格式（参考 conversation_service.py）：
```json
{"type": "stage_change", "from": "S2", "to": "S3", "label": "故障假设阶段"}
```

诊断阶段定义（前端需要知道的用于 UI 显示）：
- S0：问题收集 | S1：信息确认 | S2：初步诊断 | S3：故障假设
- S4：验证假设 | S5：方案确认 | S6：执行修复

当前 chat.ts SSE 处理位置：~第319行，pendingEventType 分支。

【任务目标】
1. 在 chat.ts 的 SSE 事件循环中新增 `stage_change` 事件处理，更新 reactive 状态
2. 创建 DiagnosticProgress.vue 组件，展示 S0→S6 的水平进度条（含当前阶段高亮）
3. 将 DiagnosticProgress.vue 集成到 ChatView.vue 或对话区头部（固定显示，非弹窗）
4. 仅在 diagnostic_stage 不为 S0 时展示进度条（S0 表示未开始，不干扰正常对话界面）
5. 对话结束（case 关闭）后进度条隐藏

【涉及服务 / 文件范围】
- frontend/customer/src/stores/chat.ts（新增 stage_change 分支 + diagnosticStage 状态）
- frontend/customer/src/components/DiagnosticProgress.vue（新建）
- frontend/customer/src/views/ChatView.vue（集成 DiagnosticProgress，只读其他区域）
- frontend/shared/src/types.ts 或相关类型文件（只读，了解现有类型）

【详细实现步骤】

Step 1：对齐文档与实现
- 阅读 docs/architecture/11_完整技术方案.md §6.2，确认 stage_change 事件格式
- 阅读 frontend/customer/src/stores/chat.ts，定位 SSE 事件分支（~第319行）
- 阅读 frontend/customer/src/views/ChatView.vue，找到合适的插入点（对话输入框上方）
- 确认 frontend/customer/src/components/ 目录中已有 ConfirmDialog.vue（参考样式风格）

Step 2：chat.ts — 新增 diagnosticStage 状态 + stage_change 分支
```typescript
// 在状态声明区（第50行附近）新增：
const diagnosticStage = ref<string>('S0')  // 当前诊断阶段

// 在 SSE 事件循环（thinking 分支之后）新增：
} else if (pendingEventType === 'stage_change') {
  try {
    const event = JSON.parse(data)
    diagnosticStage.value = event.to ?? 'S0'
  } catch {}
}
```

同时在 return 中导出 diagnosticStage，并在 `startNewConversation()` 中
将 diagnosticStage 重置为 'S0'。

Step 3：创建 DiagnosticProgress.vue 组件
组件职责：
- props: stage (String, 当前阶段 'S0'~'S6')
- 渲染水平步骤条，7个节点，当前阶段高亮（蓝色 active），已完成（绿色 done）
- 阶段标签：['问题收集','信息确认','初步诊断','故障假设','验证假设','方案确认','执行修复']
- 风格参考 ConfirmDialog.vue（Tailwind CSS，与整体保持一致）
- 响应式：移动端折叠为"第 N/7 阶段：{label}"单行文字

Step 4：集成到 ChatView.vue
- 在对话消息列表上方，输入框上方区域插入：
  `<DiagnosticProgress v-if="chatStore.diagnosticStage !== 'S0'" :stage="chatStore.diagnosticStage" />`
- 工单关闭后 diagnosticStage 重置（由 handleCloseCase 触发）

【约束】
- 不修改 SSE 解析的核心 buffer 逻辑（只加事件分支，不重构流式处理）
- Tailwind CSS 样式，不引入 UI 组件库
- 代码注释使用中文
- 组件名 DiagnosticProgress（大驼峰），不缩写

【验收标准】
- [ ] 打开对话，向后端发送消息，Chrome DevTools Network→EventStream 可看到 stage_change 事件被消费
- [ ] console.log 确认 diagnosticStage.value 从 S0 正常切换
- [ ] Stage ≠ S0 时进度条组件可见，当前阶段高亮
- [ ] 工单关闭后进度条消失（重置为 S0）
- [ ] `pnpm --filter @hci/customer build` 构建通过，无 TypeScript 错误
- [ ] 进度条在 375px 宽度下不溢出（移动端适配）
```

---

## Task T33：ReAct 端到端集成测试（P2）

```
你是一名负责 hci-troubleshoot-platform 后端测试的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
Phase 3 ReactExecutor 已实现（react_executor.py，264行），但当前没有覆盖完整
ReAct 循环的集成测试。单元测试目录（backend/conversation-service/tests/）中
已有 conftest.py 但 integration/ 子目录下暂无 ReAct 测试。

需要验证的核心行为：
1. 对话请求触发 ReactExecutor（当 REACT_ENABLED=true）
2. SSE 事件流按顺序发出：thinking → tool_executing → message（文本）
3. 工具调用结果写入 tool_audit_log 表
4. 高风险工具暂停并发出 confirm_request SSE 事件
5. 低风险工具自动执行不需要用户确认

已有可参考的 mock 模式：
- conftest.py 中应有数据库 fixture，沿用其 DB session
- SCPAdapter 和 AcliAdapter 可完全 mock（不需要真实 SCP 或 SSH 连接）

【任务目标】
1. 在 backend/conversation-service/tests/integration/ 下创建 test_react_e2e.py
2. 覆盖：正常 ReAct 循环（thinking + 工具执行 + 最终回答）的 SSE 序列
3. 覆盖：高风险操作触发 confirm_request 事件，confirm 后继续执行
4. 覆盖：tool_audit_log 写入验证（调用后 DB 中有对应记录）
5. 所有测试使用 mock adapter，不依赖外部服务

【涉及服务 / 文件范围】
- backend/conversation-service/tests/integration/test_react_e2e.py（新建）
- backend/conversation-service/tests/conftest.py（只读，复用 fixture）
- backend/conversation-service/app/core/react_executor.py（只读）
- backend/conversation-service/app/adapters/scp_adapter.py（只读，了解接口）
- backend/conversation-service/app/adapters/acli_adapter.py（只读，了解接口）
- backend/conversation-service/app/services/audit_service.py（只读，了解写入行为）
- backend/conversation-service/app/models/tool_audit_log.py（只读，了解表结构）

【详细实现步骤】

Step 1：对齐文档与实现
- 阅读 docs/architecture/11_完整技术方案.md §6.1（已实现文件清单）和 §6.3（工具集）
- 阅读 react_executor.py，确认：
  - 对于 risk_level=1 工具：自动执行，发出 tool_executing SSE 事件
  - 对于 risk_level=2/3 工具：暂停，发出 confirm_request SSE，Redis BRPOP 等待
  - 每次工具调用完成后：AuditService.log_tool_call() 写入 tool_audit_log
- 阅读 conftest.py，确认：
  - async_client fixture（HTTPX AsyncClient）
  - db_session fixture（SQLAlchemy AsyncSession）
  - 如缺失，需在 conftest.py 中新增

Step 2：创建测试文件骨架（mock 先行）
```python
# test_react_e2e.py 核心结构
import pytest
from unittest.mock import AsyncMock, patch

# Mock SCP + acli 工具函数
MOCK_TOOLS = {
    "scp_vm_list": AsyncMock(return_value={"vms": [{"id": "vm-001", "status": "error"}]}),
    "acli_vm_get": AsyncMock(return_value={"cpu_usage": 95, "mem_free_mb": 100}),
    "acli_vm_restart": AsyncMock(return_value={"result": "started"}),  # risk_level=2
}

@pytest.mark.asyncio
async def test_react_low_risk_tool_auto_executes(async_client, db_session):
    """risk_level=1 工具自动执行，SSE 事件序列：thinking → tool_executing → message"""
    ...

@pytest.mark.asyncio
async def test_react_high_risk_triggers_confirm_request(async_client, db_session):
    """risk_level=2 工具触发 confirm_request SSE，不自动执行"""
    ...

@pytest.mark.asyncio
async def test_tool_audit_log_written_after_execution(async_client, db_session):
    """工具执行后 tool_audit_log 表有对应记录"""
    ...
```

Step 3：SSE 事件收集辅助函数
编写 `collect_sse_events(response)` 辅助函数，将 SSE 流解析为事件列表：
`[{"type": "thinking", "data": {...}}, {"type": "tool_executing", ...}, ...]`
便于在测试中 assert 顺序和内容。

Step 4：patch 外部依赖
使用 `patch.object(SCPAdapter, 'execute_tool', ...)` 方式 mock，
避免改动生产代码中的依赖注入方式。
mock AuditService.log_tool_call 记录调用次数，用于验证写入行为。

Step 5：DB 记录验证
```python
# 验证 tool_audit_log 写入
from backend.conversation_service.app.models.tool_audit_log import ToolAuditLog
result = await db_session.execute(select(ToolAuditLog).where(...))
assert result.scalar_one_or_none() is not None
```

【约束】
- 所有测试均为 pytest-asyncio，不使用 unittest.TestCase
- 不启动真实 SCP 或 SSH 连接，全部 mock
- 不修改生产代码（只读 react_executor.py 等）
- 中文注释

【验收标准】
- [ ] `uv run pytest backend/conversation-service/tests/integration/test_react_e2e.py -v` 
      全部通过（≥3个测试用例）
- [ ] 测试覆盖三个场景：低风险自动执行 / 高风险 confirm_request / audit_log 写入
- [ ] 无 import 错误，无 async fixture 泄漏
- [ ] `uv run pytest backend/conversation-service/tests/ -q` 整体测试不降级
```

---

## Task T34：上下文可视化（Admin UI）（P2）

```
你是一名负责 hci-troubleshoot-platform 后端 API + 前端 Admin 的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
当前上下文可视化的数据层已就绪，但 API 和 UI 完全缺失：

【已有（后端数据层）】
- `prompt_audit` 表（case-service 数据库）
  字段：conversation_id, case_id, assistant_type, model, message_count,
        has_sop, kb_chunks_count, kb_top_score, system_prompt_chars,
        messages (JSONB, 10% 采样), captured_at
  写入：conversation_service._write_prompt_audit()，fire-and-forget
  消费：quality_score.py 内部使用，无对外 API

- `tool_audit_log` 表（conversation-service 数据库）
  已有 GET /api/v1/audit-logs 只读 API（audit.py，87行）
  支持 session_id / tool_name / risk_level 过滤

【缺失（本任务要补齐）】
1. case-service：GET /api/v1/cases/{case_id}/prompt-audit — 返回该工单的 prompt_audit 记录列表
2. Admin 前端 CaseDetailView.vue：新增"AI 上下文"标签页，展示：
   - has_sop（是否命中 SOP）、kb_chunks_count（KB 命中数）、kb_top_score（最高相似度）
   - system_prompt_chars（发给 AI 的 system prompt 字符数趋势）
   - message_count（对话轮数）
3. Admin 前端 MonitoringView.vue 或 CaseDetailView.vue：工具调用时间线
   - 调用哪个工具、参数（脱敏）、风险级别、耗时、是否成功

【任务目标】
1. 在 case-service 添加 GET /api/v1/cases/{case_id}/prompt-audit 端点
2. 在 CaseDetailView.vue 新增"AI 上下文"标签页，展示 prompt_audit 关键字段
3. 复用现有 GET /api/v1/audit-logs 端点，在 CaseDetailView.vue 中展示工具调用时间线
4. 数据安全：messages JSONB 字段（含完整对话）只在 admin 界面展示，不暴露给 customer 前端

【涉及服务 / 文件范围】
- backend/case-service/app/routes/cases.py（添加 prompt-audit 端点）
- backend/case-service/app/models/prompt_audit.py（只读，了解字段）
- backend/conversation-service/app/routes/audit.py（只读，复用工具调用 API）
- frontend/admin/src/views/CaseDetailView.vue（添加 AI 上下文标签页）
- frontend/admin/src/views/MonitoringView.vue（可选：工具调用看板）
- frontend/shared/src/api.ts（添加 fetchPromptAudit API 调用函数）

【详细实现步骤】

Step 1：对齐文档与实现
- 阅读 backend/case-service/app/models/prompt_audit.py，确认字段定义
- 阅读 backend/case-service/app/routes/cases.py，了解现有路由风格
- 阅读 backend/conversation-service/app/routes/audit.py，了解工具调用 API 格式
- 阅读 frontend/admin/src/views/CaseDetailView.vue，找到现有标签页结构

Step 2：case-service 新增 API 端点
```python
# backend/case-service/app/routes/cases.py 中新增：
@router.get("/{case_id}/prompt-audit")
async def get_prompt_audit(
    case_id: str,
    limit: int = Query(20, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),  # 仅 admin 可访问
) -> dict:
    """获取工单的 prompt_audit 记录列表（AI 上下文快照）"""
    ...
```

返回格式：
```json
{
  "case_id": "C-20260325-0001",
  "total": 15,
  "records": [
    {
      "audit_id": "...",
      "conversation_id": "...",
      "has_sop": true,
      "kb_chunks_count": 3,
      "kb_top_score": 0.84,
      "system_prompt_chars": 2800,
      "message_count": 6,
      "captured_at": "2026-03-25T10:00:00Z"
      // 注意：messages JSONB 字段**不在默认响应中**，另设 include_messages=true 查询参数
    }
  ]
}
```

Step 3：前端 Admin — CaseDetailView.vue 新增"AI 上下文"标签页
Tab 内容分两栏：
- 左栏：字段列表（has_sop、kb_chunks_count 等），最新一条记录
- 右栏：system_prompt_chars 折线图（多次对话趋势，用 inline SVG 或简单 div 高度表示）

工具调用时间线（复用 /api/v1/audit-logs?session_id=xxx）：
- 每行：[工具图标] 工具名 | 风险 | 耗时 | 状态（✅/❌）| 时间
- 展开行可见 tool_args（param 脱敏：密码类字段替换为 ***）

Step 4：共享 API 函数
在 frontend/shared/src/api.ts 中添加：
```typescript
export const createPromptAuditApi = (client: ApiClient) => ({
  listByCaseId(caseId: string, params?: { limit?: number; offset?: number }) {
    return client.get(`/api/v1/cases/${caseId}/prompt-audit`, { params })
  }
})
```

【约束】
- messages JSONB 字段仅在 include_messages=true 时返回，需要额外权限校验（防止数据泄露）
- 工具参数中 password / key / token 类字段在前端显示前脱敏（替换为 ***）
- 不修改 customer 前端（这是 admin-only 功能）
- 代码注释使用中文

【验收标准】
- [ ] `GET /api/v1/cases/C-xxx/prompt-audit` 返回 200 + 正确 JSON
- [ ] Admin 界面 CaseDetailView 出现"AI 上下文"标签页
- [ ] 标签页展示 has_sop、kb_chunks_count、kb_top_score、system_prompt_chars
- [ ] 工具调用时间线展示（至少显示工具名、风险、状态）
- [ ] 参数中密码类字段被脱敏（*** 显示）
- [ ] `uv run pytest backend/case-service/tests -q` 通过
- [ ] `pnpm --filter @hci/admin build` 构建通过
```

---

## Task T35：代码重构 — 独立可测试模块（P3）

```
你是一名负责 hci-troubleshoot-platform 代码质量的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
当前 conversation_service.py 有约 1050+ 行，三块逻辑内联其中应提取为独立模块：

1. 知识检索逻辑（当前位置：_retrieve_knowledge() 及相关代码）
   目标：独立为 knowledge_retriever.py，可单独单测
   
2. 诊断状态 Pydantic 模型（当前：散落在 conversation_manager.py 中的字典）
   目标：DiagnosticSession / StageTransition Pydantic dataclass 集中到 diagnostic_state.py

3. 对话工具（当前：confirm_service.py 提供 Redis 确认，但 ask_user() 逻辑缺失）
   目标：dialog_tools.py 实现 ask_user() 工具，可被 ReactExecutor 调用

已实现参考（不要重复）：
- backend/conversation-service/app/services/confirm_service.py（Redis BRPOP，96行）
- backend/conversation-service/app/core/tool_registry.py（工具注册表，321行）

【任务目标】
1. 从 conversation_service.py 提取知识检索逻辑到 knowledge_retriever.py，不改变行为
2. 在 diagnostic_state.py 定义 DiagnosticSession / StageTransition Pydantic 模型
3. 在 dialog_tools.py 实现 ask_user() 工具，注册到 tool_registry
4. conversation_service.py 调用层改用新模块，行数目标：<900行
5. 所有新模块有独立单元测试

【涉及服务 / 文件范围】
- backend/conversation-service/app/services/conversation_service.py（重构来源）
- backend/conversation-service/app/services/knowledge_retriever.py（新建）
- backend/conversation-service/app/models/diagnostic_state.py（新建）
- backend/conversation-service/app/adapters/dialog_tools.py（新建）
- backend/conversation-service/app/core/tool_registry.py（只读，了解 ask_user 注册方式）
- backend/conversation-service/tests/unit/（新增单测文件）

【详细实现步骤】

Step 1：分析当前内联代码
- grep -n "_retrieve_knowledge\|_build_system_prompt\|_select_knowledge" conversation_service.py
- 识别知识检索相关的私有方法（约 3-5 个），确认它们的输入输出
- 识别 conversation_manager.py 中散落的阶段状态数据类型

Step 2：knowledge_retriever.py 提取
```python
# backend/conversation-service/app/services/knowledge_retriever.py
class KnowledgeRetriever:
    """三轨知识检索：SOP → KB → 降级（完全从 conversation_service 提取，行为不变）"""
    
    async def retrieve(
        self,
        query: str,
        case_id: str,
        stage: str = "S0"
    ) -> KnowledgeResult:
        """返回：has_sop, sop_content, kb_chunks, kb_top_score, fallback_used"""
        ...
```
提取后在 conversation_service.py 中改为 `self._knowledge_retriever.retrieve(...)` 调用。

Step 3：diagnostic_state.py Pydantic 模型
```python
# backend/conversation-service/app/models/diagnostic_state.py
from pydantic import BaseModel

class StageTransition(BaseModel):
    from_stage: str
    to_stage: str
    triggered_by: str    # "llm_output" | "tool_result" | "user_input"
    confidence: float = 1.0

class DiagnosticSession(BaseModel):
    conversation_id: str
    current_stage: str = "S0"
    transitions: list[StageTransition] = []
    hypotheses: list[str] = []
    confirmed_facts: list[str] = []
```

Step 4：dialog_tools.py — ask_user() 工具
```python
# backend/conversation-service/app/adapters/dialog_tools.py
# 注册到 tool_registry 的 ask_user 工具：
# - risk_level: 1（无需授权）
# - 向 SSE 流发出一条 user_question 事件
# - 将 question 写入 Redis 并 await confirm_service 等待用户回复
```

Step 5：单元测试（每个新模块一个测试文件）
- tests/unit/test_knowledge_retriever.py：mock kb/sop 服务，测三轨检索
- tests/unit/test_diagnostic_state.py：Pydantic 模型序列化/反序列化
- tests/unit/test_dialog_tools.py：mock Redis，测 ask_user 事件发出

【约束】
- 重构必须是纯提取（行为不变），不允许修改功能逻辑
- 如果无法保证行为等价，停止并报告，不强行重构
- 新模块的导入不产生循环依赖（用 `python -c "from app.services.knowledge_retriever import KnowledgeRetriever"` 验证）
- 代码注释使用中文

【验收标准】
- [ ] `uv run pytest backend/conversation-service/tests/ -q` 全部通过（包含新单测）
- [ ] `wc -l backend/conversation-service/app/services/conversation_service.py` 输出 < 900
- [ ] `python -c "from app.services.knowledge_retriever import KnowledgeRetriever"` 无报错
- [ ] `python -c "from app.models.diagnostic_state import DiagnosticSession"` 无报错
- [ ] `make lint` 通过（无新增 linter 错误）
```

---

## 任务依赖关系

```
T31 (P0 Helm配置) ─→ 生产环境 ReactExecutor 激活（其他任务可并行）
T32 (P1 前端进度条) ─┐
T33 (P2 集成测试)   ─┤→ 均可与 T31 并行开发（T33 不依赖 T32）
T34 (P2 上下文可视化) ─┤
T35 (P3 代码重构)   ─┘
```

**推荐执行顺序**：T31 → T34 → T32 → T33 → T35  
（T31 最高优先级；T34 数据已就绪直接做；T32/T33 独立；T35 最低风险最后）

---

## 关联文档

| 文档 | 用途 |
|------|------|
| [05_AI助手层设计.md](../architecture/05_AI助手层设计.md) | 架构决策背景，理解 WHY |
| [08_HCI平台效果差距分析与重构方案.md](../architecture/08_HCI平台效果差距分析与重构方案.md) | 目标系统设计，理解 WHAT |
| [11_完整技术方案.md](../architecture/11_完整技术方案.md) | 实施规范与当前进度，理解 HOW |
| [docs/archive/19_任务编排归档.md](../archive/19_任务编排归档.md) | 历史已完成任务归档 |
