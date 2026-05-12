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
from shared.utils.exceptions import AIStreamError, ErrorCode, ExternalServiceError
from shared.utils.logger import get_logger

from ..adapters.brain_router import BrainRouter
from ..repositories.conversation_repo import ConversationRepository
from ..services.ai_client import AIAssistantRegistry
from ..services.conversation_service import ConversationService
from ..services.environment_client import EnvironmentClient
from ..services.kb_client import KBClient
from ..services.scheduler_client import SchedulerClient
from .evaluate import require_admin_token

router = APIRouter(prefix="/api/conversations", tags=["conversations"])
logger = get_logger("conversation-routes")

# 全局依赖，需要在main.py中注入
database_manager: DatabaseManager | None = None
ai_registry: AIAssistantRegistry | None = None
scheduler_client: SchedulerClient | None = None
kb_client: KBClient | None = None
environment_client: EnvironmentClient | None = None
brain_router: BrainRouter | None = None  # T1-6: 大脑路由器


def set_dependencies(
    db: DatabaseManager,
    registry: AIAssistantRegistry,
    scheduler: SchedulerClient | None = None,
    kb: KBClient | None = None,
    env_client: EnvironmentClient | None = None,
    router: BrainRouter | None = None,  # T1-6: 大脑路由器（可选）
):
    global database_manager, ai_registry, scheduler_client, kb_client, environment_client, brain_router
    database_manager = db
    ai_registry = registry
    scheduler_client = scheduler
    kb_client = kb
    environment_client = env_client
    brain_router = router


async def get_conversation_service() -> ConversationService:
    """依赖注入: 获取Conversation Service"""
    if not database_manager or not ai_registry:
        raise HTTPException(status_code=500, detail="Service dependencies not initialized")

    async for session in database_manager.get_session():
        repo = ConversationRepository(session)
        yield ConversationService(
            repo, ai_registry, scheduler_client, kb_client, environment_client,
            database_manager.async_session_factory,
            brain_router=brain_router,  # T1-6: 注入大脑路由器
        )

@router.post("/", status_code=201)
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
    return {"conversation_id": conversation.conversation_id, "case_id": conversation.case_id}

@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID,
    service: ConversationService = Depends(get_conversation_service)
):
    """获取对话详情"""
    conversation = await service.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation

@router.get("/case/{case_id}")
async def get_conversations_by_case(
    case_id: str,
    service: ConversationService = Depends(get_conversation_service)
):
    """获取工单的所有对话"""
    return await service.repository.get_conversations_by_case(case_id)

@router.get("/{conversation_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    conversation_id: uuid.UUID,
    service: ConversationService = Depends(get_conversation_service)
):
    """获取对话消息历史"""
    messages = await service.get_messages(conversation_id)
    return [MessageResponse.model_validate(msg) for msg in messages]

@router.post("/{conversation_id}/message")
async def send_message(
    conversation_id: uuid.UUID,
    message: MessageCreate,
    background_tasks: BackgroundTasks,
    service: ConversationService = Depends(get_conversation_service)
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
                conversation_id=conversation_id,
                case_id=message.case_id,
                content=message.content,
                assistant_type=message.assistant_type  # v2.2: 支持动态切换助手
            ):
                if chunk:
                    # 检测特殊内部事件标记（\x00event:<type>:<data>\x00）
                    if chunk.startswith("\x00event:") and chunk.endswith("\x00"):
                        # 解析内部事件并转换为 SSE 事件格式
                        inner = chunk[7:-1]  # 去掉前缀 \x00event: 和末尾 \x00
                        parts = inner.split(":", 1)
                        evt_type = parts[0]
                        evt_data = parts[1] if len(parts) > 1 else ""
                        if evt_type == "interactive_request":
                            # interactive_request 数据本身已是完整 JSON，直接透传，无需包装
                            yield f"event: {evt_type}\ndata: {evt_data}\n\n"
                        else:
                            event_payload = json.dumps({"to": evt_data}, ensure_ascii=False)
                            yield f"event: {evt_type}\ndata: {event_payload}\n\n"
                        continue
                    ai_content.append(chunk)
                    # JSON encode chunk 以安全保留换行符，避免 SSE 多行截断
                    encoded_chunk = json.dumps({"content": chunk}, ensure_ascii=False)
                    yield f"data: {encoded_chunk}\n\n"

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
        except ExternalServiceError as e:
            # 外部服务故障（KB/Scheduler 等），回传完整错误信息给前端
            if ai_content:
                background_tasks.add_task(
                    service.save_assistant_message,
                    conversation_id=conversation_id,
                    case_id=message.case_id,
                    content="".join(ai_content),
                )
            error_data = json.dumps(
                {"code": e.code.value, "message": e.message, "detail": e.detail},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error_data}\n\n"
        except Exception as e:
            # 其他未知错误，透传真实错误信息而非笼统的"内部错误"
            logger.error(
                event="sse_stream_error",
                message="SSE 流发生未捕获异常",
                error_type=type(e).__name__,
                error_message=str(e),
                conversation_id=str(conversation_id),
            )
            if ai_content:
                background_tasks.add_task(
                    service.save_assistant_message,
                    conversation_id=conversation_id,
                    case_id=message.case_id,
                    content="".join(ai_content),
                )
            # 透传真实错误信息，让用户了解问题根因
            error_message = str(e)[:200] if str(e) else f"{type(e).__name__}（无详细信息）"
            error_data = json.dumps(
                {
                    "code": ErrorCode.INTERNAL_ERROR.value,
                    "message": f"服务异常：{error_message}",
                    "detail": type(e).__name__,
                },
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error_data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream", background=background_tasks)


# ─────────────────────────────────────────────────────────────────────────────
# Admin 接口：修正工单关联的根因 KBD 条目
# ─────────────────────────────────────────────────────────────────────────────

class ResolvedKbdUpdateRequest(BaseModel):
    """管理员修正根因 KBD 关联请求"""

    kbd_entry_id: int | None = None


@router.patch("/admin/cases/{case_id}/resolved_kbd")
async def update_resolved_kbd(
    case_id: str,
    body: ResolvedKbdUpdateRequest,
    _: None = Depends(require_admin_token),
    service: ConversationService = Depends(get_conversation_service),
):
    """
    [Admin] 修正工单关联的根因 KBD 条目，并自动调整 hit_count

    - 若旧值非 null → 旧 KBD hit_count -1
    - 若新值非 null → 新 KBD hit_count +1
    """
    from sqlalchemy import func
    from sqlalchemy import select as sa_select
    from sqlalchemy import update as sa_update

    from ..models.conversation import Conversation as ConversationModel

    # 查找该 case 最新 conversation（按 started_at DESC 排序）
    conversations = await service.repository.get_conversations_by_case(case_id)
    if not conversations:
        raise HTTPException(status_code=404, detail="未找到该工单的对话记录")

    conv = conversations[0]
    old_kbd_id = conv.resolved_kbd_entry_id
    new_kbd_id = body.kbd_entry_id
    changed = old_kbd_id != new_kbd_id

    # case 级去重：统计 case 内其他 conversation 对 old/new KBD 的引用数（写入前统计，保证准确）
    old_ref_count = 0
    new_ref_count = 0
    if changed and service.session_factory:
        async with service.session_factory() as count_session:
            if old_kbd_id is not None:
                r = await count_session.execute(
                    sa_select(func.count())
                    .select_from(ConversationModel)
                    .where(
                        ConversationModel.case_id == case_id,
                        ConversationModel.resolved_kbd_entry_id == old_kbd_id,
                        ConversationModel.conversation_id != conv.conversation_id,
                    )
                )
                old_ref_count = r.scalar() or 0
            if new_kbd_id is not None:
                r = await count_session.execute(
                    sa_select(func.count())
                    .select_from(ConversationModel)
                    .where(
                        ConversationModel.case_id == case_id,
                        ConversationModel.resolved_kbd_entry_id == new_kbd_id,
                        ConversationModel.conversation_id != conv.conversation_id,
                    )
                )
                new_ref_count = r.scalar() or 0

    # 字段写入（不管是否变化，确保数据一致）
    await service.repository.session.execute(
        sa_update(ConversationModel)
        .where(ConversationModel.conversation_id == conv.conversation_id)
        .values(resolved_kbd_entry_id=new_kbd_id)
    )

    # 调整 hit_count（case 级去重：仅当该 case 内无其他 conversation 引用时才调整）
    if changed and kb_client:
        if old_kbd_id is not None and old_ref_count == 0:
            # 无其他 conversation 引用旧值，安全 -1
            await kb_client.decrement_kbd_hit(old_kbd_id)
        if new_kbd_id is not None and new_ref_count == 0:
            # 无其他 conversation 已引用新值，安全 +1
            await kb_client.increment_kbd_hit(new_kbd_id)

    # 查询新 KBD 信息用于前端展示
    kbd_info = None
    if new_kbd_id is not None and kb_client:
        kbd_info = await kb_client.get_kbd_info(new_kbd_id)

    logger.info(
        event="admin_resolved_kbd_updated",
        case_id=case_id,
        old_kbd_entry_id=old_kbd_id,
        new_kbd_entry_id=new_kbd_id,
        changed=changed,
    )

    return {
        "success": True,
        "case_id": case_id,
        "old_kbd_entry_id": old_kbd_id,
        "new_kbd_entry_id": new_kbd_id,
        "changed": changed,
        "kbd_info": kbd_info,
    }


# ── T-E6: ops-agent 交互响应回传 ─────────────────────────────────────────────


class InteractiveResponseBody(BaseModel):
    """POST /api/conversations/{id}/interactive-response 请求体。"""
    request_id: str               # 来自前端收到的 BrainInteractiveRequest.requestId
    acp_session_id: str           # 来自前端收到的 BrainInteractiveRequest.acpSessionId
    outcome: dict                 # {"outcome": "selected", "optionId": "A"}
                                  # 或 {"outcome": "free_text", "text": "..."}


@router.post("/{conversation_id}/interactive-response")
async def submit_interactive_response(
    conversation_id: uuid.UUID,
    body: InteractiveResponseBody,
    service: ConversationService = Depends(get_conversation_service),
):
    """将用户对 ops-agent 交互卡片（SOP操作卡 / 信息确认卡）的响应回传给 ACP 会话。

    由前端 InteractiveRequestCard 提交按钮触发。

    响应 200：{"ok": true}
    响应 503：ops-agent 适配器不可用（ops-agent 未启用或 BrainRouter 未注入）
    """
    success = await service.submit_interactive_response(
        conversation_id=conversation_id,
        request_id=body.request_id,
        acp_session_id=body.acp_session_id,
        outcome=body.outcome,
    )
    if not success:
        raise HTTPException(
            status_code=503,
            detail="OpsAgentBrainAdapter 不可用：ops-agent 未启用或 ACP 接口不可达",
        )
    return {"ok": True}


# ── 恢复 ops-agent 事件流（不提交新 prompt）──────────────────────────────────

@router.get("/{conversation_id}/resume-stream")
async def resume_ops_agent_stream(
    conversation_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    service: ConversationService = Depends(get_conversation_service),
):
    """恢复消费 ops-agent 续写事件流（不提交新 prompt，用于页面刷新后重接 SSE）。

    典型场景：
    - 用户提交了 interactive_response（选项或自由文本），ops-agent 开始续写
    - 用户刷新页面，原 SSE 连接断开
    - 前端提交 interactive_response 后调用本接口，重新接收续写内容

    若 ops-agent session 不存在或 active_prompt=False，立即返回 [DONE]。

    响应格式与 POST /{id}/message 基本一致（data: {content} / event: interactive_request / [DONE]），
    但异常时不产出 event: error 帧，而是记录日志后直接返回 [DONE]。
    """
    ai_content: list[str] = []

    async def event_generator():
        try:
            async for chunk in service.resume_ops_agent_stream(conversation_id):
                if chunk:
                    if chunk.startswith("\x00event:") and chunk.endswith("\x00"):
                        inner = chunk[7:-1]
                        parts = inner.split(":", 1)
                        evt_type = parts[0]
                        evt_data = parts[1] if len(parts) > 1 else ""
                        if evt_type == "interactive_request":
                            yield f"event: {evt_type}\ndata: {evt_data}\n\n"
                        else:
                            event_payload = json.dumps({"to": evt_data}, ensure_ascii=False)
                            yield f"event: {evt_type}\ndata: {event_payload}\n\n"
                        continue
                    ai_content.append(chunk)
                    encoded_chunk = json.dumps({"content": chunk}, ensure_ascii=False)
                    yield f"data: {encoded_chunk}\n\n"
            # 续写内容落库（fire-and-forget）
            if ai_content:
                background_tasks.add_task(
                    service.save_assistant_message_for_resume,
                    conversation_id=conversation_id,
                    content="".join(ai_content),
                )
        except asyncio.CancelledError:
            if ai_content:
                background_tasks.add_task(
                    service.save_assistant_message_for_resume,
                    conversation_id=conversation_id,
                    content="".join(ai_content),
                )
            raise
        except Exception as e:
            logger.error(
                event="resume_stream_error",
                message="resume_ops_agent_stream 异常",
                error_type=type(e).__name__,
                error_message=str(e),
                conversation_id=str(conversation_id),
            )
        else:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=background_tasks,
    )
