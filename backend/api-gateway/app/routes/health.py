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
    """就绪检查（旧路径保留兼容性）"""
    return {"status": "ready"}


# ── J-2：三级探针分级健康端点 ──────────────────────────────────────
@router.get("/health/live")
async def health_live():
    """Liveness 探针：只检查进程存活"""
    return {"status": "alive"}


@router.get("/health/startup")
async def health_startup():
    """Startup 探针：初始化完成后返回 200"""
    return {"status": "started"}


@router.get("/health/ready")
async def health_ready():
    """Readiness 探针：进程就绪（api-gateway 无状态，直接返回 200）"""
    return {"status": "ready", "checks": {}}
