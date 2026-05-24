---
status: active
category: architecture
audience: engineer
last_updated: 2026-05-23
owner: team
---

# Agent Service 设计文档

> **文档目的**：描述 agent-service 的架构设计、核心抽象与各 Agent 实现。  
> **关联文档**：[agent基类设计.md](./agent基类设计.md) | [架构设计.md](../架构设计.md) | [对话设计.md](../conversation/对话设计.md)

---

## 一、服务定位

`agent-service` 是 HCI 智能排障平台的 **AI 推理引擎**，职责边界：

| 职责 | 说明 |
|------|------|
| Agent 路由 | 根据 assistant_type 将请求分发到对应 Agent 实现 |
| 推理执行 | 控制 think/act 循环，调用 LLM + 工具 |
| 工具执行 | SCP 平台 API、acli SSH 命令执行 |
| 高危确认 | 高风险操作暂停等待人工确认（ConfirmService） |
| 流式输出 | SSE 流式返回 AgentEvent 给 conversation-service |

**不负责**：对话历史存储、工单生命周期、知识库摄入、Pod 调度。

---

## 二、架构：六边形（Ports & Adapters）

```
┌──────────────────────────────────────────────────────┐
│                     domain/                          │
│  agent_port.py   ← 对外 Protocol（AgentPort）        │
│  base_agent.py   ← 对内 ABC（BaseAgent）             │
└──────────────────────────────────────────────────────┘
         ↑ 实现
┌──────────────────────────────────────────────────────┐
│              adapters/agents/                        │
│  ├── htp/           ← 手写 ReAct 引擎（GLM）         │
│  │   ├── intent_agent.py     ← 分诊（S0，待重构）    │
│  │   ├── diagnostic_agent.py ← 调查+修复（S1-S5，待重构）│
│  │   ├── react_engine.py     ← ReAct 循环引擎        │
│  │   ├── tool_registry.py    ← 工具注册表            │
│  │   └── confirm_service.py  ← 人工确认服务          │
│  ├── ops/           ← ops-agent（ACP 协议）          │
│  │   └── ops_agent_adapter.py                        │
│  ├── pai/           ← pydantic-ai                   │
│  │   └── pai_agent_adapter.py                        │
│  └── agent_router.py         ← 大脑路由器            │
│              adapters/clients/                       │
│  ├── scp_client.py  ← HCI 平台 REST API             │
│  └── acli_client.py ← SSH/acli 命令执行              │
└──────────────────────────────────────────────────────┘
         ↑ HTTP 投递
┌──────────────────────────────────────────────────────┐
│                    routes/                           │
│  agent.py  ← POST /v1/agent/stream                  │
└──────────────────────────────────────────────────────┘
```

**设计原则**：`domain/` 不依赖任何外部框架；`adapters/` 各实现互不干扰；`routes/` 只做 HTTP 投递。

---

## 三、核心抽象：BaseAgent

> 详细设计推导见 [agent基类设计.md](./agent基类设计.md)。

```python
class BaseAgent(ABC):
    def __init__(self, name: str, max_steps: int = 15): ...

    @abstractmethod
    async def think(self, context: list[Message]) -> ToolCall | str:
        """推理：返回 ToolCall（继续循环）或 str（终止返回）"""

    @abstractmethod
    async def act(self, tool_call: ToolCall) -> Observation:
        """行动：执行工具调用，返回观察结果"""

    async def run(self, user_input: str) -> str:
        """主控制循环：think/act 直到终止或超出 max_steps"""
```

**三个核心决策**：

1. **`think()` 返回 `ToolCall | str`**：直接镜像 LLM API 的两种输出（`tool_calls` / `content`），终止条件内嵌于类型，不需要哨兵字符串或隐式状态。
2. **`context: list[Message]`**：决策输入视角（非事后记录），与 OpenAI API 格式对齐，参数名 `context` 优于 `history`。
3. **`max_steps` 是基类职责**：安全基线，所有生产级框架（LangChain、CrewAI 等）都有此约束。

---

## 四、诊断阶段设计

### 4.1 从 S0-S6 到语义命名

原有 S0-S6 命名按数字顺序编码，不表达行为语义。经第一性原理分析，按"行为差异"合并为 4 个阶段：

| 语义阶段 | 对应原阶段 | 本质行为 | 需要工具 |
|---------|-----------|---------|---------|
| **Triage（分诊）** | S0 | 将自然语言映射到 category_id | ❌ 单轮 LLM |
| **Investigation（调查）** | S1-S4 | 多轮追问 + 诊断命令执行 + 根因确认 | ✅ ReAct |
| **Remediation（修复）** | S5 | 执行修复步骤（强制人工确认） | ✅ ReAct + 确认 |
| **Closure（闭环）** | S6 | 验证解决并关闭 | ❌ 轻量 |

**S1-S4 合并依据**：当前所有阶段共用同一个 `DiagnosticAgent.process()`，`diagnostic_stage` 参数**仅影响 Prompt 里的一行描述文字**，无任何代码分支差异——S1-S4 在行为上属于同一类，只是 Prompt 变体。

> **代码现状**：代码和数据库中仍使用 S0-S6 常量（`DiagnosticStage.S0_INTENT` 等）。  
> 语义命名是设计层面的共识，代码层重命名作为独立迭代任务跟踪。

### 4.2 阶段流转

```
用户发起排障请求
        │
        ▼
┌───────────────────┐
│  TriageAgent      │  单轮 LLM，无工具，输出 category_id
│  (Triage / S0)    │
└─────────┬─────────┘
          │ category_id 确认后
          │ → AgentStageUpdate → conversation-service 更新 DB stage=S1
          ▼
┌───────────────────────┐
│ InvestigationAgent    │  ReAct 循环，有工具（acli/scp）
│ (Investigation/S1-S4) │  多轮，直到根因确认
└─────────┬─────────────┘
          │ → AgentStageUpdate stage=S5
          ▼
┌───────────────────┐
│ RemediationAgent  │  ReAct 循环，所有工具调用强制人工确认
│ (Remediation / S5)│
└─────────┬─────────┘
          │ → AgentStageUpdate stage=S6
          ▼
┌───────────────────┐
│   Closure (S6)    │  轻量确认，可复用 InvestigationAgent 或直接 LLM
└───────────────────┘
```

---

## 五、htp Agent 实现（手写 ReAct）

### 5.1 TriageAgent（分诊）

**特征**：单轮 LLM 调用，`think()` 总返回 `str`，循环一步终止。

核心流程：
1. 从 KBClient 获取故障分类列表（5 分钟内存缓存）
2. 构建 4 段式 Prompt：身份 + 环境上下文 + 分类列表 + 输出格式
3. 调用 LLM，解析 `已确认故障分类：{code}` 或候选列表
4. 输出 `AgentStageUpdate(stage="S1", metadata={"category_id": ...})` 或 `AgentInteractiveRequest`（待用户选择）

与 BaseAgent 的关系：
```python
class TriageAgent(BaseAgent):
    async def think(self, context) -> ToolCall | str:
        reply = await self._call_llm(context)
        return reply          # 直接返回 str，run() 一步终止

    async def act(self, tool_call) -> Observation:
        raise NotImplementedError   # 不可达，此 Agent 无工具
```

### 5.2 InvestigationAgent（调查）

**特征**：多轮 ReAct 循环，`think()` 可返回 `ToolCall` 或 `str`。

核心流程：
1. **三轨知识路由**：`KBClient.route_by_category(category_id)` → SOP / KBD / 机制推理
2. 构建 5 段式诊断 Prompt：身份 + 方法论 + 推理模式 + 参考资料 + 工单上下文
3. ReactEngine 执行 ReAct 循环直到根因确认或超步数
4. 高危操作触发 `ConfirmService.request_confirm()` 暂停等待

**工具集**（`ToolRegistry`）：

| 工具 | 用途 |
|------|------|
| `scp_query` | 查询 HCI 平台节点/资源状态 |
| `acli_run` | 在目标 HCI 节点执行诊断命令（只读） |
| `scp_action` | 执行 HCI 平台操作（有风险等级） |

### 5.3 RemediationAgent（修复）

结构与 InvestigationAgent 完全相同，核心区别：**所有工具调用都必须经过 ConfirmService 确认**，无论工具的默认风险等级如何。

### 5.4 ReactEngine（共享流式执行引擎）

**为什么 ReactEngine 在重构后仍然独立存在**：
`BaseAgent.run()` 在 `domain/` 层，只能返回 `str`，不能 `yield AgentEvent`（那是 adapter 层的职责）。
ReactEngine 是在 `htp/` adapter 层负责**流式输出**的引擎，在 think/act 步骤之间插入 `yield AgentEvent`。

**Investigation 和 Remediation 共用同一个 ReactEngine**，区别只是一个 flag：

```python
class ReactEngine:
    def __init__(self, require_all_confirm: bool = False):
        # require_all_confirm=True  → RemediationAgent：所有工具调用都需确认
        # require_all_confirm=False → InvestigationAgent：仅高危工具需确认
        self.require_all_confirm = require_all_confirm
```

执行流程：
```
step 0..max_steps:
  1. yield AgentStageUpdate("thinking")
  2. 调用 think(context) [委托给 BaseAgent 子类]
  3. 如果返回 str → yield AgentTextChunk(final answer) → return
  4. 如果返回 ToolCall：
       require_all_confirm or risk >= HIGH
         → yield AgentInteractiveRequest（暂停，等待 pending_confirm）
       otherwise → 调用 act(tool_call) → append Observation → continue
raise RuntimeError 超步数保护
```

---

## 六、ops-agent 与 pydantic-ai 实现

### 6.1 OpsAgentAdapter（ACP 协议）

通过 ACP REST 协议调用外部 ops-agent 服务：

```
session_new(conv_id)
    → submit_prompt(messages)
    → SSE events（text / stage / interactive）
    → submit_response(用户回答)
```

- ACP session ID 直接复用 htp conversation_id，ops-agent 跨轮次持久化会话上下文
- 支持 `AgentInteractiveRequest`（_ops/request_input → 前端 SOP 操作卡）
- 不可达时 raise `AgentUnavailableError` → AgentRouter 降级到 htp 实现

设计文档：[2026-05-08-ops-agent方案E-ACP-REST接口设计与实现.md](./events/2026-05-08-ops-agent方案E-ACP-REST接口设计与实现.md)

### 6.2 PaiAgentAdapter（pydantic-ai）

使用 pydantic-ai 框架封装，暂为实验性实现，API 与 `AgentPort` 对齐。

---

## 七、AgentPort 与 AgentRouter

### AgentPort（对外接口契约）

`domain/agent_port.py` 定义 conversation-service → agent-service 的调用契约（Protocol，非 ABC）：

```python
@runtime_checkable
class AgentPort(Protocol):
    async def process(
        self,
        *,
        session_id: str,
        messages: list[dict],
        env_context: dict | None = None,
        stream: bool = True,
    ) -> AsyncGenerator[AgentEvent, None]: ...
```

**与 BaseAgent 的区别**：

| | `AgentPort` | `BaseAgent` |
|--|-------------|-------------|
| 层级 | 对外（conv-service 调用） | 对内（htp 实现继承） |
| 模式 | Protocol（鸭子类型，无需继承） | ABC（强制继承） |
| 关注点 | 流式事件输出给调用方 | 控制循环（think/act 迭代） |
| 使用者 | AgentRouter、ConversationService | TriageAgent、InvestigationAgent |

### AgentRouter

`adapters/agents/agent_router.py` 根据 `assistant_type` 路由到对应 Agent：

```python
class AgentRouter:
    def route(self, assistant_type: str) -> AgentPort:
        # "htp-agent" → HTPAgentAdapter（分诊+调查+修复）
        # "ops-agent"  → OpsAgentAdapter（不可达降级到 htp）
        # "pai-agent"  → PaiAgentAdapter
```

`AgentRouter` 是唯一知道"有哪些 Agent 实现"的地方；`ConversationService` 只见 `AgentPort`，不依赖具体实现类。

---

## 八、知识层设计

### 8.1 三轨知识路由（S1+ 阶段）

```
category_id → KBClient.route_by_category()
                         │
               ┌─────────┴─────────┐
               ▼                   ▼
        SOP 命中？            SOP 未命中
        第 1 轨               → KBD 检索
        SOP 步骤全文注入           │
                            ┌──────┴──────┐
                            ▼             ▼
                        KBD 命中？    KBD 未命中
                        第 2 轨        第 3 轨
                        相似案例注入  【机制推理】标注
```

| 轨道 | 触发 | 处理方式 |
|------|------|---------|
| 第 1 轨 SOP | `sop_document.category_id = X` 且已发布 | SOP 步骤全文注入 Prompt |
| 第 2 轨 KBD | SOP 未命中 | BM25 + 向量 + RRF，相似案例摘要注入 |
| 第 3 轨 机制推理 | KBD 也未命中 | 依赖模型训练知识，输出标注【机制推理】|

### 8.2 知识共享策略

当前选择：**方案 A（RAG 无状态）**——Agent 实例本身无状态，知识通过外部 pgvector 检索共享。

```
Agent（无状态，可水平扩展）
    ↓ 每次推理前检索
pgvector（7000+ 案例 RAG）+ SOP 文档（三轨路由）
    ↑ 人工维护和更新
```

长期目标：**方案 C（MCP Server）**——Agent 自主按需调用知识工具，统一接口给多种 AI（htp/ops/pai）。

---

## 九、流式输出：AgentEvent 体系

Agent 通过 `AsyncGenerator[AgentEvent, None]` 流式返回事件；conversation-service 消费后通过 SSE 转发给前端：

| 事件类型 | 含义 | 前端处理 |
|---------|------|---------|
| `AgentTextChunk` | 文本流块 | 打字机效果拼接 |
| `AgentStageUpdate` | 阶段变化 / 知识命中 / category_id 确认 | 进度指示器 + 写 DB |
| `AgentEscalation` | 请求人工升级 | 创建升级工单 |
| `AgentInteractiveRequest` | SOP 操作卡 / 用户输入请求 | 渲染交互 UI，暂停等待 |

**分层说明**：`BaseAgent.run()` 返回 `str`（控制循环层），流式输出是 Adapter 层职责，在 `process()` 里通过 `yield AgentEvent` 实现。两层各司其职，互不耦合。

---

## 十、目录结构

```
backend/agent-service/app/
├── domain/
│   ├── agent_port.py            ← 对外 Protocol + AgentEvent 联合类型
│   └── base_agent.py            ← 对内 ABC（think / act / run）[新建]
├── adapters/
│   ├── agents/
│   │   ├── htp/
│   │   │   ├── intent_agent.py        ← 现状：意图识别（待重构为 TriageAgent）
│   │   │   ├── diagnostic_agent.py    ← 现状：S1-S6（待重构为 Investigation + Remediation）
│   │   │   ├── react_engine.py        ← 共享流式执行引擎（Investigation + Remediation 共用）
│   │   │   ├── tool_registry.py       ← 工具注册表
│   │   │   └── confirm_service.py     ← 人工确认服务
│   │   ├── ops/
│   │   │   └── ops_agent_adapter.py   ← ACP 协议客户端
│   │   ├── pai/
│   │   │   └── pai_agent_adapter.py   ← pydantic-ai 实现
│   │   └── agent_router.py            ← 大脑路由器
│   └── clients/
│       ├── scp_client.py              ← HCI 平台 REST API 客户端
│       └── acli_client.py             ← SSH/acli 执行客户端
└── routes/
    └── agent.py                       ← POST /v1/agent/stream
```

> `core/` 和 `services/` 已删除（"不为未来设计"原则，两者原为空目录）。

---

## 十一、重构路线图

| 任务 | 状态 | 说明 |
|------|------|------|
| BaseAgent ABC 创建 | ✅ 已完成 | `domain/base_agent.py` |
| agent基类设计.md | ✅ 已完成 | 详细设计推导文档 |
| TriageAgent 重构 | ⏳ 待做 | `intent_agent.py` → `triage_agent.py`，继承 BaseAgent |
| InvestigationAgent 重构 | ⏳ 待做 | `diagnostic_agent.py`（S1-S4）→ `investigation_agent.py` |
| RemediationAgent 重构 | ⏳ 待做 | `diagnostic_agent.py`（S5）→ `remediation_agent.py` |
| 代码层语义命名 | ⏳ 待做 | `DiagnosticStage.S0_INTENT` → `DiagnosticStage.TRIAGE` 等 |

---

## 十二、变更历史

| 日期 | 版本 | 变更摘要 |
|------|------|---------|
| 2026-05-23 | v2.0 | 全量重写：引入 BaseAgent、语义阶段命名（Triage/Investigation/Remediation/Closure）、六边形架构说明、重构路线图；原文件 AI助手设计.md 重命名为 agent设计.md |
| 2026-05-21 | v1.6 | 原 AI助手设计.md：配置迁移至 dashscope 网关，移除本地 Pod 池架构 |
| 2026-05-07 | v1.1 | 原 AI助手设计.md：重构 agent-service 目录结构 |
