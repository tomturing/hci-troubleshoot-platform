"""
Conversation Routes - 对话API路由 (v2.0 多类型AI助手)
"""

import asyncio
import json
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from shared.database.postgres import DatabaseManager
from shared.models.schemas import MessageCreate, MessageResponse
from shared.utils.exceptions import AIStreamError, ErrorCode

from ..repositories.conversation_repo import ConversationRepository
from ..schemas import ConversationSessionResponse
from ..services.ai_client import AIAssistantRegistry
from ..services.conversation_service import ConversationService
from ..services.kb_client import KBClient
from ..services.scheduler_client import SchedulerClient

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

# 全局依赖，需要在main.py中注入
database_manager: DatabaseManager | None = None
ai_registry: AIAssistantRegistry | None = None
scheduler_client: SchedulerClient | None = None
kb_client: KBClient | None = None
redis_client = None        # Redis client，供 ConfirmService 使用
_tool_router = None        # ToolRouter（Phase 4 agent 模式）
_confirm_service = None    # ConfirmService
_glm_client = None         # GLMClient（ReactExecutor 专用）
_knowledge_extractor = None  # KnowledgeExtractor（Phase 4 S6 知识闭环）


def set_dependencies(
    db: DatabaseManager,
    registry: AIAssistantRegistry,
    scheduler: SchedulerClient | None = None,
    kb: KBClient | None = None,
    redis=None,
    tool_router=None,
    confirm_service=None,
    glm_client=None,
    knowledge_extractor=None,
):
    global database_manager, ai_registry, scheduler_client, kb_client
    global redis_client, _tool_router, _confirm_service, _glm_client, _knowledge_extractor
    database_manager = db
    ai_registry = registry
    scheduler_client = scheduler
    kb_client = kb
    redis_client = redis
    _tool_router = tool_router
    _confirm_service = confirm_service
    _glm_client = glm_client
    _knowledge_extractor = knowledge_extractor


async def get_conversation_service() -> ConversationService:
    """依赖注入: 获取Conversation Service"""
    if not database_manager or not ai_registry:
        raise HTTPException(status_code=500, detail="Service dependencies not initialized")

    async for session in database_manager.get_session():
        repo = ConversationRepository(session)
        service = ConversationService(
            repo,
            ai_registry,
            scheduler_client,
            kb_client,
            database_manager.async_session_factory,
            tool_router=_tool_router,
            confirm_service=_confirm_service,
            glm_client=_glm_client,
        )
        service.knowledge_extractor = _knowledge_extractor
        yield service


@router.post("/", status_code=201, response_model=ConversationSessionResponse)
async def create_conversation(
    case_id: str,
    assistant_type: str = "openclaw",
    initial_message: str | None = None,
    case_title: str | None = None,
    case_description: str | None = None,
    service: ConversationService = Depends(get_conversation_service),
):
    """创建新对话，case_title/case_description 存入 metadata 供 Pod 分配时使用"""
    metadata: dict | None = None
    if case_title or case_description:
        metadata = {"case_title": case_title or "", "case_description": case_description or ""}
    conversation = await service.create_conversation(
        case_id=case_id, assistant_type=assistant_type, initial_message=initial_message, metadata=metadata
    )
    return ConversationSessionResponse(
        conversation_id=conversation.conversation_id,
        case_id=conversation.case_id,
        assistant_type=conversation.assistant_type or "openclaw",
        diagnostic_stage=getattr(conversation, "diagnostic_stage", "S0") or "S0",
        category_l1=getattr(conversation, "category_l1", None),
        category_l2=getattr(conversation, "category_l2", None),
        category_id=getattr(conversation, "category_id", None),
    )


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID, service: ConversationService = Depends(get_conversation_service)
):
    """获取对话详情"""
    conversation = await service.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationSessionResponse(
        conversation_id=conversation.conversation_id,
        case_id=conversation.case_id,
        assistant_type=conversation.assistant_type or "openclaw",
        diagnostic_stage=getattr(conversation, "diagnostic_stage", "S0") or "S0",
        category_l1=getattr(conversation, "category_l1", None),
        category_l2=getattr(conversation, "category_l2", None),
        category_id=getattr(conversation, "category_id", None),
    )
    return conversation


@router.get("/case/{case_id}")
async def get_conversations_by_case(case_id: str, service: ConversationService = Depends(get_conversation_service)):
    """获取工单的所有对话"""
    return await service.repository.get_conversations_by_case(case_id)


@router.get("/{conversation_id}/messages", response_model=list[MessageResponse])
async def get_messages(conversation_id: uuid.UUID, service: ConversationService = Depends(get_conversation_service)):
    """获取对话消息历史"""
    messages = await service.get_messages(conversation_id)
    return [MessageResponse.model_validate(msg) for msg in messages]


@router.post("/{conversation_id}/message")
async def send_message(
    conversation_id: uuid.UUID,
    message: MessageCreate,
    background_tasks: BackgroundTasks,
    service: ConversationService = Depends(get_conversation_service),
):
    """
    发送消息并获取SSE流式响应

    Returns:
        StreamingResponse: SSE流，格式为 data: <chunk>\n\n
    """
    ai_content = []

    async def event_generator():
        try:
            # agent 模式（ToolRouter + ReactExecutor）优先，未配置则回退普通流式
            if service.agent_mode_available:
                async for item in service.send_message_react_stream(
                    session_id=str(conversation_id),
                    conversation_id=conversation_id,
                    case_id=message.case_id,
                    content=message.content,
                ):
                    if "content" in item:
                        ai_content.append(item["content"])
                        yield f"data: {json.dumps({'content': item['content']}, ensure_ascii=False)}\n\n"
                    elif "type" in item:
                        # SSE 事件（thinking / confirm_request / tool_executing）
                        event_type = item["type"]
                        yield f"event: {event_type}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
            else:
                async for chunk in service.send_message_stream_only(
                    conversation_id=conversation_id, case_id=message.case_id, content=message.content
                ):
                    if chunk:
                        ai_content.append(chunk)
                        # JSON encode chunk to safely preserve newlines in SSE
                        encoded_chunk = json.dumps({"content": chunk}, ensure_ascii=False)
                        yield f"data: {encoded_chunk}\n\n"

            if not ai_content:
                # 上游返回 200 但未产出任何 token 时，向前端返回结构化错误，避免出现空白气泡
                empty_error = json.dumps(
                    {
                        "code": ErrorCode.AI_STREAM_FAILED.value,
                        "message": "AI 未返回有效内容，请稍后重试",
                        "detail": "empty_stream",
                    },
                    ensure_ascii=False,
                )
                yield f"event: error\ndata: {empty_error}\n\n"
                return

            # 正常流结束后，提交后台任务保存消息
            background_tasks.add_task(
                service.save_assistant_message,
                conversation_id=conversation_id,
                case_id=message.case_id,
                content="".join(ai_content),
            )

            yield "data: [DONE]\n\n"

        except asyncio.CancelledError:
            # 客户端断开连接，若已收到部分 AI 回复则在后台保存，避免丢失
            if ai_content:
                background_tasks.add_task(
                    service.save_assistant_message,
                    conversation_id=conversation_id,
                    case_id=message.case_id,
                    content="".join(ai_content),
                )
            raise
        except AIStreamError as e:
            # 结构化 AI 流错误，使用 json.dumps 安全序列化
            if ai_content:
                background_tasks.add_task(
                    service.save_assistant_message,
                    conversation_id=conversation_id,
                    case_id=message.case_id,
                    content="".join(ai_content),
                )
            yield f"event: error\ndata: {e.to_sse_data()}\n\n"
        except Exception as e:
            # 其他未知错误，构造通用错误响应
            if ai_content:
                background_tasks.add_task(
                    service.save_assistant_message,
                    conversation_id=conversation_id,
                    case_id=message.case_id,
                    content="".join(ai_content),
                )
            error_data = json.dumps(
                {
                    "code": ErrorCode.INTERNAL_ERROR.value,
                    "message": "服务内部错误，请稍后重试",
                    "detail": type(e).__name__,
                },
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error_data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream", background=background_tasks)


# ── Phase 3：人工确认接口 ──────────────────────────────────────────────────────

class ConfirmRequest(BaseModel):
    """用户工具调用确认请求体"""

    confirmed: bool
    authorized_by: str    # 当前操作用户 ID


@router.post("/{session_id}/confirm", status_code=200, summary="提交工具调用人工确认结果")
async def submit_confirm(
    session_id: str,
    req: ConfirmRequest,
):
    """
    接收用户对高风险工具调用的确认结果（确认/取消），
    通过 Redis LPUSH 通知 ReAct 执行器继续执行。

    Redis 不可用时返回 503，避免静默失败。
    """
    if redis_client is None:
        raise HTTPException(status_code=503, detail="确认服务暂不可用（Redis 未连接）")

    from ..services.confirm_service import ConfirmService
    confirm_svc = ConfirmService(redis=redis_client)
    await confirm_svc.submit_confirm(
        session_id=session_id,
        confirmed=req.confirmed,
        authorized_by=req.authorized_by,
    )
    return {"status": "ok", "session_id": session_id, "confirmed": req.confirmed}
