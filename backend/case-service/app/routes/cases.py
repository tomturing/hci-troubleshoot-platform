"""
Case Routes - API路由
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from shared.database.postgres import DatabaseManager
from shared.models.schemas import (
    CaseCreate,
    CaseListResponse,
    CaseResponse,
    CaseStatsResponse,
    ClientListResponse,
)

from ..repositories.case_repo import CaseRepository
from ..services.case_service import CaseService

router = APIRouter(prefix="/api/cases", tags=["cases"])

# 这里需要在main.py中注入database_manager
database_manager: DatabaseManager | None = None

def set_database_manager(db_manager: DatabaseManager):
    global database_manager
    database_manager = db_manager

async def get_case_service() -> CaseService:
    """依赖注入: 获取Case Service"""
    if not database_manager:
        raise HTTPException(status_code=500, detail="Database not initialized")

    async for session in database_manager.get_session():
        repo = CaseRepository(session)
        yield CaseService(repo)

# ============ Admin 路由（静态路径，优先于 {case_id}）============

@router.get("/all", response_model=CaseListResponse)
async def list_all_cases(
    skip: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    status: str | None = Query(None, description="按状态筛选"),
    client_id: str | None = Query(None, description="按客户端筛选"),
    start_time: datetime | None = Query(None, description="开始时间"),
    end_time: datetime | None = Query(None, description="结束时间"),
    service: CaseService = Depends(get_case_service),
):
    """[Admin] 获取所有工单列表（分页 + 筛选）"""
    return await service.list_all_cases(
        skip=skip, limit=limit,
        status=status, client_id=client_id,
        start_time=start_time, end_time=end_time,
    )

@router.get("/stats", response_model=CaseStatsResponse)
async def get_case_stats(
    service: CaseService = Depends(get_case_service),
):
    """[Admin] 获取工单统计"""
    return await service.get_case_stats()

@router.get("/clients", response_model=ClientListResponse)
async def get_client_list(
    service: CaseService = Depends(get_case_service),
):
    """[Admin] 获取客户端列表"""
    return await service.get_client_list()

# ============ 客户端路由 ============

@router.post("/", response_model=CaseResponse, status_code=201)
async def create_case(
    case_create: CaseCreate,
    service: CaseService = Depends(get_case_service)
):
    """创建新工单"""
    return await service.create_case(case_create)

@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(
    case_id: str,
    service: CaseService = Depends(get_case_service)
):
    """获取工单详情"""
    case = await service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case

@router.get("/", response_model=list[CaseResponse])
async def list_cases(
    client_id: str,
    service: CaseService = Depends(get_case_service)
):
    """查询工单列表"""
    return await service.list_cases(client_id)

@router.put("/{case_id}/confirm", response_model=CaseResponse)
async def confirm_case(
    case_id: str,
    service: CaseService = Depends(get_case_service)
):
    """确认工单"""
    case = await service.confirm_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case

@router.put("/{case_id}/close", response_model=CaseResponse)
async def close_case(
    case_id: str,
    service: CaseService = Depends(get_case_service)
):
    """关闭工单"""
    case = await service.close_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case
