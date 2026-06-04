"""
Agent Service - HTTP SSE 流式推理端点

POST /v1/agent/stream：接收推理请求，流式返回 AgentEvent SSE 事件。
此端点是 conversation-service AgentClient 的对端。
"""

import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from app.adapters.agents.agent_router import AgentRouter
from app.adapters.agents.htp.confirm_service import ConfirmService
from app.domain.agent_port import (
    AgentEscalation,
    AgentInteractiveRequest,
    AgentStageUpdate,
    AgentTextChunk,
    AgentUnavailableError,
)

router = APIRouter(tags=["agent"])
logger = get_logger("agent-service")


def _check_internal_auth(request: Request) -> None:
    """验证内部服务 Token（防止越权访问）"""
    from app.config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Token 无效")


# 全局 AgentRouter，由 main.py lifespan 注入
_agent_router: AgentRouter | None = None
# 全局 ConfirmService，由 main.py lifespan 注入（用于 ReAct 确认回路）
_confirm_service: ConfirmService | None = None


def set_agent_router(router_instance: AgentRouter) -> None:
    """在应用启动时注入 AgentRouter（lifespan 调用）"""
    global _agent_router
    _agent_router = router_instance


def set_confirm_service(confirm_service: ConfirmService | None) -> None:
    """在应用启动时注入 ConfirmService（lifespan 调用）"""
    global _confirm_service
    _confirm_service = confirm_service


# ── 请求/响应 Schema ──────────────────────────────────────────────────────────


class AgentStreamRequest(BaseModel):
    """流式推理请求体"""

    assistant_type: str
    session_id: str
    case_id: str
    user_id: str
    messages: list[dict[str, Any]]
    env_context: dict[str, Any] | None = None
    stream: bool = True
    diagnostic_stage: str = "S0"  # 诊断阶段（S0/S1/S2/S3/S4/S5/S6）
    category_id: str | None = None  # S0 确认的分类编码（S1+ 阶段必须）
    execution_mode: str = "direct"  # 执行模式（direct/react）
    system_prompt: str | None = None  # 自定义 system_prompt（可选）
    sop_resume_context: dict[str, Any] | None = None  # T-AGT-23: SOP 执行恢复上下文


class InteractiveResponseRequest(BaseModel):
    """ACP 交互响应请求体"""

    acp_session_id: str
    request_id: str
    outcome: Any  # 兼容 dict / str 等各类交互响应结构


# ── SSE 序列化辅助 ────────────────────────────────────────────────────────────


def _sse(data: dict) -> str:
    """将 dict 序列化为 SSE data 行"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _event_stream(
    req: AgentStreamRequest,
) -> AsyncGenerator[str, None]:
    """将 AgentRouter.process() 的事件流转换为 SSE 字节串"""
    if _agent_router is None:
        yield _sse({"type": "error", "message": "AgentRouter 未初始化"})
        return

    trace_id = get_current_trace_id()
    logger.info(
        event="agent_stream_start",
        message="推理请求开始",
        assistant_type=req.assistant_type,
        session_id=req.session_id,
        case_id=req.case_id,
        trace_id=trace_id,
    )

    _has_text_chunk = False
    try:
        async for event in _agent_router.process(
            assistant_type=req.assistant_type,
            session_id=req.session_id,
            messages=req.messages,
            env_context=req.env_context,
            stream=req.stream,
            case_id=req.case_id,
            user_id=req.user_id,
            diagnostic_stage=req.diagnostic_stage,
            category_id=req.category_id,
            execution_mode=req.execution_mode,
            system_prompt=req.system_prompt,
            sop_resume_context=req.sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文
        ):
            if isinstance(event, AgentTextChunk):
                _has_text_chunk = True
                yield _sse({"type": "text_chunk", "content": event.content})
            elif isinstance(event, AgentStageUpdate):
                yield _sse(
                    {
                        "type": "stage_update",
                        "stage": event.stage,
                        "metadata": event.metadata,
                    }
                )
            elif isinstance(event, AgentInteractiveRequest):
                yield _sse(
                    {
                        "type": "interactive_request",
                        "request_id": event.request_id,
                        "acp_session_id": event.acp_session_id,
                        "kind": event.kind,
                        "title": event.title,
                        "prompt": event.prompt,
                        "options": event.options,
                        "custom_input": event.custom_input,
                        "metadata": event.metadata,
                    }
                )
            elif isinstance(event, AgentEscalation):
                yield _sse(
                    {
                        "type": "escalation",
                        "reason": event.reason,
                        "context": event.context,
                    }
                )

        # 空流检测：整个推理过程没有任何文本输出
        if not _has_text_chunk:
            logger.warning(
                event="agent_stream_empty",
                message="推理流结束但无任何 text_chunk 输出",
                session_id=req.session_id,
                case_id=req.case_id,
                assistant_type=req.assistant_type,
            )
            yield _sse(
                {
                    "type": "error",
                    "message": "AI 推理未返回任何内容，可能是模型服务异常或配置问题",
                }
            )

        yield _sse({"type": "done"})
        logger.info(
            event="agent_stream_done",
            message="推理流完成",
            session_id=req.session_id,
            case_id=req.case_id,
        )

    except AgentUnavailableError as exc:
        logger.error(
            event="agent_unavailable",
            message=str(exc),
            agent_name=exc.agent_name,
            session_id=req.session_id,
        )
        yield _sse({"type": "error", "message": f"大脑 [{exc.agent_name}] 不可达: {exc.reason}"})
    except Exception as exc:
        logger.error(
            event="agent_stream_error",
            message=str(exc),
            session_id=req.session_id,
        )
        yield _sse({"type": "error", "message": f"推理异常: {exc!s}"})


# ── 路由 ──────────────────────────────────────────────────────────────────────


@router.post("/v1/agent/stream")
async def agent_stream(req: AgentStreamRequest) -> StreamingResponse:
    """
    流式推理端点

    conversation-service 通过此端点将推理请求委托给 agent-service，
    以 SSE 格式接收事件流并转发给前端。

    SSE 事件类型：
    - ``{"type": "text_chunk", "content": "..."}``
    - ``{"type": "stage_update", "stage": "S2", "metadata": {}}``
    - ``{"type": "interactive_request", ...}``
    - ``{"type": "escalation", "reason": "...", "context": {}}``
    - ``{"type": "done"}``
    - ``{"type": "error", "message": "..."}``
    """
    if _agent_router is None:
        raise HTTPException(status_code=503, detail="agent-service 尚未就绪")

    return StreamingResponse(
        _event_stream(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/v1/agent/health")
async def agent_health() -> dict:
    """健康检查"""
    return {
        "status": "ok",
        "agent_router": _agent_router is not None,
    }


@router.post("/v1/agent/interactive-response")
async def submit_interactive_response(req: InteractiveResponseRequest) -> dict:
    """
    转发 ACP 交互响应到 ops-agent

    conversation-service 在用户完成弹框选择后调用此接口。
    """
    if _agent_router is None:
        raise HTTPException(status_code=503, detail="agent-service 尚未就绪")

    ops_adapter = _agent_router.get_ops_agent_adapter()
    if ops_adapter is None:
        return {"success": False, "reason": "OpsAgentAdapter 未启用"}

    try:
        success = await ops_adapter.submit_acp_response(
            acp_session_id=req.acp_session_id,
            request_id=req.request_id,
            outcome=req.outcome,
        )
        return {"success": success}
    except Exception as exc:
        logger.error(
            event="interactive_response_error",
            message=str(exc),
            acp_session_id=req.acp_session_id,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/v1/agent/resume-stream/{session_id}")
async def resume_ops_agent_stream(session_id: str) -> StreamingResponse:
    """
    恢复 ops-agent 事件流（主动推送待处理事件）

    conversation-service 在认为 ops-agent 有待处理事件时调用此接口。
    """
    if _agent_router is None:
        raise HTTPException(status_code=503, detail="agent-service 尚未就绪")

    ops_adapter = _agent_router.get_ops_agent_adapter()
    if ops_adapter is None:
        raise HTTPException(status_code=404, detail="OpsAgentAdapter 未启用")

    async def _stream() -> AsyncGenerator[str, None]:
        async for event in ops_adapter.resume_event_stream(session_id):
            if isinstance(event, AgentTextChunk) and event.content:
                yield _sse({"type": "text_chunk", "content": event.content})
            elif isinstance(event, AgentInteractiveRequest):
                yield _sse(
                    {
                        "type": "interactive_request",
                        "request_id": event.request_id,
                        "acp_session_id": event.acp_session_id,
                        "kind": event.kind,
                        "title": event.title,
                        "prompt": event.prompt,
                        "options": event.options,
                        "custom_input": event.custom_input,
                        "metadata": event.metadata,
                    }
                )
            elif isinstance(event, AgentStageUpdate):
                yield _sse({"type": "stage_update", "stage": event.stage, "metadata": event.metadata})
        yield _sse({"type": "done"})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── ReAct 工具确认端点（T-AGT-03）──────────────────────────────────────────────────


class ReactConfirmRequest(BaseModel):
    """ReAct 工具确认请求体"""

    session_id: str
    confirmed: bool
    authorized_by: str = "user"


@router.post("/v1/agent/react-confirm")
async def react_confirm(request: Request, body: ReactConfirmRequest) -> dict:
    """
    ReAct 工具确认端点

    当 ReactEngine 对 risk_level>=2 的工具调用暂停并等待用户确认时，
    前端收到 interactive_request SSE（kind=tool_confirm），用户点击确认后，
    前端 POST conversation-service /interactive-response，
    conversation-service 按 kind 分叉路由到此端点。

    此端点调用 ConfirmService.submit_confirm() 向 Redis LPUSH 确认结果，
    解除 ReactEngine.request_confirm() 的 BRPOP 等待，工具继续执行。

    Args:
        request: FastAPI Request（用于提取 Authorization header）
        body: ReactConfirmRequest
            - session_id: 会话 ID（对应 conversation_id）
            - confirmed: True=用户确认执行，False=用户取消
            - authorized_by: 操作人标识（默认"user"）

    Returns:
        {"ok": True} 确认成功
        {"ok": False, "reason": "..."} ConfirmService 未注入
    """
    # 验证内部服务 Token（防止越权确认）
    _check_internal_auth(request)

    if _confirm_service is None:
        logger.warning(
            event="react_confirm_service_unavailable",
            message="ConfirmService 未注入，ReAct 确认功能不可用",
            session_id=body.session_id,
        )
        return {"ok": False, "reason": "ConfirmService 未注入"}

    try:
        await _confirm_service.submit_confirm(
            session_id=body.session_id,
            confirmed=body.confirmed,
            authorized_by=body.authorized_by,
        )
        logger.info(
            event="react_confirm_submitted",
            message="ReAct 工具确认已提交",
            session_id=body.session_id,
            confirmed=body.confirmed,
            authorized_by=body.authorized_by,
        )
        return {"ok": True}
    except Exception as exc:
        logger.error(
            event="react_confirm_error",
            message=f"submit_confirm 失败: {exc}",
            session_id=body.session_id,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
