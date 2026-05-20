# Pydantic AI Agent 类深度分析

## 概述

Pydantic AI 是一个提供者无关的 GenAI Agent 框架，其核心设计围绕 `Agent` 类展开。本文档深入分析 Agent 类的架构设计、核心数据结构、状态维护机制和主循环流程。

---

## 一、主要模块与功能

### 1.1 模块组织结构

```
pydantic_ai_slim/pydantic_ai/
├── agent/                    # Agent 核心模块
│   ├── __init__.py          # Agent 主类定义
│   ├── abstract.py          # AbstractAgent 抽象基类
│   ├── wrapper.py           # WrapperAgent 包装器
│   └── spec.py              # AgentSpec 规格类
├── _agent_graph.py          # 图执行引擎（核心循环）
├── _run_context.py          # 运行时上下文
├── messages.py              # 消息数据结构
├── tools.py                 # 工具定义
├── tool_manager.py          # 工具管理器
├── toolsets/                # 工具集模块
├── models/                  # 模型适配器
├── run.py                   # 运行结果封装
├── result.py                # 结果处理
└── output.py                # 输出处理
```

### 1.2 核心模块功能

| 模块 | 文件 | 核心功能 |
|------|------|---------|
| **Agent 主类** | `agent/__init__.py` | Agent 定义、配置、装饰器注册 |
| **抽象基类** | `agent/abstract.py` | 定义 Agent 接口契约 |
| **图执行引擎** | `_agent_graph.py` | 状态机节点、主循环逻辑 |
| **运行上下文** | `_run_context.py` | 运行时依赖注入、状态传递 |
| **消息系统** | `messages.py` | 所有消息类型的定义 |
| **工具系统** | `tools.py` + `tool_manager.py` | 工具定义、验证、执行 |
| **模型适配** | `models/` | 多 Provider 统一接口 |

### 1.3 Agent 类核心属性

```python
@dataclasses.dataclass(init=False)
class Agent(AbstractAgent[AgentDepsT, OutputDataT]):
    # 模型配置
    model: str | Model | None          # 默认模型

    # 类型参数
    deps_type: type[AgentDepsT]         # 依赖类型
    output_type: OutputSpec[OutputDataT] # 输出规格

    # 工具系统
    toolsets: list[AgentToolset[AgentDepsT]]  # 注册的工具集

    # 能力系统
    root_capability: AbstractCapability[AgentDepsT]  # 根能力

    # 可观测性
    instrument: InstrumentationSettings | None  # 仪表化设置
```

### 1.4 Agent 类核心方法

```python
class Agent:
    # 运行方法
    async def run(...) -> AgentRunResult[OutputDataT]
    def run_sync(...) -> AgentRunResult[OutputDataT]
    async def iter(...) -> AsyncIterator[AgentNode]

    # 装饰器注册
    def tool(self, func) -> None           # 注册带上下文的工具
    def tool_plain(self, func) -> None     # 注册纯函数工具
    def system_prompt(self, func) -> None  # 注册系统提示
    def instructions(self, func) -> None   # 注册指令函数

    # 配置覆盖
    @contextmanager
    def override(self, ...) -> Agent       # 临时覆盖配置
```

---

## 二、核心数据结构

### 2.1 消息类型体系

Pydantic AI 的消息系统采用**联合类型 + 判别器**模式，确保类型安全和序列化兼容。

```
ModelMessage (Union)
├── ModelRequest          # 发送给模型的请求
│   └── parts: Sequence[ModelRequestPart]
│       ├── SystemPromptPart    # 系统提示
│       ├── UserPromptPart      # 用户输入
│       ├── ToolReturnPart      # 工具返回结果
│       ├── RetryPromptPart     # 重试提示
│       └── InstructionPart     # 指令部分
│
└── ModelResponse         # 模型返回的响应
    └── parts: Sequence[ModelResponsePart]
        ├── TextPart            # 文本内容
        ├── ThinkingPart        # 思考过程
        ├── ToolCallPart        # 工具调用
        ├── NativeToolCallPart  # 原生工具调用
        └── FilePart            # 文件内容
```

### 2.2 ModelRequest 结构

```python
@dataclass
class ModelRequest:
    """发送给模型的请求"""

    parts: Sequence[ModelRequestPart]  # 请求部分列表

    # 元数据
    timestamp: datetime | None         # 时间戳
    instructions: str | None           # 渲染后的指令

    # 追踪标识
    run_id: str | None                 # 运行 ID
    conversation_id: str | None        # 会话 ID

    kind: Literal['request'] = 'request'
```

### 2.3 ModelResponse 结构

```python
@dataclass
class ModelResponse:
    """模型返回的响应"""

    parts: Sequence[ModelResponsePart]  # 响应部分列表

    # 使用统计
    usage: RequestUsage                # Token 使用量

    # 模型信息
    model_name: str | None             # 模型名称
    timestamp: datetime                # 接收时间

    # Provider 信息
    provider_name: str | None          # 提供商名称
    provider_url: str | None           # API URL
    provider_details: dict | None      # Provider 特有数据
    provider_response_id: str | None   # 请求 ID

    # 状态
    finish_reason: FinishReason | None # 结束原因
    state: ModelResponseState          # 响应状态

    # 追踪
    run_id: str | None
    conversation_id: str | None
```

### 2.4 ToolCallPart 结构

```python
@dataclass
class ToolCallPart(BaseToolCallPart):
    """工具调用部分"""

    tool_name: str                     # 工具名称
    args: Any                          # 调用参数（JSON 或 dict）
    tool_call_id: str                  # 调用 ID

    # 元数据
    tool_kind: ToolPartKind | None     # 工具类型
    provider_name: str | None          # Provider 名称
    provider_details: dict | None      # Provider 特有数据

    part_kind: Literal['tool-call'] = 'tool-call'
```

### 2.5 GraphAgentState（图状态）

```python
@dataclass
class GraphAgentState:
    """Agent 图执行过程中的状态"""

    # 消息历史
    message_history: list[ModelMessage]

    # 使用统计
    usage: RunUsage

    # 重试计数
    output_retries_used: int
    run_step: int

    # 标识符
    run_id: str                        # UUID7
    conversation_id: str               # UUID7

    # 元数据
    metadata: dict[str, Any] | None
```

### 2.6 GraphAgentDeps（图依赖）

```python
@dataclass
class GraphAgentDeps(Generic[DepsT, OutputDataT]):
    """传递给 Agent 图的依赖/配置"""

    user_deps: DepsT                   # 用户自定义依赖

    # 输入
    prompt: str | Sequence[UserContent] | None
    new_message_index: int
    resumed_request: ModelRequest | None

    # 模型配置
    model: Model
    get_model_settings: Callable       # 获取模型设置

    # 限制与策略
    usage_limits: UsageLimits
    max_output_retries: int
    end_strategy: EndStrategy

    # 输出配置
    output_schema: OutputSchema[OutputDataT]
    output_validators: list[OutputValidator]

    # 工具系统
    root_capability: AbstractCapability[DepsT]
    native_tools: list[AgentNativeTool[DepsT]]
    tool_manager: ToolManager[DepsT]

    # 可观测性
    tracer: Tracer
    instrumentation_settings: InstrumentationSettings | None
```

---

## 三、状态维护机制

### 3.1 状态层级架构

Pydantic AI 采用**三层状态架构**：

```
┌─────────────────────────────────────────────────────────────┐
│                     GraphAgentState                          │
│  (跨节点持久化状态 - message_history, usage, run_id)         │
├─────────────────────────────────────────────────────────────┤
│                     GraphAgentDeps                           │
│  (不可变配置 - model, tool_manager, output_schema)           │
├─────────────────────────────────────────────────────────────┤
│                     RunContext                               │
│  (单步运行上下文 - deps, retry, tool_call_id)                │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 ContextVar 状态传递

使用 Python 的 `contextvars` 实现跨异步调用的状态传递：

```python
# _run_context.py
_CURRENT_RUN_CONTEXT: ContextVar[RunContext[Any] | None] = ContextVar(
    'pydantic_ai.current_run_context',
    default=None,
)

def get_current_run_context() -> RunContext[Any] | None:
    """获取当前运行上下文"""
    return _CURRENT_RUN_CONTEXT.get()

@contextmanager
def set_current_run_context(run_context: RunContext[Any]) -> Iterator[None]:
    """设置当前运行上下文（上下文管理器）"""
    token = _CURRENT_RUN_CONTEXT.set(run_context)
    try:
        yield
    finally:
        _CURRENT_RUN_CONTEXT.reset(token)
```

**设计优势**：
- 工具函数可直接调用 `get_current_run_context()` 获取上下文
- 无需显式传递参数
- 支持嵌套运行（如 Agent 调用 Agent）

### 3.3 RunContext 结构

```python
@dataclass
class RunContext(Generic[RunContextAgentDepsT]):
    """当前调用的运行时信息"""

    # 核心依赖
    deps: RunContextAgentDepsT         # 用户依赖
    model: Model                       # 当前模型
    usage: RunUsage                    # 使用统计

    # Agent 引用
    agent: Agent | None                # 运行此上下文的 Agent

    # 输入输出
    prompt: str | Sequence[UserContent] | None  # 原始提示
    messages: list[ModelMessage]       # 消息历史

    # 重试状态
    retries: dict[str, int]            # 每个工具的重试次数
    retry: int                         # 当前重试次数
    max_retries: int                   # 最大重试次数

    # 工具调用状态
    tool_call_id: str | None           # 当前工具调用 ID
    tool_name: str | None              # 当前工具名称
    tool_call_approved: bool           # 工具是否被批准

    # 运行标识
    run_step: int                      # 当前步骤
    run_id: str | None                 # 运行 ID
    conversation_id: str | None        # 会话 ID

    # 可观测性
    tracer: Tracer                     # OTel Tracer
    trace_include_content: bool        # 是否追踪内容
```

### 3.4 状态更新流程

```
UserPromptNode
    │
    ├── 初始化 message_history
    ├── 构建 RunContext
    │
    ▼
ModelRequestNode
    │
    ├── 更新 usage.requests
    ├── 处理流式响应
    │
    ▼
CallToolsNode
    │
    ├── 解析响应部分
    ├── 执行工具调用
    ├── 更新 usage
    ├── 累加重试计数
    │
    └── 决定下一步：
        ├── End(FinalResult) → 结束
        └── ModelRequestNode → 继续循环
```

---

## 四、主循环流程

### 4.1 图节点定义

Agent 的主循环基于 `pydantic-graph` 库实现，采用**状态机模式**：

```python
# 节点基类
class AgentNode(BaseNode[GraphAgentState, GraphAgentDeps, FinalResult]):
    """所有 Agent 节点的基类"""
```

**核心节点**：

| 节点 | 职责 | 输入 | 输出 |
|------|------|------|------|
| `UserPromptNode` | 处理用户输入、初始化消息 | user_prompt | ModelRequestNode / CallToolsNode |
| `ModelRequestNode` | 发送请求给模型 | ModelRequest | CallToolsNode |
| `CallToolsNode` | 处理响应、执行工具 | ModelResponse | ModelRequestNode / End |

### 4.2 主循环流程图

```
                    ┌──────────────────────┐
                    │    UserPromptNode    │
                    │  (处理用户输入)        │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │   ModelRequestNode   │
                    │  (发送模型请求)        │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │    CallToolsNode     │
                    │  (处理响应/执行工具)   │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
              ▼                ▼                ▼
        ┌──────────┐    ┌──────────────┐  ┌──────────┐
        │ 工具调用  │    │ 文本输出      │  │ 空响应   │
        └────┬─────┘    └──────┬───────┘  └────┬─────┘
             │                 │               │
             │                 ▼               │
             │          ┌──────────────┐       │
             │          │ 输出验证      │       │
             │          └──────┬───────┘       │
             │                 │               │
             ▼                 ▼               ▼
        ┌─────────────────────────────────────────┐
        │              结束判断                     │
        │  - 有最终输出 → End(FinalResult)         │
        │  - 需要重试 → ModelRequestNode           │
        └─────────────────────────────────────────┘
```

### 4.3 UserPromptNode 详解

```python
@dataclass
class UserPromptNode(AgentNode):
    """处理用户提示和指令"""

    user_prompt: str | Sequence[UserContent] | None
    deferred_tool_results: DeferredToolResults | None = None
    instructions: str | None = None
    system_prompts: tuple[str, ...] = field(default_factory=tuple)

    async def run(self, ctx) -> ModelRequestNode | CallToolsNode:
        # 1. 获取或创建消息历史
        messages = ctx.state.message_history

        # 2. 处理延迟工具结果（恢复运行场景）
        if self.deferred_tool_results:
            return await self._handle_deferred_tool_results(...)

        # 3. 检查是否恢复运行（无新提示）
        if messages and isinstance(messages[-1], ModelRequest):
            if self.user_prompt is None:
                # 恢复之前的请求
                return ModelRequestNode(request=next_message)

        # 4. 构建新的请求消息
        parts = []
        if not messages:
            parts.extend(await self._sys_parts(run_context))  # 系统提示
        if self.user_prompt:
            parts.append(UserPromptPart(self.user_prompt))

        return ModelRequestNode(request=ModelRequest(parts=parts))
```

### 4.4 ModelRequestNode 详解

```python
@dataclass
class ModelRequestNode(AgentNode):
    """发送请求到模型"""

    request: ModelRequest
    is_resuming_without_prompt: bool = False

    async def run(self, ctx) -> CallToolsNode:
        # 1. 准备请求参数
        model, model_settings, model_request_params, messages, run_context = \
            await self._prepare_request(ctx)

        # 2. 发送请求（同步或流式）
        with set_current_run_context(run_context):
            response = await model.request(
                messages,
                model_settings,
                model_request_params,
                run_context
            )

        # 3. 更新状态
        ctx.state.usage.requests += 1
        messages.append(response)

        # 4. 返回工具处理节点
        return CallToolsNode(model_response=response)

    @asynccontextmanager
    async def stream(self, ctx) -> AsyncIterator[AgentStream]:
        """流式请求入口"""
        # 支持流式响应处理
        ...
```

### 4.5 CallToolsNode 详解

```python
@dataclass
class CallToolsNode(AgentNode):
    """处理模型响应，决定是否结束运行"""

    model_response: ModelResponse
    tool_call_results: dict[str, DeferredToolResult | Literal['skip']] | None = None

    async def run(self, ctx) -> ModelRequestNode | End[FinalResult]:
        # 1. 解析响应部分
        text = ''
        tool_calls: list[ToolCallPart] = []
        files: list[BinaryContent] = []

        for part in self.model_response.parts:
            if isinstance(part, TextPart):
                text += part.content
            elif isinstance(part, ToolCallPart):
                tool_calls.append(part)
            elif isinstance(part, FilePart):
                files.append(part.content)

        # 2. 处理空响应
        if not self.model_response.parts:
            if output_schema.allows_none:
                return End(FinalResult(None))
            ctx.state.consume_output_retry(ctx.deps.max_output_retries)
            return ModelRequestNode(ModelRequest(parts=[]))

        # 3. 处理工具调用
        if tool_calls:
            tool_results = await self._execute_tools(ctx, tool_calls)

            # 构建包含工具结果的请求
            parts = [ToolReturnPart(...) for ... in tool_results]
            return ModelRequestNode(ModelRequest(parts=parts))

        # 4. 处理文本输出
        if text:
            try:
                result_data = await self._handle_text_response(ctx, text)
                return End(FinalResult(result_data))
            except ToolRetryError as e:
                ctx.state.consume_output_retry(ctx.deps.max_output_retries)
                return ModelRequestNode(ModelRequest(parts=[e.tool_retry]))

        # 5. 处理文件输出
        if files:
            return End(FinalResult(files))
```

### 4.6 AgentRun 迭代接口

```python
@dataclass
class AgentRun:
    """可迭代的 Agent 运行实例"""

    _graph_run: GraphRun

    @property
    def next_node(self) -> AgentNode | End[FinalResult]:
        """下一个要执行的节点"""
        ...

    @property
    def result(self) -> AgentRunResult | None:
        """最终结果（如果已结束）"""
        ...

    async def __aiter__(self):
        """异步迭代节点执行"""
        async for step in self._graph_run:
            yield self._task_to_node(step.task)
```

**使用示例**：

```python
async with agent.iter('What is the capital of France?') as run:
    async for node in run:
        if isinstance(node, ModelRequestNode):
            print(f"Requesting model...")
        elif isinstance(node, CallToolsNode):
            print(f"Processing response...")
        elif isinstance(node, End):
            print(f"Result: {node.data.output}")
```

---

## 五、优秀设计模式

### 5.1 泛型类型安全

```python
Agent = Agent[AgentDepsT, OutputDataT]
```

- `AgentDepsT`：依赖类型（contravariant，消费）
- `OutputDataT`：输出类型（invariant）

**优势**：
- 编译时类型检查
- IDE 自动补全
- 无运行时开销

### 5.2 装饰器注册模式

```python
agent = Agent('openai:gpt-5.2')

@agent.tool
def get_weather(ctx: RunContext, city: str) -> str:
    """获取天气"""
    return f"Weather in {city}"

@agent.system_prompt
def dynamic_prompt(ctx: RunContext) -> str:
    return f"Current time: {datetime.now()}"
```

**优势**：
- 声明式配置
- 类型推导
- 代码组织清晰

### 5.3 图状态机模式

```
BaseNode → run() → NextNode | End
```

**优势**：
- 可视化执行流程
- 易于调试和追踪
- 支持断点恢复
- 可扩展新节点

### 5.4 Provider 适配器模式

```
                    ┌──────────────────┐
                    │   Agent          │
                    │   (框架层)        │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │   Model (抽象)    │
                    └────────┬─────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│ OpenAIModel   │   │ AnthropicModel│   │ GeminiModel   │
└───────────────┘   └───────────────┘   └───────────────┘
```

**核心转换**：

```python
# 模型适配器统一命名
# OpenAI: tool_calls → tool_call
# Anthropic: tool_use → tool_call
# Gemini: function_call → tool_call

# messages.py 统一使用 ToolCallPart
class ToolCallPart:
    tool_name: str
    args: Any
    tool_call_id: str
```

### 5.5 ContextVar 依赖注入

**问题**：工具函数需要访问运行时状态（如用户 ID、数据库连接），但不想显式传递。

**解决**：

```python
# 工具定义
@agent.tool
def my_tool(ctx: RunContext[MyDeps], arg: str) -> str:
    # ctx 自动注入
    return ctx.deps.db.query(arg)

# 或者无 RunContext 的工具
@agent.tool_plain
def simple_tool(arg: str) -> str:
    # 通过 ContextVar 获取
    ctx = get_current_run_context()
    return ctx.deps.db.query(arg)
```

### 5.6 能力系统（Capability）

```python
class AbstractCapability(Generic[AgentDepsT]):
    """能力抽象基类"""

    # 钩子方法
    async def before_model_request(self, ctx, params): ...
    async def after_model_request(self, ctx, response): ...
    async def prepare_tools(self, ctx, tool_defs): ...
    async def before_tool_call(self, ctx, tool_call): ...
    async def after_tool_call(self, ctx, result): ...

# 组合能力
capability = CombinedCapability([
    ToolSearchCap(),
    InstrumentationCap(),
])
```

**优势**：
- 横切关注点分离
- 可组合、可测试
- 不污染核心逻辑

### 5.7 输出模式（Output Schema）

```python
# 多种输出模式
output_schema = OutputSchema(
    mode='text' | 'tool' | 'both',  # 输出模式
    object_def=...,                  # Pydantic 模型
    allows_text=True,                # 是否允许文本
    allows_none=True,                # 是否允许空
)

# 输出验证器
@agent.output_validator
def validate_output(ctx: RunContext, output: MyOutput) -> MyOutput:
    if output.field < 0:
        raise ModelRetry("field must be positive")
    return output
```

### 5.8 重试机制

```python
class ModelRetry(Exception):
    """触发模型重试的异常"""

    def __init__(self, message: str):
        self.message = message

# 工具中触发重试
@agent.tool
def risky_tool(ctx: RunContext, arg: str) -> str:
    if not validate(arg):
        raise ModelRetry("Invalid arg, please fix")
    return "success"
```

**流程**：
1. 工具抛出 `ModelRetry`
2. `CallToolsNode` 捕获，构建 `RetryPromptPart`
3. 返回 `ModelRequestNode`，请求模型修正
4. 最多重试 `max_retries` 次

### 5.9 流式响应支持

```python
async with agent.iter(prompt) as run:
    async for node in run:
        if isinstance(node, ModelRequestNode):
            async with node.stream(ctx) as agent_stream:
                async for event in agent_stream:
                    if isinstance(event, PartStartEvent):
                        print(f"Part started: {event.part}")
                    elif isinstance(event, PartDeltaEvent):
                        print(f"Delta: {event.delta}")
```

### 5.10 可观测性集成

```python
# OpenTelemetry 集成
agent = Agent(
    'openai:gpt-5.2',
    instrument=InstrumentationSettings(
        include_content=True,
    )
)

# 自动生成 span
# - gen_ai.agent.run
# - gen_ai.model.request
# - gen_ai.tool.call
```

---

## 六、总结

Pydantic AI Agent 的设计体现了现代 Python 框架的最佳实践：

| 设计原则 | 实现方式 |
|---------|---------|
| **类型安全** | 泛型、Protocol、TypeGuard |
| **可扩展性** | 图节点、能力系统、工具集 |
| **Provider 无关** | 适配器模式、统一消息类型 |
| **可观测性** | OTel 集成、结构化日志 |
| **异步优先** | async/await、AsyncIterator |
| **声明式配置** | 装饰器、数据类 |

这种架构使得 Pydantic AI 能够：
- 支持多种 LLM Provider
- 灵活组合工具和能力
- 方便调试和追踪
- 易于测试和扩展
