# BaseAgent 模块设计文档

> 本文档基于 `/aihci/ops-agent/ops_agent/agent/base_agent.py` 源码分析撰写

## 1. 模块概述

`BaseAgent` 是所有 LLM 驱动型 Agent 的抽象基类，定义了 Agent 执行的核心框架和通用能力。它实现了：

- **执行循环控制**：管理 Agent 的多步执行流程
- **工具调用管理**：统一的工具发现、执行、错误处理机制
- **消息历史管理**：自动压缩、Token 限制、上下文窗口保护
- **智能重试机制**：针对 LLM 响应异常的自适应重试
- **MCP 协议集成**：支持外部 MCP 服务器的工具发现与调用
- **轨迹记录**：完整的执行过程记录，支持复盘与调试

## 2. 核心数据结构

### 2.1 AgentState 枚举

```python
class AgentState(Enum):
    IDLE = "idle"          # 空闲状态
    RUNNING = "running"    # 执行中
    COMPLETED = "completed" # 已完成
    ERROR = "error"        # 错误状态
```

### 2.2 AgentStepState 枚举

```python
class AgentStepState(Enum):
    THINKING = "thinking"       # LLM 推理中
    CALLING_TOOL = "calling_tool" # 执行工具
    REFLECTING = "reflecting"   # 反思阶段
    COMPLETED = "completed"     # 步骤完成
    ERROR = "error"             # 步骤出错
```

### 2.3 AgentStep 数据类

```python
@dataclass
class AgentStep:
    step_number: int                    # 步骤编号
    state: AgentStepState              # 当前状态
    thought: str | None = None         # 思考内容
    tool_calls: list[ToolCall] | None = None   # 工具调用
    tool_results: list[ToolResult] | None = None # 工具结果
    llm_response: LLMResponse | None = None    # LLM 原始响应
    reflection: str | None = None      # 反思内容
    error: str | None = None           # 错误信息
    extra: dict[str, object] | None = None  # 扩展元数据
    llm_usage: LLMUsage | None = None  # Token 使用量
    start_time: float | None = None    # 步骤开始时间
    duration: float | None = None      # 步骤耗时
```

### 2.4 AgentExecution 数据类

```python
@dataclass
class AgentExecution:
    task: str                          # 任务描述
    steps: list[AgentStep]             # 执行步骤列表
    final_result: str | None = None    # 最终结果
    success: bool = False              # 是否成功
    total_tokens: LLMUsage | None = None # 总 Token 使用量
    execution_time: float = 0.0        # 总执行时间
    agent_state: AgentState = AgentState.IDLE # Agent 状态
```

### 2.5 CompletionValidationResult 数据类

```python
@dataclass
class CompletionValidationResult:
    """任务完成前的 payload 验证结果"""
    is_valid: bool           # 是否有效
    should_retry: bool = False  # 是否应该重试
    error_message: str = ""     # 错误信息
```

## 3. 核心属性

```python
class BaseAgent(ABC):
    # 核心组件
    _llm_client: LLMClient              # LLM 客户端
    _model_config: ModelConfig          # 模型配置
    _max_steps: int                     # 最大执行步数
    _tools: list[Tool]                  # 可用工具列表
    _tool_caller: ToolExecutor          # 工具执行器

    # 轨迹与调试
    _trajectory_recorder: TrajectoryRecorder | None  # 轨迹记录器
    _debug_llm: bool                    # 是否开启 LLM 调试

    # 消息管理
    _message_summarizer: MessageSummarizer  # 消息摘要器
    _initial_messages: list[LLMMessage]     # 初始消息列表

    # 预算控制
    _tool_call_budget: int | None       # 工具调用总预算
    _tool_call_budget_used: int = 0     # 已使用预算
    BUDGET_WARNING_THRESHOLD: int = 10  # 预算预警阈值

    # 重试控制
    MAX_IN_PLACE_RETRIES: int = 3       # 最大原地重试次数
    SKIP_TOOL_FAILURE_RETRY: bool = True # 是否跳过工具失败重试

    # MCP 支持
    mcp_tools: list[Tool] = []          # MCP 工具列表
    mcp_clients: list[tuple[str, object]] = []  # MCP 客户端列表
    mcp_server_runtime: dict[str, MCPServerRuntimeInfo] = {}  # MCP 运行时状态

    # 重复调用检测
    REPEATED_TOOL_CALL_WHITELIST: set[str] = {
        "sequentialthinking",
        "ops_state_update",
        "get_info_from_user",
        "sub_agent_task_done",
        "task_done",
    }
```

## 4. 核心执行流程

### 4.1 execute_task() 主循环

```
初始化
    │
    ▼
┌─────────────────────────────────┐
│   检查并压缩消息历史           │◄───────────────────┐
│   (check_and_summarize)        │                    │
└─────────────────────────────────┘                    │
    │                                                  │
    ▼                                                  │
┌─────────────────────────────────┐                    │
│   调用 LLM 生成响应             │                    │
│   (achat)                      │                    │
└─────────────────────────────────┘                    │
    │                                                  │
    ▼                                                  │
┌─────────────────────────────────┐                    │
│   检查任务是否完成？            │                    │
│   (llm_indicates_task_completed)│                   │
└─────────────────────────────────┘                    │
    │                                                  │
    ├─ 是 → 验证 payload → 提取结果 → 结束            │
    │                                                  │
    否                                                │
    │                                                  │
    ▼                                                  │
┌─────────────────────────────────┐                    │
│   执行工具调用                  │                    │
│   (_tool_call_handler)         │                    │
└─────────────────────────────────┘                    │
    │                                                  │
    ▼                                                  │
┌─────────────────────────────────┐                    │
│   记录轨迹 + 更新控制台         │                    │
│   (_finalize_step)             │────────────────────┘
└─────────────────────────────────┘
```

### 4.2 _run_llm_step() 智能重试机制

BaseAgent 实现了四种场景的智能重试：

| 场景 | 条件 | 处理方式 |
|-----|------|---------|
| Case 1 | 空 tool_calls + 空 content | 调整 temperature 重试 |
| Case 2 | 空 tool_calls + 有 content | 调整 temperature 重试 |
| Case 3 | 有 tool_calls + 工具执行失败 | 根据 SKIP_TOOL_FAILURE_RETRY 决定 |
| Case 4 | 有 tool_calls + 全部成功 | 正常执行，无重试 |

重试温度调度：
```python
retry_temperatures = [0.6, 0.8, 1.0]
```

### 4.3 历史记录清理

在重试过程中，BaseAgent 会智能清理失败的历史记录：

1. **Anthropic 格式**：`_cleanup_failure_history_before_retry_anthropic()`
   - 保留 tool_use 块，清除文本内容
   - 确保 tool_result 与 tool_use_id 正确匹配

2. **OpenAI/GLM 格式**：`_cleanup_failure_history_before_retry()`
   - 回滚到快照位置
   - 保留最后一个 assistant 消息的 tool_calls

## 5. 工具执行机制

### 5.1 _tool_call_handler() 流程

```
接收 tool_calls
    │
    ▼
┌─────────────────────────────────┐
│   检查是否需要审批             │
│   (needs_approval)             │
└─────────────────────────────────┘
    │
    ├─ 需要审批 → 请求用户批准
    │
    ▼
┌─────────────────────────────────┐
│   检查重复工具调用             │
│   (_check_repeated_tool_calls) │
└─────────────────────────────────┘
    │
    ├─ 检测到重复 → 返回警告消息
    │
    ▼
┌─────────────────────────────────┐
│   检查预算                     │
│   (budget_guard)               │
└─────────────────────────────────┘
    │
    ├─ 预算不足 → 返回预算拦截消息
    │
    ▼
┌─────────────────────────────────┐
│   执行工具                     │
│   (parallel/sequential)        │
└─────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────┐
│   扣减预算 + 拼接预算通知      │
└─────────────────────────────────┘
    │
    ▼
返回工具结果消息
```

### 5.2 预算控制机制

```python
def _build_budget_notice_xml(self) -> str:
    """构建预算通知 XML 块"""
    remaining = max(0, self._tool_call_budget - self._tool_call_budget_used)

    if remaining == 0:
        # 预算耗尽：禁止再调用工具，强制结束
        ...
    elif remaining <= self.BUDGET_WARNING_THRESHOLD:
        # 预算预警：提示收敛
        ...
```

### 5.3 重复调用检测

```python
def _check_repeated_tool_calls(self, tool_calls: list[ToolCall]) -> tuple[bool, str | None]:
    """检查连续两轮工具调用是否完全相同

    只检测当前工具调用与紧邻的上一轮是否完全一致。
    白名单工具（sequentialthinking, ops_state_update 等）不检测。
    """
```

## 6. MCP 协议集成

### 6.1 MCP 初始化流程

```
start_mcp_initialization()
    │
    ▼
┌─────────────────────────────────┐
│   并行初始化 MCP 服务器        │
│   (max_concurrency=4)          │
└─────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────┐
│   每个服务器：                 │
│   1. connect_and_discover      │
│   2. 发现可用工具              │
│   3. 检测工具冲突              │
└─────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────┐
│   注册工具到 _tools 列表       │
│   更新 _tool_caller            │
└─────────────────────────────────┘
```

### 6.2 工具冲突处理

```python
def _warn_mcp_tool_conflict(self, server_name: str, tool_name: str, ...):
    """当 MCP 工具与内置工具同名时，跳过 MCP 工具"""
```

## 7. 消息压缩机制

### 7.1 MessageSummarizer 集成

```python
# 初始化时创建
self._message_summarizer = MessageSummarizer(
    llm_client=self._llm_client,
    model_config=self._model_config,
    token_limit=token_limit,  # 默认 150000
    compression_mode="step",  # 按步骤压缩
)

# 执行时检查
await self._message_summarizer.check_and_summarize()
```

### 7.2 消息追踪

```python
# 追踪用户消息
self._message_summarizer.track_message(
    role="user",
    content=content,
    step_number=step.step_number
)

# 追踪助手消息
self._message_summarizer.track_message(
    role="assistant",
    content=content,
    tool_calls=tool_calls,
    step_number=step.step_number
)
```

## 8. 抽象方法

子类必须实现以下抽象方法：

```python
@abstractmethod
def new_task(self, task: str, extra_args: dict[str, str] | None = None, tool_names: list[str] | None = None):
    """创建新任务"""
    pass
```

子类通常重写的钩子方法：

```python
def llm_indicates_task_completed(self, llm_response: LLMResponse) -> bool:
    """判断 LLM 是否表示任务完成"""
    pass

async def _extract_final_result(self, llm_response: LLMResponse, step: AgentStep) -> str:
    """提取最终结果"""
    pass

def validate_completion_payload(self, llm_response: LLMResponse) -> CompletionValidationResult:
    """验证任务完成的 payload"""
    pass

def _get_retry_temperature(self, retry_count: int) -> float:
    """获取重试温度"""
    pass

def _build_budget_notice_xml(self) -> str:
    """构建预算通知"""
    pass

def build_budget_guard_message(self, requested_calls: int, remaining: int) -> str:
    """构建预算拦截消息"""
    pass
```

## 9. 流式输出支持

```python
async def execute_task_streaming(self) -> AsyncGenerator[str, None]:
    """流式执行任务：每步 LLM 推理完成后立即 yield 助手文本内容"""
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def _on_step_text(content: str) -> None:
        await queue.put(content)

    self._step_text_hook = _on_step_text
    # ... 执行并 yield
```

## 10. 设计亮点

### 10.1 智能重试的三层保护

1. **空响应重试**：LLM 返回空内容时自动调整 temperature
2. **内容重试**：有内容但无工具调用时触发重试
3. **工具失败处理**：可配置是否跳过工具失败重试

### 10.2 历史记录的精确管理

- 在每次 LLM 调用前记录快照
- 重试时精确回滚到失败前状态
- 支持 Anthropic 和 OpenAI 两种消息格式

### 10.3 预算控制的渐近式提醒

- 正常预算时不打扰
- 进入预警阈值后持续提醒
- 耗尽时强制拦截

### 10.4 MCP 的异步并行初始化

- 最多 4 个并发连接
- 超时与取消处理完善
- 工具冲突自动检测

## 11. 与子类的关系

```
BaseAgent (抽象基类)
    │
    ├── OpsAgent (主 Agent)
    │   └── 负责任务 orchestration、用户交互、流程控制
    │
    └── SOPQuerySubAgent (子 Agent)
        └── 负责 SOP 检索、路径收敛、证据管理
```

BaseAgent 提供了：
- 统一的执行循环框架
- 通用的错误处理与重试逻辑
- 标准化的工具调用机制
- 可扩展的预算控制接口

子类需要：
- 实现 `new_task()` 定义任务初始化逻辑
- 重写 `llm_indicates_task_completed()` 定义完成判断
- 重写 `_extract_final_result()` 定义结果提取
- 可选重写各种钩子方法实现定制化行为
