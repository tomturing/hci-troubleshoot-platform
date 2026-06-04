"""
Agent 基类：编码感知 → 推理 → 行动 的核心控制循环。

设计原则（第一性原理）：
  Agent = think() + act() + run()
           推理     行动    控制循环

  - think()：给定上下文，决定"继续行动"还是"给出最终答案"
  - act()：执行一个工具调用，返回观察结果
  - run()：组合 think/act 循环，直到终止或超出步数限制

终止条件内嵌在 Step 类型里（Union[ToolCall, str]），
由类型系统强制检查，无需魔法标志位。

参考文档：docs/solution/agent/agent基类设计.md
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# ─── 核心数据结构 ────────────────────────────────────────────────────────────────


@dataclass
class Message:
    """Agent 上下文的最小单元（OpenAI message 格式对应物）"""

    role: str  # "user" | "assistant" | "tool"
    content: Any


@dataclass
class ToolCall:
    """Agent 决定要执行的动作"""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    """工具调用执行后返回的结果"""

    tool_call: ToolCall
    result: Any
    error: Exception | None = None


# 推理结果类型：
#   ToolCall → 需要继续执行动作（循环）
#   str      → 最终答案（终止）
Step = ToolCall | str


# ─── Agent 基类 ──────────────────────────────────────────────────────────────────


class BaseAgent(ABC):
    """
    Agent 基类：感知 → 推理 → 行动 的控制循环抽象。

    子类必须实现：
      - think(context): 给定上下文，决定下一步
      - act(tool_call):  执行工具调用，返回观察结果

    所有复杂性（记忆、多智能体、流式输出、可观测性）在基类之外叠加，
    不属于基类的职责。基类越薄，扩展性越强。
    """

    def __init__(self, name: str, max_steps: int = 15) -> None:
        self.name = name
        self.max_steps = max_steps

    @abstractmethod
    async def think(self, context: list[Message]) -> Step:
        """
        推理：给定当前上下文，决定下一步行动。

        Returns:
            ToolCall：需要执行某个工具（循环继续）
            str：最终答案文本（循环终止）
        """
        ...

    @abstractmethod
    async def act(self, tool_call: ToolCall) -> Observation:
        """行动：执行工具调用，返回观察结果"""
        ...

    async def run(self, user_input: str) -> str:
        """
        主控制循环：组合 think/act 直到终止。

        Raises:
            RuntimeError: 超过 max_steps 步数限制（防止无限循环）
        """
        context: list[Message] = [Message(role="user", content=user_input)]

        for _ in range(self.max_steps):
            step = await self.think(context)

            # 终止条件：think() 返回 str → 最终答案
            if isinstance(step, str):
                return step

            # 继续循环：执行工具调用，将观察结果追加到上下文
            obs = await self.act(step)
            context.append(Message(role="assistant", content=step))
            context.append(Message(role="tool", content=obs))

        raise RuntimeError(f"[{self.name}] 超过最大步数限制 {self.max_steps}")
