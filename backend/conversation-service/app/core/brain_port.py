"""
BrainPort Protocol：大脑可选架构的核心接口定义（T1-3）

基于六边形架构（Ports & Adapters）模式：
  - BrainPort：Port（接口），ConversationService 只依赖此 Protocol
  - HTPBrainAdapter：Adapter（见 adapters/htp_brain_adapter.py），封装原有 S0-S6 逻辑
  - OpsAgentBrainAdapter：Adapter（见 adapters/ops_agent_brain_adapter.py），反腐层

跨仓库契约：docs/contracts/brain-http-api.yaml（权威来源）
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ── 事件类型定义 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BrainTextChunk:
    """大脑输出的文本流块（用户可见内容）。

    对应 brain-http-api.yaml 中 BrainSSEChunk.choices[0].delta.content 字段。
    """

    content: str


@dataclass(frozen=True)
class BrainStageUpdate:
    """大脑内部阶段变化通知（前端可选展示进度指示器）。

    对应 brain-http-api.yaml 中 BrainSSEChunk.x_stage_update 扩展字段。
    ops-agent 侧阶段：intake / routing / validation / solution / confirmation / closed
    htp 大脑侧阶段：S0 ~ S6
    """

    stage: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrainEscalation:
    """大脑触发人工升级请求（超出当前大脑处理范围）。

    ConversationService 收到此事件后应创建升级工单。
    """

    reason: str
    context: dict[str, Any] = field(default_factory=dict)


# BrainEvent = 大脑可以产出的所有事件类型（用于 IDE 类型提示）
BrainEvent = BrainTextChunk | BrainStageUpdate | BrainEscalation


# ── 错误类型 ────────────────────────────────────────────────────────────────────

class BrainUnavailableError(Exception):
    """目标大脑不可达（服务宕机、网络超时等）。

    BrainRouter 捕获此错误后自动降级到备用大脑。
    OpsAgentBrainAdapter 在 httpx 连接失败时 raise 此错误（不透传原始异常）。
    """

    def __init__(self, brain_name: str, reason: str = "") -> None:
        self.brain_name = brain_name
        self.reason = reason
        super().__init__(f"大脑 [{brain_name}] 不可达: {reason}")


# ── 核心 Port 接口 ──────────────────────────────────────────────────────────────

@runtime_checkable
class BrainPort(Protocol):
    """大脑执行接口（Port）。

    所有具体大脑实现（Adapter）必须实现此协议。
    ConversationService 只依赖此接口，不直接引用任何 Adapter 类。

    设计约束：
    - process() 必须是 async generator，支持流式输出
    - 不负责消息持久化（由 ConversationService 负责）
    - 不负责会话状态的 DB 写入（由 ConversationService 负责）
    - 遇到不可恢复错误时 raise BrainUnavailableError，不 raise 底层异常
    """

    async def process(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        env_context: dict[str, Any] | None = None,
        stream: bool = True,
    ) -> AsyncGenerator[BrainEvent, None]:
        """执行一轮大脑推理，以流式方式产出事件序列。

        Args:
            session_id: 对话 session 唯一标识，用于跨轮次上下文恢复。
            messages: OpenAI 格式的消息列表（包含历史和当前用户消息）。
            env_context: HCI 实时环境上下文（告警、失败任务等），由 htp 侧预取后注入。
                        对应 brain-http-api.yaml 的 HCIContext 结构。
            stream: 是否启用流式输出（False 时 Adapter 可一次性 yield 最终结果）。

        Yields:
            BrainTextChunk | BrainStageUpdate | BrainEscalation

        Raises:
            BrainUnavailableError: 大脑服务不可达时，由 BrainRouter 负责降级处理。
        """
        ...  # Protocol 方法体，实际由 Adapter 实现
