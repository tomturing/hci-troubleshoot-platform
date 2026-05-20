"""可观测性共享库：结构化日志、Prometheus 指标、OpenTelemetry 追踪。

所有 backend 服务通过此子包统一导入可观测性工具：
    from shared.observability.logger import get_logger
    from shared.observability.metrics import HTTPMetricsMiddleware
    from shared.observability.otel import init_telemetry, instrument_app
    from shared.observability.trace_id import get_trace_id
"""
