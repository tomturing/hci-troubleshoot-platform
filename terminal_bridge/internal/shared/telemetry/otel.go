//go:build telemetry

// 集群真实实现（架构重构设计 §6.2）：包装 OTel SDK，OTLP HTTP 导出。
// 只有集群镜像带 -tags telemetry 构建（terminal_bridge/Dockerfile）；
// 桌面产物不带 tag，编译期即不链接本文件与整个 OTel 依赖树。
//
// 与门面的分工：W3C 传播解析在门面本体（纯标准库），本实现负责把门面
// ctx 里的远程父上下文还原成 otel SpanContext 再开 span——采样决策
// （ParentBased）与直调 otel 时代完全一致；span 开启后把新标识写回门面
// ctx，保证 obslog.TraceparentFromContext 等读到的永远是当前 span。

package telemetry

import (
	"context"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

func init() { impl = otelImpl{} }

type otelImpl struct{}

func (otelImpl) start(ctx context.Context, name string, kind spanKind, attrs []Attr) (context.Context, Span) {
	// 门面的 Extract 不经过 otel propagator：远程父上下文只存在于门面 ctx 值里，
	// 开 span 前还原回 otel ctx，采样与父子关系才能与直调时代一致。
	if info, ok := ctx.Value(ctxKey{}).(SpanInfo); ok && info.Valid && info.Remote {
		ctx = trace.ContextWithRemoteSpanContext(ctx, remoteSpanContext(info))
	}
	opts := []trace.SpanStartOption{trace.WithAttributes(toOtelAttrs(attrs)...)}
	if kind == kindClient {
		opts = append(opts, trace.WithSpanKind(trace.SpanKindClient))
	}
	ctx, span := otel.Tracer("terminal_bridge").Start(ctx, name, opts...)
	sc := span.SpanContext()
	ctx = context.WithValue(ctx, ctxKey{}, SpanInfo{
		TraceID:    sc.TraceID().String(),
		SpanID:     sc.SpanID().String(),
		TraceFlags: byte(sc.TraceFlags()),
		Tracestate: sc.TraceState().String(),
		Valid:      sc.IsValid(),
	})
	return ctx, realSpan{span: span}
}

func (otelImpl) spanFromContext(ctx context.Context) Span {
	return realSpan{span: trace.SpanFromContext(ctx)}
}

func (otelImpl) init(ctx context.Context, endpoint, serviceVersion, instanceID, environment string) (func(context.Context) error, error) {
	if endpoint == "" {
		otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
		return func(context.Context) error { return nil }, nil
	}
	exporter, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(endpoint))
	if err != nil {
		return nil, err
	}
	res, err := resource.New(ctx, resource.WithAttributes(
		attribute.String("service.name", "terminal-bridge"),
		attribute.String("service.version", serviceVersion),
		attribute.String("service.instance.id", instanceID),
		attribute.String("deployment.environment", environment),
	))
	if err != nil {
		return nil, err
	}
	provider := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exporter), sdktrace.WithResource(res))
	otel.SetTracerProvider(provider)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	return provider.Shutdown, nil
}

// remoteSpanContext 把门面 SpanInfo 还原为 otel 远程父上下文。
// info 已经门面 parseTraceparent 校验；hex 解析失败时退回无效上下文
// （otel 会把它当无父处理，与非法 traceparent 直调时代口径一致）。
func remoteSpanContext(info SpanInfo) trace.SpanContext {
	traceID, err := trace.TraceIDFromHex(info.TraceID)
	if err != nil {
		return trace.SpanContext{}
	}
	spanID, err := trace.SpanIDFromHex(info.SpanID)
	if err != nil {
		return trace.SpanContext{}
	}
	traceState, _ := trace.ParseTraceState(info.Tracestate)
	return trace.NewSpanContext(trace.SpanContextConfig{
		TraceID:    traceID,
		SpanID:     spanID,
		TraceFlags: trace.TraceFlags(info.TraceFlags),
		TraceState: traceState,
		Remote:     true,
	})
}

type realSpan struct{ span trace.Span }

func (r realSpan) AddEvent(name string, attrs ...Attr) {
	r.span.AddEvent(name, trace.WithAttributes(toOtelAttrs(attrs)...))
}

func (r realSpan) SetStatus(code, msg string) {
	r.span.SetStatus(toOtelCode(code), msg)
}

func (r realSpan) RecordError(err error) { r.span.RecordError(err) }

func (r realSpan) SetAttributes(attrs ...Attr) {
	r.span.SetAttributes(toOtelAttrs(attrs)...)
}

func (r realSpan) TraceID() string { return r.span.SpanContext().TraceID().String() }

func (r realSpan) End() { r.span.End() }

func toOtelCode(code string) codes.Code {
	switch code {
	case StatusError:
		return codes.Error
	case StatusOK:
		return codes.Ok
	default:
		return codes.Unset
	}
}

func toOtelAttrs(attrs []Attr) []attribute.KeyValue {
	out := make([]attribute.KeyValue, 0, len(attrs))
	for _, a := range attrs {
		switch a.kind {
		case kindString:
			out = append(out, attribute.String(a.key, a.str))
		case kindBool:
			out = append(out, attribute.Bool(a.key, a.boo))
		default:
			out = append(out, attribute.Int64(a.key, a.num))
		}
	}
	return out
}
