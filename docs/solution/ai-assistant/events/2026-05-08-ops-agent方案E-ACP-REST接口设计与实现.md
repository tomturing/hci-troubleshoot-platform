---
status: active
category: solution
audience: developer
last_updated: 2026-05-08
owner: platform-team
---

# 方案E：ACP REST 接口 — 设计原理与详细拆解

> 关联分析：[2026-05-08-ops-agent交互性缺陷根因分析与方案对比.md](./2026-05-08-ops-agent交互性缺陷根因分析与方案对比.md)  
> 关联任务：[2026-05-08-ops-agent方案E任务分解.md](../../../task/ai-assistant/events/2026-05-08-ops-agent方案E任务分解.md)

---

## 一、设计原理：复用而非重建

### 1.1 已有基础设施盘点

ops-agent 的 `acp/server.py` 已经实现了完整的双向交互基础设施，但只被 Streamlit 前端使用，从未对外暴露为 HTTP 接口：

```
已有，可复用：                          缺失，需补充：
──────────────────────────            ──────────────────────────
ACPServer.session_new()               HTTP REST 包装层
ACPServer.session_prompt()            异步执行（非阻塞）
ACPServer.drain_outbox()              SSE 事件推送
ACPServer.submit_client_response()    conversation-service ACP 客户端
ACPServer.get_session_state()         BrainInteractiveRequest 事件类型
ACPSession（会话状态）                前端交互 UI 组件
ACPClientBridge（线程安全桥接）
ACPConsole（工具输出转 ACP 消息）
```

**方案E的核心工作量是"包装"而不是"重写"：** 对现有基础设施做最小必要的异步化改造，然后封装成标准 HTTP REST 接口。

### 1.2 异步化改造的必要性分析（第一性原理修正）

> ⚠️ **重要修正（2026-05-08 实地分析后更新）**：早期设计建议将 `threading.Event/Queue` 替换为 asyncio 对等物。**这一方案是错误的，且会破坏现有 Streamlit 集成。** 本节按第一性原理重新推导最小改动方案。

#### 1.2.1 关键约束：Streamlit 必须保持不变

`ops_web/streamlit_app.py` 以如下方式使用 ACPServer，这些调用模式**不在 asyncio 上下文中**：

```python
# streamlit_app.py:118 — session_prompt 在 daemon 线程中同步调用
thread = threading.Thread(target=_run_prompt, daemon=True)
thread.start()

# streamlit_app.py:1030 — submit_client_response 从 Streamlit 主线程同步调用
runtime["server"].submit_client_response(session_id=..., request_id=..., result=...)

# drain_outbox 在 Streamlit 主线程中同步轮询
items = runtime["server"].drain_outbox(session_id=...)
```

若将 `threading.Event` 改为 `asyncio.Event`，则：
- `submit_client_response()` 调用的 `event.set()` 将在非 asyncio 线程中执行，产生线程安全问题
- 若改为 `asyncio.Queue`，则 `drain_outbox()` 中的 `queue.get_nowait()` 同样需要在 event loop 中调用

**结论：任何对 `threading.Event` / `queue.Queue` 的替换都会破坏 Streamlit。**

#### 1.2.2 正确方案：threading.Thread 包装（与 Streamlit 同款模式）

Streamlit 已经用实践证明了正确的集成模式：

```
Streamlit 的解法：
    threading.Thread(target=server.session_prompt, ...) → start()
    → session_prompt() 内部调用 asyncio.run(agent.run(...))
    → asyncio.run() 在独立线程中创建新事件循环，完全隔离主线程
```

**FastAPI HTTP 路由可以用完全相同的模式：**

```python
# server/acp_routes.py（新建）
@router.post("/sessions/{session_id}/prompt", status_code=202)
async def submit_prompt(session_id: str, body: PromptRequest):
    """立即返回 202，Agent 在后台线程中运行（与 Streamlit 同款模式）"""
    def _run():
        acp_server.session_prompt(
            session_id=session_id,
            prompt=body.prompt,
            message_id=body.message_id,
        )
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"started": True, "sessionId": session_id}
```

`drain_outbox()` 在 SSE 端点中通过 `asyncio.to_thread()` 调用（不阻塞事件循环）：

```python
@router.get("/sessions/{session_id}/events")
async def stream_events(session_id: str):
    async def _generate():
        while True:
            # drain_outbox 是同步的，通过 to_thread 避免阻塞事件循环
            items = await asyncio.to_thread(
                acp_server.drain_outbox, session_id=session_id
            )
            for item in items:
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("method") == "session/done":
                    return
            await asyncio.sleep(0.05)   # 50ms 轮询间隔
    return StreamingResponse(_generate(), media_type="text/event-stream")
```

#### 1.2.3 改动量对比

| 方案 | ops-agent 改动行数 | Streamlit 风险 | 复杂度 |
|------|-------------------|---------------|--------|
| 原方案：asyncio 化 threading.Event/Queue | ~120 行（侵入式） | ❌ 破坏 | 高 |
| **正确方案：threading.Thread 包装** | **~80 行（纯新增）** | **✅ 零影响** | **低** |

**ops-agent 仓库的实际改动：**
1. `acp/server.py`：可选增加 `session_new()` 的 `session_id` 参数（~3 行）—— 其余完全不变
2. `server/acp_routes.py`（新建，~80 行）—— 纯新增，对现有代码零侵入
3. `server/main.py`：注册路由，lifespan 中初始化 ACPServer（~10 行）

---

## 二、完整数据流（端到端）

### 2.1 正常对话流（无交互工具）

```
用户发消息
  │
  ▼
前端 POST /api/conversations/{conv_id}/messages?stream=true
  │
  ▼
conversation-service OpsAgentBrainAdapter.process()
  ├─ [首次] POST ops-agent:8006/acp/sessions → session_id
  └─ POST ops-agent:8006/acp/sessions/{session_id}/prompt → {started: true}
       │
       │（立即返回，Agent 后台运行）
       ▼
  GET ops-agent:8006/acp/sessions/{session_id}/events  (SSE 长连接)
       │
       │（轮到 Agent 输出文本时）
       ├─ event: session/update → update.sessionUpdate=="agent_message_chunk"
       │   └─ yield BrainTextChunk(content=text)
       │       └─ SSE 推送给前端 → 前端渲染文本
       │
       └─ event: session/done (stopReason)
           └─ 关闭 SSE → 前端显示完成
```

### 2.2 交互工具触发流（present_sop_step_instruction）

```
Agent 调用 present_sop_step_instruction(route, operation_goal, reply_options, ...)
  │
  ▼
ACPConsole.present_sop_step_instruction()
  └─ bridge.request_client("_ops/request_input", {request: {kind:"sop_step", ...}})
       └─ asyncio.Queue.put({jsonrpc: "2.0", id: "req_abc", method: "_ops/request_input", ...})
          └─ asyncio.Event.wait()  ← Agent 在此挂起，等待用户响应

──────── 与此同时，SSE 事件被推送给 conversation-service ────────────

GET /acp/sessions/{id}/events SSE 流收到：
  {jsonrpc: "2.0", id: "req_abc", method: "_ops/request_input",
   params: {request: {kind: "sop_step", title: "SOP操作卡", options: [...], ...}}}
  │
  ▼
OpsAgentBrainAdapter：识别为交互请求事件
  └─ yield BrainInteractiveRequest(request_id="req_abc", kind="sop_step",
                                    title="...", options=[...], metadata={...})
       │
       ▼
ConversationService：收到 BrainInteractiveRequest
  └─ 通过 SSE 推送给前端：
     data: {"event": "interactive_request", "requestId": "req_abc",
            "kind": "sop_step", "title": "SOP操作卡", "options": [...]}

──────── 前端展示 SOP 操作卡，用户选择 ─────────────────────────────

前端 POST /api/conversations/{conv_id}/interactive-response
  {requestId: "req_abc", sessionId: "sess_xxx",
   outcome: {outcome: "selected", optionId: "1"}}
  │
  ▼
conversation-service：转发到 ops-agent-service
  POST /acp/sessions/sess_xxx/responses/req_abc
  {result: {outcome: {outcome: "selected", optionId: "1"}}}
  │
  ▼
ops-agent-service：submit_client_response()
  └─ pending.response = result
     pending.event.set()   ← 唤醒挂起的 Agent

Agent 从 request_client() 恢复，获得用户选择结果
  └─ " 选择了选项 1: 输出中有 ESTABLISHED 状态的连接"
     └─ 继续下一步 ReAct 推理
```

---

## 三、接口设计

### 3.1 ops-agent-service 新增 ACP REST 接口

#### `POST /acp/sessions`
创建新的 ACP 会话。

```yaml
Request:
  cwd: string (optional)          # 工作目录，默认 /app
  session_id: string (optional)   # 可指定 session_id（用于 htp 侧多轮复用）

Response 201:
  sessionId: string               # 分配的 session ID
```

#### `POST /acp/sessions/{session_id}/prompt`
向会话提交用户消息，**立即返回**，Agent 后台异步运行。

```yaml
Request:
  prompt:
    - type: text
      text: string                # 用户消息文本
  message_id: string (optional)  # 幂等执行 ID

Response 202:
  started: true
  sessionId: string
```

错误情况：
- `409 Conflict`：该会话已有一个 prompt 正在运行（`active_prompt=true`）

#### `GET /acp/sessions/{session_id}/events`
以 Server-Sent Events（SSE）流方式返回会话事件。**长连接**，Agent 运行期间持续推送。

```yaml
Content-Type: text/event-stream

事件类型1 - 文本输出（对应 agent_message_chunk）：
  data: {"jsonrpc":"2.0","method":"session/update",
         "params":{"sessionId":"...","update":{"sessionUpdate":"agent_message_chunk","text":"..."}}}

事件类型2 - 用户交互请求（对应 _ops/request_input）：
  data: {"jsonrpc":"2.0","id":"req_abc","method":"_ops/request_input",
         "params":{
           "sessionId":"...",
           "request":{"kind":"sop_step","title":"SOP操作卡","prompt":"请执行以下步骤...",
                       "options":[{"optionId":"1","name":"成功"},{"optionId":"2","name":"失败"}],
                       "customInput":true,"_meta":{...}}}}

事件类型3 - 会话结束：
  data: {"jsonrpc":"2.0","method":"session/done",
         "params":{"sessionId":"...","stopReason":"end_turn"}}
```

连接断开后，conversation-service 需要重新连接并继续消费（SSE 本身无消息确认机制，queue 中事件需设置容量上限防止积压）。

#### `POST /acp/sessions/{session_id}/responses/{request_id}`
提交对 `_ops/request_input` 事件的用户响应，唤醒挂起的 Agent。

```yaml
Request:
  result:
    outcome:
      outcome: "selected" | "free_text"
      optionId: string (当 outcome=="selected" 时)
      text: string (当 outcome=="free_text" 时)

Response 200:
  ok: true
```

错误情况：
- `404 Not Found`：request_id 不存在（已超时，或已响应）
- `410 Gone`：对应的 session 已结束

#### `GET /acp/sessions/{session_id}/state`
查询会话当前状态（用于 conversation-service 重连后状态恢复）。

```yaml
Response 200:
  sessionId: string
  cwd: string
  title: string | null
  activePrompt: bool             # Agent 是否仍在运行
  lastStopReason: string | null  # "end_turn" | "refusal" | null
```

---

### 3.2 conversation-service 新增端点

#### `POST /api/conversations/{conversation_id}/interactive-response`

接收前端对 SOP 操作卡 / 用户提问的选择，转发给 ops-agent-service。

```yaml
Request:
  requestId: string              # ACP request_id（来自 interactive_request SSE 事件）
  sessionId: string              # ops-agent session_id
  outcome:
    outcome: "selected" | "free_text"
    optionId: string (optional)
    text: string (optional)

Response 200:
  ok: true
```

---

## 四、代码变更清单

> ⚠️ **重要修正（2026-05-08）**：原始文档 T-E1/T-E2/T-E3 建议对 `acp/server.py` 做侵入式 asyncio 改造。
> 经第一性原理分析，这些改动**不必要且会破坏 Streamlit**。下方为修正后的正确最小改动清单。

### 4.0 ops-agent 侧改动汇总（最小改动原则）

| 编号 | 文件 | 改动性质 | 是否必须 | Streamlit / CLI 影响 |
|------|------|---------|---------|---------------------|
| T-E1 | `acp/server.py` | 为 `session_new()` 增加可选 `session_id` 参数（~3 行） | ⚠️ 可选 | 向后兼容，零影响 |
| T-E2 | `server/acp_routes.py`（新建） | 纯新增，~80 行 HTTP 包装层 | ✅ 必须 | 纯新增，零影响 |
| T-E3 | `server/main.py` | 注册路由，lifespan 初始化 ACPServer（~10 行） | ✅ 必须 | 新增代码，不修改原逻辑 |
| ~~T-E旧1~~ | ~~`acp/server.py`~~ | ~~threading.Event → asyncio.Event~~ | ❌ **禁止** | **会破坏 Streamlit** |
| ~~T-E旧2~~ | ~~`acp/console.py`~~ | ~~所有交互方法改为 async def~~ | ❌ **禁止** | **acp/ 不需改动** |
| ~~T-E旧3~~ | ~~`acp/server.py`~~ | ~~session_prompt 改为 asyncio.create_task~~ | ❌ **禁止** | **会破坏 Streamlit** |

**不改动的文件（明确保护）**：
- `ops_agent/acp/server.py`（除可选的 T-E1 微小改动）
- `ops_agent/acp/console.py`（完全不变）
- `ops_web/streamlit_app.py`（完全不变）
- `ops_agent/agent/base_agent.py`（feature-hci 已有变更，不再增加）

### 4.1 ops-agent 仓库

#### T-E1（可选）：session_new() 增加 session_id 参数（`acp/server.py`）

**动机**：允许 htp 侧直接传入 `conversation_id` 作为 ACP session ID，消除 htp 内部维护映射表的需求。

```python
# 变更前（acp/server.py）
def session_new(self, *, cwd: str | None = None) -> JsonDict:
    session_id = f"sess_{secrets.token_hex(8)}"
    # ...

# 变更后（仅 3 行改动，完全向后兼容）
def session_new(self, *, cwd: str | None = None, session_id: str | None = None) -> JsonDict:
    if session_id is None:
        session_id = f"sess_{secrets.token_hex(8)}"
    # ...（其余完全不变）
```

**对 Streamlit 的影响**：零。Streamlit 调用 `session_new()` 时不传 `session_id`，行为与之前完全相同。

#### T-E2（必须）：新增 ACP HTTP 路由（`server/acp_routes.py` 新建）

关键设计：`POST /acp/sessions/{id}/prompt` **立即返回 202**，Agent 在 `threading.Thread` 中运行（与 Streamlit 同款模式），`GET /acp/sessions/{id}/events` 用 `asyncio.to_thread()` 轮询同步 `drain_outbox()`。

```python
"""ACP HTTP 接口路由（T-E2）—— 将 ACPServer 对外暴露为 REST API。

设计原则：acp/server.py 不做 asyncio 改造，
HTTP 层通过 threading.Thread + asyncio.to_thread 桥接同步与异步。
"""

from __future__ import annotations

import asyncio
import json
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/acp", tags=["acp"])
_acp_server = None   # 在 main.py lifespan 中注入

def init_acp_server(server) -> None:
    global _acp_server
    _acp_server = server

@router.post("/sessions", status_code=201)
async def create_session(body: dict = {}):
    """创建 ACP 会话（同步调用，极快，无需异步包装）。"""
    return _acp_server.session_new(
        cwd=body.get("cwd"),
        session_id=body.get("session_id"),   # T-E1 可选参数
    )

@router.post("/sessions/{session_id}/prompt", status_code=202)
async def submit_prompt(session_id: str, body: dict):
    """立即返回 202，Agent 在后台线程运行（与 Streamlit 同款模式）。"""
    state = _acp_server.get_session_state(session_id=session_id)
    if state.get("activePrompt"):
        raise HTTPException(status_code=409, detail="该会话已有 prompt 正在运行")

    def _run():
        _acp_server.session_prompt(
            session_id=session_id,
            prompt=body.get("prompt", []),
            message_id=body.get("message_id"),
        )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"started": True, "sessionId": session_id}

@router.get("/sessions/{session_id}/events")
async def stream_events(session_id: str):
    """SSE 事件流：以 50ms 间隔轮询 drain_outbox()，直到 session/done。"""
    async def _generate():
        while True:
            # drain_outbox 是同步的，用 asyncio.to_thread 避免阻塞事件循环
            items = await asyncio.to_thread(
                _acp_server.drain_outbox, session_id=session_id
            )
            for item in items:
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if item.get("method") == "session/done":
                    return
            # 心跳（每 30 轮 = 1.5s 发一次，防止 Nginx 断链）
            # 实现：记录循环计数，每 30 次输出 ": heartbeat\n\n"
            await asyncio.sleep(0.05)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@router.post("/sessions/{session_id}/responses/{request_id}")
async def submit_response(session_id: str, request_id: str, body: dict):
    """提交用户对 _ops/request_input 的响应，唤醒挂起的 Agent。"""
    # submit_client_response 内部是 event.set()，极快，无需 to_thread
    try:
        _acp_server.submit_client_response(
            session_id=session_id,
            request_id=request_id,
            result=body.get("result", {}),
        )
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.get("/sessions/{session_id}/state")
async def get_session_state(session_id: str):
    return _acp_server.get_session_state(session_id=session_id)
```

#### T-E3（必须）：main.py 注册路由并初始化 ACPServer

```python
# server/main.py（仅新增部分，不修改现有代码）
from ops_agent.acp.server import ACPServer
from ops_agent.server.acp_routes import router as acp_router, init_acp_server

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 新增：初始化 ACPServer（与 Streamlit 模式共享同一 ACPServer 实现）
    acp_server = ACPServer(agent_factory=create_agent_factory())
    init_acp_server(acp_server)
    yield
    # 清理（无需特殊操作，线程为 daemon=True）

# 注册路由
app.include_router(acp_router)
```

@router.get("/sessions/{session_id}/state")
async def get_session_state(session_id: str):
    server = _require_server()
    return server.get_session_state(session_id=session_id)
```

---

### 4.2 htp 仓库（编号从 T-E4 开始，与 ops-agent 侧 T-E1~T-E3 不重叠）

#### T-E4：brain_port.py 新增 BrainInteractiveRequest

```python
# 新增事件类型（brain_port.py）

@dataclass(frozen=True)
class BrainInteractiveRequest:
    """大脑发出的用户交互请求（SOP操作卡 / 用户提问）。

    ConversationService 收到此事件后，通过 SSE 推送 interactive_request
    事件给前端，前端渲染交互 UI，用户响应后通过
    POST /api/conversations/{id}/interactive-response 回传。
    """

    request_id: str              # ACP request_id，用于 submit_response 关联
    session_id: str              # ops-agent session_id
    kind: str                    # "sop_step" | "info_request"
    title: str                   # 卡片标题
    prompt: str                  # 向用户呈现的问题/引导文本
    options: list[dict]          # [{optionId, name}, ...]
    custom_input: bool = True    # 是否允许自定义文本输入
    metadata: dict = field(default_factory=dict)  # 额外元数据（route, risk_notice 等）

# 更新联合类型
BrainEvent = BrainTextChunk | BrainStageUpdate | BrainEscalation | BrainInteractiveRequest
```

#### T-E5：OpsAgentBrainAdapter 升级为 ACP 协议客户端

```python
# ops_agent_brain_adapter.py — 核心改造示意

class OpsAgentBrainAdapter:
    """ops-agent ACP 客户端适配器（方案E重构版）。

    不再直接调用 /v1/chat/completions（单向 SSE），
    而是作为 ACP 协议客户端，与 ops-agent-service 建立双向交互会话。
    """

    async def process(self, *, session_id: str, messages: list, env_context=None, ...):
        # Step 1: 获取或创建 ACP session
        acp_session_id = await self._get_or_create_acp_session(session_id, env_context)

        # Step 2: 提交本轮用户消息（非阻塞）
        user_query = self._extract_user_query(messages)
        await self._start_prompt(acp_session_id, user_query)

        # Step 3: 持续消费 SSE 事件流，翻译为 BrainEvent
        async for brain_event in self._consume_events(acp_session_id):
            yield brain_event

    async def _consume_events(self, acp_session_id: str) -> AsyncGenerator[BrainEvent, None]:
        """消费 /acp/sessions/{id}/events SSE 流，翻译为 BrainEvent。"""
        async with self._client.stream("GET", f"{self._base_url}/acp/sessions/{acp_session_id}/events") as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = json.loads(line[6:])
                method = data.get("method", "")

                if method == "session/done":
                    return

                if method == "session/update":
                    update = data.get("params", {}).get("update", {})
                    update_type = update.get("sessionUpdate")
                    if update_type == "agent_message_chunk":
                        yield BrainTextChunk(content=update.get("text", ""))
                    elif update_type == "session_info_update":
                        stage = update.get("title") or ""
                        if stage:
                            yield BrainStageUpdate(stage=stage)

                elif method == "_ops/request_input":
                    # 交互请求：翻译为 BrainInteractiveRequest
                    req_id = data.get("id")
                    params = data.get("params", {})
                    request = params.get("request", {})
                    yield BrainInteractiveRequest(
                        request_id=req_id,
                        session_id=acp_session_id,
                        kind=request.get("kind", "info_request"),
                        title=request.get("title", ""),
                        prompt=request.get("prompt", ""),
                        options=request.get("options", []),
                        custom_input=request.get("customInput", True),
                        metadata=request.get("_meta", {}),
                    )

    async def submit_response(
        self, acp_session_id: str, request_id: str, outcome: dict
    ) -> None:
        """将用户响应提交给 ops-agent-service，解除 Agent 阻塞。"""
        await self._client.post(
            f"{self._base_url}/acp/sessions/{acp_session_id}/responses/{request_id}",
            json={"result": {"outcome": outcome}},
        )
```

#### T-E6：ConversationService 处理 BrainInteractiveRequest

```python
# conversation_service.py 中 send_message_stream_only() 的事件分发逻辑扩展

async for event in brain_router.process(...):
    if isinstance(event, BrainTextChunk):
        full_reply += event.content
        yield f"data: {json.dumps({'choices': [{'delta': {'content': event.content}}]})}\n\n"

    elif isinstance(event, BrainStageUpdate):
        yield f"data: {json.dumps({'x_stage_update': {'current_stage': event.stage}})}\n\n"

    elif isinstance(event, BrainInteractiveRequest):
        # 通过 SSE 通知前端展示交互 UI
        interactive_payload = {
            "event": "interactive_request",
            "requestId": event.request_id,
            "acpSessionId": event.session_id,
            "kind": event.kind,
            "title": event.title,
            "prompt": event.prompt,
            "options": event.options,
            "customInput": event.custom_input,
            "metadata": event.metadata,
        }
        yield f"data: {json.dumps(interactive_payload, ensure_ascii=False)}\n\n"
        # 注意：此处不中断 SSE 流，Agent 在 ops-agent-service 侧挂起等待
        # 前端收到事件后展示 UI；用户响应通过 POST 接口独立回传
```

新增路由（`routes/conversations.py`）：

```python
@router.post("/{conversation_id}/interactive-response")
async def submit_interactive_response(
    conversation_id: str,
    body: InteractiveResponseBody,
    service: ConversationService = Depends(get_conversation_service),
):
    """
    接收前端对 ops-agent 交互请求（SOP操作卡/用户提问）的响应，
    转发给 ops-agent-service 解除 Agent 阻塞。
    """
    await service.submit_ops_agent_response(
        acp_session_id=body.acp_session_id,
        request_id=body.request_id,
        outcome=body.outcome.model_dump(),
    )
    return {"ok": True}
```

#### T-E7：前端 SSE 事件处理扩展

现有 SSE 处理已有 `confirm_request` 事件的处理模式（`ConfirmService`/Redis 方案）。`interactive_request` 需要类似但更丰富的 UI：

```typescript
// 现有：confirm_request → 简单的确认/取消弹窗
// 新增：interactive_request → SOP 操作卡片 / 用户提问组件

case 'interactive_request': {
  const { requestId, acpSessionId, kind, title, prompt, options, customInput } = data;
  if (kind === 'sop_step') {
    // 渲染 SOP 操作卡片（route + operation_goal + execution_guidance + reply_options）
    showSOPStepCard({ requestId, acpSessionId, title, prompt, options, customInput });
  } else if (kind === 'info_request') {
    // 渲染用户提问组件（选项列表 + 可选自由文本）
    showInfoRequestCard({ requestId, acpSessionId, title, prompt, options, customInput });
  }
  break;
}

// 用户选择后提交
async function submitInteractiveResponse(requestId, acpSessionId, outcome) {
  await fetch(`/api/conversations/${conversationId}/interactive-response`, {
    method: 'POST',
    body: JSON.stringify({ requestId, acpSessionId, outcome }),
  });
}
```

---

## 五、会话生命周期设计

### 5.1 session_id 映射策略

htp 的 `conversation.id`（UUID）与 ops-agent 的 `acp_session_id`（`sess_{hex}`）需要建立映射：

```
OpsAgentBrainAdapter 内部维护：
  Dict[htp_session_id → acp_session_id]

新对话：
  htp_session_id → 调用 POST /acp/sessions → 获得 acp_session_id → 缓存

会话恢复：
  htp_session_id 已有 acp_session_id → 直接使用（ops-agent 侧 ACPSession 仍在内存）
  ops-agent-service 重启后 ACPSession 丢失 → 重新创建（消息历史由 session_store 恢复）
```

### 5.2 并发控制

`ACPServer._run_lock` 在同步模式下防止多个 Agent 同时运行。在异步模式下，每个 ACPSession 的 `active_prompt` 标志已经保证了单会话串行，但多个不同会话可以并发运行（每个会话是独立的 asyncio.Task）。

`_run_lock` 可以**移除**（异步模式下无意义），替换为每会话的 `active_prompt` 检查。

### 5.3 超时与资源回收

| 场景 | 处理策略 |
|-----|---------|
| 用户 180s 内未响应 interactive_request | ACPClientBridge 设置 `asyncio.wait_for(..., timeout=180)` → 超时后返回空响应，Agent 继续（降级路径） |
| conversation-service SSE 消费端断开 | ops-agent 侧 `asyncio.Queue` 积压事件，outbox 设置容量上限（如 500），超限 Agent 任务被取消 |
| ops-agent-service 重启 | ACPSession 内存状态丢失，conversation-service 收到 ConnectError → raise BrainUnavailableError → 降级 |
| ACPSession 长期不活跃 | 定时任务清理超过 4h 未使用的 ACPSession（与 session_store TTL 对齐） |

---

## 六、验收标准

### 6.1 基础功能

- [ ] ops-agent 正常回复消息，前端可看到完整文本输出（修复运维问题即可验证）
- [ ] 多轮对话：第 5 轮回复中 ops-agent 仍能引用第 1 轮的故障关键词（session_id 机制）

### 6.2 交互完整性

- [ ] Agent 调用 `present_sop_step_instruction` 时，前端弹出 SOP 操作卡片
- [ ] 用户选择选项后，Agent 收到反馈并继续执行（可在 ops-agent 日志中看到用户选择）
- [ ] Agent 调用 `get_info_from_user` 时，前端弹出用户提问组件，Agent 收到答案
- [ ] 180s 超时未响应时，Agent 以降级路径继续（不 hang 死）

### 6.3 降级容错

- [ ] ops-agent-service 停止后，conversation-service 在 10s 内切换到备用助手
- [ ] ops-agent-service 恢复后，新对话自动使用 ops-agent（无需重启 conversation-service）

### 6.4 并发

- [ ] 10 个并发 ops-agent 会话（不同 session_id），互不干扰
- [ ] 同一 session_id 发第二条消息时（第一条还在运行），返回 409 并有友好提示
