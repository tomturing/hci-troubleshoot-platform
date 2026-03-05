"""
KB Service — 健康检查路由
"""

from fastapi import APIRouter, Request

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
