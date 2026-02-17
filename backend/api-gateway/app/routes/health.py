"""
Health Check Routes
"""

from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "service": "api-gateway"}

@router.get("/ready")
async def readiness_check():
    """就绪检查"""
    return {"status": "ready"}
