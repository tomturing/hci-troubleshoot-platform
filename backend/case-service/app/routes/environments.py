"""
Environment Routes - Case Service API

提供环境数据的 CRUD 操作，以及 S0 Prompt 构建所需的上下文接口。
"""

from fastapi import APIRouter, Depends, HTTPException
from shared.database.postgres import DatabaseManager
from shared.models.schemas import (
    EnvironmentContextResponse,
    EnvironmentCreate,
    EnvironmentListResponse,
    EnvironmentResponse,
    EnvironmentUpsert,
    EnvType,
)

from ..repositories.environment_repo import EnvironmentRepository
from ..services.environment_service import EnvironmentService

router = APIRouter(prefix="/api/environments", tags=["environments"])

database_manager: DatabaseManager | None = None


def set_database_manager(db_manager: DatabaseManager):
    """设置数据库管理器（由 main.py 调用）"""
    global database_manager
    database_manager = db_manager


async def get_environment_service() -> EnvironmentService:
    """依赖注入: 获取 Environment Service"""
    if not database_manager:
        raise HTTPException(status_code=500, detail="Database not initialized")

    async for session in database_manager.get_session():
        repo = EnvironmentRepository(session)
        yield EnvironmentService(repo)


@router.post("/", response_model=EnvironmentResponse, status_code=201)
async def create_environment(
    env_create: EnvironmentCreate,
    service: EnvironmentService = Depends(get_environment_service),
):
    """创建环境数据（alert/task/environment 采集）"""
    return await service.create_environment(env_create)


@router.put("/case/{case_id}/type/{env_type}", response_model=EnvironmentResponse)
async def upsert_environment(
    case_id: str,
    env_type: str,
    env_upsert: EnvironmentUpsert,  # 使用专用 schema（不包含 case_id/env_type）
    service: EnvironmentService = Depends(get_environment_service),
):
    """upsert 环境数据（幂等：有则更新，无则创建）—— REST 标准幂等 PUT

    case_id 和 env_type 由 path 参数指定，body 仅包含 env_data 和可选的 collected_at。
    """
    try:
        env_type_enum = EnvType(env_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid env_type: {env_type}")

    return await service.upsert_environment(
        case_id=case_id,
        env_type=env_type_enum,
        env_data=env_upsert.env_data,
        collected_at=env_upsert.collected_at,
    )


@router.get("/case/{case_id}", response_model=EnvironmentListResponse)
async def get_environments_by_case(
    case_id: str,
    service: EnvironmentService = Depends(get_environment_service),
):
    """获取工单所有环境数据"""
    return await service.get_environments_by_case(case_id)


@router.get("/case/{case_id}/type/{env_type}", response_model=EnvironmentResponse | None)
async def get_environment_by_type(
    case_id: str,
    env_type: str,
    service: EnvironmentService = Depends(get_environment_service),
):
    """获取工单指定类型环境数据"""
    try:
        env_type_enum = EnvType(env_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid env_type: {env_type}")

    result = await service.get_environment_by_type(case_id, env_type_enum)
    if not result:
        raise HTTPException(status_code=404, detail="Environment data not found")
    return result


@router.get("/case/{case_id}/context", response_model=EnvironmentContextResponse)
async def get_environment_context(
    case_id: str,
    service: EnvironmentService = Depends(get_environment_service),
):
    """获取 S0 阶段 Prompt 构建所需的环境上下文"""
    return await service.build_context_info(case_id)
