"""
KB Service — 健康检查路由
"""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/health", tags=["health"])
_READINESS_DB_TIMEOUT_SECONDS = 2.0


@router.get("")
async def health(request: Request):
    """健康检查，返回服务状态"""
    return {
        "status": "ok",
        "service": "kb-service",
        "version": "3.0.0",
    }


# ── J-2：三级探针分级健康端点 ─────────────────────
@router.get("/live")
async def health_live():
    """Liveness 探针：只检查进程存活"""
    return {"status": "alive"}


@router.get("/startup")
async def health_startup(request: Request):
    """Startup 探针：服务启动完成后返回 200"""
    return {"status": "started"}


@router.get("/ready")
async def health_ready(request: Request):
    """Readiness 探针：数据库就绪时才加入流量"""
    checks: dict[str, str] = {}
    db_manager = getattr(request.app.state, "database_manager", None)
    if db_manager:
        try:
            from sqlalchemy import text

            async with asyncio.timeout(_READINESS_DB_TIMEOUT_SECONDS):
                async with db_manager.async_session_factory() as session:
                    await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "unavailable"
    else:
        checks["database"] = "unavailable"
    degraded = any(v != "ok" for v in checks.values())
    return JSONResponse(
        status_code=503 if degraded else 200,
        content={"status": "degraded" if degraded else "ready", "checks": checks},
    )
