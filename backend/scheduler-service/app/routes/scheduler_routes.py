"""
Scheduler Routes - 调度API路由 (v2.0 多类型AI助手)
"""

from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, Dict, List

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from ..services.scheduler_service import SchedulerService
from ..services.k8s_client import K8sClient
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])

# 依赖注入
scheduler_service: Optional[SchedulerService] = None

def set_scheduler_service(service: SchedulerService):
    global scheduler_service
    scheduler_service = service

def get_service() -> SchedulerService:
    if not scheduler_service:
        raise HTTPException(status_code=500, detail="Scheduler Service not initialized")
    return scheduler_service

class PodAllocationRequest(BaseModel):
    case_id: str
    assistant_type: str = Field(default="openclaw", description="AI助手类型")

class PodReleaseRequest(BaseModel):
    case_id: str

class PodResponse(BaseModel):
    pod_name: str
    assistant_type: Optional[str] = None
    status: Optional[str] = None
    ip: Optional[str] = None
    endpoint: Optional[str] = None

class AssistantInfo(BaseModel):
    type: str
    name: str
    description: str
    enabled: bool
    pool_stats: dict = {}


@router.get("/assistants", response_model=List[AssistantInfo])
async def list_assistants(
    service: SchedulerService = Depends(get_service)
):
    """获取可用的AI助手列表"""
    return service.get_available_assistants()


@router.post("/pods/allocate", response_model=PodResponse)
async def allocate_pod(
    request: PodAllocationRequest,
    service: SchedulerService = Depends(get_service)
):
    """分配指定类型的Pod"""
    pod_name = await service.allocate_pod(
        case_id=request.case_id,
        assistant_type=request.assistant_type
    )
    if not pod_name:
        raise HTTPException(
            status_code=503,
            detail=f"No available pods for assistant type '{request.assistant_type}'"
        )
    info = service.get_allocation_info(request.case_id) or {}
    status = service.k8s.get_pod_status(pod_name)
    ip = service.k8s.get_pod_ip(pod_name)
    endpoint = service.get_endpoint_for_case(request.case_id)
    return {
        "pod_name": pod_name,
        "assistant_type": info.get("assistant_type", request.assistant_type),
        "status": status,
        "ip": ip,
        "endpoint": endpoint,
    }

@router.post("/pods/release")
async def release_pod(
    request: PodReleaseRequest,
    service: SchedulerService = Depends(get_service)
):
    """释放Pod"""
    success = await service.release_pod(request.case_id)
    if not success:
        raise HTTPException(status_code=404, detail="Pod or Case not found")
    return {"status": "released"}

@router.get("/pods/{case_id}", response_model=PodResponse)
async def get_pod_for_case(
    case_id: str,
    service: SchedulerService = Depends(get_service)
):
    """查询工单关联的Pod"""
    info = service.get_allocation_info(case_id)
    if not info:
        raise HTTPException(status_code=404, detail="No pod allocated for this case")
    
    pod_name = info["pod_name"]
    status = service.k8s.get_pod_status(pod_name)
    ip = service.k8s.get_pod_ip(pod_name)
    endpoint = service.get_endpoint_for_case(case_id)
    
    return {
        "pod_name": pod_name,
        "assistant_type": info["assistant_type"],
        "status": status,
        "ip": ip,
        "endpoint": endpoint,
    }

@router.get("/status")
async def get_status(
    service: SchedulerService = Depends(get_service)
):
    """获取调度器状态"""
    return service.get_status()

@router.get("/health")
async def health_check():
    """服务健康检查"""
    return {"status": "healthy"}
