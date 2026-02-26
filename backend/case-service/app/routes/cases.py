"""
Case Routes - API路由
"""

from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from shared.models.schemas import CaseCreate, CaseResponse
from shared.database.postgres import DatabaseManager
from ..services.case_service import CaseService
from ..repositories.case_repo import CaseRepository

router = APIRouter(prefix="/api/cases", tags=["cases"])

# 这里需要在main.py中注入database_manager
database_manager: Optional[DatabaseManager] = None

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

@router.get("/", response_model=List[CaseResponse])
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
