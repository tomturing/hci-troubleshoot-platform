"""
Langfuse LLM 可观测性集成模块（v3 SDK）。

在 OpenClawAssistant 的 invoke() 和 chat_completion_stream() 中嵌入 Langfuse tracing，
记录每次 LLM 调用的 prompt、completion、token 用量、延迟和模型信息。

使用方式：
  1. 设置环境变量 LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY / LANGFUSE_HOST
  2. 调用 get_langfuse() 获取客户端实例
  3. 调用 observe_invoke() / observe_stream_start() 创建 Langfuse observation
  4. 调用 end_generation() 结束 observation 并写入观测数据

若未配置环境变量，gracefully 降级为空操作（不影响业务）。

v3 架构要点：
  - 根 span 隐式创建 trace，通过 update_trace() 设置 user_id/session_id
  - generation 作为根 span 的子 observation 嵌套，记录模型调用详情
  - 所有 observation 需显式调用 .end()（流式场景无法使用 context manager）
"""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from shared.observability.logger import get_logger

logger = get_logger(__name__)

_langfuse_client: Any = None
_langfuse_checked: bool = False


def get_langfuse():
    """获取 Langfuse 客户端（懒加载单例）。

    v3 SDK 通过环境变量 LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY / LANGFUSE_HOST
    自动配置，无需显式传参。

    Returns:
        Langfuse 客户端实例，或 None（未配置时）。
    """
    global _langfuse_client, _langfuse_checked

    if _langfuse_checked:
        return _langfuse_client

    _langfuse_checked = True

    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")

    if not secret_key or not public_key:
        logger.info(event="langfuse_disabled", message="未配置 LANGFUSE_SECRET_KEY/PUBLIC_KEY，Langfuse 已禁用")
        return None

    try:
        from langfuse import get_client

        _langfuse_client = get_client()
        logger.info(
            event="langfuse_initialized",
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
        return _langfuse_client
    except ImportError:
        logger.warning(event="langfuse_import_error", message="langfuse 包未安装，执行 uv sync 安装依赖")
        return None
    except Exception as e:
        logger.error(event="langfuse_init_error", error=str(e))
        return None


async def observe_invoke(
    *,
    model: str,
    assistant_type: str,
    user_id: str,
    case_id: str,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> tuple[Any, str, float]:
    """为非流式 LLM 调用创建 Langfuse observation（根 span + generation）。

    在 invoke() 调用前调用，返回 (observations_tuple, trace_id, start_time)。
    调用完成后需调用 end_generation()。

    Returns:
        ((root_observation, generation_observation), otel_trace_id, start_timestamp)
    """
    lf = get_langfuse()
    if lf is None:
        return None, "", time.monotonic()

    try:
        from shared.observability.otel import get_current_trace_id as _get_otel_trace_id
        otel_trace_id = _get_otel_trace_id()
    except Exception:
        otel_trace_id = ""

    try:
        root = lf.start_observation(
            as_type="span",
            name=f"{assistant_type}-invoke",
        )
        root.update_trace(
            user_id=user_id or "unknown",
            session_id=case_id or user_id,
        )

        gen_metadata: dict[str, Any] = {"assistant_type": assistant_type, "otel_trace_id": otel_trace_id}
        if tools:
            gen_metadata["tools"] = [t.get("function", {}).get("name", "") for t in tools]

        gen = root.start_observation(
            as_type="generation",
            name="llm-invoke",
            model=model,
            input=messages,
            metadata=gen_metadata,
        )

        return (root, gen), otel_trace_id, time.monotonic()
    except Exception as e:
        logger.warning(event="langfuse_observe_error", error=str(e))
        return None, "", time.monotonic()


async def observe_stream_start(
    *,
    model: str,
    assistant_type: str,
    user_id: str,
    case_id: str,
    messages: list[dict],
) -> tuple[Any, str, float]:
    """为流式 LLM 调用创建 Langfuse observation（根 span + generation）。

    Returns:
        ((root_observation, generation_observation), otel_trace_id, start_timestamp)
    """
    lf = get_langfuse()
    if lf is None:
        return None, "", time.monotonic()

    try:
        from shared.observability.otel import get_current_trace_id as _get_otel_trace_id
        otel_trace_id = _get_otel_trace_id()
    except Exception:
        otel_trace_id = ""

    try:
        root = lf.start_observation(
            as_type="span",
            name=f"{assistant_type}-stream",
        )
        root.update_trace(
            user_id=user_id or "unknown",
            session_id=case_id or user_id,
        )

        gen = root.start_observation(
            as_type="generation",
            name="llm-stream",
            model=model,
            input=messages,
            metadata={"assistant_type": assistant_type, "otel_trace_id": otel_trace_id},
        )

        return (root, gen), otel_trace_id, time.monotonic()
    except Exception as e:
        logger.warning(event="langfuse_observe_stream_error", error=str(e))
        return None, "", time.monotonic()


def end_generation(
    obs_tuple: Any,
    *,
    output: str = "",
    tool_calls: list[dict] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    start_time: float = 0,
    error: str | None = None,
) -> None:
    """结束 Langfuse observation 并写入观测数据（output / token 用量 / 错误信息）。

    Args:
        obs_tuple: observe_invoke() / observe_stream_start() 返回的 (root, gen) 元组
    """
    if obs_tuple is None:
        return

    root, gen = obs_tuple
    duration_ms = (time.monotonic() - start_time) * 1000 if start_time else 0

    try:
        update_kwargs: dict[str, Any] = {}

        if tool_calls:
            update_kwargs["output"] = tool_calls
        elif output:
            update_kwargs["output"] = output

        if total_tokens > 0:
            update_kwargs["usage_details"] = {
                "input": prompt_tokens,
                "output": completion_tokens,
                "total": total_tokens,
            }

        if error:
            update_kwargs["status_message"] = error

        update_kwargs["metadata"] = {"duration_ms": round(duration_ms, 1)}

        gen.update(**update_kwargs)
        gen.end()
        root.end()
    except Exception as e:
        logger.warning(event="langfuse_end_error", error=str(e))


@contextmanager
def observe_tool(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    exec_id: str = "",
    session_id: str = "",
    risk_level: int = 1,
) -> Generator[Any, None, None]:
    """为 ReAct 工具执行创建 Langfuse tool observation（context manager）。

    在 _execute_tool_call 中嵌入，记录工具调用的 input（参数）、output（执行结果）和错误信息。

    Usage:
        with observe_tool(tool_name="acli_exec", tool_args={...}, exec_id=...) as obs:
            result = await executor.execute(...)
            if obs:
                obs.update(output=str(result))

    若 Langfuse 未配置，gracefully 降级为 noop。
    """
    lf = get_langfuse()
    if lf is None:
        yield None
        return

    try:
        with lf.start_as_current_observation(
            as_type="tool",
            name=f"tool.{tool_name}",
            input=tool_args,
            metadata={
                "exec_id": exec_id,
                "session_id": session_id,
                "risk_level": risk_level,
            },
        ) as obs:
            yield obs
    except Exception as e:
        logger.warning(event="langfuse_tool_observe_error", error=str(e))
        yield None
