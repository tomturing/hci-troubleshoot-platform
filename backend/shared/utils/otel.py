"""
OpenTelemetry 统一初始化模块
为所有微服务提供标准的分布式链路追踪能力
"""

import os
import re

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def _parse_grpc_endpoint(raw: str) -> tuple[str, bool]:
    """
    将 OTLP env var 格式的端点（http://host:port 或 https://host:port）
    转换为 gRPC channel 所需的 host:port 格式。

    gRPC channel 不接受 http:// scheme，必须剥离；
    通过 scheme 判断是否使用 insecure 通道：http → insecure，https → TLS。

    Returns:
        (endpoint_without_scheme, is_insecure)
    """
    scheme_match = re.match(r'^(https?)://', raw)
    if scheme_match:
        scheme = scheme_match.group(1)
        return raw[len(scheme) + 3:], scheme == "http"
    # 无 scheme：默认 insecure（集群内部通信）
    return raw, True


def init_telemetry(service_name: str):
    """
    初始化 OpenTelemetry 链路追踪（TracerProvider + 非 FastAPI 仪表化）

    注意: FastAPI 的仪表化必须在 app 实例创建后通过 instrument_app() 完成，
    因为 FastAPIInstrumentor().instrument() 依赖 monkey-patch fastapi.FastAPI 类，
    而 `from fastapi import FastAPI` 会保留对原始类的引用，导致 patch 无效。

    Args:
        service_name: 微服务名称（如 'api-gateway', 'case-service'）

    调用后效果:
        1. HTTPX 的所有出站请求自动传播 traceparent
        2. SQLAlchemy 的所有 DB 查询自动记录 Span
        3. 所有 Span 通过 OTLP gRPC 上报到 Tempo
        4. Logging 自动注入 trace_id / span_id
    """
    # 读取环境变量（OTEL_EXPORTER_OTLP_ENDPOINT 使用 http://host:port 格式）
    # 必须 strip scheme 后再传给 gRPC channel，否则 grpc.insecure_channel() 会把
    # "http:" 解析为主机名，导致 StatusCode.UNAVAILABLE（TCP 通但 gRPC 握手失败）
    raw_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4317")
    otlp_endpoint, is_insecure = _parse_grpc_endpoint(raw_endpoint)

    resource = Resource.create({SERVICE_NAME: service_name})

    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=is_insecure))
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    # --- 自动仪表化（不含 FastAPI，需通过 instrument_app 单独注入） ---
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except ImportError:
        pass  # 非网关服务可能不需要 httpx 仪表化

    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument()
    except ImportError:
        pass  # 部分服务可能不使用 SQLAlchemy

    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        # 注意：LoggingInstrumentor 只影响通过 Python 标准 logging 模块输出的日志
        # （如三方库 SQLAlchemy、httpx 内部日志）。
        # 业务代码使用 StructuredLogger（shared/utils/logger.py），
        # 后者已直接从 OTel Span Context 读取 trace_id/span_id，
        # 不依赖 LoggingInstrumentor，且 propagate=False 阻止了重复输出。
        LoggingInstrumentor().instrument(set_logging_format=True)
    except ImportError:
        pass


def instrument_app(app):
    """
    对 FastAPI / Starlette 应用实例注入 OpenTelemetry 中间件

    必须在 FastAPI() 实例创建之后调用，否则 Span 不会注入到请求上下文中。

    Args:
        app: FastAPI 应用实例
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor().instrument_app(app)


def get_current_trace_id() -> str:
    """获取当前上下文中的 OTel Trace ID（hex 格式），若不存在返回空串"""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id != 0:
        return format(ctx.trace_id, "032x")
    return ""


def get_current_span_id() -> str:
    """获取当前上下文中的 OTel Span ID（hex 格式），若不存在返回空串"""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.span_id != 0:
        return format(ctx.span_id, "016x")
    return ""
