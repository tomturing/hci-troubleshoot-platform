from app.services.conversation_service import _build_remote_trace_context
from opentelemetry import context as otel_context
from opentelemetry import trace


def test_build_remote_trace_context_preserves_conversation_trace_id():
    trace_id_hex = "0e80bfd78d5f052bcfb7ae67c8180b30"

    ctx = _build_remote_trace_context(trace_id_hex)
    token = otel_context.attach(ctx)
    try:
        span_context = trace.get_current_span().get_span_context()
        assert span_context.trace_id == int(trace_id_hex, 16)
        assert span_context.span_id != 0
        assert span_context.is_remote is True
        assert span_context.is_valid is True
    finally:
        otel_context.detach(token)


def test_build_remote_trace_context_rejects_invalid_ids():
    for trace_id in ("short", "0" * 32, "z" * 32):
        try:
            _build_remote_trace_context(trace_id)
        except ValueError:
            continue
        raise AssertionError(f"非法 trace_id 未被拒绝: {trace_id}")
