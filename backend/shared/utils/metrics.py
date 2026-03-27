"""
共享 Prometheus 指标定义

所有服务通过导入此模块获取统一的指标对象，避免重复注册。
每个指标均带有 service 标签以便在多服务单 Prometheus 实例下区分来源。
"""

from prometheus_client import Counter, Gauge, Histogram

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
