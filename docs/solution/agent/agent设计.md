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

## 八、Agent三轨知识路由

### 8.1 整体架构

```
用户输入
   │
   ▼ AgentRouter
S0 → TriageAgent（意图识别）
        │ 输出 category_id
        ▼
S1-S4 → InvestigationAgent（诊断调查）
             │ KBClient.route_by_category()
             ├── 第1轨: SOP 命中
             ├── 第2轨: KBD 命中（无 SOP）
             └── 第3轨: 均未命中
        ▼
S5 → RemediationAgent（修复执行）
```

### S0：意图识别（两种情况共用）

[TriageAgent](vscode-file://vscode-app/c:/Users/tomtu/scoop/apps/vscode/1.121.0/f6cfa2ea24/resources/app/out/vs/code/electron-browser/workbench/workbench.html) 获取全部分类列表，将用户描述 + 环境上下文（告警日志、任务日志）注入 prompt，单次 LLM 调用输出：

- **特征明确** → 直接确认 [category_id](vscode-file://vscode-app/c:/Users/tomtu/scoop/apps/vscode/1.121.0/f6cfa2ea24/resources/app/out/vs/code/electron-browser/workbench/workbench.html)（如 `虚拟机-003`）
- **特征模糊** → 提出 1 个澄清问题 + 2 个候选分类让用户确认

---

### S1-S4：诊断调查——三轨路由

S0 确认 [category_id](vscode-file://vscode-app/c:/Users/tomtu/scoop/apps/vscode/1.121.0/f6cfa2ea24/resources/app/out/vs/code/electron-browser/workbench/workbench.html) 后，[InvestigationAgent](vscode-file://vscode-app/c:/Users/tomtu/scoop/apps/vscode/1.121.0/f6cfa2ea24/resources/app/out/vs/code/electron-browser/workbench/workbench.html) 调用 [kb-service GET /api/kb/route](vscode-file://vscode-app/c:/Users/tomtu/scoop/apps/vscode/1.121.0/f6cfa2ea24/resources/app/out/vs/code/electron-browser/workbench/workbench.html) 进行三轨串行路由：

#### ✅ 匹配到 SOP（第1轨）

```
route_by_category() → track="sop"
         │
         ▼ _process_sop_mode()
SOP 全文注入 System Prompt
         │
         ▼ LLM 直接推理（流式）
「请严格按上述排障流程执行，在每个判断节点收集证据后再做决策」
```

- **行为**：LLM 按 SOP 步骤顺序推理，在判断节点主动收集证据后决策，严格区分临时修复和永久方案
- **事件流**：[AgentStageUpdate(stage="sop_reasoning")](vscode-file://vscode-app/c:/Users/tomtu/scoop/apps/vscode/1.121.0/f6cfa2ea24/resources/app/out/vs/code/electron-browser/workbench/workbench.html) → [AgentTextChunk(流式文本)](vscode-file://vscode-app/c:/Users/tomtu/scoop/apps/vscode/1.121.0/f6cfa2ea24/resources/app/out/vs/code/electron-browser/workbench/workbench.html)

---

#### ✅ 匹配到 KBD（第2轨，无 SOP）

```
route_by_category() → track="kbd"
         │
         ▼ KBDDiagnostic.diagnose()（贪心消除算法）
         
1. 检索 top-15 候选 KBD（kb_client.search_cases_with_steps）
2. 贪心循环（候选数 > early_stop_threshold=2 时）：
   - 统计各步骤工具在候选 KBD 中的覆盖频率
   - 选覆盖频率最高的工具（最具区分度）
   - 执行工具命令 → 获取实际输出
   - 判断各 KBD 的期望模式是否匹配（regex/contains/LLM 批判）
   - 过滤掉不匹配的 KBD
3. 剩余 ≤ 2 个候选时停止，生成诊断报告
```

- 期望模式判断优先级：__REGEX__: 正则匹配 > __CONTAINS__: 包含文本 > 自然语言（LLM 批量判断）
- 工具连续失败保护：超过 3 次连续失败时中止循环
- 事件流：kbd_diag_start → kbd_diag_step（每步）→ kbd_diag_running → kbd_diag_complete → AgentTextChunk(诊断报告) → AgentStageUpdate(stage="S4")

---
#### ❌ 均未匹配（第3轨 → 机制推理降级）

```
route_by_category() → track="human_escalation" 或 raw_cases=[]
         │
         ▼ _process_fallback_mode()
System Prompt 注入「机制推理模式」规则：
  - 所有推断必须标注【机制推理】
  - 回复末尾追加：「如能提供更具体报错信息可尝试匹配精确流程」
         │
         ▼ LLM 基于训练知识自由推理
```

- **行为**：LLM 用 HCI 平台架构知识推理，但强制标注不确定性，主动引导用户提供更多信息以触发后续知识匹配
- **事件流**：[AgentStageUpdate(stage="mechanism_reasoning")](vscode-file://vscode-app/c:/Users/tomtu/scoop/apps/vscode/1.121.0/f6cfa2ea24/resources/app/out/vs/code/electron-browser/workbench/workbench.html) → [AgentTextChunk(流式文本)](vscode-file://vscode-app/c:/Users/tomtu/scoop/apps/vscode/1.121.0/f6cfa2ea24/resources/app/out/vs/code/electron-browser/workbench/workbench.html)

---

### 核心差异对比

| 维度       | SOP 命中       | KBD 命中              | 均未命中       |
| -------- | ------------ | ------------------- | ---------- |
| **推理方式** | LLM 按权威步骤推理  | CDD 贪心消除算法 + LLM 报告 | LLM 机制推理   |
| **确定性**  | 高（有操作手册）     | 中（相似案例收敛）           | 低（纯模型知识）   |
| **工具调用** | 被动（LLM 提出）   | 主动（CDD 引擎驱动）        | 被动（LLM 提出） |
| **标注要求** | 无            | 无                   | 必须标注【机制推理】 |
| **引导策略** | 按 SOP 节点收集证据 | 逐步消除候选到 ≤2 个        | 主动引导补充信息   |

---

## 九、流式输出：AgentEvent 体系

Agent 通过 `AsyncGenerator[AgentEvent, None]` 流式返回事件；conversation-service 消费后通过 SSE 转发给前端：

| 事件类型 | 含义 | 前端处理 |
|---------|------|---------|
| `AgentTextChunk` | 文本流块 | 打字机效果拼接 |
| `AgentStageUpdate` | 阶段变化 / 知识命中 / category_id 确认 | 进度指示器 + 写 DB |
| `AgentEscalation` | 请求人工升级 | 创建升级工单 |
| `AgentInteractiveRequest(kind="variable_input")` | SOP 变量输入请求（无法自动获取时邀请用户填写） | 渲染变量输入卡片，暂停等待 |
| `AgentInteractiveRequest(kind="sop_node_confirm")` | SOP 高危节点确认（执行写操作前需用户确认） | 渲染确认卡片，暂停等待 |
| `AgentInteractiveRequest(kind="tool_confirm")` | ReAct 高危工具确认（risk_level≥2） | 渲染操作卡片，暂停等待 |

**分层说明**：`BaseAgent.run()` 返回 `str`（控制循环层），流式输出是 Adapter 层职责，在 `process()` 里通过 `yield AgentEvent` 实现。两层各司其职，互不耦合。

---

## 十、目录结构

```
backend/agent-service/app/
├── memory/                         ← Agent 工作记忆（MemGPT 外化工作记忆）
│   ├── __init__.py
│   └── variable_pool.py            ← 变量池 JIT 获取引擎（T-AGT-25）
│                                      VariableRequestResult + sop_request_variable
├── tools/                          ← Agent 公共工具实现（LLM tool_call 调用的目标）
│   ├── __init__.py
│   ├── base.py                     ← ToolDefinition 基础模型（供各 agent 工具注册表复用）
│   ├── shell/                      ← Shell 执行工具（bash_exec / acli_exec）
│   │   ├── __init__.py             ← 导出 bash_exec, acli_exec, ShellResult
│   │   ├── classifier.py           ← RiskClassifier（bash + acli 双模式风险分类）
│   │   └── executor.py             ← BridgeRelayExecutor（bridge 中转唯一执行后端）
│   └── sop/
│       ├── __init__.py
│       ├── client.py               ← ConversationSopClient（SOP 执行状态 HTTP 客户端）
│       └── nav.py                  ← get_sop_node + sop_advance 工具实现
├── domain/
│   ├── agent_port.py               ← 对外 Protocol + AgentEvent 联合类型
│   └── base_agent.py               ← 对内 ABC（think / act / run）
├── adapters/
│   ├── agents/
│   │   ├── htp/
│   │   │   ├── triage_agent.py        ← ✅ S0 意图识别（替换 intent_agent.py）
│   │   │   ├── investigation_agent.py ← ✅ S1-S4 三轨路由（替换 diagnostic_agent.py S1-S4）
│   │   │   ├── remediation_agent.py   ← 🔄 S5 修复执行（重构进行中）
│   │   │   ├── react_engine.py        ← ✅ 共享流式执行引擎（Investigation + Remediation 共用）
│   │   │   ├── tool_registry.py       ← 工具注册表（ToolDefinition 从 tools.base 导入）
│   │   │   ├── confirm_service.py     ← ✅ 人工确认服务（Redis BRPOP 确认回路）
│   │   │   └── sop_tools.py           ← 🔧 SopToolExecutor（执行协调层，分发到 tools.sop + memory）
│   │   ├── ops/
│   │   │   └── ops_agent_adapter.py   ← ACP 协议客户端
│   │   ├── pai/
│   │   │   └── pai_agent_adapter.py   ← pydantic-ai 实现
│   │   └── agent_router.py            ← 大脑路由器
│   └── clients/
│       ├── scp_client.py              ← HCI 平台 REST API 客户端
│       └── acli_client.py             ← ⚠️ 已废弃直连路径（asyncssh 直连 HCI）
│                                         新路径：tools/acli/executor.py BridgeRelayExecutor
└── routes/
    └── agent.py                       ← POST /v1/agent/stream
```

> **记忆与工具分层设计**（见 `docs/solution/agent/agent记忆设计.md`）：
> - `memory/` = 工作记忆层：`variable_pool.py` 实现 SOP 上下文变量的 JIT 获取，以 PostgreSQL `context_variables JSONB` 为外化工作记忆存储后端（MemGPT 模式）。变量赋值策略：`env_injection`（环境注入）/ `user_input`（用户输入）/ `user_confirm`（用户确认）/ `tool_call`（工具调用）/ `llm_inference`（LLM 推断）/ `agent_pass`（Agent 传递）。
> - `tools/` = 工具实现层：存放 LLM 通过 `tool_call` 调用的工具函数实现，与工具声明（`TOOL_REGISTRY`）分离，实现声明与实现解耦。`ToolDefinition` 从 `tools.base_tool` 统一导入，供 htp/ops/pai 各 agent 的注册表复用。`tools/acli/` 包含 `bash_exec`（通用 Linux Bash，acli 不覆盖时使用）和 `acli_exec`（HCI 专有 CLI，主力工具）两个通用执行工具，统一通过 `BridgeRelayExecutor` 经由 `terminal_bridge.exe` 中转执行（见 `docs/solution/agent/agent工具设计.md`）。`TOOL_REGISTRY` 在 v2.0 改为启动时从 `tool_definition` 数据库表加载（DB 为 SSOT，废弃代码硬编码）。
> - `adapters/clients/acli_client.py` 的 asyncssh 直连路径已废弃（HCI 在客户私网，云端不可达）。所有 acli/bash 命令执行路径为：`Agent → BridgeRelayExecutor → Redis → Conversation Service SSE → Frontend → terminal_bridge.exe → SSH → HCI`。

---

## 十一、重构路线图

| 任务 | 状态 | 说明 |
|------|------|------|
| BaseAgent ABC 创建 | ✅ 已完成 | `domain/base_agent.py` |
| agent基类设计.md | ✅ 已完成 | 详细设计推导文档 |
| TriageAgent 重构 | ✅ 已完成 | `intent_agent.py` → `triage_agent.py`，继承 BaseAgent |
| InvestigationAgent 重构 | ✅ 已完成 | `diagnostic_agent.py`（S1-S4）→ `investigation_agent.py`，含三轨路由 |
| RemediationAgent 重构 | 🔄 进行中 | `diagnostic_agent.py`（S5）→ `remediation_agent.py`（T-AGT-12） |
| 代码层语义命名 | ⏳ 待做 | `DiagnosticStage.S0_INTENT` → `DiagnosticStage.TRIAGE` 等 |
| SOP 执行引擎 M1（数据库） | ✅ 已完成 | `sop_execution` 表创建（T-AGT-18） |
| SOP 执行引擎 M2（导航工具） | 🔄 进行中 | `get_sop_node`、`sop_advance` 实现（T-AGT-20/21） |

---

## 十二、SOP 全链路设计分析（2026-06 深度复盘）

> 本章记录 2026-06 代码深度分析的结论，包括 SOP 生产/消费阶段的现状确认与已知问题。

### 12.1 SOP 生产阶段（现状确认）

#### 导入校验机制

| 阶段 | 接口 | 校验行为 |
|------|------|---------|
| **导入（ingest）** | `POST /api/sop/ingest` | ⚠️ **无格式校验**：直接将 `content_md` 存入 `sop_document` 表，状态设为 `draft`，不调用 `parse_sop_markdown` |
| **审核发布（approve）** | `POST /api/admin/sop/{id}/approve` | ✅ **调用 `parse_sop_markdown`**，强校验多叉决策树结构（叶节点完整性、标题语义分类等） |

**结论**：
- 导入阶段不会因为格式问题而报错，任何内容均可入库（状态 `draft`）
- 格式问题推迟到"SOP 管理审核"阶段处理，符合设计意图
- `parse_sop_markdown` 返回 `SOPValidationResult`：`is_valid=False` 时 `tree=None`，但文档仍会被发布（`status → published`），只是 `tree_json = NULL`、`tree_validation_status = 'error'`
- ⚠️ **已知缺陷**：发布时若决策树解析失败，系统仅在服务日志输出警告，前端管理页面**无告警提示**，用户不知道决策树缺失

#### sop_tree 生成与同步机制

```
内容入库（draft）
    ↓  手动触发
POST /api/admin/sop/{id}/approve
    ↓  parse_sop_markdown(content_md)
    ├── is_valid=True  → tree_json 写入 DB，tree_validation_status='valid'/'warnings'
    └── is_valid=False → tree_json=NULL，tree_validation_status='error'（文档仍发布）
```

**内容更新（重新 ingest）时的同步逻辑**：
- 若 `source_id` 相同但 `docx_hash` 不同（内容变更）：自动重置 `tree_json=None`、`status='draft'`、`published_at=None`
- 需重新走审核发布流程才能重建 sop_tree
- ✅ **一致性保障**：通过"内容变更强制 draft"机制确保 `content_md` 与 `tree_json` 不会不一致

**⚠️ 未覆盖的场景**：
- 若运维人员直接通过数据库/SQL 修改 `content_md`，不会触发 `tree_json` 重置
- 目前无管理界面的"在线编辑 SOP 内容"功能，风险可控

**markdown 内容与 sop_tree 一致性**：
- 通过"内容变更 → 强制 draft + 清空 tree"机制保障，设计合理。

---

### 12.2 SOP 消费阶段（现状确认）

### 12.2 htp-agent 问题清单与解法

> **涉及文件**：`backend/agent-service/app/main.py`、`app/adapters/agents/htp/investigation_agent.py`、`backend/kb-service/app/routes/route.py`、`backend/kb-service/app/routes/admin.py`

| 编号 | 问题描述 | 根因定位 | 运行时影响 | 严重程度 | 解法方案 | 涉及文件 |
|------|---------|---------|-----------|---------|---------|---------|
| **P1** | `main.py` 用旧参数名 `intent_agent` / `diagnostic_agent` 组装 `AgentRouter`，但构造函数期望 `triage_agent` / `investigation_agent`；同时实例化的是旧类 `IntentAgent` / `DiagnosticAgent` 而非新类 | `AgentRouter.__init__` 重构后参数名变更，`main.py` 未同步 | 服务启动即 `TypeError`，所有 htp-agent 请求失败 | 🔴 CRITICAL | 1. `main.py`：将 `IntentAgent(...)` 替换为 `TriageAgent(...)`；将 `DiagnosticAgent(...)` 替换为 `InvestigationAgent(...)`<br>2. 修正 `AgentRouter(intent_agent=..., diagnostic_agent=...)` → `AgentRouter(triage_agent=..., investigation_agent=...)` | `app/main.py` |
| **P2** | `kb-service` SOP 路由查询仅按 `updated_at DESC` 排序，完全不参考 `query` 字段，无法保证返回最相关的 SOP | `backend/kb-service/app/routes/route.py` 中 SOP 轨 SQL 无语义相似度计算 | 同分类下有多篇 SOP 时，返回的是最新修改的而非最匹配的，可能注入错误 SOP | 🟡 高 | 方案A（短期）：在 `route.py` 中对 `content_md` 用 `ts_rank` 全文检索排序<br>方案B（长期）：为 `sop_document` 加向量列，`route_by_category()` 改为向量相似度排序 | `backend/kb-service/app/routes/route.py` |
| **P3** | SOP 路由返回 `top_k=3` 候选，`investigation_agent.py` 仅取 `sop_results[0]`，第 2、3 候选被完全忽略 | `investigation_agent.py` 第 162 行：`sop_content = sop_results[0].get("content_md", "")` | 命中质量下降（与 P2 叠加时最坏情况是用了排第1但最不相关的 SOP） | 🟡 中 | P2 修复后按相关性排序，此时取 `[0]` 即最优候选，逻辑合理；**若希望多 SOP 融合**，可将 `top_k` 截取结果拼接注入（需配合 P4 的 token 保护） | `app/adapters/agents/htp/investigation_agent.py` |
| **P4** | `_build_sop_prompt()` 将完整 `content_md` 直接拼入 system prompt，无 token 上限保护。大型 SOP（>10000 字符）可能超出 LLM 上下文窗口，或大幅压缩对话历史空间 | `_build_sop_prompt()` 无截断逻辑 | 超长 SOP 下：输出质量严重下降（幻觉增多）或 API 返回 400/截断错误 | 🟡 高 | 短期：在 `_build_sop_prompt()` 中对 `sop_content` 按字符数截断（如 `sop_content[:8000]` + 截断提示）<br>长期：改用决策树 + 滑动窗口（见 P4-LT 说明） | `app/adapters/agents/htp/investigation_agent.py` |
| **P4-LT** | （P4 长期方案）SOP 全文注入替换为"多叉决策树 + 滑动窗口"：每轮只注入当前节点及直接子节点（≈500 token），避免幻觉；`tree_json` 已在 kb-service 生成，只差消费端实现 | htp-agent `_process_sop_mode()` 不使用 `tree_json`；`KBClient` 已有 `GET /api/sop/{id}/tree` 接口 | 当前全文注入在长 SOP 场景下导致严重幻觉；移植后 context 可控，推理质量大幅提升 | 🟡 高（独立迭代） | 1. `investigation_agent.py`：SOP 命中后调用 `kb_client.get_sop_tree(document_id)`<br>2. 会话状态存储 `current_node_id`（初始为根节点 `n-1`）<br>3. 每轮注入：`当前节点.diagnosis + 子节点列表`<br>4. LLM 输出 `next_node_id` → 移动窗口；叶节点时注入 `solution` | `app/adapters/agents/htp/investigation_agent.py`<br>`shared/clients/kb_client.py` |
| **P5** | SOP 模式走纯文本流（`chat_completion_stream`），不带工具定义，LLM 只能"建议用户执行命令"而无法自动通过 ReAct 调用工具执行验证 | `_process_sop_mode()` 不走 `ReactEngine`，不注册工具 | SSH 已连接时无法自动执行命令并将结果回填 Prompt；命令准确性无法验证 | 🟡 高（独立迭代） | **SSH 已连接 + 已授权**：改造 `_process_sop_mode()` 传入工具列表（`acli_execute` 等），走工具调用循环<br>**SSH 未连接 / 未授权**：SOP 中命令通过 `AgentInteractiveRequest` 渲染操作卡片（每次 1-2 个），等待用户确认后执行 | `app/adapters/agents/htp/investigation_agent.py`<br>`app/domain/agent_port.py` |
| **P6** | SOP 命中后 `_process_sop_mode()` 仅 yield `AgentStageUpdate(stage="sop_reasoning", metadata={sop_title})`，没有带 `sop_document_id`，下游无法将此次命中写入数据库或做统计分析 | `AgentStageUpdate` 事件定义中有 `sop_document_id` 字段，但 `_process_sop_mode()` 调用时未传入 | SOP 命中统计缺失；无法追踪哪篇 SOP 在诊断中被使用；`sop_document.hit_count` 无法递增 | 🟡 中 | `_process_sop_mode()` 参数增加 `sop_document_id: int`；`AgentStageUpdate` 中补全该字段；kb-service 侧接收后更新 `hit_count` | `app/adapters/agents/htp/investigation_agent.py`<br>`app/domain/agent_port.py` |
| **P7** | `POST /api/admin/sop/{id}/approve` 在 `parse_sop_markdown` 返回 `is_valid=False` 时，文档仍被发布（`status=published`），仅在服务日志打 warning，前端返回体中 `tree_generated=false` 但管理页面不展示任何警告 | `admin.py` approve 接口的"树解析失败仍发布"的设计决策 + 前端未消费 `tree_generated` 字段 | SOP 管理员以为发布成功，但实际三轨路由时决策树不可用，退化为全文注入模式（叠加 P4 风险） | 🟢 低 | 后端：`tree_generated=false` 时 HTTP 响应追加 `warnings` 列表；或在 `tree_validation_status='error'` 时返回 400 拒绝发布（更严格）<br>前端：`SOP 管理页面` 展示 `tree_validation_status` 状态徽章，error 状态标红 | `backend/kb-service/app/routes/admin.py`<br>前端 SOP 管理组件 |

---

### 12.3 pai-agent 问题清单与解法

> **涉及文件**：`backend/agent-service/app/main.py`、`app/adapters/agents/pai/pai_agent_adapter.py`

| 编号 | 问题描述 | 根因定位 | 运行时影响 | 严重程度 | 解法方案 | 涉及文件 |
|------|---------|---------|-----------|---------|---------|---------|
| **PA1** | `main.py` 第 179 行 `PaiAgentAdapter` 构造时 `scp_client=None`，导致 `get_active_alerts`、`get_vm_list`、`get_failed_tasks`、`get_cluster_detail` 四个工具在运行时均返回 `{"error": "KB 服务不可用，无法获取 SOP 决策树"}` 风格的错误（注：错误信息写的是 KB 但实为 SCP 未注入） | `main.py` 初始化 pai-agent 时未传入 `scp_client` 实例 | pai-agent 的 4 个 SCP 查询工具全部失效，LLM 无法获取任何 HCI 平台实时状态，只能依靠上下文文本推理，准确率严重下降 | 🔴 高 | `main.py`：`PaiAgentAdapter(scp_client=scp_client, ...)` 改为从已有 `scp_client` 实例注入（与 htp-agent 共享同一 `SCPClient` 实例即可） | `app/main.py` |
| **PA2** | `get_sop_tree(document_id)` 工具要求 LLM 传入具体的 `document_id`，但 pai-agent 完全绕过三轨路由（`kb_client.route_by_category`），LLM 在无任何先验信息的情况下无法知道正确的 document_id | pai-agent 架构设计：LLM 自主决策工具调用顺序，不走 `InvestigationAgent._route()` 分支 | LLM 要么不调用 `get_sop_tree`（放弃 SOP 知识），要么猜一个 ID（必然错误），实际上 SOP 功能完全不可用 | 🟡 高 | **方案A（推荐）**：在 pai-agent 的 system prompt 中注入 `category_id`，增加 `search_sop_by_category(category_id)` 工具，内部调用 `kb_client.route_by_category()` 得到 document_id，再由 LLM 决定是否进一步调用 `get_sop_tree`<br>**方案B**：pai-agent 流入口强制先查路由，将命中的 `document_id` 注入 deps（通过 `env_context`） | `app/adapters/agents/pai/pai_agent_adapter.py`<br>`shared/clients/kb_client.py` |
| **PA3** | pydantic-ai 内部工具调用循环（最多 15 轮）完全不对外暴露：不 yield `AgentStageUpdate`，不写任何工具调用日志到数据库，对外只有最终文本流。无法追踪 LLM 究竟调用了哪些工具、结果如何 | pydantic-ai `stream_text(delta=True)` 的设计：只暴露最终文字输出；内部工具循环在 pydantic-ai 框架内部处理 | 生产环境无法排查 pai-agent 的推理路径；A/B/C 测试时无法比较工具调用策略；SRE 无法判断是模型问题还是工具调用问题 | 🟡 中 | 在 `pai_agent_adapter.py` 的 `stream()` 中，利用 pydantic-ai 的 `on_tool_start` / `on_tool_end` 回调（或 `StreamedRunResult.all_messages()` 结果）拦截工具调用事件，yield `AgentStageUpdate(tool_name=..., tool_result=...)` | `app/adapters/agents/pai/pai_agent_adapter.py`<br>`app/domain/agent_port.py` |
| **PA4** | `_openai_messages_to_pydantic()` 在消息转换时跳过 `role=system` 的消息（第 183 行附近有注释说明），导致上游传入的 system 级指令（如 htp 注入的故障上下文）被静默丢弃 | OpenAI 格式允许在消息列表任意位置放 system 消息，但 pydantic-ai `ModelMessage` 不支持历史中的 system 消息，转换时只能跳过 | 若调用方将故障摘要、用户权限、会话上下文以 system 消息传入，pai-agent 全部丢失；影响诊断准确性 | 🟡 中 | 在跳过 system 消息前，将其内容合并追加到下一条 user 消息尾部（`\n\n[系统上下文]\n{system_content}`），确保信息不丢失；同时补充日志记录被合并的 system 消息数量 | `app/adapters/agents/pai/pai_agent_adapter.py` |

---

### 12.4 LLM 输出控制（未实现，独立迭代）

**设计目标**（来自需求分析）：
1. **SSH 已连接 + 客户授权自动执行**：SOP 中的命令通过 `tool_call` 方式确认输出，保证命令可正常执行
2. **SSH 未连接 / 未授权自动执行**：Admin 页面正常渲染可执行命令卡片，每次限制输出 1-2 个可执行操作

**现状**：
- `AgentInteractiveRequest` 事件类型已定义（用于 SOP 操作卡）
- `ToolCall` 领域类已定义（用于工具调用控制）
- `DeferredToolRequests`（高危工具需用户确认）在设计文档中有规划（Phase 2）
- ❌ **均未实现**：当前 SOP 模式输出纯文本，命令不区分 SSH 状态，无结构化渲染卡片

**设计方向**（需要独立开迭代）：
```
LLM 输出的命令 → 结构化解析（正则/LLM 标注）→
    ├── SSH 已连接 + 已授权 → tool_call 自动执行 → 结果回写 Prompt
    └── 未连接 / 未授权   → AgentInteractiveRequest 卡片渲染
                             （每次最多 1-2 个可执行操作，等待用户确认）
```

---

### 12.5 深度代码分析：新发现关键问题

> **分析范围**：本节基于对 `diagnostic_agent.py`、`react_engine.py`、`confirm_service.py`、`agent_router.py`、`main.py`、`routes/agent.py`、`conversation-service` 路由及前端 `chat.ts` 的完整代码走读，记录 §12.2 表格之外的额外关键问题。
>
> **架构澄清**：当前实际运行类为 `DiagnosticAgent` + `IntentAgent`（`agent_router.py` 使用），`InvestigationAgent` / `TriageAgent` 已写入但**尚未接入** `AgentRouter`，属于半成品重构。P1（参数名不匹配）需更正，服务可正常启动。

#### P-NEW-1 ｜ `DiagnosticAgent` direct 模式：同步迭代异步生成器（CRITICAL）

| 项目 | 内容 |
|------|------|
| **根因** | `diagnostic_agent.py` 第 217 行：`for chunk in ai_client.chat_completion_stream(...)` ——但 `chat_completion_stream` 是 `async def`，返回 `AsyncGenerator`；在 `async def process()` 中必须用 `async for` 才能迭代 |
| **运行时影响** | 所有 htp-agent **direct 模式**请求（含 SOP 轨、KBD 轨、机制推理）在第一个字符输出前抛出 `TypeError: 'async_generator' object is not iterable`；前端收到 error SSE，无任何输出 |
| **严重程度** | 🔴 CRITICAL——当前 SOP 消费全链路在此处即断裂 |
| **修复** | `diagnostic_agent.py` 第 217 行：`for chunk in` → `async for chunk in` |
| **涉及文件** | `backend/agent-service/app/adapters/agents/htp/diagnostic_agent.py` |

#### P-NEW-2 ｜ `DiagnosticAgent` react 模式：同步迭代异步生成器（CRITICAL）

| 项目 | 内容 |
|------|------|
| **根因** | `diagnostic_agent.py` 第 204 行：`for event in self._react_engine.execute(...)` ——`ReactEngine.execute()` 是 `async def` 返回 `AsyncGenerator`；同上原因必须用 `async for` |
| **运行时影响** | 所有 `execution_mode="react"` 的 htp-agent 请求同样以 `TypeError` 终止；工具调用完全无法触发 |
| **严重程度** | 🔴 CRITICAL——tool_call 端到端（重点 2）在此处即断裂 |
| **修复** | `diagnostic_agent.py` 第 204 行：`for event in` → `async for event in` |
| **涉及文件** | `backend/agent-service/app/adapters/agents/htp/diagnostic_agent.py` |

#### P-NEW-3 ｜ `ReactEngine` 双重执行工具（HIGH）

| 项目 | 内容 |
|------|------|
| **根因** | `react_engine.py` 主循环：① 调用 `async for event in self._execute_tool_call(...)` — 内部已调用 `await self._tool_executor.execute(tool_name, tool_args)` 并写审计日志；② 循环结束后**再次调用** `tool_result = await self._get_tool_result(tc.name, tc.arguments)` — 内部重复执行同一工具。两者均向真实基础设施发起请求 |
| **运行时影响** | **只读工具**（risk_level=1）：多一次无效 I/O，结果可能与第一次不一致（时间差）。**写操作工具**（risk_level=2，如 `acli_service_restart`、`acli_network_nic_up`）：同一操作执行两次，产生重复变更副作用，可能导致服务重启两次、网卡被禁用后立即重新启用等 |
| **严重程度** | 🔴 HIGH——写操作工具有真实副作用风险 |
| **修复** | 删除主循环中多余的 `_get_tool_result()` 调用；`_execute_tool_call()` 改为通过 `return`（或额外 yield 一个结果事件）将工具执行结果传回，主循环直接消费该结果构造 `tool` 消息 |
| **涉及文件** | `backend/agent-service/app/adapters/agents/htp/react_engine.py` |

#### P-NEW-4 ｜ `ConfirmService` 确认回路断裂——risk_level=2 工具永远超时（CRITICAL）

| 项目 | 内容 |
|------|------|
| **根因** | `ReactEngine` 对 risk_level≥2 的工具调用 `await self._confirm_service.request_confirm(session_id, ...)` → 内部执行 `Redis BRPOP confirm:{session_id}`，**阻塞等待 120s**。前端收到 `interactive_request` SSE 事件（kind=`tool_confirm`）后，用户点击确认按钮，前端 POST `conversation-service /interactive-response` → `conversation-service` 调用 `ops_adapter.submit_acp_response()` **（ACP 协议，针对 ops-agent）**。**没有任何代码路径调用 `confirm_service.submit_confirm(session_id, confirmed=True)`（即 `Redis LPUSH`）**，BRPOP 必然超时，返回 `ConfirmResult.TIMEOUT`，工具调用被取消，输出"操作已取消" |
| **运行时影响** | 所有 risk_level=2 的写操作工具（`acli_service_restart`、`acli_network_nic_up` 等）经过用户确认后仍会被取消；前端显示"操作已取消"，用户无法通过 htp-agent 执行任何变更操作 |
| **严重程度** | 🔴 CRITICAL——tool_call 端到端重点 2 的核心确认能力完全缺失 |
| **修复要点** | 需在 `agent-service` 中新增 `POST /v1/agent/react-confirm` 端点，接收 `{session_id, confirmed: bool, authorized_by}` 并调用 `confirm_service.submit_confirm()`；`conversation-service` 的 `submit_interactive_response()` 需根据 `kind` 字段分叉：`kind="tool_confirm"` → 调用 agent-service `/react-confirm`；`kind` 属于 ACP 类型 → 调用 `ops_adapter.submit_acp_response()` |
| **涉及文件** | `backend/agent-service/app/routes/agent.py`<br>`backend/agent-service/app/adapters/agents/htp/confirm_service.py`<br>`backend/conversation-service/app/services/conversation_service.py`<br>`backend/conversation-service/app/routes/conversations.py` |

#### 勘误：P1 描述不准确

§12.2 中 P1 描述的"main.py 用旧参数名组装 AgentRouter 导致 TypeError"**经代码核实不成立**：  
- `agent_router.py` 当前 `__init__` 签名确实是 `intent_agent: IntentAgent, diagnostic_agent: DiagnosticAgent`  
- `main.py` 传参与签名完全匹配，服务可正常启动  

**实际情况**：`InvestigationAgent` / `TriageAgent` 是**新重构类，尚未接入 `AgentRouter`**；`main.py` 需后续迁移至新类。当前阻断启动的问题是 P-NEW-1/2，而非 P1。

---

### 12.6 方案评估：SOP 消费——多叉决策树 + 滑动窗口是否最优？

> **结论先行**：滑动窗口是合理的**上下文管理策略**，但将其实现为"服务端按 node_id 裁剪注入"的方式并非最优。推荐**导航工具化（Navigation-as-Tool）**，将 SOP 树遍历融入 ReactEngine 主循环，消除 SOP/非 SOP 双轨分叉。

#### 五种方案横向对比

| 方案 | 工作原理 | 优点 | 缺点 | 综合评分 |
|------|---------|------|------|---------|
| **① 全文注入**（现状） | 完整 `content_md` 截断到固定字符数（当前 3000 char）注入 system prompt | 实现最简 | 硬截断可能在分支中间截断；LLM 看到所有分支，可能走错路；context 随 SOP 大小线性增长 | ❌ 不可持续 |
| **② 滑动窗口**（P4-LT） | `current_node_id` 存会话状态；每轮注入：当前节点诊断指引 + 直接子节点选项（≈500 token） | Context 可控；结构化分支清晰 | 需额外会话状态字段；线性遍历假设（回退困难）；SOP 模式与工具调用模式仍是两条独立路径 | 🟡 可行但有架构债 |
| **③ 导航工具化**（**推荐**） | 在 `TOOL_REGISTRY` 注册两个 SOP 工具：`get_sop_node(node_id)` → 返回节点内容；`sop_advance(node_id, reasoning)` → 移动到指定子节点并记录推理。SOP 命中后直接走 ReactEngine，无需单独 `_process_sop_mode()` | 导航与工具执行统一在 ReactEngine 内；回退/跳跃由 LLM 工具调用决定（灵活）；消除 SOP/非 SOP 双轨分叉；`current_node_id` 通过工具调用链自然存储在 messages 中，无需单独会话状态 | 需重构 ReactEngine 工具注册使其支持动态注入 SOP 工具（或在 session 初始化时注册）；调用链稍长（每次导航多一轮 LLM → tool） | ✅ **最优** |
| **④ RAG 检索树节点** | 对每个 SOP 节点独立 embedding；每轮根据对话内容检索 Top-K 最相关节点注入 | 无需会话状态；可处理非线性跳转；可跨多篇 SOP 融合 | 需向量基础设施；节点文本较短时 embedding 质量差；检索结果可能跨分支混合（逻辑不连贯） | 🟡 适合 SOP 库大时的长期方向 |
| **⑤ 摘要压缩** | 每轮由 LLM 生成当前诊断进度摘要（已确认事实 + 待验证假设），以摘要替代历史全文 | Context 长度完全可控；可跨 SOP 保持诊断连贯性 | 额外 LLM 调用；摘要损失细节；实现较复杂 | 🟡 适合超长对话的辅助手段 |

#### 导航工具化方案详设（推荐方案③）

```
SOP 命中 → execution_mode 强制改为 "react"
         → TOOL_REGISTRY 动态注入两个 SOP 工具（仅本次会话有效）：
             get_sop_node(node_id: str)    → 返回节点文字 + 子节点列表
             sop_advance(node_id: str, reasoning: str) → 记录推进路径
         → system_prompt 只注入：SOP 标题 + 根节点摘要 + "请从根节点开始"
         → ReactEngine 主循环：
             LLM 决策：执行诊断命令（acli/scp 工具）或 推进 SOP 节点（sop_advance）
             叶节点（solution）：注入最终解法文本，进入直接输出
```

**相比滑动窗口的核心优势**：
- 无独立 SOP 状态机：节点位置由 tool_call 历史隐式记录，不需要 `current_node_id` 字段
- 支持回退：LLM 可以调用 `get_sop_node("n-1")` 从根节点重来
- SOP + 工具执行统一循环：同一轮可以"推进节点"并"执行命令验证"，无需两次请求

**滑动窗口仍有价值的场景**：SOP 节点数量超过 50 个时，作为 `get_sop_node` 的内部实现策略（限制返回的子节点深度），两者不互斥。

---

#### 导航工具化：路径决策完全靠 LLM

**本质上是 LLM 做裁判**。具体来说：

```
get_sop_node('n-root') 返回：
  当前节点：「确认 VM 启动失败的初步现象」
  子节点列表：
    n-1  「当告警显示存储 I/O 错误时」
    n-2  「当 CPU/内存资源不足时」
    n-3  「当任务日志显示网络超时时」
    ...（共10条）

LLM 读取当前诊断上下文 + 这10条条件 →
自主调用 sop_advance('n-1', '告警中出现 CVM-DISK-READ-ERR，匹配存储 I/O 条件')
```

LLM 是这棵决策树的执行引擎。这直接带来两个后果：

**优点**：条件判断可以是模糊语义的（"当磁盘负载异常时"），不需要写精确的布尔表达式，LLM 能跨多个证据综合判断。

**风险**：相同的故障现象，两次运行可能走不同的分支（非确定性）。如果节点条件写得模糊，LLM 可能误判，且**出错时难以追溯**。

**缓解手段**：
- `sop_advance` 的 `reasoning` 参数强制记录判断依据（审计链）
- 节点条件写成精确的"当且仅当"格式，降低歧义

---

#### 滑动窗口

**核心机制：服务端管控"可见窗口"，LLM 只能看到当前帧**

```
会话状态（存 DB）:
  current_node_id = "n-3-7"

每次调用 DiagnosticAgent 时：
  1. 服务端读取 current_node_id → 从 tree_json 取出 n-3-7 节点
  2. 构建"窗口内容" = n-3-7 的完整诊断指引 + 直接子节点摘要（≈500 token）
  3. 将窗口内容注入 system_prompt（替换掉上一轮的窗口）
  4. LLM 推理 → 输出"下一步选择" → 服务端解析 → 更新 current_node_id
  5. 下一轮对话重复上述流程
```

窗口"滑动"的含义：**不是在文本上滑动，而是在树结构上滑动**——每次只暴露树的一个横截面（当前层 + 下一层），随着诊断进展，横截面沿深度方向移动。

**关键特征**：
- LLM 的"记忆"被窗口主动替换，不依赖历史消息里的上下文
- `current_node_id` 是核心状态，服务端显式维护
- LLM 无法"回头看"已经滑出窗口的节点

---

#### 两种方案都必须有状态机，变量管理才是关键

这是两种方案最容易被忽视的共同约束。

**为什么变量管理强制要求状态机**：SOP 里的命令不是静态的——

```
# SOP 原文：
acli vm start {vm_name}
acli network nic up {nic_name} --node-ip={node_ip}
acli service {service_name} restart --confirm

# 这些变量从哪来？
vm_name      ← 第2步 get_vm_list() 时 LLM 确认了是 "prod-vm-001"
nic_name     ← 第4步 acli_network_nic_list() 时发现是 "bond0"
node_ip      ← 第1步 acli_platform_node_list() 时记录了 "10.0.1.5"
service_name ← 第6步 用户在交互卡片中输入了 "libvirtd"
```

**变量的生命周期横跨整个 SOP 执行过程**，必须有地方持久化。如果不存，一旦会话中断恢复、用户刷新页面或下一轮对话 LLM 上下文被截断，变量就丢失了，无法安全执行后续命令。

两种方案都需要的 `SopExecution`（即 `sop_execution` 表的运行时视图）：

```python
@dataclass
class SopExecution:
    sop_document_id: int           # 正在执行哪篇 SOP
    current_node_id: str           # 当前位置（两种方案都需要，用于恢复）
    context_variables: dict        # 运行时变量池
    # {
    #   "vm_name":      "prod-vm-001",
    #   "node_ip":      "10.0.1.5",
    #   "nic_name":     "bond0",
    #   "service_name": "libvirtd"
    # }
    completed_steps: list[str]     # 已完成的 node_id（防重复执行）
    execution_log: list[dict]      # 每步执行结果（可审计）
```

**变量替换发生在哪一层**（重要设计决策）：

| 方案 | 变量替换方式 | 可靠性 |
|------|------------|--------|
| **方案A：LLM 自己替换** | LLM 看到 `tool_result: {"vm_id": "prod-vm-001"}`，自主调用 `acli_vm_start(vm_id="prod-vm-001")` | ❌ LLM 可能出现幻觉或搞错变量，无法在执行前做格式校验 |
| **方案B：状态机替换（推荐）** | LLM 调用 `acli_vm_start(vm_id="{vm_name}")`，工具层执行前从状态机渲染占位符，校验格式后再执行 | ✅ 变量来源明确、可校验、LLM 不需要记住具体值 |

**结论**：状态机是必需的，不是可选的。无论用哪种方案，都需要 `sop_execution` 表。

---

#### 两种方案在状态机上的差异

|  | 滑动窗口 | 导航工具化 |
|--|---------|-----------|
| `current_node_id` 由谁更新 | 服务端解析 LLM 输出后更新 | LLM 调用 `sop_advance()` 工具时更新 |
| 变量如何写入状态 | 服务端从工具结果中提取（需写提取规则） | 通过 `sop_advance` 的 `variables_extracted` 参数，LLM 主动上报 |
| 变量如何用于命令 | 服务端渲染模板后注入窗口（命令行为确定性） | 方案A 或 方案B 均可（推荐方案B） |
| 恢复中断会话 | 读 `current_node_id` + `context_variables` → 重建窗口 | 读同样字段 → 重建工具调用历史摘要 |

---

#### 八维横向对比：滑动窗口 vs 导航工具化

| 维度 | 滑动窗口 | 导航工具化 | 说明 |
|------|---------|-----------|------|
| **Context 增长模式** | ✅ 每轮稳定（窗口替换，不累积） | ❌ 线性增长（每次导航 ~600 tokens 永久留在历史） | 长 SOP（>15步）差距显著 |
| **LLM 迭代次数** | ✅ 少（导航隐含在输出里） | ❌ 多（每层多1次 `get_sop_node` 往返） | 4层→多4次 LLM 调用，约+8s 延迟 |
| **路径决策确定性** | ✅ 选项被服务端约束为当前子节点 | ❌ LLM 可调任意 `node_id`，无约束 | 滑窗能在服务端校验 `next_node_id` 合法性 |
| **命令模板渲染** | ✅ 服务端注入时已渲染占位符 | ❌ LLM 须自行从历史中查找变量值 | 渲染确定性：滑窗 > 工具化 |
| **变量提取** | ❌ 服务端需解析 LLM 输出提取变量 | ✅ LLM 直接看到 `tool_result`，可主动上报 | 工具化更自然 |
| **回退/非线性跳转** | ❌ 需专门的 `sop_back` 机制 | ✅ LLM 直接 `get_sop_node(parent_id)` | 复杂 SOP 中诊断分支可能需要回退 |
| **中断恢复** | ✅ 加载 state→重建窗口，完全确定性 | 🟡 需从 state 重建"历史摘要"注入 | 滑窗恢复更简单可靠 |
| **实现复杂度** | 🟡 需解析 LLM 输出获取 `next_node_id` | ✅ 工具调用本身是结构化的，无需解析 | 工具化不需要 output parser |

**滑动窗口的致命弱点**：LLM 输出 `next_node_id` 后，服务端需要解析——而 LLM 的自由文本输出是不可靠的。即使要求 JSON 格式，也存在格式不合规的风险，且这个解析逻辑游离于 ReactEngine 的工具调用体系之外。

**导航工具化的致命弱点**：每次导航都在 messages 里永久新增 `get_sop_node` 的 request + response（~600 tokens），4层×10叉的完整遍历在上下文里会累积 2400+ tokens 的导航历史，加上诊断工具调用，20步 SOP 的 context 轻松超过 15,000 tokens。

---

#### 最终方案：结构化滑动窗口（Structured Sliding Window）

**核心思路**：用**滑窗管理 context**，用**工具调用结构化输出**替代文本解析——集两者之长，消两者之短。

**决策1：`sop_advance()` 工具取代 LLM 文本输出解析**

```
滑窗原方案：
  Server注入窗口 → LLM输出 {"next_node_id": "n-3"} → 服务端解析 → 更新状态
  风险：LLM 不一定输出合规 JSON

新方案（结构化滑动窗口）：
  Server注入窗口 → LLM 调用 sop_advance(next_node_id="n-3", reasoning="...",
                                           variables_extracted={...})
  结构化工具调用，框架保证格式，服务端直接读取参数
```

`sop_advance` 同时完成三件事：**前进节点 + 上报变量 + 记录推理链**，一次工具调用。

**决策2：变量在窗口注入时预渲染**

```
滑动窗口注入流程：
  ① 加载 current_node 内容
  ② 遍历 context_variables，渲染占位符：
       "acli vm start {vm_name}"
       → 若 vm_name 已知 → "acli vm start prod-vm-001"（确定性）
       → 若 vm_name 未知 → "acli vm start {vm_name} ← 需先通过 get_vm_list 获取"
  ③ 注入渲染后内容

LLM 永远看到的是已解析的具体值或明确的"待获取"提示，
不需要自己从工具历史中"记忆"变量。
```

**决策3：`SopExecution` 作为单一可信源**

```python
@dataclass
class SopExecution:
    sop_document_id: int
    current_node_id: str                    # 当前位置（滑窗定位用）

    context_variables: dict[str, VarEntry]  # 变量池
    # VarEntry = {value, source, confirmed_at}
    # source: "tool_result" | "user_input" | "sop_default"

    completed_steps: list[str]              # 已完成 node_id（防重复执行）
    pending_variable_requests: list[str]    # 正在等待用户填写的变量名
    execution_log: list[StepLog]            # 每步执行记录（审计）
```

**决策4：三工具最小集**

```python
# 工具1：推进 SOP（核心，每次决策用）
sop_advance(
    next_node_id: str,           # 服务端校验：必须是 current 节点的直接子节点
    reasoning: str,              # 决策依据（审计链）
    variables_extracted: dict    # 本轮从工具结果中提取的变量
)

# 工具2：请求用户补充变量（当命令所需变量无法自动获取时）
sop_request_variable(
    variable_name: str,          # 如 "service_name"
    question_for_user: str       # 如 "请确认要重启的服务名称（如 libvirtd、exporter）"
)
# → 触发 AgentInteractiveRequest(kind="variable_input") 卡片，等待用户输入
# → 用户提交后，写入 context_variables["service_name"]

# 工具3：标记当前 SOP 完成（到达叶节点时）
sop_complete(
    summary: str,                # 本次诊断摘要
    success: bool
)
```

无 `get_sop_node` 工具——节点内容由服务端注入，不由 LLM 主动拉取。

---

#### 完整执行流（4层×10叉场景）

```
初始 System Prompt（~700 tokens）:
  身份+方法论 + SOP标题 + 根节点内容 + 10个子节点摘要（仅condition，各30 tokens）
  + 已知变量: {} + 可用诊断工具列表

─── 第1轮 LLM 迭代 ──────────────────────────────────────────
LLM 看到根节点10个分支，需要证据 → 并行调用：
  get_active_alerts()         → 告警: CVM-DISK-READ-ERR
  get_failed_tasks()          → 失败任务: VM prod-vm-001 启动失败

LLM 分析证据后 → 同轮调用：
  sop_advance(
    next_node_id = "n-3",           ← 匹配"存储I/O类故障"分支
    reasoning = "告警CVM-DISK-READ-ERR + VM启动失败任务匹配存储故障路径",
    variables_extracted = {"vm_name": "prod-vm-001"}  ← 顺手提取
  )

服务端：
  ① 校验 n-3 是根节点的合法子节点 ✓
  ② 更新 current_node_id = "n-3"
  ③ 写入 context_variables["vm_name"] = "prod-vm-001"
  ④ 下一轮注入 n-3 的窗口（n-3内容 + 10个L2子节点）

─── 第2轮 LLM 迭代 ──────────────────────────────────────────
System Prompt 中窗口已替换为 n-3 + 其10个L2子节点
节点 n-3 的命令已预渲染: "acli_vm_config prod-vm-001"

LLM 并行调用诊断工具：
  acli_vm_config("prod-vm-001")      → 磁盘挂载配置
  acli_vm_disk_check("prod-vm-001")  → 发现坏块

LLM → sop_advance("n-3-7", "磁盘坏块确认", {})

─── 第3轮 LLM 迭代 ──────────────────────────────────────────
窗口替换为 n-3-7 + 其10个L3子节点
发现命令 "acli storage disk repair {disk_id}" 有未知变量 {disk_id}
→ 窗口注入: "需先通过 acli_storage_disk_list 获取 disk_id"

LLM 调用：
  acli_storage_disk_list(node_ip="10.0.1.5")  → disk_id = "disk-004"

LLM → sop_advance("n-3-7-2", "确认磁盘编号", {"disk_id": "disk-004"})

─── 第4轮 LLM 迭代 ──────────────────────────────────────────
窗口替换为叶节点 n-3-7-2（解决方案节点）
命令已全部渲染: "acli storage disk repair disk-004"

执行修复命令：
  acli_service_restart("libvirtd", "10.0.1.5")  ← risk_level=2，触发确认卡片

用户确认后 → sop_complete("存储坏块修复完成", success=True)
```

**Context 大小全程**：系统提示 ~700 tokens（固定）+ 每轮工具结果累积 + 3次窗口替换（不累积）= 最终约 4500 tokens，与 SOP 树的节点总量无关。

---

#### 最终方案 vs 两个原方案总结

**结构化滑动窗口 = 滑动窗口（Context 管理）+ 导航工具化（结构化输出）的融合**

| 维度 | 纯滑动窗口 | 纯导航工具化 | 结构化滑动窗口（推荐）|
|------|-----------|------------|----------------------|
| Context 增长 | ✅ 不累积 | ❌ 线性增长 | ✅ 不累积（窗口替换） |
| LLM 迭代数 | ✅ 少 | ❌ 多 | ✅ 少（无额外导航迭代） |
| 路径决策确定性 | 🟡 依赖输出解析 | ❌ LLM 自由决策 | ✅ 工具调用 + 服务端校验 |
| 命令模板渲染 | ✅ 注入时预渲染 | ❌ LLM 自行解析 | ✅ 注入时预渲染 |
| 变量管理 | ❌ 需解析提取 | ✅ LLM 主动上报 | ✅ `variables_extracted` 参数 |
| 回退支持 | ❌ 需特殊机制 | ✅ 自然 | 🟡 `sop_back()` 或允许 `sop_advance` 到父节点 |
| 中断恢复 | ✅ 简单 | 🟡 需重建 | ✅ 加载 state → 重建窗口 |
| 实现复杂度 | ❌ 需 output parser | ✅ 工具调用 | ✅ 工具调用（无 parser）|
| 与 ReactEngine 集成 | ❌ 游离在外 | ✅ 天然融合 | ✅ 天然融合 |

**一句话定义**：把滑动窗口的"服务端控制内容注入"和导航工具化的"结构化工具输出"合并——用工具调用做导航决策，用窗口替换控制上下文。

**结论**：§12.7 中的"结构化滑动窗口"设计正是基于这一融合思路。§12.6 的推荐方案③（导航工具化）是出发点，最终采纳的是兼顾 Context 可控性的升级版本。

---

### 12.7 结构化滑动窗口——确认设计细节

> 本节记录 §12.6 推荐方案在以下三个关键维度上的确认设计，作为后续实现的权威规格。

#### 12.7.1 并发多工单支持

**结论：天然支持，无需额外设计。**

`sop_execution` 以 `conversation_id` 为唯一键（见 §12.7.2 关于 ID 选择的讨论），每个工单对应一行独立状态。多个工单并行执行 SOP 时：

- `sop_execution` 表中存在多行，`conversation_id` 互不相同
- `sop_tree` 只读（发布后不可变），多会话并发读取无竞争
- `sop_advance()`、`sop_request_variable()` 工具调用均在各自 `conversation_id` 上下文中执行

**不支持场景**（设计边界）：同一个工单不支持两个浏览器 Tab 同时发起 Agent 调用——这是对话层的通用限制，与 SOP 引擎无关。

---

#### 12.7.2 `sop_execution` 存储设计与中断恢复

##### 关于 ID 的澄清：`conversation_id` 而非 `session_id`

系统中存在两个外形相似但语义不同的 ID：

| 表 | 主键 | 用途 | 生命周期 |
|---|---|---|---|
| `session` | `session_id` | SSE 长连接凭证（api-gateway 维护，Redis 缓存） | 随 SSE 连接建立/过期 |
| `conversation` | `conversation_id` | 一次 AI 对话（conversation-service 维护，PostgreSQL 持久化） | 随对话开始/结束 |

一个工单可以有多个 `conversation`（断线重连创建新 conversation），每个 `conversation` 也可以对应多个 `session`（多次 SSE 重连）。

**`sop_execution` 绑定 `conversation_id`**：SOP 执行进度属于一次 AI 对话的状态，不随 SSE 断连而重置。`conversation` 表中已有 `sop_document_id` 字段作为天然锚点。

##### 关于 BUG-06：为何不在 `conversation` 表加 JSONB 字段

`desired_schema.sql` v6.2 的设计修正注释明确记载：

> **移除 conversation.hypothesis/react_state（JSONB blob 反模式）**

原 JSONB blob 导致三类问题：
1. **并发竞态**：更新数组中一个元素需要应用层 read-modify-write，两个协程并发时产生 lost update（`jsonb_set` 基于各自读到的快照，后写覆盖先写）
2. **无法独立查询**：按 `status=rejected` 过滤需要 `jsonb_array_elements` 展开，关系表直接 `WHERE status='rejected'`
3. **无独立时间戳**：数组元素无 `created_at`/`updated_at`，无法追踪生命周期

解法是将子实体拆成独立表（参考已建的 `diagnostic_item` 表）。

**重要告警（代码核实）**：`diagnostic_item` 表目前处于**半成品状态**——表和模型已建，archive（批量 UPDATE）路径已写，但各阶段 INSERT 路径从未实现，BUG-06 尚未真正修复。`sop_execution` 的实现**必须同时包含完整的 INSERT 路径**，不能重蹈只建表不写入的覆辙。

##### 关于 Redis：`sop_execution` 不需要缓存

全平台使用 Redis 的模块只有：

| 模块 | 用途 |
|---|---|
| `api-gateway` SessionManager | SSE 凭证缓存（高频 GET/SET） |
| `api-gateway` TerminalService | SSH 终端会话（高频读写） |
| `agent-service` ConfirmService | ReAct 高危操作确认（BRPOP 阻塞等待） |

conversation-service / kb-service / case-service 均**不使用** Redis。

`sop_execution` 不需要 Redis 的原因：
- 写入频率极低（每次 `sop_advance()` 推进一个节点才写一次）
- PostgreSQL UUID 主键单行查询 < 1ms（SSD），无缓存收益
- 引入 Redis 会让 conversation-service 增加不必要的外部依赖

##### 数据库表设计（纯 PostgreSQL，无 Redis）

**表名 `sop_execution` 的命名依据**：`session` 在系统中已有明确含义（SSE 长连接凭证）；`execution` 与业界编排系统对齐——Airflow `dag_run`、GitHub Actions `workflow_run`、Temporal `workflow_execution`；且与 `sop_document` 配对自然——执行（execute）一份文档。

**关键设计：`execution_log` 与 `tool_result` 职责分离**

| 存储位置 | 记录内容 | 用途 |
|---|---|---|
| `tool_result` 表（已有） | 工具调用原始输入输出、风险等级、授权人 | 安全审计、高危操作溯源 |
| `sop_execution.execution_log` | SOP 节点导航事件 | 上下文窗口重建、中断恢复摘要 |

`execution_log` 不得重复存储工具调用细节，否则违反单一事实来源原则。

```sql
-- 表: sop_execution  [模块: conversation-service]
-- 说明: SOP 执行记录表 — 记录一次 SOP 决策树的执行过程
-- 设计:
--   * 1:1 对应 conversation（UNIQUE constraint），一次对话执行一个 SOP
--   * execution_log 只记录节点导航事件，不重复 tool_result 的工具调用数据
--   * completed_steps 防止中断恢复后写操作节点被重复执行
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sop_execution (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id       UUID NOT NULL,
    sop_document_id       INTEGER NOT NULL,
    current_node_id       VARCHAR(64) NOT NULL,
    status                VARCHAR(16) NOT NULL DEFAULT 'active',
        -- active | completed | interrupted | aborted
    context_variables     JSONB NOT NULL DEFAULT '{}',
        -- 运行时变量池：{var_name: {value, source, resolved_at, resolved_by_tool}}
    completed_steps       JSONB NOT NULL DEFAULT '[]',
        -- 已完成 node_id 数组，防止恢复后写操作节点被重复执行
    pending_variable_name VARCHAR(64) DEFAULT NULL,
        -- 当前阻塞的待填变量名（NULL = 无阻塞）
        -- VARCHAR 而非 JSONB 数组：sop_request_variable() 串行等待，每次只等一个
    execution_log         JSONB NOT NULL DEFAULT '[]',
        -- 节点导航事件序列（不含工具调用细节，那是 tool_result 表的职责）
    trace_id              VARCHAR(64),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_sop_execution_conversation
        FOREIGN KEY (conversation_id) REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    CONSTRAINT fk_sop_execution_sop_document
        FOREIGN KEY (sop_document_id) REFERENCES sop_document(id),
    CONSTRAINT uq_sop_execution_conversation
        UNIQUE (conversation_id),
    CONSTRAINT chk_sop_execution_status
        CHECK (status IN ('active', 'completed', 'interrupted', 'aborted'))
);

CREATE INDEX IF NOT EXISTS idx_sop_execution_sop_document_id
    ON sop_execution (sop_document_id);
CREATE INDEX IF NOT EXISTS idx_sop_execution_active
    ON sop_execution (status) WHERE status = 'active';
```

`context_variables` JSONB 结构：

```json
{
  "vm_name": {
    "value": "prod-vm-001",
    "source": "tool_call",
    "resolved_at": "2026-05-25T10:30:00Z",
    "resolved_by_tool": "get_vm_list"
  },
  "node_ip": {
    "value": "10.0.1.5",
    "source": "env_injection",
    "resolved_at": "2026-05-25T10:28:00Z",
    "resolved_by_tool": null
  }
}
```

`execution_log` 条目结构（节点导航事件，**不含**工具调用细节）：

```json
[
  {"type": "node_entered",   "node_id": "n-3",   "entered_at": "...", "reasoning": "磁盘检查指标异常，进入 I/O 故障分支"},
  {"type": "var_resolved",   "name": "vm_name",  "resolved_at": "...", "source": "tool:get_vm_list", "value": "prod-vm-001"},
  {"type": "node_completed", "node_id": "n-3",   "completed_at": "..."},
  {"type": "node_entered",   "node_id": "n-3-7", "entered_at": "...", "reasoning": "I/O 错误率 > 30%，进入磁盘修复节点"}
]
```

##### 中断恢复流程

```
触发时机：用户关闭页面后重新打开同一工单

1. conversation-service 加载会话历史
2. 检测到 sop_execution.status = 'active'（直接 SELECT 单行，无需 Redis）
3. 构建恢复版 system_prompt：
   ┌─────────────────────────────────────────────────────┐
   │ [身份+方法论]（固定段）                               │
   │ [SOP 标题 + 恢复说明]                                │
   │   "正在执行 SOP：《VM 启动失败排障》                  │
   │    已完成步骤 3 步，当前位置：存储 I/O 故障 → 磁盘检查 │
   │    已知变量：vm_name=prod-vm-001, disk_id=disk-004"  │
   │ [当前节点窗口]（current_node_id 对应节点 + 子节点）   │
   └─────────────────────────────────────────────────────┘
4. DiagnosticAgent 以恢复后的 system_prompt 继续执行
   `sop_execution.completed_steps` 防止已执行的写操作节点被重复触发
```

---

#### 12.7.3 变量池设计：格式规范化 + 审核时解析 + 管理端可编辑

##### 核心思路

SOP 中的命令模板（如 `acli vm start {vm_name}`）含有占位符变量。不同 SOP 的变量集合不同：

| SOP 类型 | 典型变量 |
|---------|---------|
| 虚拟机相关 | `vm_name`, `vm_id`, `node_ip` |
| 存储相关 | `disk_id`, `storage_pool`, `node_ip` |
| 网络相关 | `nic_name`, `vlan_id`, `bond_name` |
| 服务重启 | `service_name`, `node_ip` |

变量池在 **SOP 审核发布时自动解析**，存入 `sop_document.variable_schema`，管理端支持查看和修订。

##### SopVariableDefinition 数据结构

```json
// sop_document.variable_schema = [
{
  "name": "vm_name",
  "display_name": "虚拟机名称",
  "description": "需要操作的目标虚拟机名称",
  "type": "string",                        // string | ip | integer | enum
  "required": true,
  "acquisition_strategy": "tool_call",     // tool_call | user_input | user_confirm | env_injection | sop_default | llm_inference | agent_pass
  "acquisition_tool": "get_vm_list",       // 从哪个工具获取（strategy=tool_call 时填写）
  "acquisition_prompt": "请通过 get_vm_list 工具查询并确认目标虚拟机名称",
  "validation_pattern": "^[a-zA-Z0-9_-]+$",
  "default_value": null
},
{
  "name": "node_ip",
  "display_name": "节点 IP",
  "description": "目标节点的管理 IP 地址",
  "type": "ip",
  "required": true,
  "acquisition_strategy": "env_injection", // 初始化时批量注入（从 SSH 连接信息、会话 metadata 直接取得）
  "acquisition_tool": null,
  "acquisition_prompt": null,
  "validation_pattern": "^\\d{1,3}(\\.\\d{1,3}){3}$",
  "default_value": null
},
{
  "name": "service_name",
  "display_name": "服务名称",
  "description": "需要重启的系统服务名称",
  "type": "enum",
  "required": true,
  "acquisition_strategy": "user_input",    // 无法自动获取，需用户直接输入（完全阻塞 JIT）
  "acquisition_tool": null,
  "acquisition_prompt": "请确认需要重启的服务名称（常见值：libvirtd、exporter、ceph-osd）",
  "validation_pattern": null,
  "default_value": null
}
// ]
```

##### 自动解析触发点：`POST /api/admin/sop/{id}/approve`

在现有审核接口中，`parse_sop_markdown()` 解析决策树后，**同步提取变量占位符**：

```
approve 流程（扩展后）：
  ① parse_sop_markdown(content_md) → SOPNode 决策树（现有）
  ② extract_sop_variables(content_md, tree) → [SopVariableDefinition]（新增）
     - 正则扫描全文：/{([a-z][a-z0-9_]*)}/g
     - 扫描 tree_json 中每个节点的 commands 字段
     - 基于变量名启发式推断 acquisition_strategy：
         *_ip, node_ip     → env_injection
         vm_name, vm_id   → tool_call: get_vm_list
         disk_id          → tool_call: acli_storage_disk_list
         nic_name         → tool_call: acli_network_nic_list
         service_name     → user_input（无法自动推断）
  ③ 写入 sop_document.variable_schema（与 tree_json 同一事务）
  ④ 响应体新增字段 variable_count: N
```

##### 管理端变量编辑 UI（已有 SOP 详情页扩展）

`GET /api/admin/sop/{id}` 响应体追加 `variable_schema` 字段，管理端在 SOP 详情页展示变量列表，支持：
- 修改 `display_name`、`description`、`acquisition_strategy`、`acquisition_prompt`
- 补充 `validation_pattern`
- 调整 `acquisition_tool`（如自动推断有误时人工修正）

`PATCH /api/admin/sop/{id}/variable-schema` 接口持久化修改结果。

##### 运行时：变量注入与渲染流程

```
SOP 命中，conversation 初始化时：
  1. 加载 sop_document.variable_schema（所有变量定义）
  2. 从连接信息预填 acquisition_strategy=env_injection 的变量
     （node_ip、cluster_id 从 SSH 连接/conversation metadata 直接注入）
  3. 初始化 sop_execution.context_variables（已知变量写入，未知变量留空）

每轮窗口注入时：
  4. 取当前节点的 commands 列表
  5. 对每条命令做模板渲染：
       已知变量 → 替换为具体值（如 "acli vm start prod-vm-001"）
       未知变量 → 保留占位符并追加提示：
                  "acli vm start {vm_name}  ← 需先通过 get_vm_list 获取"
  6. 渲染结果注入窗口

LLM 调用诊断工具获取变量值后，通过 sop_advance 的 variables_extracted 参数上报：
  7. 服务端按 variable_schema 中的 validation_pattern 校验值格式
  8. 校验通过 → 写入 context_variables，source=tool_call
  9. 下一轮窗口注入时该变量已被预渲染为具体值
```

---

#### 12.7.4 变量池深度设计（第一性原理与最佳实践）

> 本节回答六个核心设计问题，澄清实现时的决策依据。

##### Q1：变量 Schema 与变量值存储在哪里？

**两层分离，永不混存。**

| 层次 | 存储位置 | 内容 | 类比 |
|---|---|---|---|
| Schema（模板层） | `sop_document.variable_schema JSONB` | 变量名、类型、来源策略、说明 | Terraform `variable {}` 声明 |
| 值（执行层） | `sop_execution.context_variables JSONB` | 运行时实际填充的值 | Terraform `terraform.tfvars` |

业界一致：Ansible vars 声明在 playbook，值通过 `extra_vars`/`vars_files` 传入；GitHub Actions `inputs` 声明在 workflow，`context.inputs` 存运行时值。KBD 亦遵循此结构（`kbd_document.variable_schema`，值从调用方 context_variables 注入）。

##### Q2：SOP 重新导入后，变量池如何维护？

**三路合并（Three-way Merge），人工知识永不自动覆盖。**

参考：Git merge 策略 + Alembic 迁移叠加模型。

| 情况 | 策略 |
|---|---|
| 变量同时存在于旧版和新版 | **保留人工编辑的 Schema**（`source`、`description` 等字段不覆盖） |
| 新版新增的变量 | 自动添加，标记 `auto_generated: true`，等待人工审核 |
| 旧版有但新版消失的变量 | **标记 `deprecated: true`，不删除**，管理端告警供人工决策 |
| 两版都有但使用方式变了 | 校验告警，人工决策 |

**绝对禁止**：自动覆盖已人工编辑的字段。`source`、`description`、`acquisition_strategy` 一旦人工确认，即为知识资产，不可静默覆盖。

##### Q3：双向校验——声明与使用的一致性

**两种异常，严重度不同。**

| 异常类型 | 触发条件 | 严重度 | 处置 |
|---|---|---|---|
| **Undeclared Usage**（用但未声明） | 正文有 `{vm_name}` 但 `## 变量` 没有 `vm_name` | **Error** | 阻断 approve，必须修复——运行时无法获取值 |
| **Orphan Declaration**（声明但未用） | `## 变量` 有 `vm_name` 但正文无 `{vm_name}` | **Warning** | 允许 approve，提示可能是遗留声明或拼写错误 |

类比 TypeScript 编译器：引用未声明变量是 error，声明了未使用是 warning（`noUnusedLocals`）。

```
approve 流程（双向校验版）：
  ① parse_sop_markdown(content_md) → SOPNode 决策树
  ② 解析 ## 变量 章节 → declared_vars（变量名集合）
  ③ 扫描 tree_json 各节点 commands 字段 → used_vars（占位符集合）
  ④ 双向 diff：
       undeclared = used_vars - declared_vars  → Error，阻断 approve
       orphan     = declared_vars - used_vars  → Warning，写入告警字段
  ⑤ 校验通过 → 写入 sop_document.variable_schema（与 tree_json 同一事务）
  ⑥ 响应体包含 warnings 列表（orphan 声明告警）
```

##### Q4：需要用户提供的变量如何标识？

**`acquisition_strategy` 字段做全分类，不用 bool 标志位。**

| `acquisition_strategy` | 实体·动作 | 触发时机 | Agent 行为 |
|---|---|---|---|
| `env_injection` | 环境·注入 | SOP 执行初始化时批量 | 从 SSH 连接信息、conversation metadata 直接取得，无阻塞 |
| `user_input` | 用户·输入 | JIT，完全阻塞 | **Agent 完全不知道值**，触发 `sop_request_variable`，等待用户手动填写 |
| `user_confirm` | 用户·确认 | JIT，工具推荐候选后确认 | Agent 推断候选值 → 触发确认卡片，用户可接受/修改 |
| `tool_call` | 工具·调用 | JIT，自动调用 `acquisition_tool` | 进入需要该变量的节点时自动触发，无需用户介入 |
| `sop_default` | 文档·默认 | 初始化时 | SOP 文档硬编码默认值，直接使用 |
| `llm_inference` | 模型·推理 | LLM 主动上报（被动写入） | 经 `sop_advance.variables_extracted` 参数嵌入，不走 JIT |
| `agent_pass` | Agent·传递 | 父/兄弟 Agent 调用时（被动写入） | agent-to-agent 调用时随上下文传入，不走 JIT |

> **注**：`llm_inference` 和 `agent_pass` 是**被动写入路径**，不走 JIT 主动获取流程。`context_variables` 的 `source` 字段统一记录这七种来源，方便溯源审计。

**`user_input` vs `user_confirm` 的关键区别**：
- `user_input`：Agent 完全不知道值，只能等用户提供（如：客户的故障 IP 段）
- `user_confirm`：Agent 从工具输出可以推断候选值（如"只有一台 VM 匹配"），但因操作有破坏性，需人工确认，防止误操作

SOP `## 变量` 章节写法示例（全类型覆盖）：

```markdown
## 变量

| 变量名        | 类型   | 来源                             | 说明 |
|--------------|--------|----------------------------------|------|
| node_ip      | ip     | env_injection                    | 节点管理 IP，初始化时从 SSH 连接信息注入 |
| vm_name      | string | user_confirm                     | 将要操作的 VM，Agent 推断候选后需用户确认 |
| disk_id      | string | tool_call:acli_storage_disk_list | 需修复的磁盘 ID，工具自动获取 |
| service_name | string | user_input                       | 无法自动获取，需用户直接提供 |
| target_vm    | string | llm_inference                    | LLM 从工具输出中提取，经 sop_advance.variables_extracted 写入 |
```

##### Q5：变量是开头统一获取还是按节点懒加载（JIT）？

**懒加载（Just-In-Time），`env_injection` 类变量除外。**

原因：
1. SOP 是有条件分支的树，走到分支 A 就不走分支 B，B 的变量白获取
2. 变量间有依赖（`disk_id` 需要先有 `vm_name`），静态顺序无法满足
3. `user_input` 变量：在真正需要时才问用户，减少无意义的打扰

| 类型              | 何时获取              | 原因                                          |
|----------------|-----------------|---------------------------------------------|
| `env_injection` | 初始化时批量注入         | 成本接近零（读内存/conversation metadata）              |
| `tool_call`    | 进入需要该变量的节点前即时获取 | 依赖关系自然满足；未执行的分支不触发工具                        |
| `user_input`   | 进入需要该变量的节点时请求   | 只在真正需要时打扰用户                                 |
| `user_confirm` | Agent 推断候选值后立即请求 | 推断依赖工具输出，必须先执行工具                            |
| `llm_inference`| 被动写入，不走 JIT      | LLM 推理后经 sop_advance.variables_extracted 嵌入  |
| `agent_pass`   | 被动写入，不走 JIT      | 父/兄弟 Agent agent-to-agent 调用时随上下文传入           |

一旦获取，存入 `sop_execution.context_variables`，后续节点直接复用（不重复调用工具/问用户）。

##### Q6：KBD 也有变量需求，如何处理？

**KBD 的变量是 SOP 变量池的下游消费者，不是独立系统。**

| | SOP 变量 | KBD 变量 |
|---|---|---|
| 主体 | Agent 执行者 | 文档阅读者 / Agent 引用内容 |
| 是否有 `acquisition_strategy` | 有 | **无**（KBD 不主动获取值） |
| 值从哪里来 | 运行时获取/用户提供 | 从调用方 `context_variables` 注入 |

**Layer 1：KBD 自身变量声明**

KBD 也支持 `## 变量` 章节，只声明变量名、类型、说明，**不声明 `acquisition_strategy`**（不是 KBD 的职责）。审批时做同样的双向校验。存入 `kbd_document.variable_schema JSONB`。

**Layer 2：Agent 引用 KBD 时的变量注入**

Agent 在 `sop_execution.context_variables` 中已有当前上下文所有变量值。引用 KBD 内容时，把 `{placeholder}` 替换为 `context_variables` 中对应的值：
- 如果 `context_variables` 已有该变量 → 直接替换，命令可执行
- 如果 KBD 需要一个 `context_variables` 没有的变量 → 触发 JIT 获取（走 Q5 的流程）
- 如果 `context_variables` 里有 KBD 根本不使用的变量 → 忽略（多余无害）

这样 KBD 和 SOP 共享同一套变量解析机制，无需单独建系统。

---

## 十三、变更历史

| 日期 | 版本 | 变更摘要 |
|------|------|---------|
| 2026-05-28 | v5.4 | 新增 `app/tools/shell/`（classifier.py + executor.py），引入 `bash_exec`（通用 Linux Shell）和 `acli_exec`（HCI 专有 CLI）工具；废弃 `AcliClient._run_ssh()` 直连路径（HCI 在客户私网，云端不可达），确立 `BridgeRelayExecutor`（terminal_bridge 中转）为唯一可行执行路径；新增架构设计文档 `docs/solution/agent/agent工具设计.md` |
| 2026-05-29 | v5.5 | 工具架构 v2.0：`app/tools/shell/` → `app/tools/acli/`（bash_exec 归入 acli category）；移除旧 11 个结构化 acli 工具，由 `acli_exec` 统一替代；新增 4 个插件诊断工具（acli_plugin_vm_start/vm_suspend/netdoctor/asys）；`tool_definition` 数据库表确立为工具定义 SSOT，`tool_registry.py` 改为启动时 DB 加载器；详见 `agent工具设计.md` v2.0 |
| 2026-06-03 | v5.3 | 目录重构：新增 `app/memory/`（工作记忆层）和 `app/tools/`（工具实现层），变量池迁移到 `memory/variable_pool.py`，SOP 工具迁移到 `tools/sop/`，`sop_tools.py` 精简为 `SopToolExecutor` 执行协调层，`ToolDefinition` 统一从 `tools.base` 导入；变量赋值策略新增 `env_injection`（原 `env_context`）、`tool_call`（原 `tool`）、`llm_inference`（原 `llm_extracted`）、`agent_pass`（新增）|
| 2026-05-26 | v5.2 | §12.7.3 扩展双向校验（undeclared/orphan）、`user_confirm` 策略；§12.7.4 变量池深度设计（存储分层、三路合并、JIT 懒加载、KBD 下游消费者模式）|
| 2026-05-26 | v5.1 | §12.7 全面修订：sop_session_state → sop_execution（避免与 session 混淆）；移除双层 Redis 缓存；execution_log 与 tool_result 职责分离；pending_variable_name 用 VARCHAR 非 JSONB 数组；BUG-06 技术原理及 diagnostic_item 半成品告警 |
| 2026-05-26 | v5.0 | 新增 §12.7（结构化滑动窗口确认设计细节：并发多工单支持、SopExecution 双层存储+中断恢复、变量池审核时自动解析+管理端可编辑）|
| 2026-05-26 | v4.0 | 新增 §12.5（深度代码分析：4 项新增关键问题，含双重执行 bug、confirm 回路断裂、async for 误用，以及 P1 勘误）、§12.6（SOP 消费方案五路对比，推荐结构化滑动窗口方案） |
| 2026-05-25 | v3.0 | 新增第十二章：SOP 全链路深度分析，htp-agent 7 项问题详表（含 P4-LT 滑动窗口方案）、pai-agent 4 项问题详表，LLM 输出控制设计方向 |
| 2026-05-23 | v2.0 | 全量重写：引入 BaseAgent、语义阶段命名（Triage/Investigation/Remediation/Closure）、六边形架构说明、重构路线图；原文件 AI助手设计.md 重命名为 agent设计.md |
| 2026-05-21 | v1.6 | 原 AI助手设计.md：配置迁移至 dashscope 网关，移除本地 Pod 池架构 |
| 2026-05-07 | v1.1 | 原 AI助手设计.md：重构 agent-service 目录结构 |
