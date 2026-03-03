"""
Conversation Routes - 对话API路由 (v2.0 多类型AI助手)
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from shared.database.postgres import DatabaseManager
from shared.models.schemas import MessageCreate, MessageResponse

from ..repositories.conversation_repo import ConversationRepository
from ..services.ai_client import AIAssistantRegistry
from ..services.conversation_service import ConversationService
from ..services.scheduler_client import SchedulerClient

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

# 全局依赖，需要在main.py中注入
database_manager: DatabaseManager | None = None
ai_registry: AIAssistantRegistry | None = None
scheduler_client: SchedulerClient | None = None


def set_dependencies(db: DatabaseManager, registry: AIAssistantRegistry, scheduler: SchedulerClient | None = None):
    global database_manager, ai_registry, scheduler_client
    database_manager = db
    ai_registry = registry
    scheduler_client = scheduler


async def get_conversation_service() -> ConversationService:
    """依赖注入: 获取Conversation Service"""
    if not database_manager or not ai_registry:
        raise HTTPException(status_code=500, detail="Service dependencies not initialized")

    async for session in database_manager.get_session():
        repo = ConversationRepository(session)
        yield ConversationService(repo, ai_registry, scheduler_client)


@router.post("/", status_code=201)
async def create_conversation(
    case_id: str,
    assistant_type: str = "openclaw",
    initial_message: str | None = None,
    service: ConversationService = Depends(get_conversation_service),
):
    """创建新对话"""
    conversation = await service.create_conversation(
        case_id=case_id, assistant_type=assistant_type, initial_message=initial_message
    )
    return {"conversation_id": conversation.conversation_id, "case_id": conversation.case_id}


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID, service: ConversationService = Depends(get_conversation_service)
):
    """获取对话详情"""
    conversation = await service.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
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
            async for chunk in service.send_message_stream_only(
                conversation_id=conversation_id, case_id=message.case_id, content=message.content
            ):
                if chunk:
                    ai_content.append(chunk)
                    yield f"data: {chunk}\n\n"

            # 正常流结束后，提交后台任务保存消息
            background_tasks.add_task(
                service.save_assistant_message,
                conversation_id=conversation_id,
                case_id=message.case_id,
                content="".join(ai_content),
            )

            yield "data: [DONE]\n\n"

        except Exception as e:
            # 取消请求或发生错误时，依然保存生成到一半的内容
            if ai_content:
                background_tasks.add_task(
                    service.save_assistant_message,
                    conversation_id=conversation_id,
                    case_id=message.case_id,
                    content="".join(ai_content),
                )
            yield f"event: error\ndata: {str(e)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream", background=background_tasks)
