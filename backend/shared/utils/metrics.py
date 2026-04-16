"""
共享 Prometheus 指标定义

所有服务通过导入此模块获取统一的指标对象，避免重复注册。
HTTP 层指标（http_request_duration_seconds / http_requests_total）依赖
Prometheus scrape job 区分来源；业务指标（hci_* 前缀）通过 labelnames 区分。
"""

import contextlib
import time

from fastapi import Request
from prometheus_client import Counter, Gauge, Histogram
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# ──────────────────────────────────────────────
#  HTTP 层指标 (标准 SRE 黄金信号)
# ──────────────────────────────────────────────

# HTTP 请求延迟直方图（Prometheus 标准名称，供 HighApiLatency 告警使用）
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求处理耗时（秒）",
    labelnames=["method", "route"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, float("inf")],
)

# HTTP 请求总数（status 为 HTTP 状态码字符串，如 "200"、"500"）
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "HTTP 请求总数",
    labelnames=["method", "status"],
)


class HTTPMetricsMiddleware(BaseHTTPMiddleware):
    """HTTP 请求指标采集中间件

    为所有服务提供统一的 HTTP 延迟和请求计数指标，
    支持 Prometheus 告警规则 HighApiLatency 和 HighErrorRate。
    路由模板（如 /api/cases/{case_id}）优先于实际路径，避免高基数。
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.monotonic()
        status_code = "500"
        try:
            response = await call_next(request)
            status_code = str(response.status_code)
        finally:
            duration = time.monotonic() - start

            # 优先使用 FastAPI 路由模板，避免动态路径导致高基数；
            # 无匹配路由时（404/探测路径）统一归类为 "unmatched"
            route = "unmatched"
            with contextlib.suppress(KeyError, AttributeError):
                route = request.scope["route"].path

            HTTP_REQUEST_DURATION_SECONDS.labels(
                method=request.method,
                route=route,
            ).observe(duration)
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method,
                status=status_code,
            ).inc()

        return response

# ──────────────────────────────────────────────
#  AI 层指标 (O-1)
# ──────────────────────────────────────────────

# 首 Token 延迟直方图 (TTFT)
AI_TTFT_SECONDS = Histogram(
    "hci_ai_ttft_seconds",
    "AI 助手首 Token 延迟（秒）",
    labelnames=["assistant_type"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, float("inf")],
)

# AI 请求计数器（区分成功 / 错误）
AI_REQUESTS_TOTAL = Counter(
    "hci_ai_requests_total",
    "AI 请求总次数",
    labelnames=["assistant_type", "status"],  # status: success | error
)

# KB 检索耗时直方图
KB_SEARCH_DURATION_SECONDS = Histogram(
    "hci_kb_search_seconds",
    "知识库检索耗时（秒）",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 3.0, float("inf")],
)

# ──────────────────────────────────────────────
#  Pod 池指标 (O-2)
# ──────────────────────────────────────────────

# Pod 池空闲数
POD_POOL_IDLE = Gauge(
    "hci_pod_pool_idle",
    "Pod 池空闲数量",
    labelnames=["assistant_type"],
)

# Pod 池活跃数
POD_POOL_ACTIVE = Gauge(
    "hci_pod_pool_active",
    "Pod 池活跃数量",
    labelnames=["assistant_type"],
)
