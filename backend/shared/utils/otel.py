"""
OpenTelemetry 统一初始化模块
为所有微服务提供标准的分布式链路追踪能力
"""
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME


def init_telemetry(service_name: str):
    """
    初始化 OpenTelemetry 链路追踪
    
    Args:
        service_name: 微服务名称（如 'api-gateway', 'case-service'）
    
    调用后效果:
        1. FastAPI 的所有入站请求自动生成 Span
        2. HTTPX 的所有出站请求自动传播 traceparent
        3. SQLAlchemy 的所有 DB 查询自动记录 Span
        4. 所有 Span 通过 OTLP gRPC 上报到 Tempo
    """
    # Tempo 的 OTLP gRPC 端点，默认为 Docker 内网
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4317")

    resource = Resource.create({SERVICE_NAME: service_name})
    
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(
        OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    # --- 自动仪表化 ---
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor().instrument()
    
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
        LoggingInstrumentor().instrument(set_logging_format=True)
    except ImportError:
        pass


def get_current_trace_id() -> str:
    """获取当前上下文中的 OTel Trace ID（hex 格式），若不存在返回空串"""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id != 0:
        return format(ctx.trace_id, '032x')
    return ""


def get_current_span_id() -> str:
    """获取当前上下文中的 OTel Span ID（hex 格式），若不存在返回空串"""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.span_id != 0:
        return format(ctx.span_id, '016x')
    return ""
