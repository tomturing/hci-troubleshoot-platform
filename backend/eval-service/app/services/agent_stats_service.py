"""
Agent Stats Service - 三 Agent 实时指标对比

从 Prometheus 查询 agent-service 的推理统计数据：
- HTP 大脑（S 阶段）
- OpsAgent（B 大脑）
- PaiAgent（C 大脑）
"""

import httpx
from shared.observability.logger import get_logger

from app.config import settings

logger = get_logger("agent_stats_service")

# ── Prometheus 指标名称映射 ───────────────────────────────────────────────────

_METRICS_QUERY = {
    "request_total": 'sum by (agent_type) (agent_request_total{{job="agent-service"}})',
    "success_rate": 'sum by (agent_type) (rate(agent_request_total{{job="agent-service",status="success"}}[5m])) / sum by (agent_type) (rate(agent_request_total{{job="agent-service"}}[5m]))',
    "p50_latency_ms": 'histogram_quantile(0.50, sum by (agent_type, le) (rate(agent_response_duration_seconds_bucket{{job="agent-service"}}[5m]))) * 1000',
    "p95_latency_ms": 'histogram_quantile(0.95, sum by (agent_type, le) (rate(agent_response_duration_seconds_bucket{{job="agent-service"}}[5m]))) * 1000',
}


async def fetch_agent_stats() -> dict:
    """
    从 Prometheus 拉取三个大脑的实时对比指标。

    Returns:
        dict: 按 agent_type（htp / ops / pai）分组的统计数据
    """
    result: dict = {}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for metric_name, query in _METRICS_QUERY.items():
                resp = await client.get(
                    f"{settings.PROMETHEUS_URL}/api/v1/query",
                    params={"query": query},
                )
                resp.raise_for_status()
                data = resp.json()

                for vector in data.get("data", {}).get("result", []):
                    agent_type = vector["metric"].get("agent_type", "unknown")
                    value = float(vector["value"][1])
                    if agent_type not in result:
                        result[agent_type] = {}
                    result[agent_type][metric_name] = round(value, 3)

    except Exception as exc:
        logger.error(
            event="prometheus_query_failed",
            message=f"Prometheus 查询失败: {exc}",
        )
        # 降级返回空数据而非报错
        return {}

    return result
