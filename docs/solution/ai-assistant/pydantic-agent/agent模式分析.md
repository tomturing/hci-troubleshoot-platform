# Pydantic AI 支持的 Agent 模式分析

## 概述

Pydantic AI 的通用架构设计支持多种 Agent 模式，从简单的单 Agent 到复杂的多 Agent 协作系统。本文档详细分析这些模式及其实现方式。

---

## 模式概览

```
复杂度级别
    │
    │    ┌─────────────────────────────────────────────────────────────┐
    │    │  Level 5: Deep Agents                                       │
    │    │  (Planning + Files + Delegation + Sandbox + Durable)        │
    │    ├─────────────────────────────────────────────────────────────┤
    │    │  Level 4: Graph-based Control Flow                          │
    │    │  (状态机驱动的多 Agent 工作流)                               │
    │    ├─────────────────────────────────────────────────────────────┤
    │    │  Level 3: Programmatic Agent Hand-off                       │
    │    │  (应用代码控制 Agent 切换)                                   │
    │    ├─────────────────────────────────────────────────────────────┤
    │    │  Level 2: Agent Delegation                                  │
    │    │  (Agent 通过工具调用其他 Agent)                              │
    │    ├─────────────────────────────────────────────────────────────┤
    │    │  Level 1: Single Agent                                      │
    │    │  (ReAct / RAG / Structured Output / Human-in-the-loop)     │
    │    └─────────────────────────────────────────────────────────────┘
    │
    └──────────────────────────────────────────────────────────────────►
```

---

## 一、Level 1：单 Agent 模式

### 1.1 ReAct 模式（Reasoning and Acting）

**核心流程**：Thought → Action → Observation → 循环

```python
from pydantic_ai import Agent, RunContext
from pydantic_ai.usage import UsageLimits

agent = Agent(
    'openai:gpt-4.1',
    system_prompt='Think step by step before taking actions.',
    usage_limits=UsageLimits(request_limit=10)
)

@agent.tool
def search(ctx: RunContext, query: str) -> str:
    """Search for information."""
    return f"Results for: {query}"

@agent.tool
def calculate(ctx: RunContext, expression: str) -> float:
    """Calculate a mathematical expression."""
    return eval(expression)

result = agent.run_sync("What is 15% of 847?")
```

**特性**：
- 自动循环：工具结果自动传回模型
- 原生 Thinking 支持：`ThinkingPart` 记录推理过程
- 重试机制：`ModelRetry` 异常触发模型修正

### 1.2 RAG 模式（Retrieval-Augmented Generation）

**核心流程**：Query → Embed → Retrieve → Augment → Generate

```python
from pydantic_ai import Agent, RunContext, Embedder

@dataclass
class Deps:
    embedder: Embedder
    vector_db: VectorDB

agent = Agent('openai:gpt-5.2', deps_type=Deps)

@agent.tool
async def retrieve(ctx: RunContext[Deps], query: str) -> str:
    """检索相关文档"""
    # 1. 生成查询向量
    embedding = await ctx.deps.embedder.embed_query(query)
    # 2. 向量检索
    docs = await ctx.deps.vector_db.search(embedding[0], limit=5)
    # 3. 返回相关上下文
    return '\n\n'.join(doc.content for doc in docs)

# 运行
result = agent.run_sync("How do I configure logfire?", deps=deps)
```

**特性**：
- Embedding 统一接口：支持 OpenAI、Cohere、Google 等
- 向量检索工具化：将检索封装为工具
- 上下文注入：检索结果自动注入提示

### 1.3 结构化输出模式

**核心流程**：Prompt → Model → Structured Output

```python
from pydantic import BaseModel
from pydantic_ai import Agent

class User(BaseModel):
    name: str
    age: int
    email: str

class Failed(BaseModel):
    error: str

agent = Agent(
    'openai:gpt-5.2',
    output_type=User | Failed,  # 联合类型
)

result = agent.run_sync("Extract user info: John, 25, john@example.com")
if isinstance(result.output, User):
    print(f"Name: {result.output.name}")
```

**输出模式**：

| 模式 | 说明 | 使用场景 |
|------|------|---------|
| `str` | 文本输出 | 自由文本生成 |
| `BaseModel` | 结构化输出 | 数据提取 |
| `ToolOutput[T]` | 工具输出模式 | 强制工具调用 |
| `NativeOutput[T]` | 原生结构化输出 | 模型原生支持 |
| `PromptedOutput[T]` | 提示引导输出 | 无原生支持时 |
| `str \| None` | 可选输出 | 可能无结果 |

### 1.4 Human-in-the-Loop 模式

**核心流程**：Tool Call → Approval Required → Human Approve → Execute

```python
from pydantic_ai import Agent, ApprovalRequired, ToolDenied

agent = Agent('openai:gpt-5.2')

# 方式 1：声明式
@agent.tool_plain(requires_approval=True)
def delete_file(path: str) -> str:
    return f"Deleted {path}"

# 方式 2：运行时判断
@agent.tool
def update_file(ctx: RunContext, path: str) -> str:
    if path == '.env' and not ctx.tool_call_approved:
        raise ApprovalRequired("Updating .env requires approval")
    return f"Updated {path}"

# 处理审批
async with agent.iter(prompt) as run:
    async for node in run:
        if isinstance(node.output, DeferredToolRequests):
            for call in node.output.approvals:
                approved = ask_user(f"Approve {call.tool_name}?")
                if not approved:
                    results[call.tool_call_id] = ToolDenied("User denied")
```

### 1.5 Output Functions 模式

**核心流程**：Model Output → Function Processing → Final Result

```python
from pydantic_ai import Agent, TextOutput

def process_output(text: str) -> dict:
    """处理模型的文本输出"""
    return {"processed": text.upper()}

agent = Agent(
    'openai:gpt-5.2',
    output_type=TextOutput(process_output)
)

result = agent.run_sync("Hello world")
print(result.output)  # {"processed": "HELLO WORLD"}
```

---

## 二、Level 2：Agent Delegation 模式

**核心流程**：Parent Agent → Tool → Child Agent → Return Result

```python
from pydantic_ai import Agent, RunContext

# 子 Agent：专门生成笑话
joke_generator = Agent(
    'google:gemini-3-flash-preview',
    output_type=list[str]
)

# 父 Agent：选择最佳笑话
selector = Agent(
    'openai:gpt-5.2',
    instructions='Use joke_factory to get jokes, then pick the best one.'
)

@selector.tool
async def joke_factory(ctx: RunContext, count: int) -> list[str]:
    # 委托给子 Agent
    result = await joke_generator.run(
        f'Generate {count} jokes',
        usage=ctx.usage,  # 共享 usage 统计
    )
    return result.output

result = selector.run_sync("Tell me a joke")
```

**架构图**：

```
┌─────────────────────────────────────────────────────────────┐
│                     Parent Agent                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │               Tool: joke_factory                     │   │
│  │                       │                              │   │
│  │                       ▼                              │   │
│  │              ┌────────────────┐                      │   │
│  │              │  Child Agent   │                      │   │
│  │              │  (joke_gen)    │                      │   │
│  │              └───────┬────────┘                      │   │
│  │                      │                               │   │
│  │                      ▼                               │   │
│  │              Return: list[str]                       │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  Final Output: Best Joke                                    │
└─────────────────────────────────────────────────────────────┘
```

**特性**：
- Usage 共享：子 Agent 的使用量计入父 Agent
- 依赖传递：可共享或使用子集依赖
- 模型无关：父子 Agent 可使用不同模型

---

## 三、Level 3：Programmatic Agent Hand-off 模式

**核心流程**：App Code → Agent A → App Code → Agent B → App Code

```python
from pydantic_ai import Agent, RunContext

flight_agent = Agent('openai:gpt-5.2', output_type=FlightDetails | Failed)
seat_agent = Agent('openai:gpt-5.2', output_type=SeatPreference | Failed)

async def book_flight():
    usage = RunUsage()
    message_history = None
    
    # Step 1: 找航班
    for _ in range(3):
        prompt = input("Where would you like to fly?")
        result = await flight_agent.run(prompt, message_history=message_history, usage=usage)
        if isinstance(result.output, FlightDetails):
            break
        message_history = result.all_messages()
    
    # Step 2: 选座位
    while True:
        seat = input("What seat would you like?")
        result = await seat_agent.run(seat, usage=usage)
        if isinstance(result.output, SeatPreference):
            return result.output
```

**架构图**：

```
┌─────────────────────────────────────────────────────────────┐
│                      Application Code                        │
│                                                             │
│   ┌─────────────────┐                                       │
│   │  Ask User       │                                       │
│   └────────┬────────┘                                       │
│            │                                                │
│            ▼                                                │
│   ┌─────────────────┐     ┌─────────────────┐              │
│   │  Flight Agent   │────►│  Ask User       │              │
│   └────────┬────────┘     └────────┬────────┘              │
│            │                       │                        │
│            ▼                       ▼                        │
│   ┌─────────────────┐     ┌─────────────────┐              │
│   │  Ask User       │────►│  Seat Agent     │              │
│   └─────────────────┘     └────────┬────────┘              │
│                                    │                        │
│                                    ▼                        │
│                           ┌─────────────────┐              │
│                           │  Final Result   │              │
│                           └─────────────────┘              │
└─────────────────────────────────────────────────────────────┘
```

**特性**：
- 应用代码控制流程
- Agent 间无需共享依赖
- 适合人工参与的流程

---

## 四、Level 4：Graph-based Control Flow 模式

**核心流程**：State Machine → Node → Node → End

```python
from dataclasses import dataclass
from pydantic_graph import BaseNode, End, GraphBuilder, GraphRunContext

@dataclass
class AnalyzeNode(BaseNode[State]):
    async def run(self, ctx: GraphRunContext[State]) -> PlanNode | End[Result]:
        analysis = await analyze_data(ctx.state.data)
        if analysis.is_simple:
            return End(Result(analysis))
        return PlanNode(analysis)

@dataclass
class PlanNode(BaseNode[State]):
    analysis: Analysis
    
    async def run(self, ctx: GraphRunContext[State]) -> ExecuteNode:
        plan = await create_plan(self.analysis)
        return ExecuteNode(plan)

@dataclass
class ExecuteNode(BaseNode[State]):
    plan: Plan
    
    async def run(self, ctx: GraphRunContext[State]) -> End[Result]:
        result = await execute_plan(self.plan)
        return End(result)

# 构建图
graph = GraphBuilder(
    nodes=[AnalyzeNode, PlanNode, ExecuteNode]
).build()

# 运行
result = await graph.run(AnalyzeNode(data=my_data))
```

**架构图**：

```
┌─────────────────────────────────────────────────────────────┐
│                      Graph State Machine                     │
│                                                             │
│   ┌─────────────────┐                                       │
│   │  AnalyzeNode    │                                       │
│   │  (分析数据)      │                                       │
│   └────────┬────────┘                                       │
│            │                                                │
│      ┌─────┴─────┐                                          │
│      │           │                                          │
│      ▼           ▼                                          │
│  ┌────────┐  ┌─────────────────┐                            │
│  │  End   │  │   PlanNode      │                            │
│  │ (简单) │  │   (制定计划)     │                            │
│  └────────┘  └────────┬────────┘                            │
│                       │                                      │
│                       ▼                                      │
│              ┌─────────────────┐                            │
│              │  ExecuteNode    │                            │
│              │  (执行计划)      │                            │
│              └────────┬────────┘                            │
│                       │                                      │
│                       ▼                                      │
│              ┌─────────────────┐                            │
│              │      End        │                            │
│              └─────────────────┘                            │
└─────────────────────────────────────────────────────────────┘
```

**特性**：
- 类型安全：节点返回类型定义边
- 可视化：可生成 Mermaid 图
- 可恢复：支持断点续跑
- 适合复杂工作流

---

## 五、Level 5：Deep Agents 模式

Deep Agents 是综合多种能力的自主 Agent。

### 5.1 核心能力组合

```python
from pydantic_ai import Agent
from pydantic_ai.capabilities import Thinking

agent = Agent(
    'anthropic:claude-opus-4',
    capabilities=[
        Thinking(effort='high'),      # 深度推理
        HandleDeferredToolCalls(...), # Human-in-the-loop
        # ... 更多能力
    ],
    toolsets=[
        FileSystemToolset(),          # 文件操作
        CodeExecutionToolset(),       # 代码执行沙箱
        TaskManagementToolset(),      # 任务管理
        # ... 更多工具集
    ]
)
```

### 5.2 能力矩阵

| 能力 | 实现方式 | 说明 |
|------|---------|------|
| **Planning** | TaskManagementToolset | 任务分解与追踪 |
| **File Operations** | FileSystemToolset | 读写、编辑文件 |
| **Task Delegation** | Agent Delegation | 委托给子 Agent |
| **Code Execution** | CodeExecutionToolset | 沙箱执行代码 |
| **Context Management** | Message History Processing | 长对话压缩 |
| **Human-in-the-loop** | Deferred Tools | 危险操作审批 |
| **Durable Execution** | Temporal/DBOS/Prefect/Restate | 故障恢复 |

### 5.3 Durable Execution（持久化执行）

```python
# Temporal 集成示例
from temporalio import workflow
from pydantic_ai.ext.temporal import PydanticAIWorkflow

@workflow.defn
class MyAgentWorkflow(PydanticAIWorkflow):
    """支持故障恢复的 Agent 工作流"""
    
    @workflow.run
    async def run(self, prompt: str) -> str:
        # Agent 状态自动持久化
        # API 失败、应用重启后可恢复
        result = await self.agent.run(prompt)
        return result.output
```

**支持的持久化方案**：
- Temporal
- DBOS
- Prefect
- Restate

---

## 六、其他重要模式

### 6.1 Output Validators（输出验证）

```python
from pydantic_ai import Agent, ModelRetry

agent = Agent('openai:gpt-5.2', output_type=SQLQuery)

@agent.output_validator
async def validate_sql(ctx: RunContext, output: SQLQuery) -> SQLQuery:
    try:
        await ctx.deps.db.execute(f"EXPLAIN {output.query}")
    except QueryError as e:
        # 触发模型重试
        raise ModelRetry(f"Invalid SQL: {e}")
    return output
```

### 6.2 Dynamic Tools（动态工具）

```python
@agent.tool
def register_tool(ctx: RunContext, tool_code: str) -> str:
    """动态注册新工具"""
    # 运行时添加工具
    ctx.agent.tool_plain(eval(tool_code))
    return "Tool registered"
```

### 6.3 Streaming（流式输出）

```python
async with agent.run_stream(prompt) as result:
    async for text in result.stream_text(delta=True):
        print(text, end='', flush=True)
```

### 6.4 Multi-Modal（多模态）

```python
from pydantic_ai import BinaryContent

# 发送图片
result = agent.run_sync([
    "What's in this image?",
    BinaryContent(image_data, media_type='image/png')
])
```

---

## 七、模式选择指南

| 场景 | 推荐模式 | 复杂度 |
|------|---------|--------|
| 简单问答 | Single Agent + str output | ★ |
| 数据提取 | Single Agent + Structured output | ★ |
| RAG 应用 | Single Agent + Retrieve tool | ★★ |
| 需要人工审批 | Human-in-the-loop | ★★ |
| 多步骤任务 | ReAct + Tools | ★★ |
| 专业分工 | Agent Delegation | ★★★ |
| 流程控制 | Programmatic Hand-off | ★★★ |
| 复杂工作流 | Graph-based | ★★★★ |
| 自主 Agent | Deep Agents | ★★★★★ |

---

## 八、总结

Pydantic AI 的架构设计遵循**渐进式复杂度**原则：

1. **简单场景简单用**：单 Agent + 工具即可
2. **需要时再升级**：从 Delegation 到 Graph 逐步增加复杂度
3. **能力可组合**：通过 Capabilities 和 Toolsets 灵活组合
4. **类型安全贯穿始终**：从简单到复杂都保持类型检查

这种设计使得开发者可以根据实际需求选择合适的模式，而无需学习完全不同的框架。
