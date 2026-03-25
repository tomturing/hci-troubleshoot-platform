<!--
  分片标识：新 11_完整技术方案.md（实施规范 HOW）— 第 3/5 部分
  内容来源：原 11 第 265–560 行（Phase 3实施细节 + 风险 + 测试 + 里程碑 + 附录）
  合并目标：新 11 文档第三段
  说明：保持原文，无删减；进度快照（§十一）拆到 11_new_part05.md
-->

## 六、Phase 3：ReAct + 工具接入（实施状态刷新 2026-03-25）

> **重要说明**：Phase 3 核心代码（ReAct 引擎、工具层、确认机制）已于 2026-03-25 前完成编写。
> 当前剩余差距主要集中在**部署配置**和**前端体验**两个层面，而非代码开发。
> 见 §十一 实施进度快照。

### 6.1 已实现文件清单（实际路径）

```
backend/conversation-service/app/
  ├── core/                                       ← Phase 3 核心引擎
  │   ├── react_executor.py    ✅ 264行  ReAct 推理→工具→确认→审计循环
  │   ├── glm_client.py        ✅ 169行  GLM 格式适配 + JSON 修复 + 指数退避
  │   └── tool_registry.py     ✅ 321行  15 个工具注册（12个 Level-1 + 3个 Level-2）
  ├── adapters/                                   ← 工具执行后端
  │   ├── scp_adapter.py       ✅ 241行  4 个 SCP REST API 工具
  │   ├── acli_adapter.py      ✅ 186行  11 个 acli 只读/写工具（SSH 执行）
  │   └── tool_router.py       ✅ 57行   按 category 路由到 SCP/acli 适配器
  └── services/
      ├── prompt_builder.py    ✅ 194行  5段式动态 Prompt（阶段感知）
      ├── confirm_service.py   ✅ 96行   Redis BRPOP 阻塞确认（120s 超时）
      ├── audit_service.py     ✅ 80行   tool_audit_log 写入
      └── conversation_service.py  → 已新增 send_message_react_stream() 方法

frontend/customer/src/
  ├── components/ConfirmDialog.vue  ✅  高风险操作确认弹窗（含参数展示）
  └── stores/chat.ts               ✅  处理 confirm_request / tool_executing / thinking 事件

database/
  ├── init_schema.sql              ✅  conversation 表含 diagnostic_stage / hypothesis / react_state
  ├── migrate_tool_audit_log.sql   ✅  tool_audit_log 表迁移脚本
  └── migrate_p4_v1.sql            ✅  raw_cases / knowledge_atoms 表
```

**尚未实现的规划文件**（详见 §十一）：
```
backend/conversation-service/app/
  ├── services/knowledge_retriever.py  ❌  三级知识检索（逻辑目前内联在 conversation_service.py）
  ├── models/diagnostic_state.py       ❌  DiagnosticSession Pydantic 模型（字段散落在现有模型）
  └── adapters/dialog_tools.py         ❌  ask_user() / confirm_action() 对话工具

frontend/customer/src/
  ├── components/DiagnosticProgress.vue  ❌  S0→S6 诊断阶段进度条
  └── (chat.ts 里缺 stage_change 事件分支)  ❌
```

### 6.2 API 接口变更（已实现状态）

**现有接口（保持不变，向后兼容）**：
```
POST /api/v1/conversations/{case_id}/messages
GET  /api/v1/conversations/{case_id}/history
```

**已实现的新增接口**：
```
# 人工确认回调（前端确认弹窗后调用）✅
POST /api/v1/conversations/{session_id}/confirm
Body: { "confirmed": true/false }
Response: { "status": "ok", "confirmed": true }
```

**待实现的接口**：
```
# 当前诊断状态独立接口（前端进度条用）❌
GET  /api/v1/conversations/{session_id}/diagnostic-state
Response: { "stage": "S3", "hypothesis": [...], "tools_called": [...] }
```

**SSE 事件格式（已实现）**：
```
{"content": "文本片段"}                                 ← 普通 token
{"type": "thinking", "step": 1, "message": "..."}      ← ReAct 推理步骤
{"type": "tool_executing", "tool": "...", "args": {}}   ← 工具执行通知
{"type": "confirm_request", "tool_name": "...", ...}    ← 高风险操作确认请求
```

**待添加的 SSE 事件**（前端 stage_change 处理缺失）：
```
{"type": "stage_change", "from": "S2", "to": "S3"}     ← 后端已发出，前端未处理
```

### 6.3 工具集实际覆盖（2026-03-25）

**SCP REST API 工具（4个，Level-1 只读）**：
```
get_active_alerts     → SCP GET /api/v1/alerts（支持 limit 参数）
get_failed_tasks      → SCP GET /api/v1/tasks?status=FAILED（支持时间/类型过滤）
get_vm_list           → SCP GET /api/v1/vms（支持名称模糊搜索）
get_cluster_detail    → SCP GET /api/v1/clusters/{id}
```

**acli 工具（11个）**：
```
# Level-1 只读（8个）
acli_system_top          → acli system top
acli_vm_list             → acli vm list
acli_vm_config           → acli vm config get <vm_id>
acli_vm_disk_check       → acli vm disk check <vm_id>
acli_platform_node_list  → acli platform node list
acli_storage_disk_list   → acli storage asan disk list
acli_network_nic_list    → acli network nic list
acli_log_get             → acli log get --lines N（上限500）

# Level-2 写操作（3个，需用户确认）
acli_service_restart     → acli service <name> restart
acli_network_nic_up      → acli network nic up <nic>
acli_netdoctor           → acli plugin netdoctor [target_ip]
```

**未实现（Phase 3 后期接入）**：
```
vm_power_on              → 高危，Level-2，待人工授权机制稳定后接入
vm_migrate               → 高危，Level-2，同上
```

### 6.4 人工确认流程时序图

```
前端                   API Gateway          Conversation Service       Redis
 │                         │                       │                     │
 │ POST /messages           │                       │                     │
 │──────────────────────────►│                       │                     │
 │                         │ 转发                    │                     │
 │                         │────────────────────────►│                     │
 │                         │                       │ ReAct 推理...         │
 │◄──── SSE: token ─────────│◄──────────────────────│                     │
 │◄──── SSE: tool_start ────│◄──────────────────────│                     │
 │                         │                       │ 发现 Level 2 工具    │
 │◄──── SSE: confirm_request│◄──────────────────────│                     │
 │                         │                       │ BRPOP 阻塞等待...   │
 │                         │                       │─────────────────────►│
 │ [用户看到确认弹窗]        │                       │                     │
 │ POST /confirm (confirmed=true)                   │                     │
 │──────────────────────────►│                       │                     │
 │                         │ 转发                    │                     │
 │                         │────────────────────────►│ RPUSH confirm:xxx  │
 │                         │                       │─────────────────────►│
 │                         │                       │ BRPOP 返回           │
 │                         │                       │◄─────────────────────│
 │                         │                       │ 执行工具...           │
 │◄──── SSE: tool_result ───│◄──────────────────────│                     │
 │◄──── SSE: final ─────────│◄──────────────────────│                     │
```

---

## 七、关键风险与应对

### 7.1 技术风险

| 风险 | 概率 | 影响 | 应对方案 |
|------|------|------|---------|
| GLM function calling 格式不稳定 | 中 | 高 | `GLMClient._repair_json()`容错 + 详细错误日志 |
| SCP API 不稳定 / 超时 | 中 | 中 | `tenacity` 重试 + 超时 30s + 降级提示 |
| ReAct 无限循环 | 低 | 高 | `MAX_STEPS=15` 硬限制 + 步数告警 |
| 人工确认超时 | 中 | 低 | `CONFIRM_TIMEOUT=1800s`，超时返回 cancelled |
| Prompt 过长（上下文超限） | 中 | 中 | `ConversationManager.compress_if_needed()` |
| acli 命令执行副作用 | 低 | 极高 | Phase 3 晚期才接入 Level 2，先稳定 Level 1 |

### 7.2 迁移风险（现有功能不降级）

```
Phase 0-2：现有 conversation_service.py 逻辑保持，仅修改 Prompt
Phase 3：新增 ReactExecutor，通过 feature flag 控制：
  if settings.ENABLE_REACT_MODE:
      return await react_executor.run(session, user_input)
  else:
      return await legacy_conversation_service.run(session, user_input)
```

**Feature Flag 设计**：
```python
# deploy/env/platform.env
ENABLE_REACT_MODE=false          # 初始关闭，灰度打开

# 灰度策略：先对内部测试账号开启
REACT_MODE_USER_WHITELIST=admin,test-user-1
```

---

## 八、测试验证方案

### 8.1 Phase 0 验证（Prompt 修改后）

**测试用例 1：空知识库下应该能推理**
```
输入：虚拟机开不了机
期望：LLM 给出基于 HCI 机制知识的初步分析（不是"知识库无内容"）
通过标准：回复包含至少2个可能的根因方向
```

**测试用例 2：告警驱动的结构化输入**
```
输入：平台告警：STORAGE_FULL，node-01，使用率 96%
期望：直接跳到 S3 验证，不再追问"告警是什么"
通过标准：LLM 询问存储详情或尝试调用工具查询
```

### 8.2 Phase 3 验证（ReAct 接入后）

**端到端测试流程**：
```
1. 创建工单（模拟告警：VM_BOOT_FAIL）
2. 发送消息："虚拟机 vm-test-001 无法开机，管理台显示 Error 状态"
3. 验证 SSE 事件序列：
   - token（LLM 思考过程）
   - tool_start（get_vm_status）
   - tool_result（vm 状态数据）
   - token（分析结果）
   - stage_change（S3 → S4）
   - final（根因结论）
4. 查看 tool_audit_log 表，确认调用记录已写入
```

**人工确认测试**：
```
1. 诊断到需要执行 vm_power_on 步骤
2. 验证前端收到 confirm_request 事件
3. 确认弹窗显示正确（工具名、参数、风险警告）
4. 点击取消 → 验证 VM 未实际操作，审计日志记录 cancelled
5. 重新触发 → 点击确认 → 验证 VM 确实执行了操作，审计日志记录 authorized_by=user_id
```

---

## 九、里程碑与交付物（状态刷新 2026-03-25）

| 里程碑 | 计划 | 状态 | 交付物 | 验收标准 |
|-------|------|------|--------|---------|
| M0: Prompt 修复 | 第 1 周 | ✅ 已完成 | `conversation_service.py` Prompt 重写 | 空知识库下 GLM 能做初步诊断 |
| M1: 知识库上线 | 第 3 周 | ✅ 已完成 | KB Service 运行 + SOP 入库 | 告警相关问题返回相关 SOP |
| M2: 状态机 | 第 5 周 | ✅ 已完成 | 会话状态 diagnostic_stage + 阶段 Prompt | 对话沿 S0→S4 有序推进 |
| M3: ReAct 核心 | 第 8 周 | ✅ 代码完成 | ReactExecutor + GLMClient + ToolRouter + ConfirmService | 需解锁部署配置后才能端测 |
| M3-deploy: **部署解锁** | 当前 | **❌ 待做** | Helm deployment.yaml 注入 `REACT_ENABLED` / `SCP_BASE_URL` / `HCI_SSH_*` | 线上 ReactExecutor 被激活 |
| M3-fe: **前端进度** | 当前 | **❌ 待做** | `stage_change` 事件处理 + DiagnosticProgress 组件 | 诊断阶段对用户可见 |
| M3-test: **集成测试** | 当前 | **❌ 待做** | ReAct 端到端集成测试 | SSE 事件序列可验证 |
| M4: 操作工具 | 第 10 周 | ⏳ 计划中 | vm_power_on + vm_migrate + 授权审计可观测 | 授权操作端到端可审计 |
| M5: 监控看板 | 持续 | ⏳ 计划中 | tool_audit_log Grafana 面板 | 工具调用可监控 |

---

## 十、附录：目录结构（实际状态 2026-03-25）

### 已实现文件

```bash
backend/conversation-service/app/
  core/
    react_executor.py    ✅  ReAct 执行器（264行）
    glm_client.py        ✅  GLM 接入层（169行）
    tool_registry.py     ✅  工具注册表，15个工具（321行）
  adapters/
    scp_adapter.py       ✅  SCP 工具（241行，4个工具）
    acli_adapter.py      ✅  acli 工具（186行，11个命令）
    tool_router.py       ✅  工具路由（SCP + acli dispatch）
  services/
    prompt_builder.py    ✅  5段式 Prompt 构建（194行）
    confirm_service.py   ✅  Redis BRPOP 确认（96行）
    audit_service.py     ✅  审计日志写入（80行）
    conversation_service.py  ✅  已集成 send_message_react_stream()

frontend/customer/src/
  components/
    ConfirmDialog.vue    ✅  高风险操作确认弹窗
  stores/
    chat.ts              ✅  confirm_request / tool_executing / thinking 事件

database/
  init_schema.sql                ✅  含 tool_audit_log + diagnostic_stage
  migrate_tool_audit_log.sql     ✅  tool_audit_log 增量迁移
  migrate_p4_v1.sql              ✅  raw_cases + knowledge_atoms
```

### 待实现文件

```bash
# 后端独立模块（目前逻辑内联，重构为独立模块）
backend/conversation-service/app/
  services/knowledge_retriever.py   ❌  三级知识检索独立模块
  models/diagnostic_state.py        ❌  DiagnosticSession Pydantic 模型
  adapters/dialog_tools.py          ❌  ask_user() / confirm_action()

# 前端诊断体验
frontend/customer/src/
  components/DiagnosticProgress.vue ❌  S0→S6 阶段进度条
  (stores/chat.ts 需追加 stage_change 事件分支)

# 部署配置
deploy/helm/hci-platform/templates/conversation-service/deployment.yaml
  → 需注入 REACT_ENABLED / SCP_BASE_URL / SCP_API_KEY / HCI_SSH_* 环境变量

deploy/gitops/env-repo-template/
  → hci-platform-env 仓库，values.yaml 需添加 reactEnabled / scpBaseUrl / hciSsh / scpApiKey

# 测试
backend/conversation-service/tests/integration/test_react_e2e.py  ❌  端到端集成测试
```
