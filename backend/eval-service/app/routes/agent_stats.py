"""
Agent Stats 路由

GET /api/stats/agents - 三大脑实时指标对比
"""

from fastapi import APIRouter

from app.services.agent_stats_service import fetch_agent_stats

router = APIRouter(prefix="/api/stats", tags=["agent-stats"])


@router.get("/agents")
async def get_agent_stats() -> dict:
    """
    返回三个 Agent 大脑的实时对比指标（来自 Prometheus）：
    - request_total: 总请求数
    - success_rate: 5 分钟成功率
    - p50_latency_ms: P50 延迟（ms）
    - p95_latency_ms: P95 延迟（ms）

    按 agent_type（htp / ops / pai）分组返回。
    """
    data = await fetch_agent_stats()
    return {"agents": data}
