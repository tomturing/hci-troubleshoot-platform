# Pydantic AI ReAct 模式支持分析

## 概述

ReAct（Reasoning and Acting）是一种让 LLM Agent 进行"思考-行动-观察"循环的模式。本文档分析 Pydantic AI 对 ReAct 模式的支持情况。

---

## 一、结论：原生架构天然支持 ReAct

Pydantic AI **没有** 提供名为 "ReAct" 的独立类或模块，但其**架构设计天然支持 ReAct 模式**的所有核心要素。

### ReAct 模式核心要素

```
┌─────────────────────────────────────────────────────┐
│                    ReAct 循环                        │
│                                                     │
│   Thought (思考) → Action (行动) → Observation (观察) │
│         ↑                                    │      │
│         └────────────────────────────────────┘      │
└─────────────────────────────────────────────────────┘
```

### Pydantic AI 对应实现

| ReAct 要素 | Pydantic AI 实现 | 说明 |
|-----------|-----------------|------|
| **Thought** | `ThinkingPart` / 文本响应 | 模型思考过程 |
| **Action** | `ToolCallPart` | 工具调用 |
| **Observation** | `ToolReturnPart` | 工具返回结果 |
| **循环** | Agent 主循环 | 自动将结果传回模型 |

---

## 二、ThinkingPart：思考过程支持

### 2.1 定义

```python
# messages.py
@dataclass
class ThinkingPart:
    """模型的思考响应"""

    content: str              # 思考内容
    id: str | None            # 标识符
    signature: str | None     # 签名（用于 Anthropic/Google/OpenAI）
    provider_name: str | None # Provider 名称
    provider_details: dict    # Provider 特有数据

    part_kind: Literal['thinking'] = 'thinking'
```

### 2.2 支持的 Provider

| Provider | 模型示例 | Thinking 支持 |
|----------|---------|---------------|
| Anthropic | Claude 4 (extended thinking) | ✅ `signature` 字段 |
| OpenAI | o1, o3, o4-mini | ✅ `encrypted_content` 字段 |
| Google | Gemini 2.5 Pro (thinking mode) | ✅ `thought_signature` 字段 |
| DeepSeek | R1 | ✅ 原生 thinking |
| Bedrock | Claude via Bedrock | ✅ `reasoningContent` |

### 2.3 启用 Thinking

```python
from pydantic_ai import Agent
from pydantic_ai.capabilities import Thinking

# 方式 1：通过 Capability
agent = Agent(
    'anthropic:claude-opus-4',
    capabilities=[Thinking(effort='high')]
)

# 方式 2：通过 ModelSettings
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

agent = Agent(
    'openai:o3',
    model_settings=ModelSettings(thinking='high')
)
```

### 2.4 Thinking Effort 级别

```python
ThinkingEffort = Literal['minimal', 'low', 'medium', 'high', 'xhigh']
ThinkingLevel = bool | ThinkingEffort
```

- `True`: 启用（默认级别）
- `False`: 禁用（对始终思考的模型静默忽略）
- `'minimal'` ~ `'xhigh'`: 指定思考深度

---

## 三、工具调用循环：Action-Observation 实现

### 3.1 ReAct 循环在 Pydantic AI 中的体现

```
┌──────────────────────────────────────────────────────────────────┐
│                     Agent 主循环                                  │
│                                                                  │
│  UserPromptNode ──► ModelRequestNode ──► CallToolsNode          │
│                           ▲                    │                 │
│                           │                    ▼                 │
│                           │         ┌─────────────────┐          │
│                           │         │ 执行工具调用     │          │
│                           │         │ ToolCallPart    │          │
│                           │         │      ↓          │          │
│                           │         │ ToolReturnPart  │          │
│                           │         └────────┬────────┘          │
│                           │                  │                   │
│                           └──────────────────┘                   │
│                                  (循环)                          │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 消息流示例

```python
# 1. 用户输入
messages = [ModelRequest(parts=[UserPromptPart("What's the weather in Paris?")])]

# 2. 模型思考并决定调用工具
response = ModelResponse(parts=[
    ThinkingPart(content="User wants weather info, I should use the weather tool..."),
    ToolCallPart(tool_name='get_weather', args={'city': 'Paris'}, tool_call_id='001')
])

# 3. 执行工具，返回结果
tool_result = ToolReturnPart(
    tool_name='get_weather',
    content='Sunny, 22°C',
    tool_call_id='001'
)

# 4. 将结果传回模型
next_request = ModelRequest(parts=[tool_result])

# 5. 模型生成最终响应
final_response = ModelResponse(parts=[
    TextPart(content='The weather in Paris is sunny with a temperature of 22°C.')
])
```

### 3.3 完整代码示例

```python
from pydantic_ai import Agent, RunContext

# 定义 Agent
agent = Agent(
    'openai:gpt-4.1',  # 或 'anthropic:claude-opus-4' 支持原生 thinking
    system_prompt='You are a helpful assistant that can check weather.'
)

# 注册工具
@agent.tool
def get_weather(ctx: RunContext, city: str) -> str:
    """Get the current weather for a city."""
    # 模拟天气 API
    return f"Sunny, 22°C in {city}"

# 运行 Agent（自动执行 ReAct 循环）
result = agent.run_sync("What's the weather in Paris?")
print(result.output)
# > The weather in Paris is sunny with a temperature of 22°C.

# 查看消息历史
for msg in result.all_messages():
    print(type(msg).__name__, msg)
```

---

## 四、Thinking vs ReAct：区别与联系

### 4.1 概念对比

| 特性 | Thinking（原生思考） | ReAct（框架模式） |
|------|---------------------|------------------|
| **定义** | 模型内部的推理过程 | 思考-行动-观察循环 |
| **可见性** | `ThinkingPart` 显式存储 | 通过消息历史观察 |
| **控制权** | 模型控制 | 框架 + 模型共同控制 |
| **工具调用** | 可选 | 必须包含工具调用 |
| **Provider 依赖** | 需要模型支持 | 所有 Provider 支持 |

### 4.2 关系图

```
┌─────────────────────────────────────────────────────────────┐
│                        Agent Run                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │   Thought   │───►│   Action    │───►│ Observation │     │
│  │             │    │             │    │             │     │
│  │ ThinkingPart│    │ToolCallPart │    │ToolReturnPart│    │
│  │   或 Text   │    │             │    │             │     │
│  └─────────────┘    └─────────────┘    └─────────────┘     │
│        │                  │                  │              │
│        └──────────────────┴──────────────────┘              │
│                          │                                  │
│                          ▼                                  │
│                    ┌───────────┐                            │
│                    │ 下一个循环  │                            │
│                    └───────────┘                            │
└─────────────────────────────────────────────────────────────┘
```

### 4.3 使用场景

**使用原生 Thinking**：
- 需要模型的深度推理能力
- 使用支持 extended thinking 的模型（Claude 4, DeepSeek R1, o1）
- 想要保存和审计推理过程

**使用传统 ReAct**：
- 所有模型都支持
- 需要明确的工具调用
- 想要更细粒度的控制

---

## 五、实现 ReAct 模式的最佳实践

### 5.1 标准模式（推荐）

```python
from pydantic_ai import Agent, RunContext
from pydantic_ai.usage import UsageLimits

agent = Agent(
    'openai:gpt-4.1',
    system_prompt='''You are a helpful assistant.
Think step by step before taking actions.
Use tools when you need additional information.''',
    # 限制循环次数，防止无限循环
    usage_limits=UsageLimits(request_limit=10)
)

@agent.tool
def search(ctx: RunContext, query: str) -> str:
    """Search for information."""
    return f"Results for: {query}"

@agent.tool
def calculate(ctx: RunContext, expression: str) -> float:
    """Calculate a mathematical expression."""
    return eval(expression)  # 实际使用时请注意安全

result = agent.run_sync("What is 15% of 847?")
```

### 5.2 结合 Thinking（深度推理）

```python
from pydantic_ai import Agent
from pydantic_ai.capabilities import Thinking

agent = Agent(
    'anthropic:claude-opus-4',
    capabilities=[Thinking(effort='high')],
    system_prompt='Think carefully before responding.'
)

# 模型会自动输出 ThinkingPart，无需额外配置
result = agent.run_sync("Solve this complex problem...")
```

### 5.3 监控 ReAct 循环

```python
async with agent.iter(prompt) as run:
    async for node in run:
        if isinstance(node, CallToolsNode):
            # 工具执行节点
            response = node.model_response
            print(f"Thought: {response.thinking}")  # 如果有 thinking
            print(f"Actions: {[p.tool_name for p in response.tool_calls]}")
        elif isinstance(node, ModelRequestNode):
            print(f"Sending request to model...")
```

### 5.4 防止无限循环

```python
from pydantic_ai.usage import UsageLimits

agent = Agent(
    'openai:gpt-4.1',
    # 限制请求数
    usage_limits=UsageLimits(
        request_limit=5,        # 最多 5 次请求
        tool_calls_limit=10,    # 最多 10 次工具调用
        total_tokens_limit=10000  # 最多 10000 tokens
    )
)
```

---

## 六、总结

### Pydantic AI 对 ReAct 的支持方式

| 层面 | 支持情况 | 说明 |
|------|---------|------|
| **直接支持** | ❌ 无 `ReActAgent` | 不提供独立的 ReAct 类 |
| **架构支持** | ✅ 天然支持 | 工具调用循环即 ReAct |
| **Thinking 支持** | ✅ `ThinkingPart` | 原生思考过程 |
| **工具系统** | ✅ 完整支持 | `ToolCallPart` + `ToolReturnPart` |
| **循环控制** | ✅ `UsageLimits` | 防止无限循环 |
| **可观测性** | ✅ 消息历史 | 完整记录 Thought/Action/Observation |

### 设计哲学

Pydantic AI 选择**不提供独立的 ReAct 实现**，而是通过**通用架构**支持 ReAct 模式：

1. **Agent 主循环**本身就是 ReAct 循环
2. **ThinkingPart** 提供原生的思考支持
3. **工具系统**提供行动能力
4. **消息系统**记录观察结果

这种设计更加**灵活和通用**，允许开发者：
- 使用任何支持工具调用的模型
- 自由组合 thinking 和工具调用
- 精确控制循环行为
- 轻松实现更复杂的模式（如多 Agent 协作）
