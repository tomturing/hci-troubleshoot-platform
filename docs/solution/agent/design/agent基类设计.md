---
status: active
category: architecture
audience: engineer
last_updated: 2026-05-23
owner: team
---

# Agent 基类设计：第一性原理分析

> 从原始问题出发，推导 Agent 基类的最小契约和最佳存放位置。

---

## 一、Agent 是什么？（第一性原理）

剥掉所有框架噪音，Agent 的本质是一个**控制循环**：

```
感知（Perceive） → 推理（Think） → 行动（Act） → 新状态
                    ↑_________________________________|
                          直到满足终止条件
```

这是 ReAct（Yao et al., 2022）的核心，也是所有主流框架的底层结构：

| 框架 | 本质循环 |
|------|---------|
| LangGraph | 图节点迭代，终止 = 到达 END 节点 |
| OpenAI Agents SDK | `run()` 循环 + handoff，终止 = 无工具调用 |
| AutoGen | 消息传递，终止 = 终止消息 |
| 本项目 htp-agent | ReactEngine 的 while 循环，终止 = 无 tool_calls |

---

## 二、基类的最小契约

基类只需编码**三件事**：

| 关注点 | 方法 | 说明 |
|--------|------|------|
| **推理** | `think()` | 给定历史，决定：继续行动 还是 给出最终答案 |
| **行动** | `act()` | 执行一个动作，返回观察结果 |
| **循环** | `run()` | 组合上面两者，直到终止 |

**设计原则：终止条件内嵌在返回类型里，由类型系统强制检查，不需要魔法标志位。**

---

## 三、最简实现

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Union


# ─── 核心数据结构 ────────────────────────────────────────────────────────────

@dataclass
class Message:
    """对话历史的最小单元"""
    role: str   # "user" | "assistant" | "tool"
    content: Any


@dataclass
class ToolCall:
    """Agent 决定要执行的动作"""
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    """动作执行后返回的结果"""
    tool_call: ToolCall
    result: Any
    error: Exception | None = None


# 推理结果：要么继续行动，要么给出最终答案
# 终止条件编码在类型里，而非布尔标志位
Step = Union[ToolCall, str]


# ─── Agent 基类 ──────────────────────────────────────────────────────────────

class Agent(ABC):
    """
    Agent 基类：编码感知 → 推理 → 行动 的核心循环。

    子类只需实现两个方法：
    - think(): 给定历史，决定下一步
    - act():   执行动作，返回观察

    所有复杂性（记忆、多智能体、流式输出、可观测性）都在基类之外叠加。
    """

    def __init__(self, name: str, max_steps: int = 10):
        self.name = name
        self.max_steps = max_steps

    @abstractmethod
    async def think(self, context: list[Message]) -> Step:
        """
        推理：给定当前上下文，返回：
        - ToolCall：需要执行某个动作（继续循环）
        - str：最终答案（终止循环）
        """
        ...

    @abstractmethod
    async def act(self, tool_call: ToolCall) -> Observation:
        """行动：执行工具调用，返回观察结果"""
        ...

    async def run(self, user_input: str) -> str:
        """主循环：组合 think/act 直到终止"""
        context: list[Message] = [Message(role="user", content=user_input)]

        for _ in range(self.max_steps):
            step = await self.think(context)

            # 终止：推理直接给出答案（str）
            if isinstance(step, str):
                return step

            # 继续：执行动作，将观察结果追加到上下文
            obs = await self.act(step)
            context.append(Message(role="assistant", content=step))
            context.append(Message(role="tool", content=obs))

        raise RuntimeError(f"超过最大步数限制 {self.max_steps}")
```

---

## 四、设计决策解释

### 4.1 为什么 `think()` 返回 `Union[ToolCall, str]`？

终止条件内嵌在返回类型里，而非：
- ~~`is_done: bool` 标志位~~（需要额外状态同步）
- ~~`raise StopIteration`~~（异常控制流，语义模糊）
- ~~`return None` 表示终止~~（类型不安全）

Python 的 `isinstance()` 在运行时无损检查，且 IDE 类型提示完整覆盖。

### 4.2 为什么参数叫 `context` 而不是 `history`？

`history` 是事后记录视角（已发生的事），`context` 是决策输入视角（用来做判断的信息）。
Agent 调用 `think(context)` 是为了**做决策**，而不是为了"回顾历史"，语义更准确。

类型仍然是 `list[Message]`——这是 OpenAI / Anthropic API 的原生模型，比 `state: dict` 更具语义，比自定义状态类更轻量。

### 4.3 为什么 `max_steps` 是基类的职责？

所有生产级 Agent 框架（LangGraph、CrewAI、AutoGen）都有此约束——这是**安全基线**，不是业务逻辑，属于基类的核心职责之一。

### 4.4 为什么 `think` 和 `act` 分离？

| 好处 | 说明 |
|------|------|
| 独立测试 | `think` 可以单独 mock LLM；`act` 可以单独 mock 工具执行 |
| 独立替换 | 换 LLM 只改 `think`；换工具只改 `act` |
| 语义清晰 | 认知（think）与执行（act）是两种不同性质的操作 |

```
Agent = think() + act() + run()
         推理     行动    控制循环
```

所有复杂性（记忆、多智能体、流式输出、可观测性）都是在这三件事之外叠加的，而不是基类的职责。基类越薄，扩展性越强。

### 4.5 `Step` 类型别名是否必要？

`Step = Union[ToolCall, str]` 可以直接写成 `ToolCall | str`，类型别名**非必要，纯可读性**。删掉不影响任何语义。

```python
# 完全等价：
async def think(self, context: list[Message]) -> ToolCall | str: ...
async def think(self, context: list[Message]) -> Step: ...          # Step 只是别名
```

**决策**：代码文件中直接用 `ToolCall | str`，不保留 `Step` 别名。

### 4.6 Step 为什么不能简化为 `-> ToolCall`？

曾讨论过 `think() -> ToolCall`（签名更简单），但不可行：
**`ToolCall` 无法表达"最终答案"**——如果 think() 只返回 ToolCall，循环终止时答案放在哪里？

三种补救方案都有代价：

| 方案 | 实现 | 代价 |
|------|------|------|
| 返回 `ToolCall \| None` | None 表示终止，答案从 context 读 | 隐式，答案藏在 context[-1] 里 |
| 哨兵 ToolCall | `if tc.name == "final_answer"` | 魔法字符串，不类型安全 |
| 只靠 max_steps | 超步退出 | Agent 无法主动回复 |

**根本原因**：LLM API 天然产出两种输出（`tool_calls` 或 `content`），`ToolCall | str` 直接镜像这个二元性，是最自然的建模。

### 4.7 "final_answer 即工具"模式的取舍

这是一个真实存在的范式——ReAct 原论文的 `Finish[answer]`、早期 LangChain 的 `Final Answer` 都这样做：

```python
# 全部统一为 ToolCall，通过工具名终止
if tool_call.name == "final_answer":
    return tool_call.args["text"]
```

**为何不采用**：
- 原论文针对**纯文本补全模型**（2022年，无 tool-calling API）
- 本项目使用 GLM / OpenAI 兼容 API，API 本身已区分 `tool_calls` 和 `content`
- 强行统一需要：提示词强制 LLM "必须调用 final_answer 工具"，增加一次无意义的工具调用，污染工具列表

**决策**：保留 `ToolCall | str`，忠实映射 API 语义。

### 4.8 Step 是 1:1 还是 1:N？

当前基类 `think()` 每次返回一个 `ToolCall`（1:1）。现代 API 支持 parallel tool calling（1:N）。

**当前选择 1:1 的原因**：
- 符合原始 ReAct 范式：每步一个动作，易于调试和追踪
- 并行执行是扩展点，由子类或执行引擎层处理
- 基类不应预设并行语义

**需要并行时的升级路径**（不改基类契约）：
```python
# 子类在 think() 里返回一个"批量工具"，在 act() 里内部并行执行
async def act(self, tool_call: ToolCall) -> Observation:
    if tool_call.name == "__parallel__":
        results = await asyncio.gather(*[self._execute(tc) for tc in tool_call.args["calls"]])
        return Observation(tool_call=tool_call, result=results)
```

---

## 五、与本项目架构的对应关系

```
基类 Agent.think()   ←→   ReactEngine.execute()（推理循环）
基类 Agent.act()     ←→   SCPClient / AcliClient（工具执行）
基类 Agent.run()     ←→   DiagnosticAgent.stream_response()（主循环）
基类 Message         ←→   OpenAI message 格式（dict list）
基类 ToolCall        ←→   LLM 返回的 tool_calls[i]
基类 Observation     ←→   工具执行结果（注入 context）
```


本项目的 `AgentPort` Protocol 是**对外接口契约**（ConversationService 调用层），
`BaseAgent` ABC 是**对内继承契约**（各 Adapter 实现层），两者层次不同、各司其职。

---

## 六、基类放在哪里？

### 结论

**放在 `app/domain/base_agent.py`**，与 `agent_port.py` 并列。

### 推理过程

| 候选位置 | 理由 | 结论 |
|---------|------|------|
| `domain/base_agent.py` | Agent 控制循环是纯领域概念，无任何基础设施依赖 | ✅ **推荐** |
| `core/base_agent.py` | core/ 当前为空，填充它有意义，但与 domain/ 的职责重叠 | ⚠️ 可行，但有歧义 |
| `adapters/base_agent.py` | Adapter 层是具体实现，不是接口定义 | ❌ 层次错误 |

### 六边形架构视角

```
        ┌────────────────────────────────┐
        │         domain/                │
        │  agent_port.py  ← 对外 Protocol │
        │  base_agent.py  ← 对内 ABC      │  ← 已创建
        └────────────────────────────────┘
                    ↑ 实现
        ┌────────────────────────────────┐
        │         adapters/agents/       │
        │  htp/diagnostic_agent.py       │
        │  ops/ops_agent_adapter.py      │
        │  pai/pai_agent_adapter.py      │
        └────────────────────────────────┘
```

`domain/` 是服务的核心，不依赖任何外部框架——这正是 `BaseAgent` 该住的地方。

> `core/` 和 `services/` 两个空目录已删除（遵循"不为未来设计"原则）。

---

## 七、变更历史

| 日期 | 变更摘要 |
|------|---------|
| 2026-05-23 | 初版：第一性原理分析 Agent 基类设计与存放位置 |
| 2026-05-23 | 创建 `app/domain/base_agent.py`；参数 history → context；删除 core/ services/ 空目录 |
| 2026-05-23 | 补充设计决策讨论：Step 别名可选性、final_answer 工具模式取舍、1:1 vs 1:N |
