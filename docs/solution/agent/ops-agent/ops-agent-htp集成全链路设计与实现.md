# ops-agent ↔ HTP 集成全链路设计与实现

> 文档版本：2026-05-  
> 适用分支：`ops-agent/feature-hci`、`hci-troubleshoot-platform/feature/ops-agent-interactive-ui`  
> 关联设计文档：[events/2026-05-08-ops-agent方案E-ACP-REST接口设计与实现.md](../events/2026-05-08-ops-agent方案E-ACP-REST接口设计与实现.md)

---

## 目录

1. [背景与目标](#1-背景与目标)
2. [所有变更汇总](#2-所有变更汇总)
   - [ops-agent 侧变更](#21-ops-agent-侧变更)
   - [HTP 侧变更](#22-htp-侧变更)
3. [变更必要性 / 侵入性 / 一致性分析](#3-变更必要性--侵入性--一致性分析)
4. [完整数据流](#4-完整数据流)
   - [核心构件角色说明](#41-核心构件角色说明)
   - [正常请求-响应（无交互）数据流](#42-正常请求-响应无交互数据流)
   - [get_info_from_user_tool 交互数据流](#43-get_info_from_user_tool-交互数据流)
   - [present_sop_step_instruction_tool 交互数据流](#44-present_sop_step_instruction_tool-交互数据流)
5. [HTP ↔ ops-agent 交互调用步骤（对比 dev-pengwei ops_web）](#5-htp--ops-agent-交互调用步骤对比-dev-pengwei-ops_web)
   - [dev-pengwei（in-process Streamlit）模式](#51-dev-penweiin-process-streamlit模式)
   - [HTP（HTTP REST + Vue）模式](#52-htphttp-rest--vue模式)
   - [两种模式逐步对比](#53-两种模式逐步对比)
6. [开发环境验收测试](#6-开发环境验收测试)

---

## 1 背景与目标

### 问题

原 HTP 对接 ops-agent 采用 `/v1/chat/completions` OpenAI 兼容接口：

- 单向文本流，ops-agent 无法在中途暂停向用户提问
- 每轮对话都是新会话（无历史上下文）
- `get_info_from_user`、`present_sop_step_instruction` 两个交互工具的等待—响应流程**无法工作**

### 目标

升级为 **ACP REST 协议**，实现：

| 能力 | 原方案 | 新方案 |
|------|--------|--------|
| 文本流式输出 | ✅ | ✅（SSE）|
| 跨轮次上下文 | ❌ | ✅（ACP session 持久化）|
| Agent 暂停等待用户操作 | ❌ | ✅（`_ops/request_input`）|
| SOP 操作卡渲染 | ❌ | ✅（`InteractiveRequestCard.vue`）|
| ops-agent 不可达降级 | ✅ | ✅（`BrainUnavailableError`）|

---

## 2 所有变更汇总

### 2.1 ops-agent 侧变更

> 所在仓库：`ops-agent`，分支：`feature-hci`

#### T-E1：`session_new()` 支持可选 `session_id`（commit `c5bd328`）

**文件**：`ops_agent/acp/server.py`  
**变更行数**：约 +12 行

```python
def session_new(
    self,
    *,
    cwd: str | None = None,
    mcp_servers: list[JsonDict] | None = None,
    session_id: str | None = None,          # ← 新增参数
) -> JsonDict:
    session_id = session_id or f"sess_{uuid.uuid4().hex}"
    # 幂等设计：若 session_id 已存在则直接返回，不重置状态
    with self._sessions_lock:
        if session_id in self._sessions:
            return {"sessionId": session_id}
    # ... 创建新 session
```

**作用**：允许 HTP 将 `conversation_id` 直接作为 ACP session ID 传入，无需维护 ID 映射表。幂等语义保证多次调用安全。

---

#### T-E2：新增 ACP HTTP REST 路由（commit `c5bd328`）

**文件**：`ops_agent/server/acp_routes.py`（**新建**，247 行）  
**注册位置**：`ops_agent/server/main.py`（lifespan 注入 `ACPServer`）

5 个端点：

| Method | 路径 | 作用 |
|--------|------|------|
| `POST` | `/acp/sessions` | 幂等创建会话 |
| `POST` | `/acp/sessions/{id}/prompt` | 提交用户消息（202 立即返回，后台线程执行）|
| `GET`  | `/acp/sessions/{id}/events` | SSE 事件流（50ms 轮询 + 1.5s 心跳）|
| `POST` | `/acp/sessions/{id}/responses/{req_id}` | 提交用户对 `_ops/request_input` 的响应 |
| `GET`  | `/acp/sessions/{id}/state` | 查询会话状态（重连恢复用）|

**异步桥接策略**：

```
POST /prompt  →  threading.Thread(target=session_prompt)
              (与 Streamlit 完全相同模式，内部 asyncio.run() 在独立线程创建新事件循环)

GET /events   →  asyncio.to_thread(drain_outbox, ...)  每 50ms 轮询一次
              (避免阻塞 FastAPI 事件循环，drain_outbox 是同步 queue.Queue 操作)

POST /responses →  直接同步调用 submit_client_response()（event.set()，极快）
```

---

#### T-E3：`acp/server.py` 对外暴露（commit `c5bd328`）

**文件**：`ops_agent/acp/server.py`  
**变更**：在模块 `__init__` 中导出 `ACPServer`，供 `acp_routes.py` 访问。

---

#### `_tool_display_hook`（commit `d8c0541`）

**文件**：`ops_agent/agent/base_agent.py`（+22 行）

```python
self._tool_display_hook: Callable[[str], Awaitable[None]] | None = None

# 在 execute_task_streaming() 中：
if self._tool_display_hook is not None:
    for _tr in step.tool_results:
        if _tr.display:
            await self._tool_display_hook(_tr.display)
```

**作用**：`present_sop_step_instruction` 的 `ToolExecResult.display` 字段（富文本卡片内容）在 HTTP streaming 模式下通过此钩子推入 ACP outbox，确保前端能渲染完整卡片数据。

---

#### BUG-1 修复：`_extract_final_result` 空 summary 处理（本地 fix，未推送）

**文件**：`ops_agent/agent/ops_agent.py`  
**问题根因**：当 `task_done.summary == ""`（如 LLM 输出中无结构化 SUMMARY 标签）时，`print_final_summary()` 从未被调用，导致 `agent_message_chunk` 事件不进入 ACP outbox。HTP 侧收到 `session/done` 时 `text_emitted=False`，抛出 `BrainUnavailableError` → 降级为备用助手 → 前端显示"已自动切换到备用助手"。

```python
# 修复前（有 bug）：
if summary:
    self.cli_console.print_final_summary(summary, ...)
return llm_response.content or "Troubleshooting completed."

# 修复后：
if summary:
    self.cli_console.print_final_summary(summary, ...)
else:
    # summary 为空时，通过 fallback_text 触发 print_final_summary
    # 确保 agent_message_chunk 进入 ACP outbox，text_emitted 置 True
    fallback_text = llm_response.content or "Troubleshooting completed."
    if self.cli_console is not None:
        with suppress(Exception):
            self.cli_console.print_final_summary(fallback_text, title="排障总结", success=True)
    return fallback_text
```

---

### 2.2 HTP 侧变更

> 所在仓库：`hci-troubleshoot-platform`，分支：`feature/ops-agent-interactive-ui`

#### PR #245 / #246：部署层修复（commit `5637782`、`bead216`）

- **SOP 数据 HostPath 挂载**：`deploy/helm/.../ops-agent-service/deployment.yaml`
- **Helm imagePullPolicy**：统一为 `IfNotPresent`

####热修补（commit `30d8f5f`）：SOP catalog 配置格式修正

**文件**：`deploy/helm/.../ops-agent-service/configmap.yaml`  
**变更**：`sop_catalog_path: string` → `sop_catalogs: {name: path}` 字典格式。

---

#### PR #249（T-E4/T-E5）：OpsAgentBrainAdapter 完全重写（commit `0a9e02d`）

**文件 1**：`backend/conversation-service/app/core/brain_port.py`（+15 行）

新增 `BrainInteractiveRequest` 数据类：

```python
@dataclass
class BrainInteractiveRequest:
    """ops-agent _ops/request_input → 前端 SOP 操作卡 / 信息确认卡"""
    request_id: str       # JSON-RPC "id"，用于提交响应时的定位
    acp_session_id: str   # ops-agent ACP session ID（= htp conversation_id）
    kind: str             # "sop_step" | "info_request"
    title: str
    prompt: str
    options: list[dict]
    custom_input: bool | dict
    metadata: dict        # 包含 route/operationGoal 等卡片渲染字段
```

**文件 2**：`backend/conversation-service/app/adapters/ops_agent_brain_adapter.py`（完全重写，~350 行）

核心流程（见第 4 节详细数据流）：

```
process(session_id, messages) → AsyncGenerator[BrainEvent]
  ├─ _ensure_acp_session(session_id)      # POST /acp/sessions（幂等）
  ├─ _submit_prompt(session_id, prompt)   # POST /acp/sessions/{id}/prompt
  └─ _consume_events(session_id)          # GET /acp/sessions/{id}/events (SSE)
       ├─ session/update → BrainTextChunk | BrainStageUpdate
       ├─ _ops/request_input → BrainInteractiveRequest
       └─ session/done → return（或 BrainUnavailableError if !text_emitted）
```

---

#### PR #250（T-E6/T-E7）：完整交互卡片全栈实现（commit `84777fb`）

涉及文件概览（+455 行）：

| 文件 | 变更 | 作用 |
|------|------|------|
| `app/adapters/brain_router.py` | +10 行 | `get_ops_agent_adapter()` 路由辅助函数 |
| `app/adapters/ops_agent_brain_adapter.py` | +40 行 | `_ops/request_input` 处理、`submit_acp_response()` |
| `app/routes/conversations.py` | +30 行 | 新增 `POST /{id}/interactive-response` 端点 |
| `app/services/conversation_service.py` | +60 行 | `BrainInteractiveRequest` → SSE event + `submit_interactive_response()` |
| `frontend/customer/src/components/InteractiveRequestCard.vue` | **新建**，259 行 | SOP 操作卡 / 情报确认卡 UI 组件 |
| `frontend/customer/src/stores/chat.ts` | +50 行 | `pendingInteractive` 状态 + SSE `interactive_request` 分支 |
| `frontend/customer/src/components/ChatWindow.vue` | +20 行 | 挂载 `InteractiveRequestCard` |

**会话 SSE 格式扩展**（`conversation_service.py`）：

```python
elif isinstance(brain_event, BrainInteractiveRequest):
    _ir_payload = json.dumps({
        "requestId": brain_event.request_id,
        "acpSessionId": brain_event.acp_session_id,
        "kind": brain_event.kind,
        "title": brain_event.title,
        "prompt": brain_event.prompt,
        "options": brain_event.options,
        "customInput": brain_event.custom_input,
        "metadata": brain_event.metadata,
    }, ensure_ascii=False)
    yield f"\x00event:interactive_request:{_ir_payload}\x00"
```

---

#### `d3dc138`：`text_emitted` 守卫 + 6 单元测试 + CI job

**文件**：`ops_agent_brain_adapter.py`、`test_ops_agent_adapter.py`（新建）、`.github/workflows/ci.yml`

`text_emitted` 守卫逻辑（已在上方 `_consume_events` 代码中体现）：防止 ops-agent 静默失败时前端显示空白气泡，直接触发 HTP 降级路径。

---

## 3 变更必要性 / 侵入性 / 一致性分析

### 必要性

| 变更 | 为什么必须做 |
|------|-------------|
| T-E1 可选 `session_id` | 无此功能则每次对话都是新 session，历史上下文丢失，Agent 无法知晓之前的排障脉络 |
| T-E2 ACP HTTP 路由 | HTP（独立进程）无法复用 Streamlit 的 in-process `ACPServer`，必须走 HTTP |
| `_tool_display_hook` | 无此钩子则 `present_sop_step_instruction` 的 `display` 字段（卡片数据）不进入 outbox，前端永远收不到 `metadata` |
| BUG-1 fix（ops-agent 侧）| 无此修复则空摘要场景必然触发错误降级，严重影响用户体验 |
| `BrainInteractiveRequest` 数据类 | HTP 需要结构化类型在 `ConversationService` 和 `BrainAdapter` 之间传递 |
| `OpsAgentBrainAdapter` 重写 | 原适配器只用 `/v1/chat/completions`，不支持双向交互 |
| `text_emitted` 守卫 | 防止 ops-agent 内部静默失败时 HTP 无法感知，导致前端空白气泡永久挂起 |
| `InteractiveRequestCard.vue` | 原有 ChatWindow 无法渲染 SOP 操作卡 / 情报确认卡，新功能必须的 UI 组件 |

### 侵入性评估

| 变更 | 侵入范围 | 评级 |
|------|---------|------|
| T-E1 `session_new()` 新增可选参数 | 函数签名向后兼容，Streamlit 不受影响 | 🟢 极低 |
| T-E2 新建 `acp_routes.py` | 独立文件，不改动现有路由 | 🟢 极低 |
| `_tool_display_hook` | 新增字段 + 几行 if 判断，现有异步流程不受影响 | 🟢 极低 |
| BUG-1 fix | 仅在 else 分支新增代码，不改动正常路径 | 🟢 极低 |
| HTP `brain_port.py` 新增数据类 | 只增不改，现有 `BrainTextChunk` 等不受影响 | 🟢 极低 |
| `OpsAgentBrainAdapter` 重写 | `process()` 接口签名不变，`BrainRouter` / `ConversationService` 不感知 | 🟡 中等（内部逻辑完全替换）|
| `ConversationService` 新增 branch | `BrainInteractiveRequest` 分支为新 elif，不修改已有的 `BrainTextChunk` 路径 | 🟢 低 |
| `conversations.py` 新端点 | 新增路由，不修改 `/send` 等已有端点 | 🟢 极低 |
| `InteractiveRequestCard.vue` | 新建组件，ChatWindow 仅新增条件挂载 | 🟢 低 |
| `chat.ts` 新增 `pendingInteractive` | 新增 ref 和 SSE 分支，不改动已有 token 流处理 | 🟢 低 |

### 一致性保证

1. **BrainPort 接口不变**：`process()` 签名与 `GptBrainAdapter`、`CommandRBrainAdapter` 完全一致，`BrainRouter` 无改动。
2. **降级路径一致**：`BrainUnavailableError` 处理逻辑与原方案完全相同，降级后显示 fallback 助手消息。
3. **ops-agent Streamlit 不受影响**：Streamlit 走 in-process `ACPServer`，不经过任何新增 HTTP 端点。
4. **ops-agent CLI 不受影响**：CLI 模式不实例化 `ACPServer`，HTTP 路由不加载。

---

## 4 完整数据流

### 4.1 核心构件角色说明

```
┌─ hci-troubleshoot-platform ──────────────────────────────────────────────────┐
│                                                                               │
│  前端 Vue (ChatWindow.vue)                                                    │
│    ├─ pendingInteractive: ref (chat.ts)          交互卡片状态                 │
│    └─ InteractiveRequestCard.vue                 SOP/情报卡 UI                │
│                                                                               │
│  后端 FastAPI (conversations.py)                                              │
│    ├─ POST /conversations/{id}/send              主会话接口                   │
│    └─ POST /conversations/{id}/interactive-response  用户响应接口             │
│                                                                               │
│  ConversationService                                                          │
│    ├─ send_message_stream_only()                 产出 SSE 事件流              │
│    └─ submit_interactive_response()              转发用户响应给 ops-agent     │
│                                                                               │
│  OpsAgentBrainAdapter                                                         │
│    ├─ process()                                  ACP REST 调用主入口          │
│    ├─ _ensure_acp_session()                      POST /acp/sessions           │
│    ├─ _submit_prompt()                           POST /acp/sessions/{id}/prompt│
│    ├─ _consume_events()                          GET  /acp/sessions/{id}/events│
│    └─ submit_acp_response()                      POST /acp/sessions/{id}/responses/{req_id}
└───────────────────────────────────────────────────────────────────────────────┘

┌─ ops-agent ──────────────────────────────────────────────────────────────────┐
│                                                                               │
│  FastAPI HTTP Server (main.py + acp_routes.py)                               │
│    ├─ POST /acp/sessions                         session_new（幂等）          │
│    ├─ POST /acp/sessions/{id}/prompt             threading.Thread 启动 Agent  │
│    ├─ GET  /acp/sessions/{id}/events             SSE outbox 轮询              │
│    └─ POST /acp/sessions/{id}/responses/{req_id} submit_client_response       │
│                                                                               │
│  ACPServer（核心状态机）                                                      │
│    ├─ session_new()                              创建/恢复 ACPSession          │
│    ├─ session_prompt()                           运行 Agent（blocking）        │
│    ├─ drain_outbox()                             消费输出队列                  │
│    └─ submit_client_response()                   event.set() 唤醒挂起线程     │
│                                                                               │
│  ACPClientBridge（session 内嵌）                                              │
│    ├─ request_client()                           阻塞等待（event.wait()）     │
│    └─ submit_response()                          event.set() 返回响应         │
│                                                                               │
│  ACPConsole（工具调用 → ACP 协议翻译层）                                      │
│    ├─ request_info_from_user_input()             → _request_choice_input(kind="info_request")
│    └─ request_sop_step_instruction_input()       → _request_choice_input(kind="sop_step")
│                                                                               │
│  工具层                                                                       │
│    ├─ get_info_from_user_tool.py                 → request_info_from_user_input()
│    └─ present_sop_step_instruction_tool.py       → request_sop_step_instruction_input()
└───────────────────────────────────────────────────────────────────────────────┘
```

---

### 4.2 正常请求-响应（无交互）数据流

```
前端                HTP 后端               OpsAgentBrainAdapter         ops-agent
 │                    │                         │                           │
 │──POST /send───────►│                         │                           │
 │                    │──process(...)──────────►│                           │
 │                    │                         │──POST /acp/sessions──────►│
 │                    │                         │◄── {sessionId} ───────────│
 │                    │                         │──POST /acp/.../prompt────►│
 │                    │                         │◄── 202 ───────────────────│    Agent 线程启动
 │                    │                         │──GET /acp/.../events─────►│（SSE）
 │                    │                         │                           │
 │                    │                         │◄─ session/update (文本)──│  agent_message_chunk
 │                    │◄─[SSE token]────────────│                           │
 │◄─[SSE token]───────│                         │                           │
 │                    │                         │◄─ session/done ───────────│
 │                    │                         │   (text_emitted=True)     │
 │                    │                         │  return（结束 generator）  │
 │◄─[SSE close]───────│                         │                           │
```

---

### 4.3 `get_info_from_user_tool` 交互数据流

> 触发场景：Agent 需要向用户提问背景信息（非需要执行操作的问题）

```
[ops-agent 内部]                    [HTTP 层]              [HTP + 前端]
       │                                │                        │
Agent 调用 get_info_from_user_tool      │                        │
       │                                │                        │
       ▼                                │                        │
GetInfoFromUserTool.execute()           │                        │
  └─ cli_console.request_info_from_user_input(
         question=..., options={...},   │                        │
         context=..., risk_notice=...)  │                        │
           │                           │                        │
           ▼                           │                        │
ACPConsole._request_choice_input(       │                        │
  kind="info_request",                 │                        │
  title="信息确认卡",                  │                        │
  prompt=question,                     │                        │
  options={...},                       │                        │
  meta={question, context, riskNotice} │                        │
)                                      │                        │
  └─ bridge.request_client(            │                        │
       "_ops/request_input", params)   │                        │
       │                               │                        │
       ├─ outbox.put(JSON-RPC request) │                        │
       │     {"id": "req_abc",         │                        │
       │      "method": "_ops/request_input",                   │
       │      "params": {              │                        │
       │        "sessionId": "conv_id",│                        │
       │        "request": {           │                        │
       │          "kind": "info_request",                       │
       │          "title": "信息确认卡",│                       │
       │          "prompt": "question",│                        │
       │          "options": [...],    │                        │
       │          "customInput": {...},│                        │
       │          "_meta": {question, context, riskNotice}      │
       │        }                      │                        │
       │      }                        │                        │
       │    }}                         │                        │
       │                               │                        │
       └─ pending.event.wait() ───────────────────────────── 🔒 Agent 线程挂起
                                       │                        │
                                       │ SSE drain_outbox       │
                                       │──────────────────────►│
                                       │◄── JSON-RPC 上方内容 ──│
                                       │  (method=_ops/request_input)
                                       │                        │
                               _consume_events 产出             │
                               BrainInteractiveRequest:         │
                                 request_id="req_abc"           │
                                 kind="info_request"            │
                                 ...                            │
                                       │                        │
                               ConversationService:             │
                                 yield SSE:                     │
                                 "\x00event:interactive_request:{...}\x00"
                                       ──────────────────────►│
                                                               │
                                                    chat.ts 解析 SSE:
                                                    pendingInteractive.value = {...}
                                                               │
                                                    ChatWindow 渲染
                                                    InteractiveRequestCard
                                                    (kind="info_request")
                                                               │
                                                    用户选择 / 输入
                                                               │
                                                    doSubmit() 调用:
                                                    POST /conversations/{id}/interactive-response
                                                    {requestId, acpSessionId, outcome: {...}}
                                       ◄──────────────────────│
                                       │                        │
                               ConversationService              │
                                 submit_interactive_response()  │
                                   └─ adapter.submit_acp_response(
                                        acp_session_id=...,    │
                                        request_id="req_abc",  │
                                        outcome={...})         │
                                       │                        │
                               POST /acp/sessions/{id}/responses/req_abc
                               {result: {outcome: {...}}}       │
                               ──────────────────────────────►│
                                                      ACPServer.submit_client_response()
                                                        bridge.submit_response(req_id, result)
                                                          pending.response = result
                                                          pending.event.set() ── 🔓 唤醒 Agent 线程
                                                               │
       ┌──── event.wait() 返回 ────────────────────────────────┘
       │
ACPConsole._request_choice_input 返回:
  "选择了选项 1: <option_text>" 或 "自定义输入: <text>"
       │
GetInfoFromUserTool.execute() 解析并返回 ToolExecResult
  output="<clean_response>"
       │
Agent 收到工具结果，继续执行下一步
```

---

### 4.4 `present_sop_step_instruction_tool` 交互数据流

> 触发场景：Agent 引导用户执行 SOP 步骤操作，并等待用户反馈执行结果

与 `get_info_from_user_tool` 的主要差异：

1. **调用路径更深**：`execute()` → `request_sop_step_instruction_input()` → `_request_choice_input(kind="sop_step")`
2. **元数据更丰富**：`_meta` 中包含 `route`、`operationGoal`、`executionGuidance`、`expectedResult`、`feedbackRequest`、`riskNotice`、`extraNotice`（供前端渲染完整卡片）
3. **`display` 字段**：`execute()` 额外构造 `ToolExecResult.display`（标准化 sop-markup 格式）；HTTP 模式下由 `_tool_display_hook` 推入 ACP outbox
4. **前端渲染差异**：`InteractiveRequestCard.vue` 的 `sop_step` variant 显示操作路径/目标/指引/预期结果/风险提示

```
PresentSOPStepInstructionTool.execute()
  ├─ 参数校验（route / operation_goal / execution_guidance / expected_result /
  │                   feedback_request / reply_options 均必填）
  ├─ _build_display(...)      构造富文本 display（sop-markup 格式）
  │     → ToolExecResult.display
  │         （_tool_display_hook 将此字段额外推入 ACP outbox，
  │           method=session/update, sessionUpdate=tool_call_update, display=...）
  │
  ├─ request_sop_step_instruction_input(
  │     route=..., operation_goal=..., execution_guidance=...,
  │     expected_result=..., feedback_request=..., reply_options=...,
  │     risk_notice=..., extra_notice=...)
  │
  │     └─ _request_choice_input(
  │           kind="sop_step",
  │           title="SOP 操作卡",
  │           prompt=feedback_request,
  │           options={1:..., 2:..., 5:"<自定义输入>"},
  │           meta={route, operationGoal, executionGuidance,
  │                 expectedResult, feedbackRequest, riskNotice, extraNotice}
  │        )
  │
  │        → outbox.put(JSON-RPC: "_ops/request_input")
  │        → pending.event.wait()  🔒 挂起
  │
  └─ [用户在前端卡片交互 → doSubmit() → POST interactive-response → event.set()]
       → 返回 "选择了选项 N: <label>" 或 "自定义输入: <text>"
       → _parse_user_response(user_response)
       → _build_display(..., user_feedback=clean_response)  更新 display 加入反馈
       → ToolExecResult(output=clean_response, display=display_with_feedback)

Agent 收到用户反馈 → 根据 reply_options 匹配的结果决定下一步 SOP 分支
```

**`InteractiveRequestCard.vue` sop_step variant 渲染字段**：

```vue
<!-- 来自 metadata（_meta 字段） -->
route          → "当前位置" 标签
operation_goal → "操作目标" 区块
execution_guidance → "操作指引" 区块（支持 Markdown）
expected_result    → "预期结果" 区块
risk_notice    → "风险提示" 区块（红色，仅高危步骤显示）

<!-- 来自 options 列表 -->
reply_options  → 操作按钮列表（每项对应一个 optionId）

<!-- 来自 customInput -->
自定义文本输入框（当 customInput 非 false 时显示）
```

---

## 5 HTP ↔ ops-agent 交互调用步骤（对比 dev-pengwei ops_web）

### 5.1 dev-pengwei（in-process Streamlit）模式

dev-pengwei 采用 **完全进程内** 架构：ops-agent `ACPServer` 与 Streamlit UI 运行在同一 Python 进程。

**会话生命周期**：

```
Step 1 - 创建会话：
  ACPServer.session_new(cwd=...) → session_id = "sess_{uuid}"
  存入 chat["session_id"]，Streamlit session_state 持久化

Step 2 - 提交 prompt：
  thread = threading.Thread(target=ACPServer.session_prompt, ...)
  thread.start()
  # Streamlit 进入刷新轮询循环

Step 3 - 轮询 outbox：
  while True:
    items = ACPServer.drain_outbox(session_id=session_id)  # 直接内存 queue
    for envelope in items:
        apply_envelope(ui_state, envelope)
    state = ACPServer.get_session_state(session_id=session_id)
    if not state["activePrompt"]:
        break
    time.sleep(0.2)  # 或 st.rerun() 触发
```

**`apply_envelope` 处理逻辑**（`ops_web/state.py`）：

```python
def apply_envelope(ui_state, envelope, ...):
    method = envelope.get("method")
    if method == "session/update":
        _apply_session_update(ui_state, envelope["params"])
        # 处理 agent_message_chunk → 追加气泡
        # 处理 tool_call / tool_call_update → 工具时间线

    elif method == "_ops/request_input":
        ui_state["pending_input"] = deepcopy(envelope)
        # 存入状态，下次 Streamlit rerun 时渲染底部交互弹窗

    elif method == "session/done":
        ui_state["stop_reason"] = "end_turn"
```

**交互卡片渲染**（`_render_pending_interaction`）：

```python
def _render_pending_interaction(ui_state, runtime):
    if ui_state.get("pending_input"):
        request = ui_state["pending_input"]
        payload = request["params"]["request"]
        kind = payload["kind"]   # "sop_step" | "info_request"
        _open_bottom_sheet(payload["title"],
                           lambda: _render_input_request_body(ui_state, runtime),
                           request_id=..., badge="待决策")
```

**提交响应**（`_submit_client_response`）：

```python
def _submit_client_response(ui_state, runtime, *, request_id, result, ...):
    chat = _active_chat(runtime)
    try:
        chat["server"].submit_client_response(  # 直接内存调用，无 HTTP
            session_id=chat["session_id"],
            request_id=request_id,
            result=result,
        )
    except KeyError:                            # 请求已超时或重复提交
        _start_prompt(_stale_client_response_prompt(...), ...)
        st.rerun()
        return
    clear_pending_request(ui_state, request_id)
    _persist_chat(chat)
    st.rerun()
```

---

### 5.2 HTP（HTTP REST + Vue）模式

**关键架构差异**：

- ops-agent 是**独立进程**（K8s Pod），通过 ClusterIP 访问
- HTP 前端是 Vue SPA，SSE 事件直接推送到浏览器
- ops-agent 不需运行 Streamlit，UI 层在 HTP

**会话初始化与 prompt 提交**（`OpsAgentBrainAdapter.process()`）：

```
POST http://ops-agent:8006/acp/sessions
  body: {"session_id": "<htp_conversation_id>"}
  response: {"sessionId": "<htp_conversation_id>"}
  (幂等：若已存在直接返回，不重置)

POST http://ops-agent:8006/acp/sessions/{conversation_id}/prompt
  body: {"prompt": [{"type": "text", "text": "用户消息"}]}
  response: 202 {"started": true, "sessionId": "..."}
  (非阻塞：Agent 在 ops-agent 侧后台线程中执行)
```

**SSE 事件消费**（`_consume_events()`）：

```
GET http://ops-agent:8006/acp/sessions/{conversation_id}/events
  → text/event-stream

HTP 侧按行解析：
  data: {"method": "session/update", "params": {...}}
    └─ agent_message_chunk → yield BrainTextChunk(content=text)
    └─ session_info_update → yield BrainStageUpdate(stage=title)

  data: {"id": "req_xyz", "method": "_ops/request_input", "params": {...}}
    └─ yield BrainInteractiveRequest(request_id="req_xyz", ...)

  data: {"method": "session/done", ...}
    └─ if text_emitted: return
       else: raise BrainUnavailableError  → HTP 降级
```

**ConversationService → 前端 SSE 转发**：

```python
# ConversationService.send_message_stream_only() 内：
async for brain_event in adapter.process(...):
    if isinstance(brain_event, BrainTextChunk):
        yield f"\x00{brain_event.content}\x00"           # token 追加
    elif isinstance(brain_event, BrainInteractiveRequest):
        yield f"\x00event:interactive_request:{json.dumps({...})}\x00"
    elif isinstance(brain_event, BrainStageUpdate):
        yield f"\x00event:stage_update:{brain_event.stage}\x00"
```

**前端 SSE 解析**（`chat.ts`）：

```typescript
// SSE 流 token 解析循环中：
} else if (pendingEventType === 'interactive_request') {
    pendingInteractive.value = { ...JSON.parse(data) }
    // → ChatWindow 触发渲染 InteractiveRequestCard
}
```

**InteractiveRequestCard.vue `doSubmit()`**：

```typescript
async function doSubmit(outcome: {outcome: string; optionId?: string; text?: string}) {
    const resp = await fetch(`/api/conversations/${conversationId}/interactive-response`, {
        method: 'POST',
        body: JSON.stringify({
            requestId: props.requestId,
            acpSessionId: props.acpSessionId,
            outcome,
        }),
    })
    if (resp.ok) {
        visible.value = false      // 关闭卡片
        emit('submitted', outcome) // 通知父组件
    }
    // 失败时保持卡片可重试
}
```

**`conversations.py` → `ConversationService` → `OpsAgentBrainAdapter`**：

```python
@router.post("/{conversation_id}/interactive-response")
async def submit_interactive_response(conversation_id: str, body: InteractiveResponseBody, ...):
    success = await service.submit_interactive_response(
        conversation_id=conversation_id,
        request_id=body.request_id,
        acp_session_id=body.acp_session_id,
        outcome=body.outcome,
    )

# ConversationService.submit_interactive_response():
async def submit_interactive_response(self, conversation_id, request_id, acp_session_id, outcome):
    adapter = brain_router.get_ops_agent_adapter()
    await adapter.submit_acp_response(acp_session_id, request_id, outcome)

# OpsAgentBrainAdapter.submit_acp_response():
POST http://ops-agent:8006/acp/sessions/{acp_session_id}/responses/{request_id}
body: {"result": {"outcome": {"outcome": "selected", "optionId": "1"}}}
→ ops-agent: pending.event.set()  →  Agent 线程唤醒，继续执行
```

---

### 5.3 两种模式逐步对比

| # | 步骤 | dev-pengwei（in-process）| HTP（HTTP REST）|
|---|------|--------------------------|-----------------|
| 1 | 创建 session | `ACPServer.session_new()` 内存 dict | `POST /acp/sessions` HTTP |
| 2 | 提交 prompt | `threading.Thread` 直接调用 | `POST /acp/sessions/{id}/prompt` → 202 |
| 3 | 消费输出 | `drain_outbox()` 直接内存轮询（200ms）| SSE `GET /events`（50ms asyncio 轮询）|
| 4 | `_ops/request_input` 接收 | `apply_envelope()` → `ui_state["pending_input"]` | `_consume_events()` → `BrainInteractiveRequest` → HTP SSE → `chat.ts pendingInteractive` |
| 5 | 卡片渲染 | Streamlit `_render_input_request_body()` | Vue `InteractiveRequestCard.vue` |
| 6 | 用户提交响应 | `ACPServer.submit_client_response()` 内存调用 | POST `/interactive-response` → HTTP → `POST /responses/{req_id}` |
| 7 | 唤醒 Agent 线程 | `pending.event.set()` 同进程 | `pending.event.set()` 跨进程（HTTP）|
| 8 | 过期请求处理 | `KeyError` → 生成"用户已回复但 Agent 失忆"的新 prompt | 无（返回 404，前端显示错误）|
| 9 | session 持久化 | `_persist_chat()` JSON 文件 | ops-agent 内存（进程重启丢失）|
| 10 | ops-agent 进程 | Streamlit 同进程 | 独立 K8s Pod，ClusterIP 访问 |

**关键共同点**：
- 底层 `ACPServer.request_client()` → `pending.event.wait()` → `event.set()` 机制**完全相同**
- `_ops/request_input` JSON-RPC 请求格式**完全相同**
- `kind: "sop_step" | "info_request"` 语义**完全相同**
- `submit_client_response(result={"outcome": {...}})` 参数格式**完全相同**

---

## 6 开发环境验收测试

### 环境信息

```
K8s 节点 IP:       172.22.73.249
Traefik HTTP 端口: 4888
ops-agent Service: ClusterIP 10.43.159.150:8006（无 NodePort）
HTP API 入口:      http://172.22.73.249:4888/api
前端 URL:          http://172.22.73.249:4888
```

### 前置条件

1. 确认 ops-agent 当前镜像包含所有 feature-hci commits（包含 BUG-1 fix）：
   ```bash
   kubectl -n hci get pod -l app=ops-agent-service -o jsonpath='{.items[0].spec.containers[0].image}'
   ```
2. 确认 `sop_catalogs` 配置正确挂载：
   ```bash
   kubectl -n hci exec deploy/ops-agent-service -- cat /app/configs/sop_catalogs.yaml | head -5
   ```

### 验收测试 T1：ACP REST 直连验证

> 直接通过 kubectl port-forward 访问 ops-agent，验证 ACP 接口正确性

```bash
# 开启端口转发
kubectl -n hci port-forward svc/ops-agent-service 18006:8006 &

# T1-1: 创建会话（幂等）
curl -s -X POST http://localhost:18006/acp/sessions \
  -H "Content-Type: application/json" \
  -d '{"session_id": "test-conv-001"}' | python3 -m json.tool
# 期望: {"sessionId": "test-conv-001"}

# T1-2: 重复创建（验证幂等性）
curl -s -X POST http://localhost:18006/acp/sessions \
  -H "Content-Type: application/json" \
  -d '{"session_id": "test-conv-001"}' | python3 -m json.tool
# 期望: {"sessionId": "test-conv-001"}（不报错，不重置状态）

# T1-3: 提交 prompt（后台异步执行）
curl -s -X POST http://localhost:18006/acp/sessions/test-conv-001/prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt": [{"type": "text", "text": "设备无法上网，请协助排查"}]}' | python3 -m json.tool
# 期望: {"started": true, "sessionId": "test-conv-001"}（202）

# T1-4: 消费 SSE 事件流（30 秒超时）
curl -N --max-time 30 http://localhost:18006/acp/sessions/test-conv-001/events
# 期望:
# 1. 看到多行 "data: {...}" SSE 事件
# 2. 包含 method=session/update 的 agent_message_chunk（至少一条文本）
# 3. 包含 method=_ops/request_input（Agent 第一个提问）
# 4. 最终不应出现 method=session/done 后无任何文本事件的情况（BUG-1 守卫）

# T1-5: 提交用户响应（唤醒 Agent）
# （从 T1-4 输出中取 request_id，格式如 "id": "abc-123"）
REQ_ID=<从 SSE 中取出的 id>
curl -s -X POST "http://localhost:18006/acp/sessions/test-conv-001/responses/$REQ_ID" \
  -H "Content-Type: application/json" \
  -d '{"result": {"outcome": {"outcome": "selected", "optionId": "1"}}}' | python3 -m json.tool
# 期望: {"ok": true}
# 随后在 T1-4 的 SSE 流中应继续看到新的 agent_message_chunk 或下一个 _ops/request_input

kill %1  # 停止端口转发
```

**完成标准**：T1-1 至 T1-5 全部无报错，SSE 流可见文本内容，用户响应能唤醒 Agent。

---

### 验收测试 T2：HTP API 层验证

> 通过 HTP API 进行完整会话，验证 OpsAgentBrainAdapter 正确转发

```bash
HTP_BASE="http://172.22.73.249:4888/api"

# T2-1: 创建 case（获取 conversation_id）
CASE_ID=$(curl -s -X POST "$HTP_BASE/cases" \
  -H "Content-Type: application/json" \
  -d '{"title": "ACP 集成验收测试", "description": "设备无法上网"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['id'])")
echo "CASE_ID=$CASE_ID"

# T2-2: 创建 conversation（绑定 ops-agent brain）
CONV_ID=$(curl -s -X POST "$HTP_BASE/cases/$CASE_ID/conversations" \
  -H "Content-Type: application/json" \
  -d '{"brain": "ops-agent"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['id'])")
echo "CONV_ID=$CONV_ID"

# T2-3: 发送消息并监听 SSE 流（完整 Python 脚本）
python3 << 'EOF'
import sys, json, httpx, asyncio

BASE = "http://172.22.73.249:4888/api"
CONV_ID = sys.argv[1] if len(sys.argv) > 1 else exit("需要 CONV_ID 参数")

RESULT = {"text_chunks": 0, "interactive_requests": [], "session_done": False}

async def main():
    async with httpx.AsyncClient(timeout=120) as client:
        # 发送消息
        resp = await client.post(f"{BASE}/conversations/{CONV_ID}/send",
                                  json={"content": "设备无法上网，请协助排查"})
        assert resp.status_code == 200, f"send 失败: {resp.status_code}"

        # 监听 SSE
        async with client.stream("GET", f"{BASE}/conversations/{CONV_ID}/stream") as stream:
            async for line in stream.aiter_lines():
                if not line:
                    continue
                line = line.strip("\x00")
                if line.startswith("event:interactive_request:"):
                    payload = json.loads(line[len("event:interactive_request:"):])
                    RESULT["interactive_requests"].append(payload)
                    print(f"[交互请求] kind={payload['kind']} title={payload['title']}")
                    # 自动提交第一个选项
                    r = await client.post(
                        f"{BASE}/conversations/{CONV_ID}/interactive-response",
                        json={"requestId": payload["requestId"],
                              "acpSessionId": payload["acpSessionId"],
                              "outcome": {"outcome": "selected", "optionId": "1"}})
                    print(f"  → 提交响应 status={r.status_code}")
                elif line.startswith("event:stage_update:"):
                    print(f"[阶段更新] {line[19:]}")
                elif line:
                    RESULT["text_chunks"] += 1
                    print(f"[文本] {line[:60]}...")

    print(f"\n=== 结果摘要 ===")
    print(f"文本片段数: {RESULT['text_chunks']}")
    print(f"交互请求数: {len(RESULT['interactive_requests'])}")
    assert RESULT['text_chunks'] > 0, "❌ 未收到任何文本，BUG-1 可能复现"
    print("✅ 验收通过")

asyncio.run(main())
EOF
```

**完成标准**：
- 收到至少 1 个 `event:interactive_request` 事件（SOP 操作卡或情报确认卡）
- 自动提交响应后 Agent 继续产出文本
- 最终收到非空文本流（`text_chunks > 0`）

---

### 验收测试 T3：前端 UI 人工验收

**测试步骤**：

1. 打开浏览器，访问 `http://172.22.73.249:4888`
2. 创建新工单，选择 **ops-agent** 作为大脑
3. 在对话框输入：`我的设备无法访问互联网，请帮我排查`
4. 发送消息后，观察以下行为：

| # | 期望行为 | 判断 |
|---|----------|------|
| 4a | 气泡区域出现流式文本（Agent 正在思考/输出）| ⬜ |
| 4b | 文本流结束后，页面底部 / 内嵌区域出现**交互卡片**（情报确认卡或 SOP 操作卡）| ⬜ |
| 4c | 情报确认卡：显示问题、选项按钮、自定义输入框 | ⬜ |
| 4d | SOP 操作卡：显示"当前路径"、"操作目标"、"操作指引"、"预期结果"区块 | ⬜ |
| 4e | 点击一个选项按钮 → 卡片消失，Agent 继续输出文本 | ⬜ |
| 4f | 再次出现下一张卡片（Agent 继续引导下一步）或输出最终总结文本 | ⬜ |
| 4g | 整个流程无"已自动切换到备用助手"提示（即 BUG-1 不复现）| ⬜ |

**不可接受的行为**：
- 发送消息后气泡为空（BUG-1 未修复）
- 卡片出现后提交无反应（interactive-response 接口异常）
- 控制台出现 CORS / 404 / 502 错误

**完成标准**：上表 4a–4g 全部打勾，整个排障对话可以从头走到结束并看到最终总结。

---

## 变更记录

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-05-13 | v1.1 | 修复进程重启后 LLM 上下文丢失（per-session trajectory_dir） | [2026-05-13-ops-agent刷新后上下文丢失根因分析与修复方案.md](../events/2026-05-13-ops-agent刷新后上下文丢失根因分析与修复方案.md) |

---

## 附录：进程重启后上下文恢复机制（2026-05-13 补充）

### 问题背景

ops-agent REST server 的 `ACPServer(...)` 构造时未传 `trajectory_file`，
导致 `session_new` 始终以空 `llm_message_history` 初始化 `ACPSession`。
进程重启（pod 重调度、服务更新）后 `_sessions` 内存字典清空，
hci-platform 重建 session 时 ops-agent 失去全部诊断上下文，从步骤 1 重新开始。

### 修复方案（per-session trajectory_dir）

**ops-agent 侧**：`ACPSession` 新增 `trajectory_dir` 字段；`session_new` 接受该参数并用于：
1. 加载历史：`load_latest_message_history(trajectory_dir)`
2. 持久化：`_create_agent` 将 `trajectory_dir` 传给 `Agent`，`trajectory_recorder` 绑定到该路径

**hci-platform 侧**：`OpsAgentBrainAdapter._ensure_acp_session` 在请求体中附带：
```json
{
  "session_id": "<conversation_id>",
  "trajectory_dir": "/data/ops-agent-trajectories/<conversation_id>"
}
```

### 与 ops-web 机制的对比

| 维度 | ops-web（修复参考） | hci REST（修复后） |
|------|--------------------|--------------------|
| trajectory 路径来源 | `ACPServer(trajectory_file=per_chat_dir)` | `session_new(trajectory_dir=per_conv_dir)` |
| per-session 隔离 | ✅ 不同 `chat_id` 目录不同 | ✅ 不同 `conversation_id` 目录不同 |
| 历史恢复触发点 | `session_new` | `session_new` |
| ops-web 兼容性 | N/A（ops-web 自己的路径） | ✅ 不影响（参数可选，原有路径无改动） |

### 部署要求

生产环境 ops-agent pod 须将 `/data/ops-agent-trajectories` 挂载到 **PVC**。
dev 环境可使用 `hostPath`（`kubectl rollout restart` 后仍能恢复，pod 重调度后丢失）。

---

*文档由 GitHub Copilot（Claude Sonnet 4.6）自动生成，基于 ops-agent `feature-hci` 及 hci-troubleshoot-platform `feature/ops-agent-interactive-ui` 分支实际代码整理。*
