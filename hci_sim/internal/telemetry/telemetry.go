// Package telemetry 初始化 hci-sim 的 W3C Trace Context 和 OTLP 导出。
package telemetry

import (
	"context"
	"errors"
	"os"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.24.0"
)

func Init(ctx context.Context) (func(context.Context) error, error) {
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	endpoint := strings.TrimSpace(os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
	if endpoint == "" {
		return func(context.Context) error { return nil }, nil
	}
	options := []otlptracehttp.Option{otlptracehttp.WithEndpointURL(endpoint)}
	if strings.HasPrefix(endpoint, "http://") {
		options = append(options, otlptracehttp.WithInsecure())
	}
	exporter, err := otlptracehttp.New(ctx, options...)
	if err != nil {
		return nil, err
	}
	res, err := resource.Merge(resource.Default(), resource.NewWithAttributes(
		semconv.SchemaURL,
		semconv.ServiceName("hci-sim"),
		semconv.DeploymentEnvironment(strings.TrimSpace(os.Getenv("HCI_SIM_ENVIRONMENT"))),
	))
	if err != nil {
		return nil, err
	}
	provider := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exporter), sdktrace.WithResource(res))
	otel.SetTracerProvider(provider)
	return provider.Shutdown, nil
}

func ContextFromEnv(values map[string]string) context.Context {
	carrier := propagation.MapCarrier{}
	if value := strings.TrimSpace(values["TRACEPARENT"]); value != "" {
		carrier.Set("traceparent", normalizeLegacyFlags(value))
	}
	if value := strings.TrimSpace(values["TRACESTATE"]); value != "" {
		carrier.Set("tracestate", value)
	}
	return otel.GetTextMapPropagator().Extract(context.Background(), carrier)
}

// Python OTel 可能生成带 Random 位的 00 traceparent，兼容当前 Go SDK 的解析行为。
func normalizeLegacyFlags(value string) string {
	parts := strings.Split(value, "-")
	if len(parts) != 4 || parts[0] != "00" {
		return value
	}
	switch parts[3] {
	case "02":
		parts[3] = "00"
	case "03":
		parts[3] = "01"
	}
	return strings.Join(parts, "-")
}

var ErrTraceContextMissing = errors.New("trace context missing")
