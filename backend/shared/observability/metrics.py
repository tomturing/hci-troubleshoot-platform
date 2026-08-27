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

KBD_SIGNAL_VALIDATION_TOTAL = Counter(
    "hci_kbd_signal_validation_total",
    "KBD 关键信号校验失败次数",
    labelnames=["code", "operation"],
)

KBD_WORKFLOW_ITEMS_TOTAL = Counter(
    "hci_kbd_workflow_items_total",
    "KBD 工作流逐项执行结果",
    labelnames=["workflow", "stage", "status", "error_code"],
)

KBD_WORKFLOW_DURATION_SECONDS = Histogram(
    "hci_kbd_workflow_duration_seconds",
    "KBD 工作流阶段耗时（秒）",
    labelnames=["workflow", "stage"],
    buckets=[0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 900.0, float("inf")],
)

QFK_EXECUTIONS_TOTAL = Counter(
    "hci_qfk_executions_total",
    "QFK 关键信号执行终态计数",
    labelnames=["namespace", "mode", "status", "error_code"],
)

QFK_EXECUTION_DURATION_SECONDS = Histogram(
    "hci_qfk_execution_duration_seconds",
    "QFK 关键信号端到端执行耗时（秒）",
    labelnames=["namespace", "mode", "status"],
    buckets=[0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, float("inf")],
)

QFK_AI_PROCESSINGS_TOTAL = Counter(
    "hci_qfk_ai_processings_total",
    "QFK AI 处理终态计数",
    labelnames=["mode", "status", "error_code"],
)

QFK_AI_PROCESSING_DURATION_SECONDS = Histogram(
    "hci_qfk_ai_processing_duration_seconds",
    "QFK AI 处理耗时（秒）",
    labelnames=["mode", "status"],
    buckets=[0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0, 60.0, float("inf")],
)

KBD_LLM_REQUESTS_TOTAL = Counter(
    "hci_kbd_llm_requests_total",
    "KBD LLM 请求次数",
    labelnames=["operation", "model", "status", "finish_reason"],
)

KBD_LLM_TOKENS_TOTAL = Counter(
    "hci_kbd_llm_tokens_total",
    "KBD LLM Token 用量",
    labelnames=["operation", "model", "type"],
)

OFFLINE_RESOURCE_SYNC_TOTAL = Counter(
    "hci_offline_resource_sync_total",
    "离线资源同步检测次数",
    labelnames=["mode", "status"],
)

OFFLINE_RESOURCE_SYNC_CHANGES_TOTAL = Counter(
    "hci_offline_resource_sync_changes_total",
    "离线资源同步差异数量",
    labelnames=["mode", "resource_type", "change_type"],
)

OFFLINE_RESOURCE_SYNC_DURATION_SECONDS = Histogram(
    "hci_offline_resource_sync_duration_seconds",
    "离线资源同步阶段耗时（秒）",
    labelnames=["mode", "phase"],
    buckets=[0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0, 60.0, 300.0, float("inf")],
)

# Catalog 基线在线保存成功次数（按 filename / status 区分）
CATALOG_SAVE_TOTAL = Counter(
    "hci_catalog_save_total",
    "Resolution Catalog 基线在线保存次数",
    labelnames=["filename", "status"],  # status: success | error
)

# Catalog 基线写磁盘失败次数（持久化/权限/IO 异常）
CATALOG_SAVE_ERRORS = Counter(
    "hci_catalog_save_errors_total",
    "Resolution Catalog 基线写回失败次数（PV 未挂载或 IO 异常时上升）",
    labelnames=["filename", "error_type"],
)

# 契约门禁：结构兼容性演进（旧契约结构指纹不等但旧信号仍可通过当前 schema 校验）-> 放行 + 观测，不阻断。
KBD_CONTRACT_SOFT_STALE_TOTAL = Counter(
    "hci_kbd_contract_soft_stale_total",
    "KBD 契约语义漂移（结构兼容、可继续执行）次数，提示建议重新发布但不阻断",
    labelnames=["support_id", "category"],
)

# 契约门禁：结构破坏性变更（结构指纹不等且旧信号无法在新契约下执行）-> 真阻断。
KBD_CONTRACT_HARD_BREAK_TOTAL = Counter(
    "hci_kbd_contract_hard_break_total",
    "KBD 契约结构性破坏（旧信号无法在新契约下执行，必须重新发布）次数",
    labelnames=["support_id", "category"],
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
#  qkv_vm_console 控制台截图指标（设计文档 §10.2）
# ──────────────────────────────────────────────

# 各阶段成功/失败率（stage: inventory/baseline/recapture/wake/vision/...）
VM_CONSOLE_CAPTURE_TOTAL = Counter(
    "hci_vm_console_capture_total",
    "虚拟机控制台截图各阶段结果计数",
    labelnames=["stage", "status", "mode"],  # mode: online | offline
)

# 截图/上传/识图时延
VM_CONSOLE_CAPTURE_DURATION_SECONDS = Histogram(
    "hci_vm_console_capture_duration_seconds",
    "虚拟机控制台截图阶段耗时（秒）",
    labelnames=["stage", "mode"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, float("inf")],
)

# 熄屏/黑屏发生比例
VM_CONSOLE_NEAR_BLACK_TOTAL = Counter(
    "hci_vm_console_near_black_total",
    "控制台截图近黑判定计数",
    labelnames=["near_black", "mode"],
)

# 唤醒确认、拒绝和结果（decision: confirmed/declined/non_interactive/timed_out；result: success/failed/not_attempted）
VM_CONSOLE_WAKE_TOTAL = Counter(
    "hci_vm_console_wake_total",
    "控制台近黑唤醒决定与结果计数",
    labelnames=["decision", "result", "mode"],
)

# 识别状态与置信度分布（confidence_band: high/medium/low）
VM_CONSOLE_VISION_TOTAL = Counter(
    "hci_vm_console_vision_total",
    "控制台视觉提取结果计数",
    labelnames=["state", "confidence_band", "mode"],
)

# 制品大小和成本控制（累计字节）
VM_CONSOLE_ARTIFACT_BYTES_TOTAL = Counter(
    "hci_vm_console_artifact_bytes_total",
    "控制台截图制品累计字节数",
    labelnames=["kind", "mode"],  # kind: ppm | png
)

# 权属/参数/哈希安全拒绝
VM_CONSOLE_SECURITY_REJECTION_TOTAL = Counter(
    "hci_vm_console_security_rejection_total",
    "控制台截图安全拒绝计数",
    labelnames=["reason", "mode"],
)


def vm_console_confidence_band(confidence: float) -> str:
    """视觉置信度分档（用于指标标签，避免高基数）。"""

    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


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
