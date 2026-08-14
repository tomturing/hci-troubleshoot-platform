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

import contextvars
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from typing import Any

from shared.observability.logger import get_logger

logger = get_logger(__name__)

_langfuse_client: Any = None
_langfuse_checked: bool = False
_current_workflow_observation: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "langfuse_workflow_observation",
    default=None,
)
_current_content_capture: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "langfuse_content_capture",
    default=None,
)


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
        from langfuse import Langfuse
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider

        # Langfuse 与 Tempo 使用各自的 TracerProvider。Langfuse 只接收显式创建的
        # workflow/generation/tool observation；HTTPX、SQLAlchemy 等基础设施 Span
        # 继续由 otel.py 的私有 Provider 发往 Tempo，避免 Langfuse 出现大量噪声。
        langfuse_provider = TracerProvider(
            resource=Resource.create({SERVICE_NAME: os.environ.get("SERVICE_NAME", "hci-platform")})
        )
        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            tracer_provider=langfuse_provider,
            blocked_instrumentation_scopes=[
                "opentelemetry.instrumentation.httpx",
                "opentelemetry.instrumentation.sqlalchemy",
                "opentelemetry.instrumentation.fastapi",
            ],
        )

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


def _env_bool(name: str, default: bool = False) -> bool:
    """读取布尔环境变量。"""

    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _capture_content_for_operation(operation: str) -> bool:
    """按业务类型决定是否记录正文；专用配置优先于全局配置。"""

    operation_key = re.sub(r"[^A-Z0-9]+", "_", operation.upper()).strip("_")
    dedicated_name = f"LANGFUSE_CAPTURE_{operation_key}_CONTENT"
    if dedicated_name in os.environ:
        return _env_bool(dedicated_name)
    return _env_bool("LANGFUSE_CAPTURE_CONTENT", default=True)


def _content_summary(value: Any, *, capture_content: bool | None = None) -> Any:
    """生成默认脱敏观测内容；显式开启后才发送正文。"""

    from shared.observability.redaction import redact_observation_value

    redacted = redact_observation_value(value)
    if capture_content is None:
        capture_content = _env_bool("LANGFUSE_CAPTURE_CONTENT", default=True)
    if capture_content:
        return redacted
    try:
        serialized = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        serialized = str(redacted)
    return {
        "content_redacted": True,
        "content_chars": len(serialized),
        "content_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def _current_otel_trace_context() -> dict[str, str] | None:
    """把当前 Tempo Span 身份用于 Langfuse Trace 关联，不共享 Exporter。"""

    try:
        from shared.observability.otel import get_current_span_id, get_current_trace_id

        trace_id = get_current_trace_id()
        span_id = get_current_span_id()
        if len(trace_id) == 32:
            context = {"trace_id": trace_id}
            if len(span_id) == 16:
                context["parent_span_id"] = span_id
            return context
    except Exception:
        pass
    return None


def _start_explicit_observation(
    *,
    name: str,
    as_type: str,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    model: str | None = None,
    model_parameters: dict[str, Any] | None = None,
    trace_id: str = "",
    capture_content: bool | None = None,
) -> Any:
    """在当前业务 observation 下创建显式子节点，避免污染全局 OTel 上下文。"""

    lf = get_langfuse()
    if lf is None:
        return None
    kwargs: dict[str, Any] = {
        "as_type": as_type,
        "name": name,
        "input": _content_summary(input, capture_content=capture_content),
        "metadata": metadata or {},
    }
    if model:
        kwargs["model"] = model
    if model_parameters:
        kwargs["model_parameters"] = model_parameters
    parent = _current_workflow_observation.get()
    if parent is not None:
        return parent.start_observation(**kwargs)
    trace_context = _current_otel_trace_context()
    if trace_context is None and len(trace_id) == 32:
        trace_context = {"trace_id": trace_id}
    if trace_context:
        kwargs["trace_context"] = trace_context
    return lf.start_observation(**kwargs)


@contextmanager
def observe_workflow(
    *,
    name: str,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    session_id: str = "",
    user_id: str = "system",
    trace_id: str = "",
) -> Generator[Any, None, None]:
    """观测一次 KBD/离线诊断业务步骤；未配置 Langfuse 时安全降级。"""

    from contextlib import nullcontext

    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState, set_span_in_context

        from shared.observability.otel import get_otel_provider

        provider = get_otel_provider()
        parent_context = None
        current_context = otel_trace.get_current_span().get_span_context()
        if (
            provider is not None
            and not current_context.is_valid
            and len(trace_id) == 32
        ):
            try:
                explicit_trace_id = int(trace_id, 16)
            except ValueError:
                explicit_trace_id = 0
            if explicit_trace_id:
                # 后台任务脱离原请求 Context 后，使用持久化 trace_id 构造远程父上下文，
                # 让日志、Tempo、Langfuse 和批次审计仍能以同一 Trace ID 互查。
                parent_span_context = SpanContext(
                    trace_id=explicit_trace_id,
                    span_id=secrets.randbits(64) or 1,
                    is_remote=True,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                    trace_state=TraceState(),
                )
                parent_context = set_span_in_context(NonRecordingSpan(parent_span_context))
        otel_context = (
            provider.get_tracer("hci.business-workflow").start_as_current_span(name, context=parent_context)
            if provider is not None
            else nullcontext()
        )
    except Exception:
        otel_context = nullcontext()

    with otel_context:
        try:
            observation = _start_explicit_observation(
                name=name,
                as_type="span",
                input=input,
                metadata=metadata,
                trace_id=trace_id,
            )
        except Exception as exc:
            logger.warning(event="langfuse_workflow_start_error", workflow=name, error=str(exc))
            observation = None
        if observation is None:
            yield None
            return

        token = _current_workflow_observation.set(observation)
        try:
            try:
                observation.update_trace(user_id=user_id or "system", session_id=session_id or name)
            except Exception as exc:
                logger.warning(event="langfuse_trace_update_error", workflow=name, error=str(exc))
            yield observation
        except Exception as exc:
            with suppress(Exception):
                observation.update(level="ERROR", status_message=str(exc)[:1000])
            raise
        finally:
            _current_workflow_observation.reset(token)
            try:
                observation.end()
            except Exception as exc:
                logger.warning(event="langfuse_workflow_end_error", workflow=name, error=str(exc))


@contextmanager
def observe_llm_generation(
    *,
    operation: str,
    model: str,
    input: Any,
    metadata: dict[str, Any] | None = None,
    model_parameters: dict[str, Any] | None = None,
) -> Generator[Any, None, None]:
    """观测一次独立 LLM 尝试；格式重试和网络重试不得覆盖前一轮。"""

    capture_content = _capture_content_for_operation(operation)
    try:
        observation = _start_explicit_observation(
            name=f"llm.{operation}",
            as_type="generation",
            input=input,
            metadata=metadata,
            model=model,
            model_parameters=model_parameters,
            capture_content=capture_content,
        )
    except Exception as exc:
        logger.warning(event="langfuse_generation_start_error", operation=operation, error=str(exc))
        observation = None
    capture_token = _current_content_capture.set(capture_content)
    try:
        yield observation
    except Exception as exc:
        if observation is not None:
            with suppress(Exception):
                observation.update(level="ERROR", status_message=str(exc)[:1000])
        raise
    finally:
        _current_content_capture.reset(capture_token)
        if observation is not None:
            try:
                observation.end()
            except Exception as exc:
                logger.warning(event="langfuse_generation_end_error", operation=operation, error=str(exc))


def update_observation(
    observation: Any,
    *,
    output: Any = None,
    metadata: dict[str, Any] | None = None,
    usage_details: dict[str, int] | None = None,
    level: str | None = None,
    status_message: str | None = None,
) -> None:
    """以统一脱敏策略结束前更新 observation。"""

    if observation is None:
        return
    try:
        kwargs: dict[str, Any] = {
            "output": _content_summary(output, capture_content=_current_content_capture.get()),
            "metadata": metadata or {},
        }
        if usage_details:
            kwargs["usage_details"] = usage_details
        if level:
            kwargs["level"] = level
        if status_message:
            kwargs["status_message"] = status_message[:1000]
        observation.update(**kwargs)
    except Exception as exc:
        logger.warning(event="langfuse_observation_update_error", error=str(exc))


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
        elif output is not None:
            # 空字符串 "" 也是合法的输出（如 LLM 返回空回复），
            # 避免因 falsy 检查跳过导致 Langfuse observation 完全没有 output 字段
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


def start_agent_observation(
    *,
    user_id: str = "",
    case_id: str = "",
    assistant_type: str = "",
    execution_mode: str = "",
    sop_mode: bool = False,
    max_iterations: int = 15,
) -> tuple[Any, Any]:
    """创建 Agent 执行顶层 observation，串联所有子 LLM 调用和工具执行。

    在 react_engine.execute() 入口调用，返回 (observation, context_manager)。
    用 try/finally 模式包裹整个 ReAct 循环，finally 中调用 ctx.__exit__(None, None, None)。

    此 observation 创建后，所有 observe_invoke / observe_stream_start / observe_tool
    调用都会通过 Langfuse v3 SDK 的 OpenTelemetry context 自动嵌套为子 observation。

    Returns:
        (observation, context_manager) — Langfuse 未配置时返回 (None, None)
    """
    lf = get_langfuse()
    if lf is None:
        return None, None

    try:
        from shared.observability.otel import get_current_trace_id as _get_otel_trace_id

        otel_trace_id = _get_otel_trace_id()
    except Exception:
        otel_trace_id = ""

    try:
        ctx = lf.start_as_current_observation(
            as_type="span",
            name="agent-execution",
            metadata={
                "assistant_type": assistant_type,
                "case_id": case_id,
                "otel_trace_id": otel_trace_id,
                "execution_mode": execution_mode,
                "sop_mode": sop_mode,
                "max_iterations": max_iterations,
            },
        )
        obs = ctx.__enter__()
        obs.update_trace(
            user_id=user_id or "unknown",
            session_id=case_id or user_id,
        )
        return obs, ctx
    except Exception as e:
        logger.warning(event="langfuse_agent_start_error", error=str(e))
        return None, None


@contextmanager
def observe_tool(
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    exec_id: str = "",
    session_id: str = "",
    risk_level: int = 1,
    trace_id: str = "",
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
        from shared.observability.redaction import redact_observation_value

        with lf.start_as_current_observation(
            as_type="tool",
            name=f"tool.{tool_name}",
            input=redact_observation_value(tool_args),
            metadata={
                "exec_id": exec_id,
                "session_id": session_id,
                "risk_level": risk_level,
                "otel_trace_id": trace_id,
            },
        ) as obs:
            yield obs
    except Exception as e:
        logger.warning(event="langfuse_tool_observe_error", error=str(e))
        yield None


@contextmanager
def observe_skill(
    *,
    skill_name: str,
    variable_name: str = "",
    context_variables: dict[str, Any] | None = None,
    conversation_id: str = "",
    case_id: str = "",
) -> Generator[Any, None, None]:
    """为动态 Skill 执行创建 Langfuse skill observation（context manager）。

    在 DynamicSkillRunner.execute() 中嵌入，记录 skill 调用的 input（上下文变量）、
    output（执行结果）和错误信息。若 Langfuse 未配置，gracefully 降级为 noop。

    Usage:
        with observe_skill(skill_name="hci-alert-parsing", variable_name="node_ip",
                           context_variables={...}) as obs:
            result = await runner._execute_skill(...)
            if obs:
                obs.update(output=result)
    """
    lf = get_langfuse()
    if lf is None:
        yield None
        return

    try:
        with lf.start_as_current_observation(
            as_type="span",
            name=f"skill.{skill_name}",
            input=context_variables,
            metadata={
                "skill_name": skill_name,
                "variable_name": variable_name,
                "conversation_id": conversation_id,
                "case_id": case_id,
            },
        ) as obs:
            yield obs
    except Exception as e:
        logger.warning(event="langfuse_skill_observe_error", error=str(e))
        yield None
