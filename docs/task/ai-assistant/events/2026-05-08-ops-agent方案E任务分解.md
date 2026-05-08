---
status: active
category: task
audience: developer
last_updated: 2026-05-08
owner: platform-team
---

# ops-agent 方案E：任务分解

> 关联方案：[2026-05-08-ops-agent方案E-ACP-REST接口设计与实现.md](../../../solution/ai-assistant/events/2026-05-08-ops-agent方案E-ACP-REST接口设计与实现.md)  
> 关联分析：[2026-05-08-ops-agent交互性缺陷根因分析与方案对比.md](../../../solution/ai-assistant/events/2026-05-08-ops-agent交互性缺陷根因分析与方案对比.md)

---

## 前置任务：运维问题修复（立即，优先级最高）

> **目标**：修复当前 ops-agent-service 不可达的运维根因，使方案C（现行无交互模式）可以正常工作。  
> 这是方案E开发的前提条件，也是独立的可验证里程碑。

### T0-1：✅ 已完成 — ops-agent-service OOMKilled 修复

- **仓库**：hci-platform-env
- **根因（已确认）**：
  - `kubectl describe pod ops-agent-service-6684cb6fc5-ms4f9 -n hci-dev`
  - `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`
  - 内存 limit 512Mi 不足，Agent 首次推理峰值 >512Mi
- **修复（已推送）**：
  - `hci-platform-env/environments/dev/values.yaml`：memory limit 512Mi→2Gi，CPU limit 500m→2000m
  - commit `d389052` 推送至 `tomturing/hci-platform-env` main，ArgoCD 自动同步
- **次要问题（非致命）**：`Failed to export span batch` — OTEL collector `http://hci-platform-otel-collector:4318` 不可达，仅丢失 span，不影响功能
- **验收标准**：
  - [x] `kubectl describe pod` 确认 `Reason: OOMKilled`（已确认）
  - [x] memory limit 调整为 2Gi 并推送（已完成）
  - [ ] Pod 重启后 `kubectl get pod` 显示 `Running 1/1`，RESTARTS 不再增加
  - [ ] 选择 Ops-Agent 助手发消息，前端可收到文本回复（方案C验证）

---

## Phase 1：ops-agent 侧 HTTP 包装层（仓库：ops-agent）

> **目标**：在 `ops_agent/server/` 中新增 ACP REST 接口，**完全不修改 `acp/` 目录**，保障 Streamlit 和其他现有接入系统不受影响。
> **依赖**：T0-1（需先确认运行时环境正常）  
> **预计总工作量**：2-3 天

---

> ⚠️ **设计原则（第一性原理修正，2026-05-08）**
>
> 原计划的 T-E1（ACPSession asyncio 化）、T-E2（ACPConsole async 化）、T-E3（session_prompt asyncio.create_task）**均已废除**。
> 原因：Streamlit 从非 asyncio 线程同步调用 `submit_client_response()` 和 `drain_outbox()`；将 `threading.Event/Queue` 改为 asyncio 对等物会产生线程安全问题，破坏现有 Streamlit 集成。
>
> **正确方案**：HTTP 路由层通过 `threading.Thread`（投递 prompt）+ `asyncio.to_thread`（轮询 drain_outbox）
> 桥接同步与异步，`acp/` 目录完全不变，与 Streamlit 的集成模式完全一致。

---

### T-E1：session_new() 增加可选 session_id 参数（`acp/server.py`）

- **文件**：`ops_agent/acp/server.py`
- **描述**：
  - 为 `session_new()` 增加可选 `session_id: str | None = None` 参数
  - 调用方传入时直接使用；不传时沿用原有 `f"sess_{secrets.token_hex(8)}"` 逻辑
  - **改动范围**：约 3 行，完全向后兼容
- **必要性**：⚠️ 可选。允许 htp 传入 `conversation_id` 作为 session ID，消除 htp 内部维护 ID 映射表的需求。不实现此改动则 htp 侧需额外维护映射。
- **对其他系统的影响**：
  - Streamlit：`session_new()` 不传 `session_id` 参数，行为与之前完全相同 ✅
  - CLI 模式：不使用 ACPServer，无影响 ✅
- **验收标准**：
  - [ ] `server.session_new(session_id="my-custom-id")` 返回 `{"sessionId": "my-custom-id", ...}`
  - [ ] `server.session_new()`（不传参数）行为与原来完全一致
  - [ ] Streamlit UI 正常运行（`ops-agent serve` 命令）
- **预计耗时**：0.5 小时

---

### T-E2：新增 ACP HTTP 路由（`server/acp_routes.py` 新建）

- **文件（新建）**：`ops_agent/server/acp_routes.py`
- **描述**：
  - 纯新增文件，**不修改 `acp/` 中任何文件**
  - 实现 5 个 REST 端点（详见方案文档）：
    - `POST /acp/sessions`（调用 `server.session_new()`，同步，极快）
    - `POST /acp/sessions/{id}/prompt`（在 `threading.Thread` 中运行 `session_prompt()`，立即返回 202）
    - `GET /acp/sessions/{id}/events`（SSE：`asyncio.to_thread(drain_outbox)` + 50ms 轮询）
    - `POST /acp/sessions/{id}/responses/{req_id}`（调用 `submit_client_response()`，同步，极快）
    - `GET /acp/sessions/{id}/state`（调用 `get_session_state()`，同步，极快）
  - 暴露 `init_acp_server(server)` 供 `main.py` 注入 ACPServer 实例
- **必要性**：✅ 必须
- **对其他系统的影响**：
  - 纯新增文件，对所有现有模块零侵入 ✅
  - Streamlit 使用 `ACPServer` in-process，不经过 HTTP，无影响 ✅
  - CLI 模式不使用 ACPServer，无影响 ✅
- **验收标准**：
  - [ ] `POST /acp/sessions` 返回 201 + sessionId
  - [ ] `POST /acp/sessions/{id}/prompt` 在 200ms 内返回 202（Agent 尚未完成）
  - [ ] `GET /acp/sessions/{id}/events` SSE 流可接收 `agent_message_chunk` 事件
  - [ ] `POST /acp/sessions/{id}/responses/{req_id}` 可提交响应
  - [ ] 重复 prompt 同一 session → 返回 409
- **预计耗时**：1 天

---

### T-E3：main.py 注册路由并初始化 ACPServer

- **文件**：`ops_agent/server/main.py`
- **描述**：
  - 在 `lifespan` 中初始化 `ACPServer` 并调用 `init_acp_server()`
  - `app.include_router(acp_router)`
  - **改动范围**：约 10 行新增代码，不修改任何现有逻辑
- **必要性**：✅ 必须
- **对其他系统的影响**：
  - 现有 `/v1/chat/completions` 接口完全不变 ✅
  - 现有 `/health` 接口完全不变 ✅
- **验收标准**：
  - [ ] `GET /openapi.json` 可见 `/acp/*` 接口定义
  - [ ] 现有 `curl /v1/chat/completions` 调用不受影响
- **预计耗时**：0.5 天

---

---

## Phase 2：htp 侧 ACP 客户端改造（仓库：hci-troubleshoot-platform）

> **目标**：将 conversation-service 改造为 ACP 协议客户端，支持完整的交互事件循环。  
> **依赖**：T-E3（ops-agent ACP REST 接口就绪）  
> **预计总工作量**：4-5 天

---

### 第一性原理分析：htp 侧改动必要性（2026-05-08）

| 任务 | 文件 | 改动性质 | 是否必须 | 影响分析 |
|------|------|---------|---------|---------|
| T-E5：BrainInteractiveRequest 类型 | `brain_port.py` | 纯新增 dataclass + 更新联合类型 | ✅ 必须（T-E6 依赖） | 零影响：旧 BrainTextChunk/BrainStageUpdate/BrainEscalation 完全不变 |
| T-E6：OpsAgentBrainAdapter ACP 化 | `ops_agent_brain_adapter.py` | 内部实现替换（接口签名不变） | ✅ 必须（核心切换点） | `process()` 签名 100% 不变，BrainRouter/ConversationService 无需改动；BrainUnavailableError 降级路径不变 |
| T-E7：ConversationService 处理交互事件 | `conversation_service.py` | 新增 isinstance 分支 + 方法 | ⚠️ 渐进式必须 | Phase1（仅文本）：BrainInteractiveRequest 静默丢弃，文字回复不受影响；Phase2（完整交互）：必须实现 |
| T-E8：/interactive-response 路由 | `conversations.py` | 纯新增路由 | ⚠️ 渐进式必须（同 T-E7） | 新增端点，对现有路由零影响 |

**第一性原理推导**：
- T-E5/T-E6 是 **必须一起实施**的最小集合，实现后即可获得"ops-agent 正常文字回复"的核心价值
- T-E7/T-E8 构成完整交互闭环，可在 T-E5/T-E6 验证通过后作为独立迭代实施
- 三个系统保护点（均不需改动）：
  1. `BrainRouter`：`process()` 签名不变，降级逻辑不变 ✅
  2. `HTPBrainAdapter`：完全没有涉及 ✅
  3. `BrainPort Protocol`：只扩展 BrainEvent 联合类型，不改变 process() 约束 ✅

---

### T-E5：✅ 已完成 — brain_port.py 新增 BrainInteractiveRequest

- **文件**：`backend/conversation-service/app/core/brain_port.py`
- **必要性**：✅ T-E6 的前提，纯新增零风险
- **实现内容**：新增 `BrainInteractiveRequest` dataclass，更新 `BrainEvent` 联合类型定义
- **验收标准**：
  - [x] `from app.core.brain_port import BrainInteractiveRequest` 无导入错误
  - [x] `BrainEvent` 类型包含 `BrainInteractiveRequest`

---

### T-E6：✅ 已完成 — OpsAgentBrainAdapter 重构为 ACP 客户端

- **文件**：`backend/conversation-service/app/adapters/ops_agent_brain_adapter.py`
- **必要性**：✅ 核心切换点，从 `/v1/chat/completions`（无交互）→ ACP REST（完整交互）
- **实现内容**：
  - 保留 `process()` 接口签名（BrainPort 兼容，BrainRouter/ConversationService 无需改动）
  - ACP session ID 直接使用 htp `conversation_id`（利用 T-E1 可选参数，无需维护 ID 映射表）
  - `_ensure_acp_session()` ← `POST /acp/sessions`（幂等，已存在则返回）
  - `_submit_prompt()` ← `POST /acp/sessions/{id}/prompt`
  - `_consume_events()` ← `GET /acp/sessions/{id}/events`，翻译为 BrainEvent
  - 新增 `submit_acp_response()` 方法 ← `POST /acp/sessions/{id}/responses/{req_id}`（T-E7 使用）
- **验收标准**：
  - [x] `process()` 接口签名与 BrainPort 兼容（验证通过）
  - [ ] 发送消息后可以收到 BrainTextChunk 事件流（文本内容）
  - [ ] 触发 SOP 操作卡时，收到 BrainInteractiveRequest 事件
  - [ ] ops-agent-service 不可达时，raise BrainUnavailableError（降级机制不变）

---

### T-E7：ConversationService 处理 BrainInteractiveRequest

- **文件**：`backend/conversation-service/app/services/conversation_service.py`
- **必要性**：⚠️ Phase2 必须。Phase1 可跳过（BrainInteractiveRequest 静默丢弃，文字响应正常）
- **描述**：
  - 在 `send_message_stream_only()` 的事件分发 `async for brain_event` 循环中，增加：
    ```python
    elif isinstance(brain_event, BrainInteractiveRequest):
        # 通过 SSE 通知前端渲染交互 UI
        yield f"\x00event:interactive_request:{json.dumps({...})}\x00"
    ```
  - 新增 `submit_ops_agent_response()` 方法（委托给 `OpsAgentBrainAdapter.submit_acp_response()`）
- **影响分析**：
  - 现有 `BrainTextChunk` / `BrainStageUpdate` 处理路径：零影响（新增 elif 分支）
  - `BrainRouter._route_ops_agent()` 降级路径：零影响
  - htp 原有大脑（HTPBrainAdapter）：永远不会 yield BrainInteractiveRequest，零影响
- **验收标准**：
  - [ ] 收到 BrainInteractiveRequest 后，前端 SSE 可接收 `interactive_request` 事件
  - [ ] `submit_ops_agent_response()` 成功调用 ops-agent-service
- **预计耗时**：0.5 天

---

### T-E8：新增 /interactive-response 路由

- **文件**：`backend/conversation-service/app/routes/conversations.py`
- **必要性**：⚠️ Phase2 必须（依赖 T-E7）
- **描述**：
  新增端点 `POST /api/conversations/{conversation_id}/interactive-response`，
  校验 requestId / acpSessionId / outcome 格式，委托 ConversationService.submit_ops_agent_response() 处理。
- **影响分析**：纯新增路由，对现有路由（发消息、查询历史等）零影响
- **验收标准**：
  - [ ] 正确 outcome 格式返回 200
  - [ ] 无效 requestId 返回 404（透传 ops-agent-service 的 404）
  - [ ] 接口文档更新（openapi schema 可见）
- **预计耗时**：0.5 天

---

## Phase 3：前端交互 UI（仓库：hci-troubleshoot-platform frontend）

> **目标**：接收 `interactive_request` SSE 事件，渲染 SOP 操作卡片和用户提问组件。  
> **依赖**：T-E8（后端接口就绪）  
> **预计总工作量**：3-4 天

---

### T-E9：SSE 处理器新增 interactive_request 事件类型

- **文件**：前端 SSE 消费逻辑（具体路径待确认）
- **描述**：新增 `interactive_request` 事件的解析和分发
- **预计耗时**：0.5 天

---

### T-E10：SOP 操作卡片 UI 组件

- **描述**：根据 `interactive_request` 的 kind=="sop_step" 渲染卡片：
  - 展示：route（当前位置）/ operation_goal / execution_guidance / expected_result / feedback_request / risk_notice
  - 用户操作：点击 reply_options 中的选项，或输入自定义文本（customInput=true 时）
  - 提交后调用 `POST /api/conversations/{id}/interactive-response`
  - 提交后卡片显示为"已确认"状态（不可再次提交）
- **预计耗时**：2 天

---

### T-E11：用户提问组件 UI 组件

- **描述**：根据 `interactive_request` 的 kind=="info_request" 渲染提问组件：
  - 展示：问题文本 + 选项列表（支持多选项）
  - 用户操作：点击选项，或在 customInput=true 时输入自定义文本
  - 提交逻辑同 T-E10
- **预计耗时**：1 天

---

## 任务依赖关系

```
T0-1（运维修复，立即开始）
  │
  ▼
Phase 1（ops-agent 侧）
  T-E1 → T-E2 → T-E3 → T-E4
                              │
                              ▼
                   Phase 2（htp 侧）
                   T-E5 → T-E6 → T-E7 → T-E8
                                              │
                                              ▼
                                     Phase 3（前端）
                                     T-E9 → T-E10
                                          → T-E11
```

T-E1 / T-E2 / T-E3 之间存在顺序依赖，但 T-E5（brain_port.py 新增类型）可以与 Phase 1 并行开发。  
T-E10 和 T-E11 可并行。

---

## 里程碑

| 里程碑 | 完成条件 | 对应任务 | 状态 |
|-------|---------|---------|------|
| M0：运维恢复 | ops-agent 无交互模式正常回复 | T0-1 | ✅ 已修复推送 |
| M1：ACP REST 接口就绪（ops-agent 侧） | curl 测试 ACP REST 接口全部可用 | T-E1~T-E3 | ✅ 已实现 |
| M2：ACP 客户端对接（htp 侧文字回复） | 选 ops-agent 助手可收到文字回复（无交互卡片） | T-E5~T-E6 | ✅ 已实现（待集成验证） |
| M3：完整交互后端闭环 | SOP 操作卡事件流转，/interactive-response 可响应 | T-E7~T-E8 | ⏳ 待实现 |
| M4：完整交互端到端 | 前端展示操作卡，用户选择后 Agent 继续推理 | T-E9~T-E11 | ⏳ 待实现 |
