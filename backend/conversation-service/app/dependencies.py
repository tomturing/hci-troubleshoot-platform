"""
I-2: FastAPI 依赖注入标准化
将全局变量依赖注入改为 FastAPI Depends + app.state 模式，
实现类型安全的依赖传递，便于单测 mock 和依赖图分析。
"""

from typing import AsyncGenerator

from fastapi import Depends, HTTPException, Request

from shared.database.postgres import DatabaseManager
from ..repositories.conversation_repo import ConversationRepository
from ..services.ai_client import AIAssistantRegistry
from ..services.conversation_service import ConversationService
from ..services.kb_client import KBClient
from ..services.scheduler_client import SchedulerClient


def get_database_manager(request: Request) -> DatabaseManager:
    """从 app.state 获取 DatabaseManager（类型安全的依赖注入）"""
    db: DatabaseManager | None = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="数据库未初始化，服务尚未就绪")
    return db


def get_ai_registry(request: Request) -> AIAssistantRegistry:
    """从 app.state 获取 AI AssistantRegistry"""
    registry: AIAssistantRegistry | None = getattr(request.app.state, "ai_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="AI 注册表未初始化，服务尚未就绪")
    return registry


def get_kb_client(request: Request) -> KBClient | None:
    """从 app.state 获取 KBClient（可选依赖，KB 未配置时返回 None）"""
    return getattr(request.app.state, "kb_client", None)


def get_scheduler_client(request: Request) -> SchedulerClient | None:
    """从 app.state 获取 SchedulerClient（可选依赖，Scheduler 未配置时返回 None）"""
    return getattr(request.app.state, "scheduler_client", None)


async def get_conversation_service(
    db: DatabaseManager = Depends(get_database_manager),
    registry: AIAssistantRegistry = Depends(get_ai_registry),
    kb: KBClient | None = Depends(get_kb_client),
    scheduler: SchedulerClient | None = Depends(get_scheduler_client),
) -> AsyncGenerator[ConversationService, None]:
    """
    标准化的 ConversationService 依赖注入（I-2 改进）。

    通过 Depends 链式声明依赖关系，FastAPI 自动管理依赖生命周期：
    - 每个请求创建独立的 AsyncSession（线程安全）
    - yield 后 finally 块确保 session 正确关闭
    - 依赖图可被 FastAPI 工具链可视化和测试
    """
    async for session in db.get_session():
        repo = ConversationRepository(session)
        yield ConversationService(
            repo,
            registry,
            scheduler,
            kb,
            db.async_session_factory,
        )
