"""
Scheduler Routes - 调度API路由
"""

from fastapi import APIRouter, Depends, HTTPException, Header
from typing import Optional, Dict

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from ..services.scheduler_service import SchedulerService
from ..services.k8s_client import K8sClient
from pydantic import BaseModel

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

class PodReleaseRequest(BaseModel):
    case_id: str

class PodResponse(BaseModel):
    pod_name: str
    status: Optional[str] = None
    ip: Optional[str] = None

@router.post("/pods/allocate", response_model=PodResponse)
async def allocate_pod(
    request: PodAllocationRequest,
    x_trace_id: Optional[str] = Header(None),
    service: SchedulerService = Depends(get_service)
):
    """分配Pod"""
    pod_name = await service.allocate_pod(request.case_id, x_trace_id)
    if not pod_name:
        raise HTTPException(status_code=503, detail="No available pods or allocation failed")
    return {"pod_name": pod_name}

@router.post("/pods/release")
async def release_pod(
    request: PodReleaseRequest,
    x_trace_id: Optional[str] = Header(None),
    service: SchedulerService = Depends(get_service)
):
    """释放Pod"""
    success = await service.release_pod(request.case_id, x_trace_id)
    if not success:
        raise HTTPException(status_code=404, detail="Pod or Case not found")
    return {"status": "released"}

@router.get("/pods/{case_id}", response_model=PodResponse)
async def get_pod_for_case(
    case_id: str,
    service: SchedulerService = Depends(get_service)
):
    """查询工单关联的Pod"""
    pod_name = await service.get_pod_for_case(case_id)
    if not pod_name:
        raise HTTPException(status_code=404, detail="No pod allocated for this case")
        
    status = service.k8s.get_pod_status(pod_name)
    ip = service.k8s.get_pod_ip(pod_name)
    
    return {
        "pod_name": pod_name,
        "status": status,
        "ip": ip
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
