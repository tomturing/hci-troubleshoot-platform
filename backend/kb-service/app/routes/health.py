"""
KB Service — 健康检查路由
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health(request: Request):
    """健康检查，返回服务状态和 SOP 索引大小"""
    sop_matcher = getattr(request.app.state, "sop_matcher", None)
    return {
        "status": "ok",
        "service": "kb-service",
        "version": "3.0.0",
        "sop_index_size": sop_matcher.index_size if sop_matcher else 0,
    }


# ── J-2：三级探针分级健康端点 ─────────────────────
@router.get("/live")
async def health_live():
    """Liveness 探针：只检查进程存活"""
    return {"status": "alive"}


@router.get("/startup")
async def health_startup(request: Request):
    """Startup 探针：SOP 索引初始化完成后返回 200"""
    sop_matcher = getattr(request.app.state, "sop_matcher", None)
    if sop_matcher is None:
        raise HTTPException(status_code=503, detail="SOP 索引仍在初始化")
    return {"status": "started"}


@router.get("/ready")
async def health_ready(request: Request):
    """Readiness 探针：数据库 + SOP 索引就绪时才加入流量"""
    checks: dict[str, str] = {}
    sop_matcher = getattr(request.app.state, "sop_matcher", None)
    checks["sop_index"] = "ok" if sop_matcher else "unavailable"
    db = getattr(request.app.state, "db", None)
    if db:
        try:
            from sqlalchemy import text
            async with db() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "unavailable"
    degraded = any(v != "ok" for v in checks.values())
    return JSONResponse(
        status_code=503 if degraded else 200,
        content={"status": "degraded" if degraded else "ready", "checks": checks},
    )
